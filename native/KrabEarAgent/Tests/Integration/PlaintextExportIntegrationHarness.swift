// Отдельный executable тест: production IPC/coordinator, без NSApplication.
import Foundation

@main
struct PlaintextExportIntegrationHarness {
    @MainActor static func main() async {
        guard CommandLine.arguments.count == 4,
              let expected = Int(CommandLine.arguments[3]) else { exit(2) }
        let client = IPCClient(socketPath: CommandLine.arguments[1])
        let coordinator = PlaintextExportCoordinator(ipc: client)
        let output = URL(fileURLWithPath: CommandLine.arguments[2])
        var writes = 0
        var repeatedDenied = false
        do {
            let ticket = try await coordinator.prepare(sink: .historyMarkdown, consent: { true })
            do {
                try await coordinator.perform(ticket) {
                    try Data("A53_INTEGRATION_SYNTHETIC_TEXT".utf8).write(to: output, options: .atomic)
                    writes += 1
                }
            } catch is PlaintextExportCoordinator.Failure {
                // Только counting outcome: raw error/response не выводятся.
            }
            do {
                try await coordinator.perform(ticket) {
                    try Data("duplicate".utf8).write(to: output, options: .atomic)
                    writes += 1
                }
            } catch is PlaintextExportCoordinator.Failure { repeatedDenied = true }
            if let cleanup = coordinator.shutdown() { await cleanup.value }
            let summary: [String: Any] = ["writes": writes, "repeated_callback_denied": repeatedDenied]
            let data = try JSONSerialization.data(withJSONObject: summary, options: [.sortedKeys])
            print(String(decoding: data, as: UTF8.self))
            if writes != expected || !repeatedDenied { exit(1) }
        } catch {
            print("{\"failed\":true}")
            exit(1)
        }
    }
}
