import Foundation
#if !PLAINTEXT_EXPORT_STANDALONE
import XCTest
@testable import KrabEarAgent
#endif

/// Один fake transport для XCTest и standalone runner; production coordinator не подменяется.
@MainActor
private final class ExportSocket: IPCSocketProviding, @unchecked Sendable {
    var encrypted = true
    var privacy = false
    var generation = 1
    var epoch = String(repeating: "a", count: 64)
    let capability = String(repeating: "g", count: 43)
    var calls: [(String, [String: Any])] = []
    var before: (@MainActor (String, [String: Any]) async throws -> Void)?
    var rawTransform: ((String, String) -> String)?
    var transform: ((String, [String: Any]) -> [String: Any])?
    var repeatedReceipt = false
    var timeoutValidation = false

    nonisolated func send(payload: Data, timeoutSec: Int) async throws -> Data {
        try await respond(payload)
    }

    private func respond(_ payload: Data) async throws -> Data {
        let request = try JSONSerialization.jsonObject(with: payload) as! [String: Any]
        let method = request["method"] as! String
        let params = request["params"] as! [String: Any]
        calls.append((method, params))
        try await before?(method, params)
        var result: [String: Any]
        switch method {
        case "get_plaintext_export_policy":
            result = ["ok": !privacy, "epoch": epoch, "policy_generation": generation,
                      "encryption_enabled": encrypted, "privacy_mode_enabled": privacy,
                      "allowed_without_grant": !encrypted && !privacy]
            if privacy { result["reason"] = "privacy_mode_active" }
        case "grant_plaintext_export_session":
            if privacy || params["expected_epoch"] as? String != epoch || params["expected_policy_generation"] as? Int != generation {
                result = ["ok": false, "reason": "plaintext_session_expired"]
            } else {
                result = ["ok": true, "epoch": epoch, "policy_generation": generation, "capability": capability]
            }
        case "validate_plaintext_export":
            if timeoutValidation { throw IPCError.backendError("SECRET_SENTINEL") }
            if privacy || params["epoch"] as? String != epoch || params["expected_policy_generation"] as? Int != generation
                || (encrypted && params["capability"] as? String != capability) {
                result = ["ok": false, "reason": privacy ? "privacy_mode_active" : "plaintext_session_expired"]
            } else {
                let seq = params["operation_seq"] as! Int
                result = ["ok": true, "epoch": epoch, "policy_generation": generation,
                          "receipt": String(format: "%043d", repeatedReceipt ? 1 : seq),
                          "operation_seq": seq, "sink_kind": params["sink_kind"]!]
            }
        case "export_history", "run_obsidian_sync":
            let context = params["plaintext_export"] as? [String: Any] ?? [:]
            if privacy || context["epoch"] as? String != epoch || context["expected_policy_generation"] as? Int != generation {
                result = ["ok": false, "reason": "plaintext_session_expired"]
            } else if method == "export_history" {
                result = ["path": "/isolated/history.md", "total_items": 1, "content": "test markdown"]
            } else {
                result = ["synced_count": 1, "skipped_count": 0, "errors": [], "new_files": [],
                          "updated_files": [], "partial": false, "reason": NSNull()]
            }
        default:
            result = ["ok": true]
        }
        var envelope: [String: Any] = ["id": request["id"]!, "ok": true, "result": result]
        envelope = transform?(method, envelope) ?? envelope
        let data = try JSONSerialization.data(withJSONObject: envelope, options: [.sortedKeys])
        let json = String(data: data, encoding: .utf8)!
        return Data((rawTransform?(method, json) ?? json).utf8) + Data("\n".utf8)
    }

    func count(_ method: String) -> Int { calls.filter { $0.0 == method }.count }
    func make() -> PlaintextExportCoordinator {
        PlaintextExportCoordinator(ipc: IPCClient(socketProvider: self))
    }
}

