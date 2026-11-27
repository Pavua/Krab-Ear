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
            // Поллер ставит wake_word_take_preroll в очередь ДО onDetection; даём ему дойти до сервера.
            onDetection: { DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { exit(0) } }
        )
        poller.activate()
        withExtendedLifetime(poller) { RunLoop.main.run() }
        exit(1)
    }
}
