/*
 QuickCaptureSSEReconnectSourceContractTests — S3 Task 8 (спека Р12, находка I-E).

 Живого SSE-стрима (реального REST-сервера) в unit-тестах нет — поведение
 реконнекта на реальной сети тестируется живым смоком координатора (задача 12).
 Здесь — source-контракт по образцу MainHealthMonitorSourceContractTests:
 фиксирует НАМЕРЕНИЕ кода текстом файла, чтобы регрессия (например, кто-то
 вернёт старую конструкцию SSESessionDelegate без onComplete, или скопирует
 give-up-кап LiveSubtitlesOverlay дословно) стала красной сразу, а не молчаливым
 «панель снова замерла при обрыве».

 Покрывает:
   1. test_sse_delegate_constructed_with_completion_handler
      — SSESessionDelegate в QuickCapturePanelController.swift создаётся с
        параметром onComplete (до этой задачи — не передавался вовсе).
   2. test_no_liveSubtitlesOverlay_style_giveup_cap
      — переподключение НЕ использует кап вида maxReconnectAttempts/
        `sseReconnectAttempts < N` — LiveSubtitlesOverlay-кап (5 попыток,
        сдаётся за ~30с) сдался бы раньше цикла сторожа REST (≥минута) и
        воспроизвёл бы застывший экран, который чинит эта задача.
   3. test_reconnect_gated_on_panel_isVisible_not_isRecording
      — реконнект проверяет panel.isVisible (жизненный цикл SSE привязан к
        show()/close()), а не isRecording — в контроллере нет отдельного
        понятия «режим активен» (см. докстринг задачи 8).
   4. test_handleSSECompletion_invalidatesSessionBeforeClearing
      — S3 финальное ревью, фикс 2: handleSSECompletion() обязан
        инвалидировать sseSession (invalidateAndCancel()) ПЕРЕД обнулением —
        URLSession с кастомным делегатом держит его сильной ссылкой до явной
        инвалидации; голое `sseSession = nil` копило неинвалидированную
        сессию+делегат на КАЖДЫЙ обрыв (обычный путь при живущей задаче 7 —
        не редкий кейс) без верхней границы.
*/

import XCTest
@testable import KrabEarAgent

final class QuickCaptureSSEReconnectSourceContractTests: XCTestCase {

    func test_sse_delegate_constructed_with_completion_handler() throws {
        let src = try Self.readSource()
        XCTAssertTrue(
            src.contains("onComplete: { [weak self] _ in"),
            "startSSE() обязан создавать SSESessionDelegate с обработчиком " +
            "завершения (onComplete) — без него обрыв SSE-потока тихо роняет " +
            "задачу, и панель навсегда показывает застывший текст (S3 Task 8, I-E)."
        )
    }

    func test_no_liveSubtitlesOverlay_style_giveup_cap() throws {
        let src = try Self.readSource()
        XCTAssertFalse(
            src.contains("maxReconnectAttempts"),
            "QuickCapturePanelController не должен копировать give-up-кап " +
            "LiveSubtitlesOverlay (5 попыток / ~30с) — цикл сторожа REST из " +
            "задачи 7 занимает не меньше минуты на детекцию плюс рестарт, " +
            "скопированный кап сдался бы раньше и воспроизвёл застывший экран."
        )
        XCTAssertFalse(
            src.contains("sseReconnectAttempts <"),
            "Переподключение не должно прекращаться по достижении лимита " +
            "попыток, пока панель видима — только give-up-таймаутов/капов нет, " +
            "backoff по-прежнему ограничен сверху (sseReconnectMaxDelay)."
        )
    }

    func test_reconnect_gated_on_panel_isVisible_not_isRecording() throws {
        let src = try Self.readSource()
        XCTAssertTrue(
            src.contains("guard panel.isVisible else { return }"),
            "handleSSECompletion обязан прекращать реконнект-цепочку, когда " +
            "панель закрыта (panel.isVisible == false) — тот же принцип, что " +
            "resyncTimerAndPulseIfNeeded/windowWillClose."
        )
        XCTAssertTrue(
            src.contains("self.sseGeneration == generation, self.panel.isVisible"),
            "Отложенный реконнект (DispatchWorkItem) обязан перепроверять " +
            "generation и panel.isVisible непосредственно перед стартом — " +
            "иначе устаревший таймер поднимет соединение поверх уже закрытой " +
            "панели или уже идущей новой попытки."
        )
    }

    func test_handleSSECompletion_invalidatesSessionBeforeClearing() throws {
        let src = try Self.readSource()
        let body = try Self.functionBody(
            named: "private func handleSSECompletion",
            nextAnchor: "private func scheduleSSEReconnect",
            in: src
        )

        let invalidateRange = body.range(of: "sseSession?.invalidateAndCancel()")
        XCTAssertNotNil(
            invalidateRange,
            "handleSSECompletion обязан звать sseSession?.invalidateAndCancel() — " +
            "иначе URLSession с кастомным делегатом держит его сильной ссылкой " +
            "навсегда (утечка на каждый обрыв SSE, не только редкий кейс)."
        )
        if let invalidateRange, let clearRange = body.range(of: "sseSession = nil") {
            XCTAssertTrue(
                invalidateRange.upperBound <= clearRange.lowerBound,
                "invalidateAndCancel() обязан идти ДО обнуления sseSession — " +
                "иначе ссылка на сессию теряется прежде инвалидации."
            )
        } else {
            XCTFail("handleSSECompletion обязан обнулять sseSession после инвалидации")
        }
    }

    /// Вырезает тело метода `named...{` до `nextAnchor` — достаточно грубо
    /// (не парсит скобки), но устойчиво для наших плоских methods без
    /// вложенных `private func` внутри тела.
    private static func functionBody(named marker: String, nextAnchor: String, in src: String) throws -> Substring {
        guard let start = src.range(of: marker) else {
            throw XCTSkip("Маркер \(marker) не найден — возможно метод переименован")
        }
        guard let end = src.range(of: nextAnchor, range: start.upperBound..<src.endIndex) else {
            throw XCTSkip("Маркер \(nextAnchor) не найден — возможно метод переименован")
        }
        return src[start.upperBound..<end.lowerBound]
    }

    /// Резолвит Sources/KrabEarAgent/QuickCapturePanelController.swift от
    /// bundle-пути тестового рантайма, с walk-up-фоллбэком от `#file` (тот же
    /// приём, что mainSwiftURL в MainHealthMonitorSourceContractTests) — прямой
    /// подсчёт `deletingLastPathComponent()` от `#file` ненадёжен под `swift
    /// test` (нормализованный/относительный путь макроса), первый прогон это
    /// подтвердил.
    private static func readSource() throws -> String {
        let bundleURL = Bundle(for: QuickCaptureSSEReconnectSourceContractTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/QuickCapturePanelController.swift")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return try String(contentsOf: candidate, encoding: .utf8)
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #file)
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent (package root)
            .appendingPathComponent("Sources/KrabEarAgent/QuickCapturePanelController.swift")
        return try String(contentsOf: fileURL, encoding: .utf8)
    }
}
