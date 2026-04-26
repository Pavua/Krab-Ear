/*
 HistoryPanelActionItemsFormatTests — юнит-тесты pure helpers вывода
 результатов extract_action_items (PR feat/action-items-ui #294).

 Тестируемые функции (`nonisolated static`):
 1. `formatActionItemsResult(result:itemID:) -> String`
 2. `actionItemsStatusText(result:) -> String`
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelActionItemsFormatTests: XCTestCase {

    // MARK: - Helpers

    private func _result(
        ok: Bool = true,
        actionItems: [[String: Any]] = [],
        decisions: [String] = [],
        questions: [String] = [],
        fallbackReason: String? = nil,
        latencyMs: Int? = nil
    ) -> [String: Any] {
        var r: [String: Any] = [
            "ok": ok,
            "action_items": actionItems,
            "decisions": decisions,
            "questions": questions,
        ]
        if let f = fallbackReason { r["fallback_reason"] = f }
        if let l = latencyMs { r["latency_ms"] = l }
        return r
    }

    // MARK: - formatActionItemsResult — error path

    func test_format_okFalse_includesReason() {
        let r = _result(ok: false, fallbackReason: "timeout")
        let out = HistoryPanelController.formatActionItemsResult(result: r, itemID: "abc12345-xxx")
        XCTAssertTrue(out.contains("Не удалось извлечь"))
        XCTAssertTrue(out.contains("timeout"))
    }

    func test_format_okFalse_unknownReason() {
        let r = _result(ok: false)  // no fallback_reason
        let out = HistoryPanelController.formatActionItemsResult(result: r, itemID: "abc")
        XCTAssertTrue(out.contains("unknown"))
    }

    // MARK: - formatActionItemsResult — empty success

    func test_format_okTrueButAllEmpty_says_nothing_found() {
        let r = _result(ok: true)
        let out = HistoryPanelController.formatActionItemsResult(result: r, itemID: "abc")
        XCTAssertTrue(out.contains("не найдено"), "Должен сообщить что ничего не извлечено")
    }

    // MARK: - formatActionItemsResult — content paths

    func test_format_includesItemIDPrefix() {
        let r = _result(ok: true, actionItems: [["text": "T"]])
        let out = HistoryPanelController.formatActionItemsResult(result: r, itemID: "abc12345-xxx-yyy")
        XCTAssertTrue(out.contains("abc12345"), "ID prefix (8 chars) должен быть в выводе")
    }

    func test_format_actionItemsHeader_includesCount() {
        let r = _result(ok: true, actionItems: [["text": "T1"], ["text": "T2"], ["text": "T3"]])
        let out = HistoryPanelController.formatActionItemsResult(result: r, itemID: "abc")
        XCTAssertTrue(out.contains("ЗАДАЧИ (3)"))
    }

    func test_format_priorityMarkers() {
        let r = _result(ok: true, actionItems: [
            ["text": "high task", "priority": "high"],
            ["text": "medium task", "priority": "medium"],
            ["text": "low task", "priority": "low"],
            ["text": "default task"],  // → medium
        ])
        let out = HistoryPanelController.formatActionItemsResult(result: r, itemID: "abc")
        XCTAssertTrue(out.contains("🔴"))
        XCTAssertTrue(out.contains("🟡"))
        XCTAssertTrue(out.contains("⚪"))
    }

    func test_format_actionItem_assignee_and_due() {
        let r = _result(ok: true, actionItems: [
            ["text": "Подготовить отчёт", "assignee": "Иван", "due": "пятница"]
        ])
        let out = HistoryPanelController.formatActionItemsResult(result: r, itemID: "abc")
        XCTAssertTrue(out.contains("@Иван"))
        XCTAssertTrue(out.contains("⏰ пятница"))
    }

    func test_format_decisions_sectionHeader() {
        let r = _result(ok: true, decisions: ["d1", "d2"])
        let out = HistoryPanelController.formatActionItemsResult(result: r, itemID: "abc")
        XCTAssertTrue(out.contains("РЕШЕНИЯ (2)"))
        XCTAssertTrue(out.contains("✓ d1"))
        XCTAssertTrue(out.contains("✓ d2"))
    }

    func test_format_questions_sectionHeader() {
        let r = _result(ok: true, questions: ["q?"])
        let out = HistoryPanelController.formatActionItemsResult(result: r, itemID: "abc")
        XCTAssertTrue(out.contains("ВОПРОСЫ (1)"))
        XCTAssertTrue(out.contains("? q?"))
    }

    func test_format_allThreeSectionsTogether() {
        let r = _result(
            ok: true,
            actionItems: [["text": "task"]],
            decisions: ["decision"],
            questions: ["question"]
        )
        let out = HistoryPanelController.formatActionItemsResult(result: r, itemID: "abc")
        XCTAssertTrue(out.contains("ЗАДАЧИ (1)"))
        XCTAssertTrue(out.contains("РЕШЕНИЯ (1)"))
        XCTAssertTrue(out.contains("ВОПРОСЫ (1)"))
    }

    // MARK: - actionItemsStatusText

    func test_status_okFalse_returnsErrorWithReason() {
        let r = _result(ok: false, fallbackReason: "circuit_open")
        let s = HistoryPanelController.actionItemsStatusText(result: r)
        XCTAssertTrue(s.starts(with: "Ошибка:"))
        XCTAssertTrue(s.contains("circuit_open"))
    }

    func test_status_okTrue_includesAllCounts() {
        let r = _result(
            ok: true,
            actionItems: [["text": "1"], ["text": "2"]],
            decisions: ["a"],
            questions: ["q1", "q2", "q3"]
        )
        let s = HistoryPanelController.actionItemsStatusText(result: r)
        XCTAssertTrue(s.contains("задач=2"))
        XCTAssertTrue(s.contains("решений=1"))
        XCTAssertTrue(s.contains("вопросов=3"))
    }

    func test_status_includesLatencyWhenPresent() {
        let r = _result(ok: true, actionItems: [["text": "T"]], latencyMs: 1234)
        let s = HistoryPanelController.actionItemsStatusText(result: r)
        XCTAssertTrue(s.contains("1234 мс"))
    }

    func test_status_skipsLatencyWhenAbsent() {
        let r = _result(ok: true, actionItems: [["text": "T"]])
        let s = HistoryPanelController.actionItemsStatusText(result: r)
        XCTAssertFalse(s.contains("мс"), "Latency не должен фейково появиться")
    }

    func test_status_zeroCounts() {
        let r = _result(ok: true)
        let s = HistoryPanelController.actionItemsStatusText(result: r)
        XCTAssertTrue(s.contains("задач=0"))
        XCTAssertTrue(s.contains("решений=0"))
        XCTAssertTrue(s.contains("вопросов=0"))
    }
}
