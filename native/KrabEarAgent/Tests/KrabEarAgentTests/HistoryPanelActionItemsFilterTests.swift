/*
 HistoryPanelActionItemsFilterTests — юнит-тесты client-side filter
 для action_items / decisions / questions (PR feat/action-items-filter-ui).

 Тестируется `nonisolated static func filterByActionItemsPresence(items:mode:)`.
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelActionItemsFilterTests: XCTestCase {

    // MARK: - Helpers

    private func _itemWith(
        id: String,
        actionItems: [ActionItem] = [],
        decisions: [String] = [],
        questions: [String] = []
    ) -> HistoryItem {
        var payload: [String: Any] = [
            "id": id,
            "ts": "2026-04-25T10:00:00Z",
            "text": "test",
        ]
        payload["action_items"] = actionItems.map { $0.to_dict_payload() }
        payload["decisions"] = decisions
        payload["questions"] = questions
        return HistoryItem(payload: payload)!
    }

    // MARK: - Mode 0 (passthrough)

    func test_mode0_returnsAllItems() {
        let items = [
            _itemWith(id: "a"),
            _itemWith(id: "b", decisions: ["d"]),
            _itemWith(id: "c"),
        ]
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: items, mode: 0)
        XCTAssertEqual(filtered.count, 3, "mode=0 → passthrough всех items")
    }

    func test_mode0_emptyInput_returnsEmpty() {
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: [], mode: 0)
        XCTAssertTrue(filtered.isEmpty)
    }

    func test_mode_negativeOrInvalid_passthrough() {
        // Switch default case — passthrough.
        let items = [_itemWith(id: "a"), _itemWith(id: "b")]
        let m999 = HistoryPanelController.filterByActionItemsPresence(items: items, mode: 999)
        let mNeg = HistoryPanelController.filterByActionItemsPresence(items: items, mode: -1)
        XCTAssertEqual(m999.count, 2)
        XCTAssertEqual(mNeg.count, 2)
    }

    // MARK: - Mode 1 (только с action data)

    func test_mode1_keepsOnlyItemsWithActionItems() {
        let items = [
            _itemWith(id: "empty"),
            _itemWith(id: "withTask", actionItems: [ActionItem(payload: ["text": "T"])!]),
            _itemWith(id: "alsoEmpty"),
        ]
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: items, mode: 1)
        XCTAssertEqual(filtered.count, 1)
        XCTAssertEqual(filtered[0].id, "withTask")
    }

    func test_mode1_keepsItemsWithOnlyDecisions() {
        let items = [
            _itemWith(id: "empty"),
            _itemWith(id: "withDecision", decisions: ["d1"]),
        ]
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: items, mode: 1)
        XCTAssertEqual(filtered.count, 1)
        XCTAssertEqual(filtered[0].id, "withDecision")
    }

    func test_mode1_keepsItemsWithOnlyQuestions() {
        let items = [
            _itemWith(id: "empty"),
            _itemWith(id: "withQuestion", questions: ["q?"]),
        ]
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: items, mode: 1)
        XCTAssertEqual(filtered.count, 1)
        XCTAssertEqual(filtered[0].id, "withQuestion")
    }

    func test_mode1_keepsItemsWithMixed() {
        let items = [
            _itemWith(id: "empty"),
            _itemWith(
                id: "all",
                actionItems: [ActionItem(payload: ["text": "T"])!],
                decisions: ["d"],
                questions: ["q"]
            ),
        ]
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: items, mode: 1)
        XCTAssertEqual(filtered.count, 1)
        XCTAssertEqual(filtered[0].id, "all")
    }

    func test_mode1_emptyInput_returnsEmpty() {
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: [], mode: 1)
        XCTAssertTrue(filtered.isEmpty)
    }

    func test_mode1_allEmpty_returnsEmpty() {
        let items = [
            _itemWith(id: "a"),
            _itemWith(id: "b"),
        ]
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: items, mode: 1)
        XCTAssertTrue(filtered.isEmpty)
    }

    // MARK: - Mode 2 (только без action data)

    func test_mode2_keepsOnlyEmptyItems() {
        let items = [
            _itemWith(id: "empty"),
            _itemWith(id: "withTask", actionItems: [ActionItem(payload: ["text": "T"])!]),
            _itemWith(id: "alsoEmpty"),
        ]
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: items, mode: 2)
        XCTAssertEqual(filtered.count, 2)
        XCTAssertEqual(Set(filtered.map { $0.id }), Set(["empty", "alsoEmpty"]))
    }

    func test_mode2_excludesItemsWithDecisions() {
        let items = [
            _itemWith(id: "empty"),
            _itemWith(id: "withDecision", decisions: ["d"]),
        ]
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: items, mode: 2)
        XCTAssertEqual(filtered.count, 1)
        XCTAssertEqual(filtered[0].id, "empty")
    }

    func test_mode2_allHaveData_returnsEmpty() {
        let items = [
            _itemWith(id: "a", decisions: ["d"]),
            _itemWith(id: "b", questions: ["q"]),
        ]
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: items, mode: 2)
        XCTAssertTrue(filtered.isEmpty)
    }

    // MARK: - Order preservation

    func test_orderPreserved_mode1() {
        let items = [
            _itemWith(id: "a", decisions: ["d1"]),
            _itemWith(id: "b"),
            _itemWith(id: "c", questions: ["q"]),
            _itemWith(id: "d", actionItems: [ActionItem(payload: ["text": "T"])!]),
        ]
        let filtered = HistoryPanelController.filterByActionItemsPresence(items: items, mode: 1)
        XCTAssertEqual(filtered.map { $0.id }, ["a", "c", "d"], "Должен сохранить original порядок")
    }
}

// Helper для построения payload из ActionItem.
fileprivate extension ActionItem {
    func to_dict_payload() -> [String: Any] {
        return [
            "text": text,
            "assignee": assignee,
            "due": due,
            "priority": priority,
        ]
    }
}
