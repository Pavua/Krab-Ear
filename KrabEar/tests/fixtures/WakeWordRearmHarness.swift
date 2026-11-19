import Foundation

// Изолированный процесс поллера: журнал приложения и его singleton не загружаются.
struct AgentLogger {
    static let shared = AgentLogger()
    func info(_ message: String) {}
    func warn(_ message: String) {}
}

@main
struct WakeWordRearmHarness {
    @MainActor static func main() {
        let ipc = IPCClient(socketPath: CommandLine.arguments[1])
        let poller = WakeWordPoller(
            ipcProvider: { ipc },
            isToggleEnabled: { true },
            onDetection: { exit(0) }
        )
        poller.activate()
        withExtendedLifetime(poller) { RunLoop.main.run() }
        exit(1)
    }
}
