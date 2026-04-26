/*
 HistoryPanelOverviewFormatTests — юнит-тесты pure helper'а
 `formatHistoryOverview` из +History.swift (PR D fix Part 3).

 Helper форматирует ответ `get_history_overview` IPC в человекочитаемый
 overview label.
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelOverviewFormatTests: XCTestCase {

    func test_emptyResult_defaultsToZeros() {
        let s = HistoryPanelController.formatHistoryOverview(result: [:])
        XCTAssertEqual(s, "Сегодня: 0 | 24ч: 0 | Paste: ✓0 ✗0")
    }

    func test_fullResult() {
        let r: [String: Any] = [
            "today_count": 5,
            "last_24h_count": 12,
            "paste_ok": 8,
            "paste_failed": 1,
        ]
        let s = HistoryPanelController.formatHistoryOverview(result: r)
        XCTAssertEqual(s, "Сегодня: 5 | 24ч: 12 | Paste: ✓8 ✗1")
    }

    func test_partialResult_missingFieldsDefault0() {
        let r: [String: Any] = ["today_count": 3]
        let s = HistoryPanelController.formatHistoryOverview(result: r)
        XCTAssertTrue(s.contains("Сегодня: 3"))
        XCTAssertTrue(s.contains("24ч: 0"))
        XCTAssertTrue(s.contains("✓0"))
        XCTAssertTrue(s.contains("✗0"))
    }

    func test_largeNumbers() {
        let r: [String: Any] = [
            "today_count": 1234,
            "last_24h_count": 9876,
            "paste_ok": 9999,
            "paste_failed": 100,
        ]
        let s = HistoryPanelController.formatHistoryOverview(result: r)
        XCTAssertTrue(s.contains("Сегодня: 1234"))
        XCTAssertTrue(s.contains("24ч: 9876"))
        XCTAssertTrue(s.contains("✓9999"))
        XCTAssertTrue(s.contains("✗100"))
    }

    func test_wrongTypes_fallbackToZero() {
        // Строки вместо Int — fallback на 0.
        let r: [String: Any] = [
            "today_count": "not a number",
            "last_24h_count": ["nested": "dict"],
            "paste_ok": 3.14,  // Double — не Int → fallback 0
            "paste_failed": true,  // Bool — не Int → fallback 0
        ]
        let s = HistoryPanelController.formatHistoryOverview(result: r)
        XCTAssertEqual(s, "Сегодня: 0 | 24ч: 0 | Paste: ✓0 ✗0")
    }

    func test_unicodeCheckmarks_present() {
        let s = HistoryPanelController.formatHistoryOverview(result: ["paste_ok": 1, "paste_failed": 1])
        XCTAssertTrue(s.contains("✓"))
        XCTAssertTrue(s.contains("✗"))
    }

    // MARK: - formatBytesIfStatic helper

    func test_formatBytesIfStatic_zero() {
        XCTAssertEqual(HistoryPanelController.formatBytesIfStatic(0), "0 B")
    }

    func test_formatBytesIfStatic_negative_clamped() {
        XCTAssertEqual(HistoryPanelController.formatBytesIfStatic(-1), "0 B")
    }

    func test_formatBytesIfStatic_kb() {
        XCTAssertEqual(HistoryPanelController.formatBytesIfStatic(1024), "1.0 KB")
    }

    func test_formatBytesIfStatic_mb() {
        XCTAssertEqual(HistoryPanelController.formatBytesIfStatic(1024 * 1024), "1.0 MB")
    }

    func test_formatBytesIfStatic_gb() {
        XCTAssertEqual(HistoryPanelController.formatBytesIfStatic(1024 * 1024 * 1024), "1.00 GB")
    }
}
