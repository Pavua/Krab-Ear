/*
 HistoryPanelAnalyticsTests — юнит-тесты чистой логики +Analytics расширения.

 Стратегия:
 - HistoryPanelController нельзя инстанцировать в headless-тестах.
 - Тестируем static-хелперы, добавленные в +Analytics как "Testable static helpers":
   1. usageLabelTexts — форматирование get_usage_stats ответа в строки меток.
   2. scoreLabelText — форматирование score_transcription результата.
   3. errorStatsText — форматирование get_error_stats dict в строку диагностики.
   4. componentHealthy — маппинг bool-флагов health_check на статус компонентов.
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelAnalyticsTests: XCTestCase {

    // MARK: - usageLabelTexts

    /// Полный результат с данными — метки содержат корректные значения.
    func test_usageLabelTexts_fullResult() {
        let result: [String: Any] = ["today": 5, "week": 42, "total": 317]
        let texts = HistoryPanelController.usageLabelTexts(from: result)
        XCTAssertEqual(texts.today, "Сегодня: 5")
        XCTAssertEqual(texts.week,  "Неделя: 42")
        XCTAssertEqual(texts.total, "Всего: 317")
    }

    /// Пустой результат — fallback "0" для каждой метки.
    func test_usageLabelTexts_emptyResult_fallsBackToZero() {
        let result: [String: Any] = [:]
        let texts = HistoryPanelController.usageLabelTexts(from: result)
        XCTAssertEqual(texts.today, "Сегодня: 0")
        XCTAssertEqual(texts.week,  "Неделя: 0")
        XCTAssertEqual(texts.total, "Всего: 0")
    }

    /// Строковые значения из backend тоже корректно отображаются.
    func test_usageLabelTexts_stringValues() {
        let result: [String: Any] = ["today": "3", "week": "21", "total": "99"]
        let texts = HistoryPanelController.usageLabelTexts(from: result)
        XCTAssertEqual(texts.today, "Сегодня: 3")
        XCTAssertEqual(texts.week,  "Неделя: 21")
        XCTAssertEqual(texts.total, "Всего: 99")
    }

    // MARK: - scoreLabelText

    /// Числовой score форматируется в метку "Оценка: <число>".
    func test_scoreLabelText_withScore() {
        let result: [String: Any] = ["score": 87]
        XCTAssertEqual(HistoryPanelController.scoreLabelText(from: result), "Оценка: 87")
    }

    /// Строковый score (например "B+") тоже поддерживается.
    func test_scoreLabelText_withStringScore() {
        let result: [String: Any] = ["score": "B+"]
        XCTAssertEqual(HistoryPanelController.scoreLabelText(from: result), "Оценка: B+")
    }

    /// Отсутствующий score → fallback "—".
    func test_scoreLabelText_missingScore_fallback() {
        let result: [String: Any] = [:]
        XCTAssertEqual(HistoryPanelController.scoreLabelText(from: result), "Оценка: —")
    }

    // MARK: - errorStatsText

    /// Непустой dict → строка содержит заголовок и ключи.
    func test_errorStatsText_nonEmpty() {
        let result: [String: Any] = ["ipc": 2, "stt": 0]
        let text = HistoryPanelController.errorStatsText(from: result)
        XCTAssertTrue(text.hasPrefix("Статистика ошибок:"),
                      "Должен начинаться с 'Статистика ошибок:'")
        XCTAssertTrue(text.contains("ipc: 2"), "Должен содержать 'ipc: 2'")
        XCTAssertTrue(text.contains("stt: 0"), "Должен содержать 'stt: 0'")
    }

    /// Пустой dict → только заголовок без данных.
    func test_errorStatsText_empty() {
        let text = HistoryPanelController.errorStatsText(from: [:])
        XCTAssertEqual(text, "Статистика ошибок:\n")
    }

    // MARK: - componentHealthy (health_check маппинг)

    /// true → компонент здоров.
    func test_componentHealthy_true() {
        let result: [String: Any] = ["stt": true, "llm": false]
        XCTAssertTrue(HistoryPanelController.componentHealthy(result, key: "stt"))
        XCTAssertFalse(HistoryPanelController.componentHealthy(result, key: "llm"))
    }

    /// Отсутствующий ключ → false (компонент считается нездоровым).
    func test_componentHealthy_missingKey_returnsFalse() {
        let result: [String: Any] = [:]
        XCTAssertFalse(HistoryPanelController.componentHealthy(result, key: "history"))
    }

    /// Нулевое значение (не Bool) → false.
    func test_componentHealthy_nonBoolValue_returnsFalse() {
        let result: [String: Any] = ["translation": 1]  // Int, не Bool
        XCTAssertFalse(HistoryPanelController.componentHealthy(result, key: "translation"),
                       "Int=1 не является Bool true — должен возвращать false")
    }

    /// Все четыре компонента здоровы → все возвращают true.
    func test_componentHealthy_allHealthy() {
        let result: [String: Any] = ["stt": true, "llm": true, "history": true, "translation": true]
        for key in ["stt", "llm", "history", "translation"] {
            XCTAssertTrue(HistoryPanelController.componentHealthy(result, key: key),
                          "Компонент '\(key)' должен быть здоров")
        }
    }
}
