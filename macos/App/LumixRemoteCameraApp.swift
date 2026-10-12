import SwiftUI

@main
struct LumixRemoteCameraApp: App {
    @StateObject private var manager = ExtensionManager()

    var body: some Scene {
        WindowGroup("Lumix Remote Camera") {
            ContentView()
                .environmentObject(manager)
                .frame(minWidth: 460, idealWidth: 480, minHeight: 300)
        }
        .windowResizability(.contentSize)
    }
}

struct ContentView: View {
    @EnvironmentObject var manager: ExtensionManager
    @AppStorage("sourceURL") private var sourceURL = "http://127.0.0.1:8770"

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Lumix Remote Camera").font(.title2.bold())
            Text("Adds a \"\(ExtensionManager.cameraName)\" camera that Zoom, FaceTime, Meet and Photo Booth can use. Frames come from the Lumix Remote server's live view.")
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            GroupBox {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 8) {
                        Circle().fill(manager.statusColor).frame(width: 10, height: 10)
                        Text(manager.status).fixedSize(horizontal: false, vertical: true)
                    }
                    HStack {
                        Button("Install camera") { manager.activate() }
                        Button("Uninstall") { manager.deactivate() }
                        Spacer()
                        Button("Refresh") { manager.refresh() }
                    }
                }.padding(6)
            }

            GroupBox("Source") {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        TextField("http://127.0.0.1:8770", text: $sourceURL)
                            .textFieldStyle(.roundedBorder)
                            .onSubmit { manager.applySource(sourceURL) }
                        Button("Apply") { manager.applySource(sourceURL) }
                    }
                    Text("Server URL (uses /api/liveview.mjpg) or a full stream / .jpg URL.")
                        .font(.caption).foregroundStyle(.secondary)
                    if let note = manager.sourceNote {
                        Text(note).font(.caption).foregroundStyle(.secondary)
                    }
                }.padding(6)
            }
            Spacer(minLength: 0)
        }
        .padding(20)
        .onAppear {
            manager.refresh()
            if CommandLine.arguments.contains("--install") { manager.activate() }
            if CommandLine.arguments.contains("--uninstall") { manager.deactivate() }
        }
    }
}
