import Foundation
#if !HISTORY_MIGRATION_OBSERVATION_STANDALONE
import XCTest
@testable import KrabEarAgent
#endif

private struct MigrationCheckFailure: Error, CustomStringConvertible {
    let description: String
}

@MainActor
private func check(_ value: @autoclosure () -> Bool, _ message: String) throws {
    if !value() { throw MigrationCheckFailure(description: message) }
}

/// Fake только на границах; удержанные completion/timer/sheet вызывают production код.
@MainActor
private final class MigrationEffects {
    typealias Coordinator = HistoryMigrationObservationCoordinator
    var trace: [String] = []
    var requests: [(String, Coordinator.Completion)] = []
    var scheduled: [(TimeInterval, Coordinator.ScheduledAction)] = []
    var confirmations: [(Int, Coordinator.Confirmation)] = []
    var events: [Coordinator.RenderEvent] = []
    var watcher = false
    var terminal: String?
    var synchronousResponse: [String: Any]?

    lazy var coordinator = Coordinator(
        request: { [unowned self] method, completion in
            trace.append("request:\(method)")
            if let synchronousResponse { completion(synchronousResponse) }
            else { requests.append((method, completion)) }
        },
        schedule: { [unowned self] delay, action in
            trace.append("schedule:\(delay)")
            scheduled.append((delay, action))
        },
        startWatcher: { [unowned self] in
            trace.append("watch:start"); watcher = true; terminal = nil
        },
        stopWatcher: { [unowned self] in
            trace.append("watch:stop"); watcher = false; terminal = nil
        },
        hasWatcher: { [unowned self] in watcher },
        terminalStatus: { [unowned self] in terminal },
        render: { [unowned self] event in trace.append("render"); events.append(event) },
        presentConfirmation: { [unowned self] count, completion in
            trace.append("sheet:\(count)"); confirmations.append((count, completion))
        }
    )

    func activate() { coordinator.activate() }

    func takeRequest(_ method: String) throws -> Coordinator.Completion {
        try check(!requests.isEmpty, "нет запроса \(method)")
        let entry = requests.removeFirst()
        try check(entry.0 == method, "ожидался \(method), получен \(entry.0)")
        return entry.1
    }

    func begin() throws -> Coordinator.Completion {
        coordinator.startMigration()
        let completion = try takeRequest("migrate_history_encryption")
        completion(["result": ["ok": true, "status": "started"]])
        return try takeRequest("get_history_encryption_status")
    }

    func status(_ result: [String: Any]) -> [String: Any] { ["result": result] }
}

@MainActor
private enum MigrationObservationChecks {
    typealias Case = (String, @MainActor () throws -> Void)

