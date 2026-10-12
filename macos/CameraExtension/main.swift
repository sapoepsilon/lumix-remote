import CoreMediaIO
import Foundation

// Entry point of the camera system extension (runs as the _cmiodalassistants role user).
let providerSource = LumixProviderSource(clientQueue: nil)
CMIOExtensionProvider.startService(provider: providerSource.provider)
CFRunLoopRun()
