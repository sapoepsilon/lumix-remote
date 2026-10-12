import CoreImage
import CoreMediaIO
import CoreText
import CoreVideo
import Foundation

/// Pulls JPEG frames from the Lumix Remote server while a client is streaming, scales them
/// (aspect-fit, Lanczos + light sharpening) into BGRA buffers and emits them at a steady
/// 30 fps, repeating the last frame when the source is slower and showing an "offline"
/// placeholder when no frame has arrived for a while.
final class FramePipeline {
    weak var stream: CMIOExtensionStream?

    private let queue = DispatchQueue(label: "lumixremote.camera.frames", qos: .userInteractive)
    private let renderQueue = DispatchQueue(label: "lumixremote.camera.render", qos: .userInteractive)
    private var timer: DispatchSourceTimer?
    private var client: SourceClient?
    private var output = outputFormats[0]
    private var pool: CVPixelBufferPool?
    private var current: CVPixelBuffer?        // last rendered camera frame (never mutated)
    private var placeholder: CVPixelBuffer?
    private var lastJPEG: Data?
    private var lastFrameAt: TimeInterval = 0
    private var rendering = false
    private var pendingJPEG: Data?
    private var running = false

    /// Seconds without a source frame before the placeholder is shown.
    private let offlineAfter: TimeInterval = 2.5

