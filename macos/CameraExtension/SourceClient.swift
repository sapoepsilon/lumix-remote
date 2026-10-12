import Foundation

/// Reads JPEG frames over HTTP: a multipart MJPEG stream (default, /api/liveview.mjpg) or,
/// for URLs ending in ".jpg", by polling a latest-frame endpoint. Reconnects forever until
/// stopped; a stalled stream (no frame for 5 s) is dropped and reopened.
final class SourceClient: NSObject, URLSessionDataDelegate {
    private let url: URL
    private let onFrame: (Data) -> Void
    private let queue = OperationQueue()
    private var session: URLSession!
    private var task: URLSessionDataTask?
    private var buffer = Data()
    private var stopped = false
    private var lastFrame = Date.distantPast
    private var watchdog: Timer?
    private let poll: Bool
    /// URLSession splits multipart/x-mixed-replace itself: each part arrives as its own
    /// response (image/jpeg) followed by that part's body. Otherwise we parse the multipart.
    private var partMode = false

    init(url: URL, onFrame: @escaping (Data) -> Void) {
        self.url = url
        self.onFrame = onFrame
        self.poll = url.path.lowercased().hasSuffix(".jpg") || url.path.lowercased().hasSuffix(".jpeg")
        super.init()
        queue.maxConcurrentOperationCount = 1
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 10
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.httpMaximumConnectionsPerHost = 2
        session = URLSession(configuration: cfg, delegate: self, delegateQueue: queue)
    }

    func start() {
        queue.addOperation { self.connect() }
    }

    func stop() {
        queue.addOperation {
            self.stopped = true
            self.task?.cancel()
            self.task = nil
            self.session.invalidateAndCancel()
        }
    }

    private func connect() {
        guard !stopped else { return }
        buffer.removeAll(keepingCapacity: true)
        var req = URLRequest(url: url)
        req.setValue("LumixRemoteCamera/1.0", forHTTPHeaderField: "User-Agent")
        task = session.dataTask(with: req)
        lastFrame = Date()
        task?.resume()
        if !poll { scheduleWatchdog() }
    }

    private func scheduleWatchdog() {
        DispatchQueue.global().asyncAfter(deadline: .now() + 2) { [weak self] in
            self?.queue.addOperation {
                guard let self, !self.stopped else { return }
                if Date().timeIntervalSince(self.lastFrame) > 5 {
                    extLog.info("source stalled, reconnecting")
                    self.task?.cancel()          // didCompleteWithError reconnects
                } else {
                    self.scheduleWatchdog()
                }
            }
        }
    }

    private func reconnect(after delay: TimeInterval) {
        guard !stopped else { return }
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.queue.addOperation { self?.connect() }
        }
    }

    // MARK: URLSessionDataDelegate

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        let ok = (response as? HTTPURLResponse).map { (200..<300).contains($0.statusCode) } ?? true
        let mime = response.mimeType?.lowercased() ?? ""
        if !poll {
            if partMode, !buffer.isEmpty { emitPart() }    // previous part never ended in EOI
            partMode = mime.hasPrefix("image/")
            buffer.removeAll(keepingCapacity: true)
        }
        completionHandler(ok ? .allow : .cancel)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard dataTask === task else { return }
        buffer.append(data)
        if poll { return }
        if partMode {
            if buffer.count >= 4, buffer.suffix(2) == Data([0xFF, 0xD9]) { emitPart() }
        } else {
            drainMultipart()
        }
    }

    func urlSession(_ session: URLSession, task finished: URLSessionTask, didCompleteWithError error: Error?) {
        guard finished === task else { return }
        task = nil
        if poll {
            if error == nil, buffer.count > 4, buffer.prefix(2) == Data([0xFF, 0xD8]) {
                lastFrame = Date()
                onFrame(buffer)
                reconnect(after: 1.0 / 30)
            } else {
                reconnect(after: 1)
            }
        } else {
            reconnect(after: 1)
        }
    }

    // MARK: multipart parsing

    private static let headerEnd = Data("\r\n\r\n".utf8)

    private func drainMultipart() {
        while true {
            guard let hdr = buffer.range(of: Self.headerEnd) else {
                if buffer.count > 64 * 1024 { buffer.removeAll(keepingCapacity: true) }
                return
            }
            let header = String(decoding: buffer[buffer.startIndex..<hdr.lowerBound], as: UTF8.self)
            let start = hdr.upperBound
            if let len = Self.contentLength(header) {
                guard buffer.endIndex - start >= len else { return }
                emit(buffer.subdata(in: start..<start + len))
                buffer = Data(buffer[(start + len)...])
            } else {
                // No Content-Length: cut at the JPEG end-of-image marker.
                guard let soi = buffer.range(of: Data([0xFF, 0xD8]), in: start..<buffer.endIndex),
                      let eoi = buffer.range(of: Data([0xFF, 0xD9]), in: soi.upperBound..<buffer.endIndex)
                else {
                    if buffer.count > 16 * 1024 * 1024 { buffer.removeAll(keepingCapacity: true) }
                    return
                }
                emit(buffer.subdata(in: soi.lowerBound..<eoi.upperBound))
                buffer = Data(buffer[eoi.upperBound...])
            }
        }
    }

    private func emitPart() {
        if buffer.prefix(2) == Data([0xFF, 0xD8]) { emit(buffer) }
        buffer.removeAll(keepingCapacity: true)
    }

    private func emit(_ jpg: Data) {
        guard jpg.count > 4 else { return }
        lastFrame = Date()
        onFrame(jpg)
    }

    static func contentLength(_ header: String) -> Int? {
        for line in header.split(whereSeparator: { $0 == "\r" || $0 == "\n" }) {
            let parts = line.split(separator: ":", maxSplits: 1)
            if parts.count == 2, parts[0].trimmingCharacters(in: .whitespaces).lowercased() == "content-length" {
                return Int(parts[1].trimmingCharacters(in: .whitespaces))
            }
        }
        return nil
    }
}