private struct CheckFailure: Error { let message: String }
@MainActor
private enum ExportChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) throws {
        if !condition() { throw CheckFailure(message: message) }
    }
    static func denied(_ body: () async throws -> Void) async throws {
        do { try await body() } catch is PlaintextExportCoordinator.Failure { return }
        throw CheckFailure(message: "operation must fail with a safe typed error")
    }
    static func cancel() async throws {
        let socket = ExportSocket(), coordinator = socket.make()
        try await denied { _ = try await coordinator.prepare(sink: .historyMarkdown, consent: { false }) }
        try check(socket.count("grant_plaintext_export_session") == 0, "cancel granted capability")
        try check(socket.count("validate_plaintext_export") == 0, "cancel validated write")
    }
    static func offAndRepeat() async throws {
        let socket = ExportSocket(); socket.encrypted = false
        let coordinator = socket.make()
        var consent = 0, writes = 0
        let ticket = try await coordinator.prepare(sink: .historyMarkdown, consent: { consent += 1; return true })
        try await coordinator.perform(ticket) { writes += 1 }
        try await denied { try await coordinator.perform(ticket) { writes += 1 } }
        try check(writes == 1 && consent == 0, "OFF/repeat write count")
        try check(socket.count("grant_plaintext_export_session") == 0, "OFF granted")
        try check(socket.count("validate_plaintext_export") == 1, "OFF needs one fresh validation")
    }
    static func heldContexts() async throws {
        let sinks: [PlaintextExportSink] = [.historyMarkdown, .historyNDJSON, .historySelected,
            .historyActionItems, .historyMeetingReport, .historyStatsReport, .historyPDF]
        for sink in sinks {
            for mutation in 0..<4 {
                let socket = ExportSocket(), coordinator = socket.make()
                let ticket = try await coordinator.prepare(sink: sink, consent: { true })
                switch mutation {
                case 0: socket.epoch = String(repeating: "b", count: 64)
                case 1: socket.generation += 1
                case 2: socket.privacy = true
                default: socket.encrypted = false; socket.generation += 1
                }
                var writes = 0
                try await denied { try await coordinator.perform(ticket) { writes += 1 } }
                try await denied { try await coordinator.perform(ticket) { writes += 1 } }
                try check(writes == 0, "held panel/renderer wrote after context change")
                try check(socket.count("validate_plaintext_export") == 1, "duplicate callback revalidated")
                try check(socket.count("grant_plaintext_export_session") == 1, "silent regrant")
            }
        }
    }
    static func heldConsent() async throws {
        let socket = ExportSocket(), coordinator = socket.make()
        try await denied {
            _ = try await coordinator.prepare(sink: .historyPDF, consent: {
                socket.generation += 1
                return true
            })
        }
        try check(socket.count("grant_plaintext_export_session") == 1, "grant silently retried after held consent")
        var prompts = 0
        let ticket = try await coordinator.prepare(sink: .historyPDF, consent: { prompts += 1; return true })
        var writes = 0
        try await coordinator.perform(ticket) { writes += 1 }
        try check(prompts == 1 && writes == 1, "new explicit action did not require new consent")
    }
    static func repeatedReceiptAndGrantReuse() async throws {
        let socket = ExportSocket(); socket.repeatedReceipt = true
        let coordinator = socket.make()
        var prompts = 0, writes = 0
        let first = try await coordinator.prepare(sink: .historyMarkdown, consent: { prompts += 1; return true })
        let second = try await coordinator.prepare(sink: .historyPDF, consent: { prompts += 1; return true })
        try await coordinator.perform(first) { writes += 1 }
        try await denied { try await coordinator.perform(second) { writes += 1 } }
        try check(prompts == 1 && writes == 1, "grant reuse or duplicate receipt accepted")
        try check(socket.count("validate_plaintext_export") == 2, "each distinct file needs validation")
    }
    static func uncertainAndSanitized() async throws {
        let socket = ExportSocket(), coordinator = socket.make()
        let ticket = try await coordinator.prepare(sink: .historyPDF, consent: { true })
        socket.timeoutValidation = true
        var writes = 0
        do {
            try await coordinator.perform(ticket) { writes += 1 }
            throw CheckFailure(message: "uncertain validation wrote")
        } catch let failure as PlaintextExportCoordinator.Failure {
            try check(!PlaintextExportCoordinator.errorMessage(failure).contains("SECRET_SENTINEL"), "secret in safe failure")
        }
        try check(writes == 0 && socket.count("validate_plaintext_export") == 1, "uncertain validation retried")
        socket.timeoutValidation = false
        var prompts = 0
        _ = try await coordinator.prepare(sink: .historyPDF, consent: { prompts += 1; return true })
        try check(prompts == 1, "uncertain failure retained grant")
        try check(!PlaintextExportCoordinator.errorMessage(IPCError.backendError("SECRET_SENTINEL")).contains("SECRET_SENTINEL"),
                  "foreign error propagated secret")
    }
    static func malformedPolicy() async throws {
        let mutations: [(inout [String: Any]) -> Void] = [
            { $0["ok"] = 1 }, { $0["epoch"] = "BAD" }, { $0["policy_generation"] = true },
            { $0["policy_generation"] = -1 }, { $0["encryption_enabled"] = 1 },
            { $0["privacy_mode_enabled"] = 0 }, { $0["allowed_without_grant"] = true },
            { $0.removeValue(forKey: "privacy_mode_enabled") }, { $0["reason"] = "SECRET_SENTINEL" },
        ]
        for mutation in mutations {
            let socket = ExportSocket(), coordinator = socket.make()
            socket.transform = { method, envelope in
                guard method == "get_plaintext_export_policy" else { return envelope }
                var out = envelope, result = envelope["result"] as! [String: Any]
                mutation(&result); out["result"] = result; return out
            }
            try await denied { _ = try await coordinator.prepare(sink: .historyMarkdown, consent: { true }) }
            try check(socket.count("grant_plaintext_export_session") == 0, "malformed policy granted")
        }
        for raw in ["true", "1.0", "1e0", "1.5"] {
            let socket = ExportSocket(), coordinator = socket.make()
            socket.rawTransform = { method, json in
                method == "get_plaintext_export_policy"
                    ? json.replacingOccurrences(of: "\"policy_generation\":1", with: "\"policy_generation\":" + raw) : json
            }
            try await denied { _ = try await coordinator.prepare(sink: .historyMarkdown, consent: { true }) }
        }
    }
    static func malformedValidation() async throws {
        let mutations: [(inout [String: Any]) -> Void] = [
            { $0["ok"] = 1 }, { $0["epoch"] = String(repeating: "b", count: 64) },
            { $0["policy_generation"] = 2 }, { $0["policy_generation"] = true },
            { $0["operation_seq"] = 2 }, { $0["operation_seq"] = true },
            { $0["sink_kind"] = "history-md" }, { $0["receipt"] = "SECRET_SENTINEL" },
            { $0.removeValue(forKey: "receipt") }, { $0["error"] = "SECRET_SENTINEL" },
            { $0["ok"] = false; $0["reason"] = "SECRET_SENTINEL" },
        ]
        for mutation in mutations {
            let socket = ExportSocket(), coordinator = socket.make()
            let ticket = try await coordinator.prepare(sink: .historyPDF, consent: { true })
            socket.transform = { method, envelope in
                guard method == "validate_plaintext_export" else { return envelope }
                var out = envelope, result = envelope["result"] as! [String: Any]
                mutation(&result); out["result"] = result; return out
            }
            var writes = 0
            try await denied { try await coordinator.perform(ticket) { writes += 1 } }
            try check(writes == 0, "malformed validation wrote")
        }
        for field in ["operation_seq", "policy_generation"] {
            let socket = ExportSocket(), coordinator = socket.make()
            let ticket = try await coordinator.prepare(sink: .historyPDF, consent: { true })
            socket.rawTransform = { method, json in
                method == "validate_plaintext_export"
                    ? json.replacingOccurrences(of: "\"" + field + "\":1", with: "\"" + field + "\":1.0") : json
            }
            try await denied { try await coordinator.perform(ticket) {} }
        }
    }
    static func malformedEnvelopeAndGrant() async throws {
        for mode in 0..<8 {
            let socket = ExportSocket(), coordinator = socket.make()
            socket.transform = { method, envelope in
                var out = envelope
                if method == "get_plaintext_export_policy" && mode < 4 {
                    switch mode {
                    case 0: out["ok"] = 1
                    case 1: out.removeValue(forKey: "ok")
                    case 2: out["error"] = "SECRET_SENTINEL"
                    default: out["result"] = []
                    }
                } else if method == "grant_plaintext_export_session" && mode >= 4 {
                    var result = out["result"] as! [String: Any]
                    switch mode {
                    case 4: result["ok"] = 1
                    case 5: result["epoch"] = String(repeating: "b", count: 64)
                    case 6: result["policy_generation"] = true
                    default: result["capability"] = "SECRET_SENTINEL"
                    }
                    out["result"] = result
                }
                return out
            }
            try await denied { _ = try await coordinator.prepare(sink: .historyPDF, consent: { true }) }
        }
    }
    static func fifo() async throws {
        let socket = ExportSocket(), coordinator = socket.make()
        var release: CheckedContinuation<Void, Never>?
        var prompts = 0
        let firstTask = Task { @MainActor in
            try await coordinator.prepare(sink: .historyMarkdown, consent: {
                prompts += 1
                await withCheckedContinuation { release = $0 }
                return true
            })
        }
        while release == nil { await Task.yield() }
        let secondTask = Task { @MainActor in
            try await coordinator.prepare(sink: .historyPDF, consent: { prompts += 1; return true })
        }
        for _ in 0..<10 { await Task.yield() }
        try check(socket.count("get_plaintext_export_policy") == 1 && prompts == 1, "prepare queue reentered")
        release?.resume()
        let first = try await firstTask.value, second = try await secondTask.value
        var held: CheckedContinuation<Void, Never>?
        socket.before = { method, _ in
            if method == "validate_plaintext_export" && held == nil {
                await withCheckedContinuation { held = $0 }
            }
        }
        var writes = 0
        let write1 = Task { @MainActor in try await coordinator.perform(first) { writes += 1 } }
        while held == nil { await Task.yield() }
        let write2 = Task { @MainActor in try await coordinator.perform(second) { writes += 1 } }
        for _ in 0..<10 { await Task.yield() }
        try check(socket.count("validate_plaintext_export") == 1, "validation queue reentered")
        held?.resume()
        try await write1.value
        try await write2.value
        let seqs = socket.calls.filter { $0.0 == "validate_plaintext_export" }.map { $0.1["operation_seq"] as! Int }
        try check(seqs == [1, 2] && writes == 2 && prompts == 1, "FIFO sequence/grant reuse")
    }
    static func shutdownPending() async throws {
        for pendingMethod in ["grant_plaintext_export_session", "validate_plaintext_export"] {
            let socket = ExportSocket(), coordinator = socket.make()
            var release: CheckedContinuation<Void, Never>?
            socket.before = { method, _ in
                if method == pendingMethod { await withCheckedContinuation { release = $0 } }
            }
            var writes = 0
            let operation = Task { @MainActor in
                let ticket = try await coordinator.prepare(sink: .historyPDF, consent: { true })
                try await coordinator.perform(ticket) { writes += 1 }
            }
            while release == nil { await Task.yield() }
            let revoke = coordinator.shutdown()
            try check(revoke != nil, "normal quit cannot exit before pending grant cleanup")
            release?.resume()
            try await denied { try await operation.value }
            await revoke?.value
            for _ in 0..<20 { await Task.yield() }
            try check(writes == 0 && socket.count("revoke_plaintext_export_session") == 1, "quit/pending grant not revoked")
            try await denied { _ = try await coordinator.prepare(sink: .historyPDF, consent: { true }) }
            try check(coordinator.shutdown() == nil, "quit must be idempotent")
        }
    }
    static func backendNamespaceAndSecondCopy() async throws {
        let socket = ExportSocket(), coordinator = socket.make()
        let local = try await coordinator.prepare(sink: .historyMarkdown, consent: { true })
        let backend = try coordinator.deriveBackendTicket(from: local, method: .exportHistoryMarkdown)
        var writes = 0
        try await coordinator.perform(local) { writes += 1 }
        _ = try await coordinator.performBackend(backend, params: [:])
        try await denied { _ = try await coordinator.performBackend(backend, params: [:]) }
        let context = socket.calls.first { $0.0 == "export_history" }!.1["plaintext_export"] as! [String: Any]
        try check(Set(context.keys) == Set(["app_session_id", "epoch", "capability", "expected_policy_generation"]),
                  "backend namespace must not contain Swift seq")
        try check(writes == 1 && socket.count("export_history") == 1, "second copy count")
        let session = context["app_session_id"] as! String
        try check(UUID(uuidString: session)?.uuidString.lowercased() == session, "session is not canonical UUID")
        let next = try await coordinator.prepare(sink: .historyMarkdown, consent: { true })
        let staleCopy = try coordinator.deriveBackendTicket(from: next, method: .exportHistoryMarkdown)
        try await coordinator.perform(next) { writes += 1 }
        socket.generation += 1
        try await denied { _ = try await coordinator.performBackend(staleCopy, params: [:]) }
        try check(socket.count("grant_plaintext_export_session") == 1, "second copy silently regranted")
    }
    static func failedFirstCopyAndForeignTicket() async throws {
        let socket = ExportSocket(), coordinator = socket.make()
        let ticket = try await coordinator.prepare(sink: .historyMarkdown, consent: { true })
        let backend = try coordinator.deriveBackendTicket(from: ticket, method: .exportHistoryMarkdown)
        try await denied {
            try await coordinator.perform(ticket) { throw IPCError.backendError("SECRET_SENTINEL") }
        }
        try await denied { _ = try await coordinator.performBackend(backend, params: [:]) }
        try check(socket.count("export_history") == 0, "backend copy ran after local failure")
        let other = socket.make()
        let foreign = try await coordinator.prepare(sink: .historyPDF, consent: { true })
        try await denied { try await other.perform(foreign) {} }
        coordinator.discard(foreign)
        try await denied { try await coordinator.perform(foreign) {} }
    }
    static func obsidianResults() async throws {
        for mode in 0..<7 {
            let socket = ExportSocket(), coordinator = socket.make()
            socket.encrypted = false
            let ticket = try await coordinator.prepareBackend(method: .runObsidianSync, consent: { false })
            socket.transform = { method, envelope in
                guard method == "run_obsidian_sync" else { return envelope }
                var out = envelope, result = envelope["result"] as! [String: Any]
                switch mode {
                case 1: result["partial"] = true
                case 2: result["errors"] = ["SECRET_SENTINEL"]
                case 3: result["synced_count"] = true
                case 4: result["partial"] = 0
                case 5: result = ["error": "privacy", "user_msg": "SECRET_SENTINEL"]
                case 6: result = [:]
                default: break
                }
                out["result"] = result; return out
            }
            if mode == 0 { _ = try await coordinator.performBackend(ticket, params: ["force": true]) }
            else { try await denied { _ = try await coordinator.performBackend(ticket, params: ["force": true]) } }
            try check(socket.count("grant_plaintext_export_session") == 0, "OFF backend granted")
            try check(socket.count("validate_plaintext_export") == 0, "backend used Swift receipt protocol")
        }
    }
    static func queuedInvalidation() async throws {
        for backend in [false, true] {
            let socket = ExportSocket(), coordinator = socket.make()
            let first = try await coordinator.prepare(sink: .historyPDF, consent: { true })
            let next = try await coordinator.prepare(sink: .historyMarkdown, consent: { true })
            let nextBackend = try await coordinator.prepareBackend(method: .runObsidianSync, consent: { true })
            var release: CheckedContinuation<Void, Never>?
            socket.before = { method, _ in
                if method == "validate_plaintext_export" && socket.count(method) == 1 {
                    await withCheckedContinuation { release = $0 }
                    throw IPCError.timeout
                }
            }
            var writes = 0
            let failed = Task { @MainActor in try await coordinator.perform(first) { writes += 1 } }
            while release == nil { await Task.yield() }
            let queued = Task { @MainActor in
                if backend { _ = try await coordinator.performBackend(nextBackend, params: [:]) }
                else { try await coordinator.perform(next) { writes += 1 } }
            }
            for _ in 0..<10 { await Task.yield() }
            release?.resume()
            try await denied { try await failed.value }
            try await denied { try await queued.value }
            try check(writes == 0 && socket.count("validate_plaintext_export") == 1 && socket.count("run_obsidian_sync") == 0,
                      "queued operation retained invalidated grant")
        }
    }
    static func privacyBackendReason() async throws {
        let socket = ExportSocket(), coordinator = socket.make()
        let ticket = try await coordinator.prepareBackend(method: .runObsidianSync, consent: { true })
        socket.transform = { method, envelope in
            guard method == "run_obsidian_sync" else { return envelope }
            var out = envelope
            out["result"] = ["ok": false, "error": "privacy_mode_active", "user_msg": "SECRET_SENTINEL"]
            return out
        }
        do {
            _ = try await coordinator.performBackend(ticket, params: [:])
            throw CheckFailure(message: "privacy accepted")
        } catch PlaintextExportCoordinator.Failure.privacy { return }
        catch { throw CheckFailure(message: "privacy backend reason not preserved safely") }
    }
    static func invalidationRevokes() async throws {
        let socket = ExportSocket(), coordinator = socket.make()
        _ = try await coordinator.prepare(sink: .historyPDF, consent: { true })
        var releaseRevoke: CheckedContinuation<Void, Never>?
        socket.before = { method, _ in
            if method == "get_plaintext_export_policy" { throw IPCError.timeout }
            if method == "revoke_plaintext_export_session" {
                await withCheckedContinuation { releaseRevoke = $0 }
            }
        }
        try await denied { _ = try await coordinator.prepare(sink: .historyPDF, consent: { true }) }
        for _ in 0..<20 { await Task.yield() }
        try check(socket.count("revoke_plaintext_export_session") == 0, "old revoke may race a new grant")
        let drain = coordinator.shutdown()
        try check(drain != nil, "quit lost invalidated known grant")
        while releaseRevoke == nil { await Task.yield() }
        try check(socket.count("revoke_plaintext_export_session") == 1, "known grant was not revoked on quit")
        var drained = false
        let observe = Task { @MainActor in await drain?.value; drained = true }
        for _ in 0..<10 { await Task.yield() }
        try check(!drained, "quit drain finished before revoke response")
        releaseRevoke?.resume()
        await observe.value
        try check(drained && socket.count("revoke_plaintext_export_session") == 1, "revoke retry or lost completion")
    }
    static func runAll() async throws -> Int {
        try await invalidationRevokes()
        try await cancel()
        try await offAndRepeat()
        try await heldContexts()
        try await heldConsent()
        try await repeatedReceiptAndGrantReuse()
        try await uncertainAndSanitized()
        try await malformedPolicy()
        try await malformedValidation()
        try await malformedEnvelopeAndGrant()
        try await fifo()
        try await shutdownPending()
        try await backendNamespaceAndSecondCopy()
        try await failedFirstCopyAndForeignTicket()
        try await obsidianResults()
        try await queuedInvalidation()
        try await privacyBackendReason()
        return 17
    }
}