    static let controls: [Case] = [
        ("watcher_before_migrate_and_already_running", {
            let f = MigrationEffects(); f.activate()
            f.coordinator.startMigration()
            try check(f.trace == ["watch:start", "render", "request:migrate_history_encryption"], "порядок watcher/start IPC изменён")
            let start = try f.takeRequest("migrate_history_encryption")
            start(f.status(["ok": true, "status": "already_running"]))
            try check(f.coordinator.polling && f.watcher, "already_running не запустил наблюдение")
            try check(f.events == [.starting, .polling], "start/poll render изменён")
            _ = try f.takeRequest("get_history_encryption_status")
        }),
        ("refresh_takeover_and_visibility", {
            let f = MigrationEffects(); f.activate(); f.coordinator.refresh()
            let refresh = try f.takeRequest("get_history_encryption_status")
            refresh(f.status(["enabled": true, "plaintext": 8, "migrating": false]))
            try check(f.events == [.visibility(enabled: true, plaintext: 8)], "visibility изменилась")
            f.coordinator.refresh()
            let takeover = try f.takeRequest("get_history_encryption_status")
            takeover(f.status(["migrating": true]))
            try check(f.coordinator.polling && f.watcher, "refresh не подхватил migrating")
            _ = try f.takeRequest("get_history_encryption_status")
            let snapshot = f.trace; f.coordinator.refresh()
            try check(f.trace == snapshot, "refresh должен уступать polling")
        }),
        ("retry_half_second_for_error_and_migrating", {
            for response: [String: Any]? in [nil, ["result": "malformed"], ["result": ["migrating": true, "total": 9, "encrypted": 4, "plaintext": 5, "pct": 44]]] {
                let f = MigrationEffects(); f.activate(); let poll = try f.begin()
                poll(response)
                try check(f.scheduled.count == 1 && f.scheduled[0].0 == 0.5, "IPC error/migrating должен повторяться через 0.5")
                if response?["result"] is [String: Any] {
                    try check(f.events.last == .progress(pct: 44, encrypted: 4, total: 9, showCount: true), "прогресс/счётчик изменён")
                }
                f.scheduled.removeFirst().1()
                _ = try f.takeRequest("get_history_encryption_status")
            }
        }),
        ("ipc_over_sse_and_all_terminal_outcomes", {
            let rows: [(String?, String?, Int, HistoryMigrationOutcome)] = [
                ("done", "backup_cleanup_failed", 0, .done),
                ("backup_cleanup_failed", "done", 0, .backupCleanupFailed),
                ("verification_failed", "done", 0, .failed(status: "verification_failed")),
                (nil, "error", 0, .failed(status: "error")),
                ("", "backup_cleanup_failed", 0, .backupCleanupFailed),
                (nil, nil, 7, .partial(plaintext: 7))
            ]
            for (ipc, sse, remaining, expected) in rows {
                let f = MigrationEffects(); f.activate(); let poll = try f.begin()
                f.terminal = sse
                var result: [String: Any] = ["migrating": false, "plaintext": remaining]
                if let ipc { result["last_migration_status"] = ipc }
                poll(f.status(result))
                try check(f.events.last == .outcome(expected), "literal outcome \(expected) не получен")
                try check(!f.coordinator.polling && !f.watcher && f.scheduled.isEmpty, "terminal не завершил наблюдение")
            }
        }),
        ("terminal_fallback_one_and_half_seconds", {
            let f = MigrationEffects(); f.activate(); let poll = try f.begin()
            poll(f.status(["migrating": false, "plaintext": 0]))
            try check(f.events.last == .finishing && !f.coordinator.polling && f.watcher, "нет фазы завершения")
            try check(f.scheduled.count == 1 && f.scheduled[0].0 == 1.5, "fallback должен ждать 1.5")
            f.terminal = "backup_cleanup_failed"
            f.scheduled.removeFirst().1()
            try check(f.events.last == .outcome(.backupCleanupFailed) && !f.watcher, "fallback не прочитал late SSE")
        }),
        ("confirmation_positive_decline_and_empty_count", {
            for confirmed in [true, false] {
                let f = MigrationEffects(); f.activate(); f.coordinator.confirmAndStartMigration()
                let count = try f.takeRequest("get_history_encryption_status")
                count(f.status(["plaintext": 11]))
                try check(f.confirmations.count == 1 && f.confirmations[0].0 == 11, "sheet count изменён")
                f.confirmations.removeFirst().1(confirmed)
                try check(f.requests.count == (confirmed ? 1 : 0), "sheet response не соблюдён")
                if confirmed { _ = try f.takeRequest("migrate_history_encryption") }
            }
            let f = MigrationEffects(); f.activate(); f.coordinator.confirmAndStartMigration()
            let count = try f.takeRequest("get_history_encryption_status")
            count(nil)
            try check(f.confirmations.isEmpty, "ошибка count не должна показывать sheet")
            _ = try f.takeRequest("get_history_encryption_status")
        }),
        ("synchronous_completion_and_repeated_activate", {
            let f = MigrationEffects(); f.activate()
            f.synchronousResponse = ["result": ["enabled": true, "plaintext": 4]]
            f.coordinator.refresh()
            try check(f.events == [.visibility(enabled: true, plaintext: 4)], "sync refresh не работает")
            let immediate = MigrationEffects(); immediate.activate()
            immediate.synchronousResponse = ["result": ["ok": true, "status": "started", "migrating": false,
                                                       "plaintext": 0, "last_migration_status": "done"]]
            immediate.coordinator.startMigration()
            try check(immediate.trace == ["watch:start", "render", "request:migrate_history_encryption",
                                          "render", "request:get_history_encryption_status", "render", "watch:stop", "render"],
                      "sync start/status нарушили порядок watcher/IPC/render")
            try check(immediate.events.last == .outcome(.done) && !immediate.watcher && !immediate.coordinator.polling,
                      "sync start/status не завершили наблюдение")
            f.synchronousResponse = nil
            _ = try f.begin()
            let snapshot = f.trace; f.activate(); f.coordinator.refresh()
            try check(f.trace == snapshot && f.coordinator.polling && f.watcher, "повторный activate разрушил наблюдение")
        }),
        ("malformed_refresh_and_start_preserve_failures", {
            for response: [String: Any]? in [nil, ["result": "bad"]] {
                let f = MigrationEffects(); f.activate(); f.coordinator.refresh()
                let refresh = try f.takeRequest("get_history_encryption_status")
                refresh(response)
                try check(f.events.isEmpty && f.scheduled.isEmpty, "malformed refresh изменяет UI")
                f.coordinator.startMigration()
                let start = try f.takeRequest("migrate_history_encryption")
                start(response)
                try check(f.events.last == .unavailable && !f.watcher && !f.coordinator.polling, "malformed start должен показать unavailable")
                f.coordinator.refresh()
                _ = try f.takeRequest("get_history_encryption_status")
            }
        })
    ]

