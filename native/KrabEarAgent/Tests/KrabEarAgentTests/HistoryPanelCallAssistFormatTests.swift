/*
 HistoryPanelCallAssistFormatTests — юнит-тесты pure helpers форматирования
 для Call Assist (PR test/swift-callassist-format-helpers).

 Тестируемые функции (`nonisolated static`):
 1. `formatCallTimelinePreview(items:) -> String`
 2. `formatCallSummary(_:) -> String`
 3. `formatCallCostEstimate(_:) -> String`
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelCallAssistFormatTests: XCTestCase {

    // MARK: - formatCallTimelinePreview

    func test_timeline_emptyItems_returnsEmpty() {
        let s = HistoryPanelController.formatCallTimelinePreview(items: [])
        XCTAssertEqual(s, "")
    }

    func test_timeline_singleItem() {
        let items: [[String: Any]] = [
            ["ts": "2026-04-25T10:00:00Z", "kind": "stt_partial", "text": "Привет"]
        ]
        let s = HistoryPanelController.formatCallTimelinePreview(items: items)
        XCTAssertTrue(s.contains("[2026-04-25T10:00:00Z]"))
        XCTAssertTrue(s.contains("stt_partial"))
        XCTAssertTrue(s.contains("Привет"))
    }

    func test_timeline_emptyText_showsPlaceholder() {
        let items: [[String: Any]] = [["ts": "t", "kind": "k", "text": ""]]
        let s = HistoryPanelController.formatCallTimelinePreview(items: items)
        XCTAssertTrue(s.contains("(без текста)"))
    }

    func test_timeline_textTruncatedAt120() {
        let longText = String(repeating: "а", count: 200)
        let items: [[String: Any]] = [["ts": "t", "kind": "k", "text": longText]]
        let s = HistoryPanelController.formatCallTimelinePreview(items: items)
        let expected120 = String(repeating: "а", count: 120) + "…"
        XCTAssertTrue(s.contains(expected120))
    }

    func test_timeline_textShorterThan120_keptAsIs() {
        let items: [[String: Any]] = [["ts": "t", "kind": "k", "text": "короткий"]]
        let s = HistoryPanelController.formatCallTimelinePreview(items: items)
        XCTAssertTrue(s.contains("короткий"))
        XCTAssertFalse(s.contains("…"))
    }

    func test_timeline_missingFields_useDefaults() {
        let items: [[String: Any]] = [[:]]
        let s = HistoryPanelController.formatCallTimelinePreview(items: items)
        XCTAssertTrue(s.contains("[-]"))     // ts default "-"
        XCTAssertTrue(s.contains("unknown"))  // kind default
        XCTAssertTrue(s.contains("(без текста)"))
    }

    func test_timeline_multipleItems_joinedByNewline() {
        let items: [[String: Any]] = [
            ["ts": "t1", "kind": "stt", "text": "first"],
            ["ts": "t2", "kind": "tts", "text": "second"],
        ]
        let s = HistoryPanelController.formatCallTimelinePreview(items: items)
        XCTAssertEqual(s.components(separatedBy: "\n").count, 2)
    }

    func test_timeline_textTrimmed() {
        let items: [[String: Any]] = [["ts": "t", "kind": "k", "text": "  трим  "]]
        let s = HistoryPanelController.formatCallTimelinePreview(items: items)
        XCTAssertTrue(s.contains("трим"))
        XCTAssertFalse(s.contains("  трим  "), "Whitespace должен быть trimmed")
    }

    // MARK: - formatCallSummary

    func test_summary_emptyPayload() {
        let s = HistoryPanelController.formatCallSummary([:])
        XCTAssertTrue(s.contains("—"), "Пустой summary → placeholder —")
        XCTAssertTrue(s.contains("(нет задач)"))
    }

    func test_summary_textOnly() {
        let s = HistoryPanelController.formatCallSummary(["summary": "Звонок прошёл успешно"])
        XCTAssertTrue(s.contains("Звонок прошёл успешно"))
    }

    func test_summary_tasks_dictWithTask() {
        let payload: [String: Any] = [
            "summary": "S",
            "tasks": [
                ["task": "Перезвонить"],
                ["task": "Отправить договор"],
            ]
        ]
        let s = HistoryPanelController.formatCallSummary(payload)
        XCTAssertTrue(s.contains("1. Перезвонить"))
        XCTAssertTrue(s.contains("2. Отправить договор"))
    }

    func test_summary_tasks_dictWithTitle() {
        // backend может возвращать "title" вместо "task"
        let payload: [String: Any] = ["tasks": [["title": "T1"]]]
        let s = HistoryPanelController.formatCallSummary(payload)
        XCTAssertTrue(s.contains("1. T1"))
    }

    func test_summary_tasks_dictWithText() {
        let payload: [String: Any] = ["tasks": [["text": "T1"]]]
        let s = HistoryPanelController.formatCallSummary(payload)
        XCTAssertTrue(s.contains("1. T1"))
    }

    func test_summary_tasks_plainStrings() {
        let payload: [String: Any] = ["tasks": ["str1", "str2"]]
        let s = HistoryPanelController.formatCallSummary(payload)
        XCTAssertTrue(s.contains("1. str1"))
        XCTAssertTrue(s.contains("2. str2"))
    }

    func test_summary_tasks_emptyDictsSkipped() {
        let payload: [String: Any] = ["tasks": [["task": ""], ["task": "valid"]]]
        let s = HistoryPanelController.formatCallSummary(payload)
        XCTAssertTrue(s.contains("1. valid"))
        XCTAssertFalse(s.contains("2. "), "Пустая задача должна быть пропущена")
    }

    func test_summary_tasks_truncatedAt10() {
        var tasks: [[String: Any]] = []
        for i in 0..<15 {
            tasks.append(["task": "task_\(i)"])
        }
        let payload: [String: Any] = ["tasks": tasks]
        let s = HistoryPanelController.formatCallSummary(payload)
        XCTAssertTrue(s.contains("task_9"))
        XCTAssertFalse(s.contains("task_10"), "После 10й задачи отрезается")
        XCTAssertFalse(s.contains("task_14"))
    }

    // MARK: - formatCallCostEstimate

    func test_cost_emptyPayload_defaults() {
        let s = HistoryPanelController.formatCallCostEstimate([:])
        XCTAssertTrue(s.contains("country: n/a"))
        XCTAssertTrue(s.contains("rates_source: unknown"))
        XCTAssertTrue(s.contains("total_usd: 0.000"))
    }

    func test_cost_includesCountryAndRatesSource() {
        let payload: [String: Any] = [
            "country": "ES",
            "rates_source": "twilio_live",
        ]
        let s = HistoryPanelController.formatCallCostEstimate(payload)
        XCTAssertTrue(s.contains("country: ES"))
        XCTAssertTrue(s.contains("rates_source: twilio_live"))
    }

    func test_cost_ratesNote_includedWhenPresent() {
        let payload: [String: Any] = ["rates_note": "spec rate"]
        let s = HistoryPanelController.formatCallCostEstimate(payload)
        XCTAssertTrue(s.contains("rates_note: spec rate"))
    }

    func test_cost_ratesNote_skippedWhenEmpty() {
        let payload: [String: Any] = ["rates_note": ""]
        let s = HistoryPanelController.formatCallCostEstimate(payload)
        XCTAssertFalse(s.contains("rates_note:"))
    }

    func test_cost_telephonyTotal_formatted3decimals() {
        let payload: [String: Any] = ["telephony_usd": ["total": 1.23456]]
        let s = HistoryPanelController.formatCallCostEstimate(payload)
        XCTAssertTrue(s.contains("telephony_total_usd: 1.235"))
    }

    func test_cost_aiTotal_formatted3decimals() {
        let payload: [String: Any] = ["ai_usd": ["total": 0.0789]]
        let s = HistoryPanelController.formatCallCostEstimate(payload)
        XCTAssertTrue(s.contains("ai_total_usd: 0.079"))
    }

    func test_cost_total_formatted3decimals() {
        let payload: [String: Any] = ["total_usd": 5.5]
        let s = HistoryPanelController.formatCallCostEstimate(payload)
        XCTAssertTrue(s.contains("total_usd: 5.500"))
    }

    func test_cost_total_acceptsNSNumber() {
        let payload: [String: Any] = ["total_usd": NSNumber(value: 3.14)]
        let s = HistoryPanelController.formatCallCostEstimate(payload)
        XCTAssertTrue(s.contains("total_usd: 3.140"))
    }
}
