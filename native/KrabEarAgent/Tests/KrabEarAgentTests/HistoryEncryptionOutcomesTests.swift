import XCTest
@testable import KrabEarAgent

/// Honest-state логика секции «Безопасность»: итог миграции и переключатель шифрования.
final class HistoryEncryptionOutcomesTests: XCTestCase {

    // MARK: Миграция

    /// G3-P04-04: «0 незашифрованных» без подтверждённого итога — не «Готово»: backend отдаёт
    /// терминальный статус всегда, а его отсутствие значит «миграция не наша/перезапуск», и
    /// судьбу plaintext .bak подтвердить нечем.
    func test_migration_plaintext_zero_without_terminal_status_is_pending_not_done() {
        XCTAssertEqual(HistoryMigrationOutcome.resolve(plaintext: 0, terminalStatus: nil), .pending)
        XCTAssertEqual(HistoryMigrationOutcome.resolve(plaintext: 0, terminalStatus: ""), .pending)
        XCTAssertEqual(HistoryMigrationOutcome.resolve(plaintext: 0, terminalStatus: "done"), .done)
    }

    func test_migration_done_status_with_remaining_plaintext_is_partial() {
        XCTAssertEqual(HistoryMigrationOutcome.resolve(plaintext: 3, terminalStatus: "done"), .partial(plaintext: 3))
    }

    func test_presenter_has_pending_branch_without_done_glyph() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/HistoryPanelController+SecuritySettings.swift")
        let src = try String(contentsOf: url, encoding: .utf8)
        let start = try XCTUnwrap(src.range(of: "case .pending:"))
        let tail = String(src[start.upperBound...])
        let end = tail.range(of: "case .failed")?.lowerBound ?? tail.endIndex
        let branch = String(tail[..<end])
        XCTAssertFalse(branch.contains("Готово"), "pending не должен показывать «Готово»")
        XCTAssertTrue(branch.contains("не подтвержд"))
    }

    func test_migration_backup_cleanup_failed_is_not_done_even_when_plaintext_zero() {
        XCTAssertEqual(
            HistoryMigrationOutcome.resolve(plaintext: 0, terminalStatus: "backup_cleanup_failed"),
            .backupCleanupFailed)
    }

    func test_migration_remaining_plaintext_is_partial() {
        XCTAssertEqual(HistoryMigrationOutcome.resolve(plaintext: 7, terminalStatus: nil), .partial(plaintext: 7))
    }

    func test_migration_event_line_parsing() {
        XCTAssertEqual(
            HistoryMigrationTerminalWatcher.terminalStatus(
                fromSSELine: #"data: {"total": 3, "done": 3, "encrypted": 3, "pct": 0, "status": "backup_cleanup_failed"}"#),
            "backup_cleanup_failed")
        XCTAssertEqual(
            HistoryMigrationTerminalWatcher.terminalStatus(fromSSELine: #"data: {"status": "done", "pct": 100}"#),
            "done")
        // Промежуточный прогресс и служебные строки — не терминальные.
        XCTAssertNil(HistoryMigrationTerminalWatcher.terminalStatus(fromSSELine: #"data: {"status": "encrypting", "pct": 40}"#))
        XCTAssertNil(HistoryMigrationTerminalWatcher.terminalStatus(fromSSELine: "event: history_encryption.migrate.progress"))
        XCTAssertNil(HistoryMigrationTerminalWatcher.terminalStatus(fromSSELine: ": keepalive"))
        XCTAssertNil(HistoryMigrationTerminalWatcher.terminalStatus(fromSSELine: "data: not-json"))
    }

    func test_watcher_remembers_terminal_status_until_reset() {
        let w = HistoryMigrationTerminalWatcher()
        XCTAssertNil(w.terminalStatus)
        w.ingest(line: #"data: {"status": "encrypting"}"#)
        XCTAssertNil(w.terminalStatus)
        w.ingest(line: #"data: {"status": "backup_cleanup_failed"}"#)
        XCTAssertEqual(w.terminalStatus, "backup_cleanup_failed")
        w.reset()
        XCTAssertNil(w.terminalStatus)
    }

    // MARK: set_history_encryption

    func test_encryption_change_success() {
        let resp: [String: Any] = ["ok": true, "result": ["ok": true, "enabled": true, "available": true]]
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: resp, desired: true), .applied)
    }

    func test_encryption_change_requires_confirmed_target_and_strict_booleans() throws {
        let cases: [(String, EncryptionChangeOutcome)] = [
            (#"{"ok":true,"result":{"ok":true,"available":true}}"#, .failed(enabled: nil)),
            (#"{"ok":true,"result":{"ok":true,"enabled":false,"available":true}}"#, .failed(enabled: false)),
            (#"{"ok":true,"result":{"ok":1,"enabled":true,"available":true}}"#, .failed(enabled: true)),
            (#"{"ok":true,"result":{"ok":true,"enabled":1,"available":true}}"#, .failed(enabled: nil)),
            (#"{"ok":true,"result":{"ok":true,"enabled":true,"available":1}}"#, .failed(enabled: true)),
            (#"{"ok":0,"result":{"ok":true,"enabled":true,"available":true}}"#, .failed(enabled: nil)),
            (#"{"ok":true,"result":{"ok":false,"error":"keychain_unavailable","enabled":0}}"#, .keychainUnavailable(enabled: nil)),
        ]
        for (wire, expected) in cases {
            let response = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(wire.utf8)) as? [String: Any])
            XCTAssertEqual(EncryptionChangeOutcome.resolve(response: response, desired: true), expected, wire)
        }
    }

    func test_encryption_change_keychain_unavailable() {
        let resp: [String: Any] = ["ok": true, "result": ["ok": false, "error": "keychain_unavailable", "enabled": false]]
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: resp, desired: true), .keychainUnavailable(enabled: false))
    }

    func test_encryption_change_other_rejection_is_failed_with_reported_state() {
        let resp: [String: Any] = ["ok": true, "result": ["ok": false, "error": "settings_write_failed", "enabled": true]]
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: resp, desired: true), .failed(enabled: true))
    }

    func test_encryption_change_missing_ok_is_failed_not_applied() {
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: ["ok": true, "result": ["enabled": true]], desired: true), .failed(enabled: true))
    }

    func test_encryption_change_ipc_failure_or_garbage_is_failed() {
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: nil, desired: true), .failed(enabled: nil))
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: ["ok": false], desired: true), .failed(enabled: nil))
    }

    func test_encryption_change_confirms_disabled_target() {
        let response: [String: Any] = ["ok": true, "result": ["ok": true, "enabled": false, "available": true]]
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: response, desired: false), .applied)
    }
}

