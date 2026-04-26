/*
 HistoryPanelSemanticSearchFormatTests — юнит-тесты pure helpers для
 форматирования результатов и keyword fallback (PR feat/semantic-search-ui #293).

 Тестируемые функции (`nonisolated static` в `+SemanticSearch.swift`):
 1. `keywordFallback(query:items:topK:) -> [(String, Double)]`
 2. `formatSearchResults(results:items:mode:) -> String`
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelSemanticSearchFormatTests: XCTestCase {

    // MARK: - Helpers

    private func _historyItem(id: String, text: String) -> HistoryItem {
        let payload: [String: Any] = [
            "id": id,
            "ts": "2026-04-25T10:00:00Z",
            "text": text,
        ]
        return HistoryItem(payload: payload)!
    }

    // MARK: - keywordFallback

    func test_keywordFallback_emptyQuery_returnsEmpty() {
        let items: [[String: String]] = [["id": "1", "text": "привет мир"]]
        let r = HistoryPanelController.keywordFallback(query: "", items: items, topK: 10)
        XCTAssertTrue(r.isEmpty)
    }

    func test_keywordFallback_emptyItems_returnsEmpty() {
        let r = HistoryPanelController.keywordFallback(query: "тест", items: [], topK: 10)
        XCTAssertTrue(r.isEmpty)
    }

    func test_keywordFallback_singleMatch() {
        let items: [[String: String]] = [
            ["id": "a", "text": "привет мир"],
            ["id": "b", "text": "пока всем"],
        ]
        let r = HistoryPanelController.keywordFallback(query: "привет", items: items, topK: 10)
        XCTAssertEqual(r.count, 1)
        XCTAssertEqual(r[0].0, "a")
        XCTAssertEqual(r[0].1, 1.0, accuracy: 0.001)  // 1/1 word matched
    }

    func test_keywordFallback_partialMatch_score() {
        let items: [[String: String]] = [
            ["id": "a", "text": "привет красивый мир"],  // matches both
            ["id": "b", "text": "только привет"],         // matches one
        ]
        let r = HistoryPanelController.keywordFallback(query: "привет красивый", items: items, topK: 10)
        XCTAssertEqual(r.count, 2)
        XCTAssertEqual(r[0].0, "a", "Полный матч должен быть первым")
        XCTAssertEqual(r[0].1, 1.0, accuracy: 0.001)
        XCTAssertEqual(r[1].0, "b")
        XCTAssertEqual(r[1].1, 0.5, accuracy: 0.001)
    }

    func test_keywordFallback_caseInsensitive() {
        let items: [[String: String]] = [
            ["id": "a", "text": "ПРИВЕТ Мир"],
        ]
        let r = HistoryPanelController.keywordFallback(query: "привет", items: items, topK: 10)
        XCTAssertEqual(r.count, 1)
    }

    func test_keywordFallback_topKLimit() {
        var items: [[String: String]] = []
        for i in 0..<20 {
            items.append(["id": "id_\(i)", "text": "тест запись \(i)"])
        }
        let r = HistoryPanelController.keywordFallback(query: "тест", items: items, topK: 5)
        XCTAssertEqual(r.count, 5, "Должен ограничиться topK=5")
    }

    func test_keywordFallback_skipsItemsWithEmptyID() {
        let items: [[String: String]] = [
            ["id": "", "text": "привет"],
            ["id": "a", "text": "привет"],
        ]
        let r = HistoryPanelController.keywordFallback(query: "привет", items: items, topK: 10)
        XCTAssertEqual(r.count, 1)
        XCTAssertEqual(r[0].0, "a")
    }

    func test_keywordFallback_skipsItemsWithEmptyText() {
        let items: [[String: String]] = [
            ["id": "a", "text": ""],
            ["id": "b", "text": "привет"],
        ]
        let r = HistoryPanelController.keywordFallback(query: "привет", items: items, topK: 10)
        XCTAssertEqual(r.count, 1)
        XCTAssertEqual(r[0].0, "b")
    }

    func test_keywordFallback_sortedByScoreDescending() {
        let items: [[String: String]] = [
            ["id": "low", "text": "только один"],          // 1/3
            ["id": "high", "text": "один два три"],        // 3/3
            ["id": "mid", "text": "один два"],             // 2/3
        ]
        let r = HistoryPanelController.keywordFallback(query: "один два три", items: items, topK: 10)
        XCTAssertEqual(r.count, 3)
        XCTAssertEqual(r[0].0, "high")
        XCTAssertEqual(r[1].0, "mid")
        XCTAssertEqual(r[2].0, "low")
    }

    // MARK: - formatSearchResults

    func test_format_emptyResults_includesMode() {
        let s = HistoryPanelController.formatSearchResults(results: [], items: [], mode: "semantic")
        XCTAssertTrue(s.contains("Ничего не найдено"))
        XCTAssertTrue(s.contains("semantic"), "Должен включать mode")
    }

    func test_format_includesModeAndCount_in_header() {
        let item = _historyItem(id: "abc12345", text: "тест")
        let s = HistoryPanelController.formatSearchResults(
            results: [("abc12345", 0.82)],
            items: [item],
            mode: "keyword"
        )
        XCTAssertTrue(s.contains("Режим: keyword"))
        XCTAssertTrue(s.contains("результатов: 1"))
    }

    func test_format_scoreShownAsPercentage() {
        let item = _historyItem(id: "abc12345", text: "тест")
        let s = HistoryPanelController.formatSearchResults(
            results: [("abc12345", 0.82)],
            items: [item],
            mode: "semantic"
        )
        XCTAssertTrue(s.contains("82%"))
    }

    func test_format_idShortenedTo8Chars() {
        let item = _historyItem(id: "abcdefgh-1234-5678-9abc-def012345678", text: "тест")
        let s = HistoryPanelController.formatSearchResults(
            results: [("abcdefgh-1234-5678-9abc-def012345678", 0.5)],
            items: [item],
            mode: "semantic"
        )
        XCTAssertTrue(s.contains("abcdefgh"), "Префикс ID должен быть в выводе")
    }

    func test_format_previewTruncatedAt100() {
        let longText = String(repeating: "а", count: 200)
        let item = _historyItem(id: "abc12345", text: longText)
        let s = HistoryPanelController.formatSearchResults(
            results: [("abc12345", 0.5)],
            items: [item],
            mode: "semantic"
        )
        let expected100 = String(repeating: "а", count: 100) + "…"
        XCTAssertTrue(s.contains(expected100), "Preview должен быть обрезан до 100 + ellipsis")
    }

    func test_format_newlinesReplacedInPreview() {
        let item = _historyItem(id: "abc12345", text: "первая\nвторая\nтретья")
        let s = HistoryPanelController.formatSearchResults(
            results: [("abc12345", 0.5)],
            items: [item],
            mode: "semantic"
        )
        XCTAssertTrue(s.contains("первая вторая третья"))
    }

    func test_format_missingItemFallback() {
        // ID в результате есть, но самого item нет в списке (out of page)
        let s = HistoryPanelController.formatSearchResults(
            results: [("missing", 0.5)],
            items: [],
            mode: "semantic"
        )
        XCTAssertTrue(s.contains("не найдено в текущей странице"))
    }

    func test_format_scorePercentageRounded() {
        let item = _historyItem(id: "abc12345", text: "т")
        // 0.876 → 88%
        let s = HistoryPanelController.formatSearchResults(
            results: [("abc12345", 0.876)],
            items: [item],
            mode: "semantic"
        )
        XCTAssertTrue(s.contains("88%"))
    }

    func test_format_multipleResultsNumbered() {
        let items = [
            _historyItem(id: "id1aaaaa", text: "first"),
            _historyItem(id: "id2bbbbb", text: "second"),
        ]
        let s = HistoryPanelController.formatSearchResults(
            results: [("id1aaaaa", 0.9), ("id2bbbbb", 0.5)],
            items: items,
            mode: "semantic"
        )
        XCTAssertTrue(s.contains("1. ["))
        XCTAssertTrue(s.contains("2. ["))
    }
}

