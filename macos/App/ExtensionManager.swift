import AVFoundation
import CoreMediaIO
import SwiftUI
import SystemExtensions

/// Installs / removes the embedded camera extension and pushes the source URL to it.
final class ExtensionManager: NSObject, ObservableObject, OSSystemExtensionRequestDelegate {
    static let cameraName = "Lumix S5 (Wi-Fi)"

    @Published var status = "Checking…"
    @Published var statusColor: Color = .gray
    @Published var sourceNote: String?

    private var pendingAction = ""

    private var extensionID: String {
        (Bundle.main.bundleIdentifier ?? "") + ".CameraExtension"
    }

    // MARK: actions

    func activate() {
        pendingAction = "install"
        set("Installing…", .orange)
        let req = OSSystemExtensionRequest.activationRequest(forExtensionWithIdentifier: extensionID,
                                                              queue: .main)
        req.delegate = self
        OSSystemExtensionManager.shared.submitRequest(req)
    }

    func deactivate() {
        pendingAction = "uninstall"
        set("Removing…", .orange)
        let req = OSSystemExtensionRequest.deactivationRequest(forExtensionWithIdentifier: extensionID,
                                                                queue: .main)
        req.delegate = self
        OSSystemExtensionManager.shared.submitRequest(req)
    }

    func refresh() {
        pendingAction = "query"
        if #available(macOS 12.0, *) {
            let req = OSSystemExtensionRequest.propertiesRequest(forExtensionWithIdentifier: extensionID,
                                                                  queue: .main)
            req.delegate = self
            OSSystemExtensionManager.shared.submitRequest(req)
        }
    }

    func applySource(_ url: String) {
        guard let dev = Self.findCMIODevice(named: Self.cameraName) else {
            sourceNote = "Camera not found yet — install it first."
            return
        }
        sourceNote = Self.setSourceURL(url, on: dev)
            ? "Source set to \(url)." : "Could not update the camera's source."
    }

    // MARK: OSSystemExtensionRequestDelegate

    func request(_ request: OSSystemExtensionRequest,
                 actionForReplacingExtension existing: OSSystemExtensionProperties,
                 withExtension ext: OSSystemExtensionProperties) -> OSSystemExtensionRequest.ReplacementAction {
        .replace
    }

    func requestNeedsUserApproval(_ request: OSSystemExtensionRequest) {
        set("Waiting for approval: System Settings → General → Login Items & Extensions → Camera Extensions.", .orange)
    }

    func request(_ request: OSSystemExtensionRequest,
                 didFinishWithResult result: OSSystemExtensionRequest.Result) {
        switch pendingAction {
        case "uninstall": set("Uninstalled.", .gray)
        default:
            set(result == .willCompleteAfterReboot ? "Installed — finishes after a restart."
                : "Installed and active. Pick \"\(Self.cameraName)\" in your video app.", .green)
            let saved = UserDefaults.standard.string(forKey: "sourceURL") ?? "http://127.0.0.1:8770"
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.applySource(saved) }
        }
    }

    func request(_ request: OSSystemExtensionRequest, didFailWithError error: Error) {
        set("\(pendingAction.capitalized) failed: \(error.localizedDescription)", .red)
    }

    @available(macOS 12.0, *)
    func request(_ request: OSSystemExtensionRequest,
                 foundProperties properties: [OSSystemExtensionProperties]) {
        if properties.contains(where: { $0.isEnabled }) {
            set("Installed and active. Pick \"\(Self.cameraName)\" in your video app.", .green)
        } else if properties.contains(where: { $0.isAwaitingUserApproval }) {
            set("Waiting for approval: System Settings → General → Login Items & Extensions → Camera Extensions.", .orange)
        } else {
            set("Not installed.", .gray)
        }
    }

    private func set(_ text: String, _ color: Color) {
        status = text
        statusColor = color
    }

    // MARK: CoreMediaIO custom property ('lsrc' on the device, CFString)

    private static let sourceSelector: CMIOObjectPropertySelector = 0x6C73_7263 // 'lsrc'

    static func findCMIODevice(named name: String) -> CMIOObjectID? {
        var addr = CMIOObjectPropertyAddress(
            mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyDevices),
            mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
            mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
        var size: UInt32 = 0
        guard CMIOObjectGetPropertyDataSize(CMIOObjectID(kCMIOObjectSystemObject), &addr, 0, nil, &size) == 0,
              size > 0 else { return nil }
        var ids = [CMIOObjectID](repeating: 0, count: Int(size) / MemoryLayout<CMIOObjectID>.size)
        var used: UInt32 = 0
        guard CMIOObjectGetPropertyData(CMIOObjectID(kCMIOObjectSystemObject), &addr, 0, nil, size,
                                        &used, &ids) == 0 else { return nil }
        for id in ids {
            var nameAddr = CMIOObjectPropertyAddress(
                mSelector: CMIOObjectPropertySelector(kCMIOObjectPropertyName),
                mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
                mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
            var cfName: CFString = "" as CFString
            var got: UInt32 = 0
            let status = withUnsafeMutablePointer(to: &cfName) { ptr in
                CMIOObjectGetPropertyData(id, &nameAddr, 0, nil,
                                          UInt32(MemoryLayout<CFString>.size), &got, ptr)
            }
            if status == 0, (cfName as String) == name { return id }
        }
        return nil
    }

    static func setSourceURL(_ url: String, on device: CMIOObjectID) -> Bool {
        var addr = CMIOObjectPropertyAddress(
            mSelector: sourceSelector,
            mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
            mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
        guard CMIOObjectHasProperty(device, &addr) else { return false }
        var value: CFString = url as CFString
        let status = withUnsafeMutablePointer(to: &value) { ptr in
            CMIOObjectSetPropertyData(device, &addr, 0, nil, UInt32(MemoryLayout<CFString>.size), ptr)
        }
        return status == 0
    }
}