/// Проводка: SecuritySettings обязан реально звать чистые функции (иначе они декоративны).
final class SecuritySettingsWiringContractTests: XCTestCase {
    private func source() throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/HistoryPanelController+SecuritySettings.swift")
        return try String(contentsOf: url, encoding: .utf8)
    }

    private func body(of marker: String, in src: String) throws -> String {
        let start = try XCTUnwrap(src.range(of: marker), "нет \(marker)")
        let rest = src[start.upperBound...]
        let end = rest.range(of: "\n    // MARK: ")?.lowerBound ?? rest.endIndex
        return String(rest[..<end])
    }

    func test_encryption_toggle_failure_rereads_state_and_does_not_return_silently() throws {
        let fn = try body(of: "private func applyEncryptionChange(", in: try source())
        XCTAssertTrue(fn.contains("EncryptionChangeOutcome.resolve(response: resp, desired: desired)"))
        let reread = try XCTUnwrap(fn.range(of: "method: \"get_encryption_status\""))
        let parsed = try XCTUnwrap(fn.range(of: "confirmedEncryptionStatus(rereadResponse)"))
        let resolved = try XCTUnwrap(fn.range(of: "outcome.knownEnabled(desired: desired, reread: status?.enabled)"))
        let rendered = try XCTUnwrap(fn.range(of: "syncSecuritySettings(enabled: known,"))
        XCTAssertLessThan(reread.lowerBound, parsed.lowerBound)
        XCTAssertLessThan(parsed.lowerBound, resolved.lowerBound)
        XCTAssertLessThan(resolved.lowerBound, rendered.lowerBound)
        XCTAssertTrue(fn.contains("presentAlertSheet(alert, for: self.window)"))
        XCTAssertFalse(fn.contains(") else { return }"),"IPC-сбой не должен молча оставлять тоггл в выбранной позиции")
        XCTAssertFalse(fn.contains("runModal"))
    }

    func test_migration_adapter_renders_resolved_outcome_and_delegates_callbacks() throws {
        let src = try source()
        let fn = try body(of: "private func presentMigrationOutcome(", in: src)
        XCTAssertTrue(fn.contains("switch outcome"))
        XCTAssertTrue(fn.contains("case .backupCleanupFailed:"))
        XCTAssertFalse(fn.contains("HistoryMigrationOutcome.resolve"))
        XCTAssertTrue(src.contains("filter=history_encryption.migrate.progress"))
        XCTAssertTrue(src.contains("migrationObservationCoordinator.confirmAndStartMigration()"))
        XCTAssertTrue(src.contains("migrationObservationCoordinator.refresh()"))
        XCTAssertFalse(src.contains("migratePolling"))
        XCTAssertFalse(src.contains("private func pollMigrationOnce()"))
        XCTAssertFalse(src.contains("private func finishMigrationPolling("))
        XCTAssertTrue(src.contains("startWatcher: { [weak self] in self?.startMigrationEventWatch() }"))
        XCTAssertTrue(src.contains("terminalStatus: { [weak self] in"))
        XCTAssertTrue(src.contains("self.ipcClient.call(method: method, params: [:])"))
        XCTAssertTrue(src.contains("DispatchQueue.main.async { completion(response) }"))
        XCTAssertTrue(src.contains("completion(response == .alertFirstButtonReturn)"))
    }

    // MARK: - IPC-итог миграции (last_migration_status)

    func test_migration_any_other_terminal_status_is_failed_not_done() {
        XCTAssertEqual(
            HistoryMigrationOutcome.resolve(plaintext: 0, terminalStatus: "verification_failed"),
            .failed(status: "verification_failed"))
        XCTAssertEqual(
            HistoryMigrationOutcome.resolve(plaintext: 0, terminalStatus: "error"),
            .failed(status: "error"))
    }

    func test_migration_encrypting_is_not_terminal() {
        XCTAssertEqual(HistoryMigrationOutcome.resolve(plaintext: 0, terminalStatus: "encrypting"), .pending)
        XCTAssertEqual(
            HistoryMigrationOutcome.resolve(plaintext: 2, terminalStatus: "encrypting"),
            .partial(plaintext: 2))
    }

    func test_effective_terminal_status_prefers_ipc_over_sse() {
        XCTAssertEqual(HistoryMigrationOutcome.effectiveTerminalStatus(ipc: "done", sse: "backup_cleanup_failed"), "done")
        XCTAssertEqual(HistoryMigrationOutcome.effectiveTerminalStatus(ipc: nil, sse: "backup_cleanup_failed"), "backup_cleanup_failed")
        XCTAssertNil(HistoryMigrationOutcome.effectiveTerminalStatus(ipc: nil, sse: nil))
        XCTAssertNil(HistoryMigrationOutcome.effectiveTerminalStatus(ipc: "", sse: nil))
    }

    func test_coordinator_owns_terminal_parsing_and_adapter_render() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent")
        let coordinator = try String(contentsOf: root.appendingPathComponent("HistoryMigrationObservationCoordinator.swift"), encoding: .utf8)
        XCTAssertTrue(coordinator.contains("result[\"last_migration_status\"] as? String"))
        XCTAssertTrue(coordinator.contains("HistoryMigrationOutcome.effectiveTerminalStatus("))
        XCTAssertTrue(coordinator.contains("HistoryMigrationOutcome.resolve(plaintext: plaintext, terminalStatus: terminal)"))
        let present = try body(of: "private func presentMigrationOutcome(", in: try source())
        XCTAssertTrue(present.contains("case .failed"))
    }

}

