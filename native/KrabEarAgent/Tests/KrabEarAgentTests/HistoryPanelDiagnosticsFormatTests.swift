/*
 HistoryPanelDiagnosticsFormatTests — юнит-тесты pure helper'а
 `formatNestedResult` из +Diagnostics.swift.

 Helper форматирует JSON-dict ответ от backend в plain-text для
 отображения в diagnostics output (NSTextView). Используется
 множеством handlers: onDiagnostics, onMetrics, onRecordingStats,
 onStorageInfo.
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelDiagnosticsFormatTests: XCTestCase {

    // MARK: - Title rendering

    func test_includesTitleInHeader() {
        let r: [String: Any] = ["a": 1]
        let s = HistoryPanelController.formatNestedResult(r, title: "Метрики")
        XCTAssertTrue(s.contains("=== Метрики ==="))
    }

    func test_emptyDict_onlyHeader() {
        let s = HistoryPanelController.formatNestedResult([:], title: "Empty")
        XCTAssertEqual(s, "=== Empty ===")
    }

    // MARK: - Flat key/value rendering

    func test_flatKeysSortedAlphabetically() {
        let r: [String: Any] = ["zebra": 1, "alpha": 2, "mango": 3]
        let s = HistoryPanelController.formatNestedResult(r, title: "T")
        let lines = s.components(separatedBy: "\n")
        // Header + 3 keys, в алфавитном порядке.
        XCTAssertEqual(lines.count, 4)
        XCTAssertTrue(lines[1].hasPrefix("alpha"))
        XCTAssertTrue(lines[2].hasPrefix("mango"))
        XCTAssertTrue(lines[3].hasPrefix("zebra"))
    }

    func test_flatValuesAsString() {
        let r: [String: Any] = ["int_val": 42, "str_val": "hello"]
        let s = HistoryPanelController.formatNestedResult(r, title: "T")
        XCTAssertTrue(s.contains("int_val: 42"))
        XCTAssertTrue(s.contains("str_val: hello"))
    }

    func test_flatBoolValue() {
        let r: [String: Any] = ["enabled": true, "disabled": false]
        let s = HistoryPanelController.formatNestedResult(r, title: "T")
        XCTAssertTrue(s.contains("enabled: true"))
        XCTAssertTrue(s.contains("disabled: false"))
    }

    // MARK: - Nested dict rendering

    func test_nestedDict_withSubheader() {
        let r: [String: Any] = [
            "stt": ["model": "whisper", "loaded": true]
        ]
        let s = HistoryPanelController.formatNestedResult(r, title: "Diagnostics")
        XCTAssertTrue(s.contains("[stt]"), "Должна быть subheader [stt] для nested dict")
        XCTAssertTrue(s.contains("  loaded:"))  // 2-space indent
        XCTAssertTrue(s.contains("  model:"))
    }

    func test_nestedDict_keysAlsoSorted() {
        let r: [String: Any] = [
            "section": ["zebra": 1, "alpha": 2]
        ]
        let s = HistoryPanelController.formatNestedResult(r, title: "T")
        // Найти позиции keys в выводе.
        let alphaIdx = s.range(of: "alpha:")!
        let zebraIdx = s.range(of: "zebra:")!
        XCTAssertLessThan(alphaIdx.lowerBound, zebraIdx.lowerBound, "alpha должна идти первой")
    }

    func test_mixedFlatAndNested() {
        let r: [String: Any] = [
            "version": "1.0",
            "nested": ["a": 1, "b": 2],
            "uptime_sec": 3600,
        ]
        let s = HistoryPanelController.formatNestedResult(r, title: "Mix")
        // Все 3 top-level keys должны быть в выводе.
        XCTAssertTrue(s.contains("version: 1.0"))
        XCTAssertTrue(s.contains("uptime_sec: 3600"))
        XCTAssertTrue(s.contains("[nested]"))
    }

    func test_nestedSubheader_hasLeadingNewline() {
        // Subheader формируется как "\n[key]" — должен быть пустой strok между sections.
        let r: [String: Any] = ["x": 1, "section": ["a": 1]]
        let s = HistoryPanelController.formatNestedResult(r, title: "T")
        XCTAssertTrue(s.contains("\n[section]"))
    }

    // MARK: - Edge cases

    func test_unicodeKeysAndValues() {
        let r: [String: Any] = [
            "статус": "активен",
            "ёмодзи": "🦀",
        ]
        let s = HistoryPanelController.formatNestedResult(r, title: "Тест")
        XCTAssertTrue(s.contains("статус: активен"))
        XCTAssertTrue(s.contains("ёмодзи: 🦀"))
        XCTAssertTrue(s.contains("=== Тест ==="))
    }

    func test_outputIsValidUTF8() {
        let r: [String: Any] = ["k": "v 🌍"]
        let s = HistoryPanelController.formatNestedResult(r, title: "T")
        XCTAssertNotNil(s.data(using: .utf8))
    }

    // MARK: - A5.2b2: предупреждение о незавершённом restore

    /// Пока `restore.pending == true`, журналы могут быть смесью — владелец
    /// обязан видеть это ДО сырого вывода, иначе сигнал читает только бэкенд.
    func test_restorePendingWarning_presentWhenPending() {
        let diag: [String: Any] = ["restore": [
            "pending": true,
            "state": "COMMITTING",
            "reason": "snapshot_recovery_pending",
            "pre_restore_snapshot": "/data/backups/snapshot_prerestore-x",
        ]]
        let w = HistoryPanelController.restorePendingWarning(diag)
        XCTAssertNotNil(w)
        XCTAssertTrue(w?.contains("Восстановление истории не завершено") == true)
        XCTAssertTrue(w?.contains("snapshot_recovery_pending") == true,
                      "причина показывается, а не пересказывается на глаз")
        XCTAssertTrue(w?.contains("snapshot_prerestore-x") == true,
                      "путь к страховочной копии должен быть виден владельцу")
    }

    func test_restorePendingWarning_absentWhenNotPending() {
        XCTAssertNil(HistoryPanelController.restorePendingWarning([:]))
        XCTAssertNil(HistoryPanelController.restorePendingWarning(["restore": ["pending": false]]))
    }

    /// Verdict с завершённой докачкой (маркера уже нет) — предупреждения быть
    /// не должно: показывать его после успешного восстановления неверно.
    func test_restorePendingWarning_absentAfterCompletedRecovery() {
        let diag: [String: Any] = ["restore": [
            "pending": false,
            "state": "COMMITTED",
            "rolled_forward": true,
            "records_carried": 2,
        ]]
        XCTAssertNil(HistoryPanelController.restorePendingWarning(diag))
    }
}
