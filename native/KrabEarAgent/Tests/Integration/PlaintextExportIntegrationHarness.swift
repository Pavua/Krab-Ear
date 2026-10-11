// Отдельный executable тест: production IPC/coordinator, без NSApplication.
import Foundation

@main
struct PlaintextExportIntegrationHarness {
    @MainActor static func main() async {
        if CommandLine.arguments.count == 6, CommandLine.arguments[2] == "backend" {
            await runBackend()
            return
        }
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

    /// BackendTicket/context остаются в RAM Swift. stdin barrier перед perform
    /// позволяет тесту менять backend epoch/privacy после настоящего prepare.
    @MainActor static func runBackend() async {
        guard let method = PlaintextExportCoordinator.BackendMethod(rawValue: CommandLine.arguments[3]),
              [.exportTimelineSVG, .exportTimelineJSON, .exportTimelineICal].contains(method),
              ["grant", "decline"].contains(CommandLine.arguments[4]),
              ["run", "pause"].contains(CommandLine.arguments[5]) else { exit(2) }
        let coordinator = PlaintextExportCoordinator(ipc: IPCClient(socketPath: CommandLine.arguments[1]))
        var consentCalls = 0
        var performed = 0
        var blocks = 0
        var denied = false
        var denialReason = ""
        var repeatedDenied = false
        do {
            let ticket = try await coordinator.prepareBackend(method: method, consent: {
                consentCalls += 1
                return CommandLine.arguments[4] == "grant"
            })
            if CommandLine.arguments[5] == "pause" {
                // Только boolean readiness, без auth/error/path/text diagnostics.
                FileHandle.standardOutput.write(Data("{\"prepared\":true}\n".utf8))
                guard readLine() == "continue" else {
                    if let cleanup = coordinator.shutdown() { await cleanup.value }
                    exit(2)
                }
            }
            do {
                let result = try await coordinator.performBackend(ticket, params: [:])
                performed += 1
                blocks = result["blocks"] as? Int ?? 0
            } catch {
                denied = true
                denialReason = failureLabel(error)
            }
            do {
                _ = try await coordinator.performBackend(ticket, params: [:])
            } catch { repeatedDenied = true }
        } catch {
            denied = true
            denialReason = failureLabel(error)
        }
        if let cleanup = coordinator.shutdown() { await cleanup.value }
        let summary: [String: Any] = ["consent_calls": consentCalls, "performed": performed,
                                      "blocks": blocks, "denied": denied,
                                      "denial_reason": denialReason,
                                      "repeated_callback_denied": repeatedDenied]
        guard let data = try? JSONSerialization.data(withJSONObject: summary, options: [.sortedKeys]) else { exit(2) }
        print(String(decoding: data, as: UTF8.self))
    }

    /// Только фиксированные labels production enum; raw error не сериализуется.
    static func failureLabel(_ error: Error) -> String {
        guard let failure = error as? PlaintextExportCoordinator.Failure else { return "unknown" }
        switch failure {
        case .cancelled: return "cancelled"
        case .closed: return "closed"
        case .expired: return "expired"
        case .privacy: return "privacy"
        case .unavailable: return "unavailable"
        case .malformed: return "malformed"
        case .transport: return "transport"
        case .writeFailed: return "writeFailed"
        case .partial: return "partial"
        }
    }
}