/// Тоггл шифрования: состояние, которое не удалось подтвердить, не выдаётся за известное;
/// SSE-наблюдатель миграции отменяется при закрытии окна.
final class EncryptionToggleUnknownStateTests: XCTestCase {
    func test_known_enabled_prefers_reread_then_reported_and_is_nil_when_unknown() {
        let failed = EncryptionChangeOutcome.failed(enabled: true)
        XCTAssertEqual(failed.knownEnabled(desired: false, reread: false), false)
        XCTAssertEqual(failed.knownEnabled(desired: false, reread: nil), true)
        XCTAssertNil(EncryptionChangeOutcome.failed(enabled: nil).knownEnabled(desired: true, reread: nil))
        XCTAssertNil(EncryptionChangeOutcome.keychainUnavailable(enabled: nil).knownEnabled(desired: true, reread: nil))
        XCTAssertEqual(EncryptionChangeOutcome.applied.knownEnabled(desired: true, reread: nil), true)
    }

    private func source(_ file: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent")
        return try String(contentsOf: root.appendingPathComponent(file), encoding: .utf8)
    }

    func test_toggle_failure_uses_known_state_and_honest_unknown_message() throws {
        let src = try source("HistoryPanelController+SecuritySettings.swift")
        XCTAssertTrue(src.contains("else if known == nil"))
        XCTAssertTrue(src.contains("Не удалось подтвердить состояние шифрования"))
        let marker = try XCTUnwrap(src.range(of: "func syncSecuritySettings("))
        let render = String(src[marker.lowerBound...])
        XCTAssertTrue(render.contains("enabled.map { $0 ? .on : .off } ?? .mixed"))
        XCTAssertTrue(render.contains("let canToggle = available == true && enabled != nil"))
        XCTAssertTrue(render.contains("Статус шифрования недоступен"))
        XCTAssertTrue(render.contains("SecurityAssocKeys.encryptionToggle"))
        XCTAssertTrue(render.contains("SecurityAssocKeys.cdEncryptionToggle"))
        XCTAssertEqual(render.components(separatedBy: "toggle.state = toggleState").count - 1, 2)
        XCTAssertEqual(render.components(separatedBy: "toggle.isEnabled = canToggle").count - 1, 2)
    }