    /// Для каждой ветки доставляем настоящий удержанный callback после close и после reopen B.
    static func staleCases() -> [Case] {
        var cases: [Case] = []
        for reopen in [false, true] {
            for kind in ["start_success", "start_unavailable", "start_error", "start_malformed", "refresh_migrating", "refresh_visibility", "poll_terminal", "poll_migrating", "poll_error", "poll_malformed", "retry_0.5", "fallback_1.5", "confirmation_count", "confirmation_zero", "confirmation_error", "sheet_accept", "sheet_decline"] {
                cases.append(("stale_\(kind)_\(reopen ? "reopen" : "closed")", {
                    let f = MigrationEffects(); f.activate()
                    let late: @MainActor () -> Void
                    switch kind {
                    case "start_success", "start_unavailable", "start_error", "start_malformed":
                        f.coordinator.startMigration()
                        let callback = try f.takeRequest("migrate_history_encryption")
                        let response: [String: Any]?
                        if kind == "start_success" { response = f.status(["ok": true, "status": "started"]) }
                        else if kind == "start_unavailable" { response = f.status(["ok": false, "status": "encryption_unavailable"]) }
                        else if kind == "start_malformed" { response = ["result": "bad"] }
                        else { response = nil }
                        late = { callback(response) }
                    case "refresh_migrating", "refresh_visibility":
                        f.coordinator.refresh()
                        let callback = try f.takeRequest("get_history_encryption_status")
                        late = { callback(f.status(["enabled": true, "plaintext": 8, "migrating": kind == "refresh_migrating"])) }
                    case "poll_terminal", "poll_migrating", "poll_error", "poll_malformed":
                        let callback = try f.begin()
                        let response: [String: Any]?
                        if kind == "poll_error" { response = nil }
                        else if kind == "poll_malformed" { response = ["result": "bad"] }
                        else { response = f.status(["migrating": kind == "poll_migrating", "plaintext": 0, "last_migration_status": "done"]) }
                        late = { callback(response) }
                    case "retry_0.5", "fallback_1.5":
                        let callback = try f.begin()
                        callback(kind == "retry_0.5" ? nil : f.status(["migrating": false, "plaintext": 0]))
                        try check(f.scheduled.count == 1, "нет queued timer")
                        let timer = f.scheduled.removeFirst().1
                        late = { timer() }
                    default:
                        f.coordinator.confirmAndStartMigration()
                        let callback = try f.takeRequest("get_history_encryption_status")
                        if kind.hasPrefix("sheet_") {
                            callback(f.status(["plaintext": 9]))
                            let sheet = f.confirmations.removeFirst().1
                            late = { sheet(kind == "sheet_accept") }
                        } else {
                            late = { callback(kind == "confirmation_error" ? nil : f.status(["plaintext": kind == "confirmation_zero" ? 0 : 9])) }
                        }
                    }
                    f.coordinator.close()
                    try check(!f.watcher, "close не остановил watcher")
                    if reopen {
                        f.activate(); f.coordinator.startMigration()
                        let startB = try f.takeRequest("migrate_history_encryption")
                        startB(f.status(["ok": true, "status": "started"]))
                    }
                    let snapshot = f.trace
                    let polling = f.coordinator.polling
                    late()
                    try check(f.trace == snapshot && f.coordinator.polling == polling, "late \(kind) изменил effects/polling после \(reopen ? "reopen B" : "close"): \(Array(f.trace.dropFirst(snapshot.count)))")
                    if reopen {
                        try check(f.watcher && f.coordinator.polling, "A повредил watcher/polling B")
                        let next = try f.takeRequest("get_history_encryption_status")
                        next(f.status(["migrating": true, "pct": 63, "encrypted": 5, "total": 8, "plaintext": 3]))
                        try check(f.events.last == .progress(pct: 63, encrypted: 5, total: 8, showCount: true), "B не получил собственный status")
                        try check(f.scheduled.last?.0 == 0.5, "B не запланировал свой retry")
                        f.scheduled.removeLast().1()
                        let terminal = try f.takeRequest("get_history_encryption_status")
                        terminal(f.status(["migrating": false, "plaintext": 0, "last_migration_status": "done"]))
                        try check(f.events.last == .outcome(.done) && !f.watcher && !f.coordinator.polling, "B не завершился своим terminal")
                    }
                }))
            }
        }
        cases.append(("refresh_started_before_new_observation_cannot_repaint", {
            for phase in ["start_pending", "polling", "done"] {
                let f = MigrationEffects(); f.activate(); f.coordinator.refresh()
                let refresh = try f.takeRequest("get_history_encryption_status")
                if phase == "start_pending" { f.coordinator.startMigration() }
                else {
                    let poll = try f.begin()
                    if phase == "done" { poll(f.status(["migrating": false, "plaintext": 0, "last_migration_status": "done"])) }
                }
                let snapshot = f.trace
                refresh(f.status(["enabled": true, "plaintext": 14, "migrating": false]))
                try check(f.trace == snapshot, "старый refresh перерисовал новое состояние \(phase)")
            }
        }))
        cases.append(("refresh_during_pending_start_cannot_repaint", {
            let f = MigrationEffects(); f.activate(); f.coordinator.startMigration()
            let start = try f.takeRequest("migrate_history_encryption")
            let snapshot = f.trace
            f.activate()
            f.coordinator.refresh()  // Повторный show при удержанном start completion.
            if !f.requests.isEmpty {
                let refresh = try f.takeRequest("get_history_encryption_status")
                refresh(f.status(["enabled": true, "plaintext": 14, "migrating": false]))
            }
            try check(f.trace == snapshot && f.events.last == .starting && f.watcher,
                      "refresh во время pending start изменил starting UI/watch/request state")
            start(f.status(["ok": true, "status": "started"]))
            try check(f.coordinator.polling && f.watcher, "собственный start не запустил polling")
            let poll = try f.takeRequest("get_history_encryption_status")
            poll(f.status(["migrating": false, "plaintext": 0, "last_migration_status": "done"]))
            f.coordinator.refresh()
            _ = try f.takeRequest("get_history_encryption_status")
        }))
        cases.append(("inactive_entries_and_close_idempotence", {
            let f = MigrationEffects()
            f.coordinator.refresh(); f.coordinator.startMigration(); f.coordinator.confirmAndStartMigration()
            try check(f.trace.isEmpty, "inactive entry создал effects")
            f.activate(); _ = try f.begin(); f.coordinator.close()
            try check(!f.watcher && !f.coordinator.polling, "close не очистил polling/watcher")
            let snapshot = f.trace
            f.coordinator.close(); f.coordinator.refresh(); f.coordinator.startMigration(); f.coordinator.confirmAndStartMigration()
            try check(f.trace == snapshot, "повторный close/inactive entry создал effects")
        }))
        return cases
    }
}

#if HISTORY_MIGRATION_OBSERVATION_STANDALONE
@main
private struct MigrationObservationRunner {
    @MainActor
    static func main() {
        let cases = MigrationObservationChecks.controls + (CommandLine.arguments.contains("--controls-only") ? [] : MigrationObservationChecks.staleCases())
        var failures = 0
        for (name, run) in cases {
            do { try run(); print("PASS \(name)") }
            catch { failures += 1; print("FAIL \(name): \(error)") }
        }
        print("\(cases.count - failures)/\(cases.count) migration observation checks passed")
        if failures > 0 { exit(1) }
    }
}
#else
final class HistoryMigrationObservationCoordinatorTests: XCTestCase {
    func test_shared_production_callback_suite() async {
        await MainActor.run {
            for (name, run) in MigrationObservationChecks.controls + MigrationObservationChecks.staleCases() {
                do { try run() }
                catch { XCTFail("\(name): \(error)") }
            }
        }
    }
}
#endif
