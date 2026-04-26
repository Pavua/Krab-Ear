/*
 HistoryPanelActionItemsMarkdownTests — юнит-тесты pure helpers для
 markdown export action items (PR feat/action-items-markdown-export).

 Тестируемые функции (`nonisolated static` в `HistoryPanelController+ActionItems.swift`):
 1. `countItemsWithActionContent(items:) -> Int`
 2. `formatHistoryItemsAsMarkdown(items:) -> String`

 Стратегия: input — raw IPC payload (как от `get_history_page`). Никаких
 instance dependencies — тесты работают полностью без `HistoryPanelController`.
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelActionItemsMarkdownTests: XCTestCase {

    // MARK: - Helpers

    private func _itemWithActions(
        id: String = "abc12345-aaaa-bbbb-cccc-dddddddddddd",
        ts: String = "2026-04-25T14:30:00Z",
        text: String = "Тестовая запись",
        actionItems: [[String: Any]] = [],
        decisions: [String] = [],
        questions: [String] = []
    ) -> [String: Any] {
        return [
            "id": id,
            "ts": ts,
            "text": text,
            "action_items": actionItems,
            "decisions": decisions,
            "questions": questions,
        ]
    }

    // MARK: - countItemsWithActionContent

    func test_count_emptyItems() {
        XCTAssertEqual(HistoryPanelController.countItemsWithActionContent(items: []), 0)
    }

    func test_count_noActionData() {
        let items = [
            _itemWithActions(id: "a"),
            _itemWithActions(id: "b"),
        ]
        XCTAssertEqual(HistoryPanelController.countItemsWithActionContent(items: items), 0)
    }

    func test_count_oneItemWithActions() {
        let items = [
            _itemWithActions(id: "a"),  // empty
            _itemWithActions(id: "b", actionItems: [["text": "task"]]),
            _itemWithActions(id: "c"),  // empty
        ]
        XCTAssertEqual(HistoryPanelController.countItemsWithActionContent(items: items), 1)
    }

    func test_count_decisionsCounted() {
        let items = [
            _itemWithActions(id: "a", decisions: ["решение"]),
        ]
        XCTAssertEqual(HistoryPanelController.countItemsWithActionContent(items: items), 1)
    }

    func test_count_questionsCounted() {
        let items = [
            _itemWithActions(id: "a", questions: ["вопрос?"]),
        ]
        XCTAssertEqual(HistoryPanelController.countItemsWithActionContent(items: items), 1)
    }

    func test_count_mixedItems() {
        let items = [
            _itemWithActions(id: "a", actionItems: [["text": "t1"]]),
            _itemWithActions(id: "b", decisions: ["d1"]),
            _itemWithActions(id: "c"),  // empty
            _itemWithActions(id: "d", questions: ["q1"]),
            _itemWithActions(id: "e", actionItems: [["text": "t2"]], decisions: ["d2"], questions: ["q2"]),
        ]
        XCTAssertEqual(HistoryPanelController.countItemsWithActionContent(items: items), 4)
    }

    // MARK: - formatHistoryItemsAsMarkdown

    func test_markdown_emptyItems_emitsHeader() {
        let md = HistoryPanelController.formatHistoryItemsAsMarkdown(items: [])
        XCTAssertTrue(md.contains("# Krab Ear — Action Items"), "Должен содержать заголовок")
        XCTAssertTrue(md.contains("_Экспортировано: "), "Должна быть дата экспорта")
        XCTAssertTrue(md.contains("Нет записей"), "Должен сообщить что данных нет")
    }

    func test_markdown_skipsItemsWithoutActionData() {
        let items = [_itemWithActions(id: "empty")]
        let md = HistoryPanelController.formatHistoryItemsAsMarkdown(items: items)
        XCTAssertFalse(md.contains("empty"), "Запись без actions должна быть пропущена")
        XCTAssertTrue(md.contains("Нет записей"), "Должен сообщить что данных нет")
    }

    func test_markdown_actionItem_basic() {
        let items = [_itemWithActions(
            id: "rec1abcd-bbbb-cccc-dddd-eeeeeeeeeeee",
            actionItems: [["text": "Подготовить отчёт", "priority": "high"]]
        )]
        let md = HistoryPanelController.formatHistoryItemsAsMarkdown(items: items)
        XCTAssertTrue(md.contains("Подготовить отчёт"))
        XCTAssertTrue(md.contains("🔴"), "high priority → 🔴")
        XCTAssertTrue(md.contains("- [ ]"), "Должен быть GitHub-style checkbox")
        XCTAssertTrue(md.contains("rec1abcd"), "ID prefix должен быть в заголовке")
    }

    func test_markdown_priorityMarkers() {
        let items = [_itemWithActions(
            id: "rec1",
            actionItems: [
                ["text": "high task", "priority": "high"],
                ["text": "medium task", "priority": "medium"],
                ["text": "low task", "priority": "low"],
                ["text": "default task"],  // no priority → medium
            ]
        )]
        let md = HistoryPanelController.formatHistoryItemsAsMarkdown(items: items)
        XCTAssertTrue(md.contains("🔴"))
        XCTAssertTrue(md.contains("🟡"))
        XCTAssertTrue(md.contains("⚪"))
    }

    func test_markdown_assigneeAndDue() {
        let items = [_itemWithActions(
            id: "rec1",
            actionItems: [["text": "Task", "assignee": "Иван", "due": "пятница"]]
        )]
        let md = HistoryPanelController.formatHistoryItemsAsMarkdown(items: items)
        XCTAssertTrue(md.contains("@Иван"), "Assignee marker должен быть @Имя")
        XCTAssertTrue(md.contains("⏰ пятница"), "Due marker должен быть ⏰ дата")
    }

    func test_markdown_decisions() {
        let items = [_itemWithActions(
            id: "rec1",
            decisions: ["Перенести релиз", "Удалить feature flag"]
        )]
        let md = HistoryPanelController.formatHistoryItemsAsMarkdown(items: items)
        XCTAssertTrue(md.contains("### Решения (2)"))
        XCTAssertTrue(md.contains("- ✓ Перенести релиз"))
        XCTAssertTrue(md.contains("- ✓ Удалить feature flag"))
    }

    func test_markdown_questions() {
        let items = [_itemWithActions(
            id: "rec1",
            questions: ["Кто отвечает?", "Когда дедлайн?"]
        )]
        let md = HistoryPanelController.formatHistoryItemsAsMarkdown(items: items)
        XCTAssertTrue(md.contains("### Вопросы (2)"))
        XCTAssertTrue(md.contains("- ? Кто отвечает?"))
        XCTAssertTrue(md.contains("- ? Когда дедлайн?"))
    }

    func test_markdown_textSnippetTruncatedAt200() {
        let longText = String(repeating: "а", count: 500)
        let items = [_itemWithActions(
            id: "rec1",
            text: longText,
            actionItems: [["text": "T"]]
        )]
        let md = HistoryPanelController.formatHistoryItemsAsMarkdown(items: items)
        // Должен содержать первые 200 символов + "…"
        let snippetMarker = String(repeating: "а", count: 200) + "…"
        XCTAssertTrue(md.contains(snippetMarker), "Длинный текст должен быть обрезан до 200 + ellipsis")
    }

    func test_markdown_textNewlinesReplacedInQuote() {
        let items = [_itemWithActions(
            id: "rec1",
            text: "Первая строка\nВторая строка",
            actionItems: [["text": "T"]]
        )]
        let md = HistoryPanelController.formatHistoryItemsAsMarkdown(items: items)
        // В markdown blockquote `>` newlines в text должны быть заменены на пробелы
        XCTAssertTrue(md.contains("> Первая строка Вторая строка"))
    }

    func test_markdown_multipleItemsSeparator() {
        let items = [
            _itemWithActions(id: "rec1", actionItems: [["text": "T1"]]),
            _itemWithActions(id: "rec2", decisions: ["D1"]),
        ]
        let md = HistoryPanelController.formatHistoryItemsAsMarkdown(items: items)
        // Между записями должен быть разделитель ---
        let separators = md.components(separatedBy: "\n---\n").count - 1
        XCTAssertGreaterThanOrEqual(separators, 1, "Должен быть хотя бы один разделитель между записями")
    }

    func test_markdown_outputIsValidUTF8() {
        let items = [_itemWithActions(
            id: "rec1",
            text: "Текст с ёмодзи 🦀 и кириллицей",
            actionItems: [["text": "Задача с ёмодзи 🔴"]],
            decisions: ["Решение"],
            questions: ["Вопрос?"]
        )]
        let md = HistoryPanelController.formatHistoryItemsAsMarkdown(items: items)
        let data = md.data(using: .utf8)
        XCTAssertNotNil(data, "Markdown должен быть валидным UTF-8")
        XCTAssertTrue(md.contains("🦀"))
        XCTAssertTrue(md.contains("🔴"))
    }
}