    func test_escape_closes_migration_observation_before_hiding_panel() throws {
        let keyboard = try source("HistoryPanelController+KeyboardShortcuts.swift")
        XCTAssertTrue(keyboard.contains("NSEvent.addLocalMonitorForEvents(matching: .keyDown)"))
        XCTAssertTrue(keyboard.contains("self.window?.isKeyWindow == true"))
        let escape = try XCTUnwrap(keyboard.range(of: "if event.keyCode == Keycode.escape.rawValue {"))
        let rest = keyboard[escape.upperBound...]
        let end = try XCTUnwrap(rest.range(of: "\n            }"))
        let body = String(rest[..<end.lowerBound])
        let cleanup = try XCTUnwrap(body.range(of: "self.cancelMigrationEventWatch()"))
        let hide = try XCTUnwrap(body.range(of: "self.window?.orderOut(nil)"))
        XCTAssertLessThan(cleanup.lowerBound, hide.lowerBound)
        XCTAssertTrue(body.contains("return nil"))
    }

    func test_window_hooks_share_one_migration_coordinator() throws {
        let controller = try source("HistoryPanelController.swift")
        let start = try XCTUnwrap(controller.range(of: "func windowWillClose(_ notification: Notification) {"))
        let rest = controller[start.upperBound...]
        let end = rest.range(of: "\n    }\n")?.lowerBound ?? rest.endIndex
        XCTAssertTrue(String(rest[..<end]).contains("cancelMigrationEventWatch()"))
        let show = try XCTUnwrap(controller.range(of: "func showPanel() {"))
        let showRest = String(controller[show.upperBound...])
        let activation = try XCTUnwrap(showRest.range(of: "migrationObservationCoordinator.activate()"))
        let refresh = try XCTUnwrap(showRest.range(of: "refreshMigrationStatus()"))
        let sync = try XCTUnwrap(showRest.range(of: "syncSettingsControls()"))
        XCTAssertLessThan(activation.lowerBound, refresh.lowerBound)
        XCTAssertLessThan(refresh.lowerBound, sync.lowerBound)
        let adapter = try source("HistoryPanelController+SecuritySettings.swift")
        let close = try XCTUnwrap(adapter.range(of: "func cancelMigrationEventWatch() {"))
        let closeRest = adapter[close.upperBound...]
        let closeEnd = try XCTUnwrap(closeRest.range(of: "\n    }"))
        XCTAssertTrue(closeRest[..<closeEnd.lowerBound].contains("migrationObservationCoordinator.close()"))
        XCTAssertTrue(adapter.contains("as? HistoryMigrationObservationCoordinator { return existing }"))
    }

}
