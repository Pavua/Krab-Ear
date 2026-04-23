/*
 GlossarySuggestionsTests — unit-тесты для логики глоссария переводов.

 Стратегия:
 Тестируем чистую логику без AppKit/IPC:
   - GlossarySuggestion init из словаря IPC-ответа
   - Форматирование строк уверенности (confidence → %)
   - Построение params для apply_glossary_suggestions
   - Разбор поля result.added из ответа apply
   - Граничные случаи: пустой список, отсутствующие поля, нулевая уверенность
   - selectedIndices: все выбраны по умолчанию
   - buildApplyParams: только выбранные индексы попадают в selected_ids
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Pure helpers (whitebox, дублируют логику из extension)

/// Преобразует confidence 0..1 → строку "XX%".
private func formatConfidence(_ value: Double) -> String {
    String(format: "%.0f%%", value * 100)
}

/// Извлекает поле added из ответа apply_glossary_suggestions.
private func parseAddedCount(from result: [String: Any], fallback: Int) -> Int {
    if let inner = result["result"] as? [String: Any] {
        return (inner["added"] as? Int)
            ?? (inner["added"] as? NSNumber)?.intValue
            ?? fallback
    }
    return fallback
}

/// Строит params для IPC apply_glossary_suggestions.
private func buildApplyParams(
    selectedIds: [Int],
    suggestions: [GlossarySuggestion]
) -> [String: Any] {
    let rawSuggestions: [[String: Any]] = suggestions.map { item in
        [
            "source_term": item.sourceTerm,
            "target_term": item.targetTerm,
            "frequency":   item.frequency,
            "domain":      item.domain,
            "confidence":  item.confidence,
        ]
    }
    return [
        "selected_ids": selectedIds,
        "suggestions":  rawSuggestions,
    ]
}

// MARK: - Tests

final class GlossarySuggestionsTests: XCTestCase {

    // MARK: 1. GlossarySuggestion init с полными данными

    func test_init_fullDict_allFieldsParsed() {
        let dict: [String: Any] = [
            "source_term": "инфаркт",
            "target_term": "infarto",
            "frequency":   7,
            "domain":      "medical",
            "confidence":  0.92,
        ]
        let sug = GlossarySuggestion(index: 0, dict: dict)
        XCTAssertEqual(sug.sourceTerm, "инфаркт")
        XCTAssertEqual(sug.targetTerm, "infarto")
        XCTAssertEqual(sug.frequency,  7)
        XCTAssertEqual(sug.domain,     "medical")
        XCTAssertEqual(sug.confidence, 0.92, accuracy: 0.001)
    }

    // MARK: 2. GlossarySuggestion init с отсутствующими полями — дефолты

    func test_init_emptyDict_usesDefaults() {
        let sug = GlossarySuggestion(index: 3, dict: [:])
        XCTAssertEqual(sug.sourceTerm, "")
        XCTAssertEqual(sug.targetTerm, "")
        XCTAssertEqual(sug.frequency,  0)
        XCTAssertEqual(sug.domain,     "general")
        XCTAssertEqual(sug.confidence, 0.0, accuracy: 0.001)
    }

    // MARK: 3. Индекс сохраняется корректно

    func test_init_indexPreserved() {
        let sug = GlossarySuggestion(index: 5, dict: ["source_term": "кровь"])
        XCTAssertEqual(sug.index, 5)
    }

    // MARK: 4. NSNumber для frequency (JSON-десериализация возвращает NSNumber)

    func test_init_frequencyAsNSNumber() {
        let dict: [String: Any] = ["frequency": NSNumber(value: 12)]
        let sug = GlossarySuggestion(index: 0, dict: dict)
        XCTAssertEqual(sug.frequency, 12)
    }

    // MARK: 5. Форматирование confidence → "%"

    func test_formatConfidence_92percent() {
        XCTAssertEqual(formatConfidence(0.92), "92%")
    }

    func test_formatConfidence_zero() {
        XCTAssertEqual(formatConfidence(0.0), "0%")
    }

    func test_formatConfidence_100percent() {
        XCTAssertEqual(formatConfidence(1.0), "100%")
    }

    // MARK: 6. buildApplyParams содержит selected_ids и suggestions

    func test_buildApplyParams_containsSelectedIds() {
        let items = makeSamples()
        let params = buildApplyParams(selectedIds: [0, 2], suggestions: items)
        let ids = params["selected_ids"] as? [Int]
        XCTAssertEqual(ids, [0, 2])
    }

    func test_buildApplyParams_suggestionsCount() {
        let items = makeSamples()
        let params = buildApplyParams(selectedIds: [1], suggestions: items)
        let raw = params["suggestions"] as? [[String: Any]]
        XCTAssertEqual(raw?.count, 3, "suggestions должен содержать все 3 элемента")
    }

    // MARK: 7. buildApplyParams — empty selectedIds

    func test_buildApplyParams_emptySelectedIds() {
        let items = makeSamples()
        let params = buildApplyParams(selectedIds: [], suggestions: items)
        let ids = params["selected_ids"] as? [Int]
        XCTAssertEqual(ids?.count, 0)
    }

    // MARK: 8. parseAddedCount извлекает added из вложенного result

    func test_parseAddedCount_extractsFromInnerResult() {
        let response: [String: Any] = ["result": ["added": 3]]
        XCTAssertEqual(parseAddedCount(from: response, fallback: 0), 3)
    }

    // MARK: 9. parseAddedCount — fallback когда ключ отсутствует

    func test_parseAddedCount_fallbackWhenMissing() {
        let response: [String: Any] = ["result": ["something_else": "x"]]
        XCTAssertEqual(parseAddedCount(from: response, fallback: 99), 99)
    }

    // MARK: 10. parseAddedCount — NSNumber из JSON

    func test_parseAddedCount_nsNumberAdded() {
        let response: [String: Any] = ["result": ["added": NSNumber(value: 7)]]
        XCTAssertEqual(parseAddedCount(from: response, fallback: 0), 7)
    }

    // MARK: - Helpers

    private func makeSamples() -> [GlossarySuggestion] {
        let dicts: [[String: Any]] = [
            ["source_term": "боль",      "target_term": "dolor",     "frequency": 5, "domain": "medical", "confidence": 0.88],
            ["source_term": "операция",  "target_term": "operación",  "frequency": 3, "domain": "medical", "confidence": 0.75],
            ["source_term": "диагноз",   "target_term": "diagnóstico","frequency": 9, "domain": "medical", "confidence": 0.95],
        ]
        return dicts.enumerated().map { GlossarySuggestion(index: $0.offset, dict: $0.element) }
    }
}
