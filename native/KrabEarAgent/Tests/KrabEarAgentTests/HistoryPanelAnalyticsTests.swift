/*
 HistoryPanelAnalyticsTests — юнит-тесты чистой логики +Analytics расширения.

 Стратегия:
 - HistoryPanelController нельзя инстанцировать в headless-тестах.
 - Тестируем static-хелперы, добавленные в +Analytics как "Testable static helpers":
   1. usageLabelTexts — форматирование get_usage_stats ответа в строки меток.
   2. scoreLabelText — форматирование score_transcription результата.
   3. errorStatsText — форматирование get_error_stats dict в строку диагностики.
   4. componentHealthy / backendOverallHealthy — маппинг health_check ответа.

 🔴 Контракт-фиксация (response-field drift wave): эти тесты раньше кормили
 хелперы НЕСУЩЕСТВУЮЩИМИ плоскими ключами (today/week/total, score, stt:Bool),
 совпадавшими с багнутым чтением Swift, — поэтому они были зелёными, а фичи в
 проде молча не работали. Теперь тесты кормят РЕАЛЬНУЮ форму backend-ответов:
   - get_usage_stats: вложенные {today,this_week,all_time}:{recordings,...}
   - score_transcription: {overall_score, grade, ...}
   - health_check: {checks: {stt_model:{status:"ok"}, ...}, status: "..."}
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class HistoryPanelAnalyticsTests: XCTestCase {

    // MARK: - usageLabelTexts
    // Backend get_usage_stats → периоды вложенные dict'ы со счётчиком "recordings".

    /// Полный результат — метки берут recordings из вложенных периодов.
    func test_usageLabelTexts_fullResult() {
        let result: [String: Any] = [
            "today": ["recordings": 5, "total_duration_sec": 12.0, "total_words": 30],
            "this_week": ["recordings": 42, "total_words": 900],
            "all_time": ["recordings": 317, "total_words": 7000],
        ]
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

    /// Частичные данные — отсутствующие периоды дают "0", присутствующий читает recordings.
    func test_usageLabelTexts_partialResult() {
        let result: [String: Any] = ["today": ["recordings": 3, "total_words": 12]]
        let texts = HistoryPanelController.usageLabelTexts(from: result)
        XCTAssertEqual(texts.today, "Сегодня: 3")
        XCTAssertEqual(texts.week,  "Неделя: 0")
        XCTAssertEqual(texts.total, "Всего: 0")
    }

    // MARK: - scoreLabelText
    // Backend score_transcription → {overall_score, grade, ...}.

    /// overall_score + grade → "Оценка: <score> (<grade>)".
    func test_scoreLabelText_withScoreAndGrade() {
        let result: [String: Any] = ["overall_score": 87, "grade": "B"]
        XCTAssertEqual(HistoryPanelController.scoreLabelText(from: result), "Оценка: 87 (B)")
    }

    /// overall_score без grade → "Оценка: <score>".
    func test_scoreLabelText_scoreOnly() {
        let result: [String: Any] = ["overall_score": 91]
        XCTAssertEqual(HistoryPanelController.scoreLabelText(from: result), "Оценка: 91")
    }

    /// overall_score приходит как Double (transcription_scorer round()) → должно
    /// отображаться целым ("87", не "87.0").
    func test_scoreLabelText_floatScoreRendersAsInt() {
        let result: [String: Any] = ["overall_score": 87.0, "grade": "B"]
        XCTAssertEqual(HistoryPanelController.scoreLabelText(from: result), "Оценка: 87 (B)")
    }

    /// Отсутствующий overall_score → fallback "—".
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

    /// UI action обязан использовать тот же formatter, что и unit-тесты;
    /// иначе два почти одинаковых пути могут разойтись незаметно для CI.
    func test_fetchErrorStatsAction_uses_production_formatter() throws {
        let source = try String(contentsOf: Self.analyticsSourceURL, encoding: .utf8)
        XCTAssertTrue(
            source.contains("HistoryPanelController.errorStatsText(from: result)"),
            "fetchErrorStatsAction должен делегировать форматирование production helper-у."
        )
    }

    // MARK: - componentHealthy (health_check маппинг)
    // Backend health_check → {checks: {<имя>: {status: "ok"|"error"|...}}, status: "..."}.

    /// status "ok" → здоров; иной статус → нездоров.
    func test_componentHealthy_okVsError() {
        let result: [String: Any] = [
            "checks": [
                "stt_model": ["status": "ok", "model": "balanced"],
                "llm": ["status": "error", "error": "timeout"],
            ],
        ]
        XCTAssertTrue(HistoryPanelController.componentHealthy(result, key: "stt_model"))
        XCTAssertFalse(HistoryPanelController.componentHealthy(result, key: "llm"))
    }

    /// Отсутствующая проверка → false.
    func test_componentHealthy_missingCheck_returnsFalse() {
        let result: [String: Any] = ["checks": [:]]
        XCTAssertFalse(HistoryPanelController.componentHealthy(result, key: "history_store"))
    }

    /// Нет ключа "checks" вовсе → false (а не краш).
    func test_componentHealthy_noChecksKey_returnsFalse() {
        let result: [String: Any] = [:]
        XCTAssertFalse(HistoryPanelController.componentHealthy(result, key: "stt_model"))
    }

    /// Статус "unavailable"/"warn" → не "ok" → false.
    func test_componentHealthy_nonOkStatus_returnsFalse() {
        let result: [String: Any] = ["checks": ["audio_devices": ["status": "unavailable"]]]
        XCTAssertFalse(HistoryPanelController.componentHealthy(result, key: "audio_devices"))
    }

    /// Все реальные backend-проверки "ok" → все здоровы.
    func test_componentHealthy_allRealChecksHealthy() {
        let result: [String: Any] = [
            "checks": [
                "stt_model": ["status": "ok"],
                "llm": ["status": "ok"],
                "history_store": ["status": "ok"],
            ],
        ]
        for key in ["stt_model", "llm", "history_store"] {
            XCTAssertTrue(HistoryPanelController.componentHealthy(result, key: key),
                          "Компонент '\(key)' должен быть здоров")
        }
    }

    // MARK: - backendOverallHealthy (для подсистем без отдельной health-проверки)

    /// status "ok"/"degraded" → backend в целом здоров; "error" → нет.
    func test_backendOverallHealthy() {
        // _aggregate_status returns ONLY healthy/degraded/unhealthy (never "error").
        // Green only when fully healthy; degraded/unhealthy/missing → not green.
        XCTAssertTrue(HistoryPanelController.backendOverallHealthy(["status": "healthy"]))
        XCTAssertFalse(HistoryPanelController.backendOverallHealthy(["status": "degraded"]))
        XCTAssertFalse(HistoryPanelController.backendOverallHealthy(["status": "unhealthy"]))
        XCTAssertFalse(HistoryPanelController.backendOverallHealthy([:]))
    }

    private static var analyticsSourceURL: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/HistoryPanelController+Analytics.swift")
    }
}
