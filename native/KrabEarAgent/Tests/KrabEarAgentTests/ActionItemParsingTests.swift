/*
 ActionItemParsingTests — юнит-тесты `ActionItem.init?(payload:)` +
 extended `HistoryItem.init?(payload:)` для action_items / decisions /
 questions полей (PR feat/history-item-action-items-fields #295).

 ModelsTests.swift уже покрывает HistoryItem core fields, но не trogает
 action data. Этот файл закрывает gap.
*/

import XCTest
@testable import KrabEarAgent

final class ActionItemParsingTests: XCTestCase {

    // MARK: - ActionItem.init

    func test_actionItem_fullPayload() {
        let payload: [String: Any] = [
            "text": "Подготовить отчёт",
            "assignee": "Иван",
            "due": "пятница",
            "priority": "high",
        ]
        let item = ActionItem(payload: payload)
        XCTAssertNotNil(item)
        XCTAssertEqual(item?.text, "Подготовить отчёт")
        XCTAssertEqual(item?.assignee, "Иван")
        XCTAssertEqual(item?.due, "пятница")
        XCTAssertEqual(item?.priority, "high")
    }

    func test_actionItem_missingText_returnsNil() {
        let payload: [String: Any] = ["priority": "low"]
        XCTAssertNil(ActionItem(payload: payload))
    }

    func test_actionItem_emptyText_returnsNil() {
        let payload: [String: Any] = ["text": ""]
        XCTAssertNil(ActionItem(payload: payload))
    }

    func test_actionItem_textOnly_useDefaults() {
        let payload: [String: Any] = ["text": "Just a task"]
        let item = ActionItem(payload: payload)
        XCTAssertNotNil(item)
        XCTAssertEqual(item?.text, "Just a task")
        XCTAssertEqual(item?.assignee, "")
        XCTAssertEqual(item?.due, "")
        XCTAssertEqual(item?.priority, "medium", "Default priority должен быть medium")
    }

    func test_actionItem_invalidPriority_fallbackMedium() {
        let payload: [String: Any] = ["text": "T", "priority": "URGENT_RED_ALERT"]
        let item = ActionItem(payload: payload)
        XCTAssertEqual(item?.priority, "medium")
    }

    func test_actionItem_priority_caseInsensitive() {
        let payload: [String: Any] = ["text": "T", "priority": "HIGH"]
        let item = ActionItem(payload: payload)
        XCTAssertEqual(item?.priority, "high", "HIGH должен normalize в high")
    }

    func test_actionItem_priority_lowercased_low() {
        let payload: [String: Any] = ["text": "T", "priority": "low"]
        let item = ActionItem(payload: payload)
        XCTAssertEqual(item?.priority, "low")
    }

    func test_actionItem_invalidTextType_returnsNil() {
        // text должен быть String — Int или другой тип → nil
        let payload: [String: Any] = ["text": 42]
        XCTAssertNil(ActionItem(payload: payload))
    }

    // MARK: - HistoryItem с action_items array

    private func _historyPayload(actionItems: [[String: Any]]? = nil,
                                 decisions: [String]? = nil,
                                 questions: [String]? = nil) -> [String: Any] {
        var p: [String: Any] = [
            "id": "test-id",
            "ts": "2026-04-25T10:00:00Z",
            "text": "test",
        ]
        if let a = actionItems { p["action_items"] = a }
        if let d = decisions { p["decisions"] = d }
        if let q = questions { p["questions"] = q }
        return p
    }

    func test_historyItem_withActionItemsArray() {
        let payload = _historyPayload(actionItems: [
            ["text": "T1", "priority": "high"],
            ["text": "T2"],
        ])
        let item = HistoryItem(payload: payload)
        XCTAssertNotNil(item)
        XCTAssertEqual(item?.actionItems.count, 2)
        XCTAssertEqual(item?.actionItems[0].text, "T1")
        XCTAssertEqual(item?.actionItems[0].priority, "high")
        XCTAssertEqual(item?.actionItems[1].priority, "medium")
    }

    func test_historyItem_actionItems_skipsInvalid() {
        // ActionItem без text должен быть отфильтрован compactMap.
        let payload = _historyPayload(actionItems: [
            ["text": "valid"],
            ["priority": "low"],  // no text → пропущен
            ["text": "valid2"],
        ])
        let item = HistoryItem(payload: payload)
        XCTAssertEqual(item?.actionItems.count, 2)
        XCTAssertEqual(item?.actionItems[0].text, "valid")
        XCTAssertEqual(item?.actionItems[1].text, "valid2")
    }

    func test_historyItem_decisionsArray() {
        let payload = _historyPayload(decisions: ["Решение 1", "Решение 2"])
        let item = HistoryItem(payload: payload)
        XCTAssertEqual(item?.decisions, ["Решение 1", "Решение 2"])
    }

    func test_historyItem_questionsArray() {
        let payload = _historyPayload(questions: ["Q1?", "Q2?"])
        let item = HistoryItem(payload: payload)
        XCTAssertEqual(item?.questions, ["Q1?", "Q2?"])
    }

    func test_historyItem_emptyAction_emptyArrays() {
        let payload = _historyPayload()  // none of action_items / decisions / questions
        let item = HistoryItem(payload: payload)
        XCTAssertNotNil(item)
        XCTAssertTrue(item!.actionItems.isEmpty)
        XCTAssertTrue(item!.decisions.isEmpty)
        XCTAssertTrue(item!.questions.isEmpty)
    }

    func test_historyItem_wrongTypeActionItems_emptyArray() {
        // action_items должен быть [[String: Any]]; String → пропустить, не crash
        var payload: [String: Any] = [
            "id": "test", "ts": "ts", "text": "txt",
        ]
        payload["action_items"] = "not an array"
        let item = HistoryItem(payload: payload)
        XCTAssertNotNil(item)
        XCTAssertTrue(item!.actionItems.isEmpty)
    }

    func test_historyItem_wrongTypeDecisions_emptyArray() {
        var payload: [String: Any] = [
            "id": "test", "ts": "ts", "text": "txt",
        ]
        payload["decisions"] = 42
        let item = HistoryItem(payload: payload)
        XCTAssertTrue(item!.decisions.isEmpty)
    }

    func test_historyItem_allThreeArraysSimultaneously() {
        let payload = _historyPayload(
            actionItems: [["text": "task"]],
            decisions: ["d"],
            questions: ["q?"]
        )
        let item = HistoryItem(payload: payload)
        XCTAssertEqual(item?.actionItems.count, 1)
        XCTAssertEqual(item?.decisions, ["d"])
        XCTAssertEqual(item?.questions, ["q?"])
    }

    // MARK: - Backward compat

    func test_historyItem_withoutAnyActionFields_stillParses() {
        // Old-format payload без action data должен parse без crash
        // и иметь empty arrays defaults.
        let payload: [String: Any] = [
            "id": "old", "ts": "ts", "text": "old text",
        ]
        let item = HistoryItem(payload: payload)
        XCTAssertNotNil(item)
        XCTAssertEqual(item?.id, "old")
        XCTAssertTrue(item!.actionItems.isEmpty)
        XCTAssertTrue(item!.decisions.isEmpty)
        XCTAssertTrue(item!.questions.isEmpty)
    }
}
