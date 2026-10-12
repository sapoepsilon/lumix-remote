// Lists video capture devices and grabs one frame from the Lumix virtual camera.
//   swift macos/tools/webcam-check.swift [output.png] [device-name-substring]
// The terminal app running this needs Camera permission (macOS asks the first time).
import AVFoundation
import CoreImage
import Foundation
import ImageIO
import UniformTypeIdentifiers

setvbuf(stdout, nil, _IONBF, 0)
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp/lumix-webcam-frame.png"
let want = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "Lumix"

var types: [AVCaptureDevice.DeviceType] = [.builtInWideAngleCamera]
if #available(macOS 14.0, *) { types += [.external, .continuityCamera] } else { types.append(.externalUnknown) }
let devices = AVCaptureDevice.DiscoverySession(deviceTypes: types, mediaType: .video, position: .unspecified).devices
print("video devices:")
for d in devices { print("  - \(d.localizedName)  [\(d.modelID)]  \(d.uniqueID)") }
guard let dev = devices.first(where: { $0.localizedName.localizedCaseInsensitiveContains(want) }) else {
    print("no device matching \"\(want)\""); exit(2)
}

final class Grabber: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    var count = 0
    let done = DispatchSemaphore(value: 0)
    func captureOutput(_ output: AVCaptureOutput, didOutput sb: CMSampleBuffer, from c: AVCaptureConnection) {
        count += 1
        guard count == 45, let px = CMSampleBufferGetImageBuffer(sb) else { return }   // ~1.5 s in
        let ci = CIImage(cvPixelBuffer: px)
        if let cg = CIContext().createCGImage(ci, from: ci.extent),
           let dst = CGImageDestinationCreateWithURL(URL(fileURLWithPath: out) as CFURL,
                                                     UTType.png.identifier as CFString, 1, nil) {
            CGImageDestinationAddImage(dst, cg, nil)
            CGImageDestinationFinalize(dst)
            print("saved \(CVPixelBufferGetWidth(px))x\(CVPixelBufferGetHeight(px)) frame to \(out)")
        }
        done.signal()
    }
}

let session = AVCaptureSession()
let input = try AVCaptureDeviceInput(device: dev)
session.addInput(input)
let output = AVCaptureVideoDataOutput()
let grabber = Grabber()
output.setSampleBufferDelegate(grabber, queue: DispatchQueue(label: "grab"))
session.addOutput(output)
print("capturing from \(dev.localizedName) (\(dev.activeFormat.formatDescription.dimensions.width)x\(dev.activeFormat.formatDescription.dimensions.height))")
let t0 = Date()
session.startRunning()
let ok = grabber.done.wait(timeout: .now() + 15) == .success
session.stopRunning()
print(String(format: "%d frames in %.1fs", grabber.count, Date().timeIntervalSince(t0)))
exit(ok ? 0 : 1)
