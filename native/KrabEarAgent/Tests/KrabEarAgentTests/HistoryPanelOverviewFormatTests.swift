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
        XCTAssertEqual(s, "Обзор: сегодня 0, 24ч 0, вставка ok/err 0/0, перевод ok/err 0/0")
    }

    func test_fullResult() {
        let r: [String: Any] = [
            "today_count": 5,
            "last_24h_count": 12,
            "paste_ok": 8,
            "paste_failed": 1,
            "translated_ok": 4,
            "translated_error": 0,
        ]
        let s = HistoryPanelController.formatHistoryOverview(result: r)
        XCTAssertEqual(s, "Обзор: сегодня 5, 24ч 12, вставка ok/err 8/1, перевод ok/err 4/0")
    }

    func test_partialResult_missingFieldsDefault0() {
        let r: [String: Any] = ["today_count": 3]
        let s = HistoryPanelController.formatHistoryOverview(result: r)
        XCTAssertTrue(s.contains("сегодня 3"))
        XCTAssertTrue(s.contains("24ч 0"))
        XCTAssertTrue(s.contains("ok/err 0/0"))
    }

    func test_largeNumbers() {
        let r: [String: Any] = [
            "today_count": 1234,
            "last_24h_count": 9876,
            "paste_ok": 9999,
            "paste_failed": 100,
            "translated_ok": 500,
            "translated_error": 50,
        ]
        let s = HistoryPanelController.formatHistoryOverview(result: r)
        XCTAssertTrue(s.contains("сегодня 1234"))
        XCTAssertTrue(s.contains("24ч 9876"))
        XCTAssertTrue(s.contains("вставка ok/err 9999/100"))
        XCTAssertTrue(s.contains("перевод ok/err 500/50"))
    }

    func test_wrongTypes_fallbackToZero() {
        let r: [String: Any] = [
            "today_count": "not a number",
            "last_24h_count": ["nested": "dict"],
            "paste_ok": 3.14,
            "paste_failed": true,
        ]
        let s = HistoryPanelController.formatHistoryOverview(result: r)
        XCTAssertEqual(s, "Обзор: сегодня 0, 24ч 0, вставка ok/err 0/0, перевод ok/err 0/0")
    }

    func test_translationFieldsIncluded() {
        let r: [String: Any] = ["translated_ok": 7, "translated_error": 2]
        let s = HistoryPanelController.formatHistoryOverview(result: r)
        XCTAssertTrue(s.contains("перевод ok/err 7/2"))
    }

    func test_overviewPrefix_present() {
        let s = HistoryPanelController.formatHistoryOverview(result: [:])
        XCTAssertTrue(s.hasPrefix("Обзор:"))
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