#if PLAINTEXT_EXPORT_STANDALONE
@main
struct PlaintextExportHarness {
    @MainActor static func main() async {
        do {
            let count = try await ExportChecks.runAll()
            print("PlaintextExportCoordinator behavioral checks: \(count) passed")
        } catch {
            // Никакой raw ошибки/параметров: fake намеренно содержит секретные sentinel.
            if let failure = error as? CheckFailure { print("Check failed: " + failure.message) }
            print("PlaintextExportCoordinator behavioral checks: FAILED")
            exit(1)
        }
    }
}
#else
@MainActor
final class PlaintextExportCoordinatorTests: XCTestCase {
    func testConsentCancellation() async throws { try await ExportChecks.cancel() }
    func testOffAndRepeatedCallback() async throws { try await ExportChecks.offAndRepeat() }
    func testHeldPanelsAndRenderers() async throws { try await ExportChecks.heldContexts() }
    func testHeldConsent() async throws { try await ExportChecks.heldConsent() }
    func testReceiptReplayAndGrantReuse() async throws { try await ExportChecks.repeatedReceiptAndGrantReuse() }
    func testUncertainValidationAndSanitization() async throws { try await ExportChecks.uncertainAndSanitized() }
    func testMalformedPolicy() async throws { try await ExportChecks.malformedPolicy() }
    func testMalformedValidation() async throws { try await ExportChecks.malformedValidation() }
    func testMalformedEnvelopeAndGrant() async throws { try await ExportChecks.malformedEnvelopeAndGrant() }
    func testFIFO() async throws { try await ExportChecks.fifo() }
    func testShutdownDuringRequests() async throws { try await ExportChecks.shutdownPending() }
    func testBackendNamespaceAndSecondCopy() async throws { try await ExportChecks.backendNamespaceAndSecondCopy() }
    func testFailedFirstCopyAndForeignTicket() async throws { try await ExportChecks.failedFirstCopyAndForeignTicket() }
    func testQueuedInvalidation() async throws { try await ExportChecks.queuedInvalidation() }
    func testPrivacyBackendReason() async throws { try await ExportChecks.privacyBackendReason() }
    func testInvalidationRevokes() async throws { try await ExportChecks.invalidationRevokes() }
    func testObsidianResults() async throws { try await ExportChecks.obsidianResults() }
}
#endif
