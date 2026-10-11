import XCTest
@testable import KrabEarAgent

/// Honest-state логика секции «Безопасность»: итог миграции и переключатель шифрования.
final class HistoryEncryptionOutcomesTests: XCTestCase {

    // MARK: Миграция

    func test_migration_plaintext_zero_without_terminal_status_is_done() {
        XCTAssertEqual(HistoryMigrationOutcome.resolve(plaintext: 0, terminalStatus: nil), .done)
        XCTAssertEqual(HistoryMigrationOutcome.resolve(plaintext: 0, terminalStatus: "done"), .done)
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
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: resp), .applied)
    }

    func test_encryption_change_keychain_unavailable() {
        let resp: [String: Any] = ["result": ["ok": false, "error": "keychain_unavailable", "enabled": false]]
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: resp), .keychainUnavailable(enabled: false))
    }

    func test_encryption_change_other_rejection_is_failed_with_reported_state() {
        let resp: [String: Any] = ["result": ["ok": false, "error": "settings_write_failed", "enabled": true]]
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: resp), .failed(enabled: true))
    }

    func test_encryption_change_missing_ok_is_failed_not_applied() {
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: ["result": ["enabled": true]]), .failed(enabled: true))
    }

    func test_encryption_change_ipc_failure_or_garbage_is_failed() {
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: nil), .failed(enabled: nil))
        XCTAssertEqual(EncryptionChangeOutcome.resolve(response: ["ok": false]), .failed(enabled: nil))
    }

    func test_actual_enabled_prefers_reread_then_reported_then_negated_desire() {
        XCTAssertFalse(EncryptionChangeOutcome.failed(enabled: true).actualEnabled(desired: true, reread: false))
        XCTAssertFalse(EncryptionChangeOutcome.failed(enabled: false).actualEnabled(desired: true, reread: nil))
        XCTAssertFalse(EncryptionChangeOutcome.failed(enabled: nil).actualEnabled(desired: true, reread: nil))
        XCTAssertTrue(EncryptionChangeOutcome.failed(enabled: nil).actualEnabled(desired: false, reread: nil))
        XCTAssertTrue(EncryptionChangeOutcome.applied.actualEnabled(desired: true, reread: nil))
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
        XCTAssertTrue(fn.contains("EncryptionChangeOutcome.resolve(response: resp)"))
        XCTAssertTrue(fn.contains("method: \"get_encryption_status\""))
        XCTAssertTrue(fn.contains("outcome.actualEnabled(desired: desired, reread: reread)"))
        XCTAssertTrue(fn.contains("toggleRef.state = actual ? .on : .off"))
        XCTAssertFalse(fn.contains(") else { return }"),"IPC-сбой не должен молча оставлять тоггл в выбранной позиции")
        XCTAssertFalse(fn.contains("runModal"))
    }

    func test_migration_verdict_uses_outcome_with_watcher_status() throws {
        let src = try source()
        let fn = try body(of: "private func presentMigrationOutcome(", in: src)
        XCTAssertTrue(fn.contains("HistoryMigrationOutcome.resolve(plaintext: plaintext, terminalStatus: terminal)"))
        XCTAssertTrue(fn.contains("case .backupCleanupFailed:"))
        XCTAssertTrue(src.contains("filter=history_encryption.migrate.progress"))
        // Watcher должен быть поднят ДО запуска миграции.
        let start = try body(of: "private func startHistoryMigration()", in: src)
        let watchPos = try XCTUnwrap(start.range(of: "startMigrationEventWatch()"))
        let ipcPos = try XCTUnwrap(start.range(of: "\"migrate_history_encryption\""))
        XCTAssertLessThan(watchPos.lowerBound, ipcPos.lowerBound)
    }
}
