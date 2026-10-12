import CoreMediaIO
import CoreVideo
import Foundation
import os.log

let extLog = Logger(subsystem: "lumixremote.camera", category: "extension")

/// Custom device property carrying the source URL (CFString). The host app reads and writes it
/// through the CoreMediaIO C API; the selector is the four-char code 'lsrc'.
let sourceURLProperty = CMIOExtensionProperty(rawValue: "4cc_lsrc_glob_0000")
let defaultSourceURL = "http://127.0.0.1:8770"

/// Output formats offered to clients (BGRA, 30 fps).
struct OutputFormat { let width: Int32; let height: Int32 }
let outputFormats = [OutputFormat(width: 1920, height: 1080), OutputFormat(width: 1280, height: 720)]
let frameRate: Int32 = 30

// MARK: - Provider

final class LumixProviderSource: NSObject, CMIOExtensionProviderSource {
    private(set) var provider: CMIOExtensionProvider!
    private var deviceSource: LumixDeviceSource!

    init(clientQueue: DispatchQueue?) {
        super.init()
        provider = CMIOExtensionProvider(source: self, clientQueue: clientQueue)
        deviceSource = LumixDeviceSource(localizedName: "Lumix S5 (Wi-Fi)")
        do { try provider.addDevice(deviceSource.device) } catch {
            fatalError("addDevice failed: \(error)")
        }
    }

    func connect(to client: CMIOExtensionClient) throws {}
    func disconnect(from client: CMIOExtensionClient) {}

    var availableProperties: Set<CMIOExtensionProperty> { [.providerManufacturer, .providerName] }

    func providerProperties(forProperties properties: Set<CMIOExtensionProperty>) throws
        -> CMIOExtensionProviderProperties {
        let p = CMIOExtensionProviderProperties(dictionary: [:])
        if properties.contains(.providerManufacturer) { p.manufacturer = "Lumix Remote" }
        if properties.contains(.providerName) { p.name = "Lumix Remote Camera" }
        return p
    }

    func setProviderProperties(_ providerProperties: CMIOExtensionProviderProperties) throws {}
}

// MARK: - Device

final class LumixDeviceSource: NSObject, CMIOExtensionDeviceSource {
    private(set) var device: CMIOExtensionDevice!
    private var streamSource: LumixStreamSource!
    let pipeline = FramePipeline()

    init(localizedName: String) {
        super.init()
        device = CMIOExtensionDevice(localizedName: localizedName,
                                     deviceID: UUID(uuidString: "6C1F3D2A-6E0B-4B8C-9C41-5A2E7F0D1B11")!,
                                     legacyDeviceID: nil, source: self)
        streamSource = LumixStreamSource(localizedName: "Lumix S5 Live View", pipeline: pipeline)
        do { try device.addStream(streamSource.stream) } catch {
            fatalError("addStream failed: \(error)")
        }
    }

    var availableProperties: Set<CMIOExtensionProperty> {
        [.deviceTransportType, .deviceModel, sourceURLProperty]
    }

    func deviceProperties(forProperties properties: Set<CMIOExtensionProperty>) throws
        -> CMIOExtensionDeviceProperties {
        let p = CMIOExtensionDeviceProperties(dictionary: [:])
        if properties.contains(.deviceTransportType) { p.transportType = 0x76697274 } // 'virt'
        if properties.contains(.deviceModel) { p.model = "Lumix S5 (Wi-Fi live view)" }
        if properties.contains(sourceURLProperty) {
            p.setPropertyState(CMIOExtensionPropertyState(value: pipeline.sourceURL as NSString),
                               forProperty: sourceURLProperty)
        }
        return p
    }

    func setDeviceProperties(_ deviceProperties: CMIOExtensionDeviceProperties) throws {
        if let state = deviceProperties.propertiesDictionary[sourceURLProperty],
           let value = state.value as? String {
            pipeline.sourceURL = value
        }
    }
}

// MARK: - Stream

final class LumixStreamSource: NSObject, CMIOExtensionStreamSource {
    private(set) var stream: CMIOExtensionStream!
    private let pipeline: FramePipeline
    private let streamFormats: [CMIOExtensionStreamFormat]

    init(localizedName: String, pipeline: FramePipeline) {
        self.pipeline = pipeline
        let duration = CMTime(value: 1, timescale: frameRate)
        streamFormats = outputFormats.map { f in
            var desc: CMFormatDescription?
            CMVideoFormatDescriptionCreate(allocator: kCFAllocatorDefault,
                                           codecType: kCVPixelFormatType_32BGRA,
                                           width: f.width, height: f.height,
                                           extensions: nil, formatDescriptionOut: &desc)
            return CMIOExtensionStreamFormat(formatDescription: desc!, maxFrameDuration: duration,
                                             minFrameDuration: duration, validFrameDurations: nil)
        }
        super.init()
        stream = CMIOExtensionStream(localizedName: localizedName,
                                     streamID: UUID(uuidString: "0E5B7C44-2B9A-4D4F-8F1E-3C6A9D2B7E21")!,
                                     direction: .source, clockType: .hostTime, source: self)
        pipeline.stream = stream
    }

    var formats: [CMIOExtensionStreamFormat] { streamFormats }

    var activeFormatIndex: Int = 0 {
        didSet {
            if activeFormatIndex >= outputFormats.count { activeFormatIndex = 0 }
            pipeline.setOutput(outputFormats[activeFormatIndex])
        }
    }

    var availableProperties: Set<CMIOExtensionProperty> {
        [.streamActiveFormatIndex, .streamFrameDuration]
    }

    func streamProperties(forProperties properties: Set<CMIOExtensionProperty>) throws
        -> CMIOExtensionStreamProperties {
        let p = CMIOExtensionStreamProperties(dictionary: [:])
        if properties.contains(.streamActiveFormatIndex) { p.activeFormatIndex = activeFormatIndex }
        if properties.contains(.streamFrameDuration) {
            p.frameDuration = CMTime(value: 1, timescale: frameRate)
        }
        return p
    }

    func setStreamProperties(_ streamProperties: CMIOExtensionStreamProperties) throws {
        if let idx = streamProperties.activeFormatIndex { activeFormatIndex = idx }
    }

    func authorizedToStartStream(for client: CMIOExtensionClient) -> Bool { true }

    func startStream() throws { pipeline.start() }
    func stopStream() throws { pipeline.stop() }
}