    private let ciContext = CIContext(options: [.cacheIntermediates: false,
                                                .workingColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
    private let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!

    private static let defaultsKey = "sourceURL"

    var sourceURL: String {
        get { queue.sync { UserDefaults.standard.string(forKey: Self.defaultsKey) ?? defaultSourceURL } }
        set {
            let trimmed = newValue.trimmingCharacters(in: .whitespacesAndNewlines)
            queue.async {
                UserDefaults.standard.set(trimmed.isEmpty ? defaultSourceURL : trimmed,
                                          forKey: Self.defaultsKey)
                extLog.info("source URL set to \(trimmed, privacy: .public)")
                if self.running { self.restartClient() }
            }
        }
    }

    func setOutput(_ format: OutputFormat) {
        queue.async {
            guard format.width != self.output.width || format.height != self.output.height else { return }
            self.output = format
            self.pool = nil
            self.current = nil
            self.placeholder = nil
            if let jpg = self.lastJPEG { self.submit(jpg) }
        }
    }

    func start() {
        queue.async {
            guard !self.running else { return }
            self.running = true
            self.lastFrameAt = 0
            self.restartClient()
            let t = DispatchSource.makeTimerSource(flags: .strict, queue: self.queue)
            t.schedule(deadline: .now(), repeating: .nanoseconds(Int(1_000_000_000 / frameRate)),
                       leeway: .milliseconds(1))
            t.setEventHandler { [weak self] in self?.tick() }
            t.resume()
            self.timer = t
            extLog.info("stream started")
        }
    }

    func stop() {
        queue.async {
            self.running = false
            self.timer?.cancel()
            self.timer = nil
            self.client?.stop()
            self.client = nil
            extLog.info("stream stopped")
        }
    }

    // MARK: queue-confined

    private func restartClient() {
        client?.stop()
        let url = Self.resolve(UserDefaults.standard.string(forKey: Self.defaultsKey) ?? defaultSourceURL)
        let c = SourceClient(url: url) { [weak self] jpg in
            guard let self else { return }
            self.queue.async { self.submit(jpg) }
        }
        client = c
        c.start()
    }

    /// A bare server URL gets the MJPEG live view path; anything with a path is used as-is
    /// (".jpg" URLs are polled, everything else is read as multipart MJPEG).
    static func resolve(_ raw: String) -> URL {
        var s = raw
        if !s.contains("://") { s = "http://" + s }
        guard var comps = URLComponents(string: s) else { return URL(string: defaultSourceURL + "/api/liveview.mjpg")! }
        if comps.path.isEmpty || comps.path == "/" { comps.path = "/api/liveview.mjpg" }
        return comps.url ?? URL(string: defaultSourceURL + "/api/liveview.mjpg")!
    }

    private func submit(_ jpg: Data) {
        lastJPEG = jpg
        lastFrameAt = ProcessInfo.processInfo.systemUptime
        if rendering { pendingJPEG = jpg; return }   // latest wins
        rendering = true
        let format = output
        let pool = bufferPool()
        renderQueue.async {
            let buf = self.render(jpg, into: pool, format: format)
            self.queue.async {
                if let buf, format.width == self.output.width { self.current = buf }
                self.rendering = false
                if let next = self.pendingJPEG { self.pendingJPEG = nil; self.submit(next) }
            }
        }
    }

    private func tick() {
        guard running, let stream else { return }
        let online = lastFrameAt > 0 && ProcessInfo.processInfo.systemUptime - lastFrameAt < offlineAfter
        let buffer: CVPixelBuffer?
        if online, let current { buffer = current } else { buffer = placeholderBuffer() }
        guard let buffer else { return }

        let now = CMClockGetTime(CMClockGetHostTimeClock())
        var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: frameRate),
                                        presentationTimeStamp: now, decodeTimeStamp: .invalid)
        var desc: CMFormatDescription?
        CMVideoFormatDescriptionCreateForImageBuffer(allocator: kCFAllocatorDefault,
                                                     imageBuffer: buffer, formatDescriptionOut: &desc)
        guard let desc else { return }
        var sample: CMSampleBuffer?
        CMSampleBufferCreateReadyWithImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: buffer,
                                                 formatDescription: desc, sampleTiming: &timing,
                                                 sampleBufferOut: &sample)
        guard let sample else { return }
        stream.send(sample, discontinuity: [],
                    hostTimeInNanoseconds: UInt64(now.seconds * Double(NSEC_PER_SEC)))
    }

    func bufferPool() -> CVPixelBufferPool? {
        if let pool { return pool }
        let attrs: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: Int(output.width),
            kCVPixelBufferHeightKey as String: Int(output.height),
            kCVPixelBufferIOSurfacePropertiesKey as String: [:],
        ]
        var p: CVPixelBufferPool?
        CVPixelBufferPoolCreate(kCFAllocatorDefault, [kCVPixelBufferPoolMinimumBufferCountKey: 6] as CFDictionary,
                                attrs as CFDictionary, &p)
        pool = p
        return p
    }

    // MARK: rendering (renderQueue)

    func render(_ jpg: Data, into pool: CVPixelBufferPool?, format: OutputFormat) -> CVPixelBuffer? {
        guard let pool, let src = CIImage(data: jpg, options: [.applyOrientationProperty: true]) else { return nil }
        var buf: CVPixelBuffer?
        CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &buf)
        guard let buf else { return nil }
        let W = CGFloat(format.width), H = CGFloat(format.height)
        let ext = src.extent
        let scale = min(W / ext.width, H / ext.height)

        var img = src.applyingFilter("CILanczosScaleTransform",
                                     parameters: [kCIInputScaleKey: scale, kCIInputAspectRatioKey: 1.0])
        if scale > 1.05 {
            img = img.applyingFilter("CISharpenLuminance",
                                     parameters: [kCIInputSharpnessKey: 0.35, kCIInputRadiusKey: 1.2])
                .cropped(to: img.extent)
        }
        let e = img.extent
        img = img.transformed(by: CGAffineTransform(translationX: ((W - e.width) / 2 - e.minX).rounded(),
                                                    y: ((H - e.height) / 2 - e.minY).rounded()))
        let canvas = CGRect(x: 0, y: 0, width: W, height: H)
        img = img.composited(over: CIImage(color: .black).cropped(to: canvas)).cropped(to: canvas)
        ciContext.render(img, to: buf, bounds: canvas, colorSpace: sRGB)
        return buf
    }

    func placeholderBuffer() -> CVPixelBuffer? {
        if let placeholder { return placeholder }
        let W = Int(output.width), H = Int(output.height)
        var buf: CVPixelBuffer?
        CVPixelBufferCreate(kCFAllocatorDefault, W, H, kCVPixelFormatType_32BGRA,
                            [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buf)
        guard let buf else { return nil }
        CVPixelBufferLockBaseAddress(buf, [])
        defer { CVPixelBufferUnlockBaseAddress(buf, []) }
        guard let ctx = CGContext(data: CVPixelBufferGetBaseAddress(buf), width: W, height: H,
                                  bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(buf),
                                  space: sRGB,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue
                                      | CGBitmapInfo.byteOrder32Little.rawValue) else { return nil }
        let s = CGFloat(H) / 1080
        ctx.setFillColor(CGColor(srgbRed: 0.07, green: 0.075, blue: 0.085, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))

        // Simple camera glyph: rounded body + lens ring.
        let cx = CGFloat(W) / 2, cy = CGFloat(H) / 2 + 90 * s
        ctx.setStrokeColor(CGColor(srgbRed: 0.55, green: 0.57, blue: 0.6, alpha: 1))
        ctx.setLineWidth(6 * s)
        let body = CGRect(x: cx - 90 * s, y: cy - 55 * s, width: 180 * s, height: 110 * s)
        ctx.addPath(CGPath(roundedRect: body, cornerWidth: 18 * s, cornerHeight: 18 * s, transform: nil))
        ctx.strokePath()
        ctx.strokeEllipse(in: CGRect(x: cx - 32 * s, y: cy - 32 * s, width: 64 * s, height: 64 * s))
        ctx.move(to: CGPoint(x: cx - 110 * s, y: cy - 75 * s))
        ctx.addLine(to: CGPoint(x: cx + 110 * s, y: cy + 75 * s))
        ctx.strokePath()

        drawCentered(ctx, "Camera offline", size: 64 * s, weight: true, y: CGFloat(H) / 2 - 60 * s,
                     width: CGFloat(W), gray: 0.93)
        drawCentered(ctx, "Open Lumix Remote to start the live view", size: 34 * s, weight: false,
                     y: CGFloat(H) / 2 - 120 * s, width: CGFloat(W), gray: 0.6)
        placeholder = buf
        return buf
    }

    private func drawCentered(_ ctx: CGContext, _ text: String, size: CGFloat, weight: Bool, y: CGFloat,
                              width: CGFloat, gray: CGFloat) {
        let base = CTFontCreateUIFontForLanguage(weight ? .emphasizedSystem : .system, size, nil)
            ?? CTFontCreateWithName("Helvetica" as CFString, size, nil)
        let attrs: [NSAttributedString.Key: Any] = [
            NSAttributedString.Key(kCTFontAttributeName as String): base,
            NSAttributedString.Key(kCTForegroundColorAttributeName as String):
                CGColor(srgbRed: gray, green: gray, blue: gray, alpha: 1),
        ]
        let line = CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: attrs))
        let w = CTLineGetTypographicBounds(line, nil, nil, nil)
        ctx.textPosition = CGPoint(x: (width - CGFloat(w)) / 2, y: y)
        CTLineDraw(line, ctx)
    }
}
