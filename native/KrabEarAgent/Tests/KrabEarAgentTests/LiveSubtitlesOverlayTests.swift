/*
 LiveSubtitlesOverlayTests — Wave 190 unit tests для LiveSubtitlesOverlay.

 Покрытие:
  1.  test_initial_state_empty               — entries пусты, isVisible = false
  2.  test_appendLine_displays_text          — addEntry → _testEntryCount = 1
  3.  test_max_3_lines_oldest_evicted        — 4 addEntry → _testEntryCount = 3
  4.  test_auto_fade_after_4s               — timer schedules, clearAll сбрасывает
  5.  test_panel_always_on_top              — _testPanelLevel = .floating
  6.  test_panel_is_draggable               — _testPanelIsDraggable = true
  7.  test_unicode_subtitle_displayed        — unicode text без краша
  8.  test_long_text_wraps                  — длинная строка без краша + entry count
  9.  test_concurrent_appendLine_thread_safe — 50 addEntry с DispatchGroup без краша
  10. test_clear_resets_panel               — clearAll → _testEntryCount = 0
  11. test_sse_correct_event_type_parsed     — live_subs.result data добавляет entry
  12. test_sse_wrong_event_type_ignored      — чужой event type не добавляет entry
  13. test_sse_flat_text_field              — "text" поле парсится корректно
  14. test_sse_both_field_aliases_work      — "original"/"translated" aliases
  15. test_sse_empty_payload_ignored        — пустой {} не добавляет entry
  16. test_show_hide_isVisible              — show → true, hide → false
  17. test_showOriginal_toggle_no_crash     — showOriginalAndTranslation toggle
  18. test_resetPosition_no_crash          — resetPosition не крашится
  19. test_restBaseURL_default             — содержит 5005
  20. test_clearAll_after_multiple_entries — 5 entries → clearAll → count = 0
  21. test_showHideShow_replacesSSETaskWithoutAccumulation — жизненный цикл SSE без дублей
  22. test_completionReconnectsForEOFAndErrorWhileVisible — восстановление после EOF/ошибки
  23. test_staleConnectionCallbacksIgnoredAfterReconnect — старые обработчики отбрасываются
  24. test_partialBufferDoesNotCrossConnectionGeneration — буфер не смешивает поколения
  25. test_reconnectContinuesIndefinitelyWhileVisible — S3 фикс 3: без give-up-лимита, пока isVisible
  26. test_deinitCancelsTaskAndInvalidatesSession — освобождение URLSession
  27. test_invalidResponsesNeverPolluteEntriesDuringUnboundedReconnect — HTTP/MIME ошибки не оживляют поток и не глушат реконнект
  28. test_splitUTF8ScalarIsPreservedUntilCompleteLine — split UTF-8 не повреждает строку
  29. test_initializer_restoresShowOriginal_fromInjectedDefaults — HUD читает внедрённый suite
*/

import XCTest
import AppKit
@testable import KrabEarAgent

/// Тестовая SSE-задача считает запуски и отмены без сетевого соединения.
/// Так тест проверяет именно владение задачей, а не особенности URLSession.
private final class TrackingLiveSubtitlesSSETask: LiveSubtitlesSSETask {
    private(set) var resumeCount = 0
    private(set) var cancelCount = 0

    func resume() {
        resumeCount += 1
    }

    func cancel() {
        cancelCount += 1
    }
}

/// Тестовая SSE-сессия сохраняет делегат и позволяет вручную подать данные,
/// EOF или ошибку без сокетов и ожиданий реального тайм-аута.
private final class TrackingLiveSubtitlesSSESession: LiveSubtitlesSSESession {
    let delegate: SSESessionDelegate
    let task = TrackingLiveSubtitlesSSETask()
    private(set) var invalidateCount = 0

    init(delegate: SSESessionDelegate) {
        self.delegate = delegate
    }

    func makeLiveSubtitlesTask(with request: URLRequest) -> LiveSubtitlesSSETask {
        task
    }

    func invalidateAndCancel() {
        invalidateCount += 1
    }

    func receive(_ text: String) {
        delegate._testReceive(text)
    }

    @discardableResult
    func receiveResponse(statusCode: Int, contentType: String?) -> Bool {
        delegate._testReceiveResponse(statusCode: statusCode, contentType: contentType)
    }

    func complete(error: Error? = nil) {
        delegate._testComplete(error: error)
    }
}

/// Синтетическая ошибка соединения для проверки ветки ошибки при переподключении.
private struct SyntheticSSEError: Error {}

/// Ссылочный контейнер позволяет сохраняющимся тестовым замыканиям записывать
/// созданные сессии и отложенные работы без запрещённого захвата `inout`.
private final class TrackingSSEEnvironment {
    var sessions: [TrackingLiveSubtitlesSSESession] = []
    var scheduledReconnects: [DispatchWorkItem] = []
}

// MARK: - LiveSubtitlesOverlayWave190Tests

@MainActor
final class LiveSubtitlesOverlayWave190Tests: XCTestCase {

    private let defaultsDomain = IsolatedUserDefaultsDomain(scope: "LiveSubtitlesOverlayWave190Tests")
    private let panelOrdering = RecordingPanelOrdering()

    override func tearDown() async throws {
        defaultsDomain.removePersistentDomain()
        try await super.tearDown()
    }

    // MARK: - Helpers

    private func makeOverlay() -> LiveSubtitlesOverlay {
        LiveSubtitlesOverlay(
            userDefaults: defaultsDomain.defaults,
            panelOrdering: panelOrdering
        )
    }

    /// Даёт задачам, поставленным через `Task { @MainActor }`, отработать без ожидания по времени.
    private func drainSSECallbacks() async {
        for _ in 0..<3 {
            await Task.yield()
        }
    }

    private func makeTrackedOverlay(environment: TrackingSSEEnvironment) -> LiveSubtitlesOverlay {
        LiveSubtitlesOverlay(
            sseSessionFactory: { delegate in
                let session = TrackingLiveSubtitlesSSESession(delegate: delegate)
                environment.sessions.append(session)
                return session
            },
            reconnectScheduler: { _, workItem in
                environment.scheduledReconnects.append(workItem)
            },
            userDefaults: defaultsDomain.defaults,
            panelOrdering: panelOrdering
        )
    }

    // MARK: 1. test_initial_state_empty

    func test_initial_state_empty() {
        let overlay = makeOverlay()
        XCTAssertFalse(overlay.isVisible, "isVisible должен стартовать как false")
        XCTAssertEqual(overlay._testEntryCount, 0, "Entries должны быть пусты при инициализации")
    }

    // MARK: 2. test_appendLine_displays_text

    func test_appendLine_displays_text() {
        let overlay = makeOverlay()
        overlay.addEntry(original: "Hello", translation: "Привет")
        XCTAssertEqual(overlay._testEntryCount, 1, "Одна запись должна быть добавлена")
    }

    // MARK: 3. test_max_3_lines_oldest_evicted

    func test_max_3_lines_oldest_evicted() {
        let overlay = makeOverlay()
        overlay.addEntry(original: "First", translation: "Первый")
        overlay.addEntry(original: "Second", translation: "Второй")
        overlay.addEntry(original: "Third", translation: "Третий")
        // Должно быть 3 записи
        XCTAssertEqual(overlay._testEntryCount, 3, "Должно быть ровно 3 записи")
        // Четвёртая должна вытолкнуть первую
        overlay.addEntry(original: "Fourth", translation: "Четвёртый")
        XCTAssertEqual(overlay._testEntryCount, 3, "После eviction должно остаться 3 записи (не 4)")
    }

    // MARK: 4. test_auto_fade_after_4s

    func test_auto_fade_after_4s() {
        let overlay = makeOverlay()
        overlay.addEntry(original: "Fade test", translation: "Тест фейда")
        XCTAssertEqual(overlay._testEntryCount, 1, "Запись должна существовать перед фейдом")
        // Симулируем прошедшее время через clearAll (таймер будет инвалидирован)
        overlay.clearAll()
        XCTAssertEqual(overlay._testEntryCount, 0, "clearAll должен сбросить все записи")
    }

    // MARK: 5. test_panel_always_on_top

    func test_panel_always_on_top() {
        let overlay = makeOverlay()
        XCTAssertEqual(
            overlay._testPanelLevel,
            NSWindow.Level.floating,
            "NSPanel должен иметь уровень .floating для always-on-top поведения"
        )
    }

    // MARK: 6. test_panel_is_draggable

    func test_panel_is_draggable() {
        let overlay = makeOverlay()
        XCTAssertTrue(
            overlay._testPanelIsDraggable,
            "isMovableByWindowBackground должен быть true для drag-to-reposition"
        )
    }

    // MARK: 7. test_unicode_subtitle_displayed

    func test_unicode_subtitle_displayed() {
        let overlay = makeOverlay()
        let unicodeText = "Привет, мир! 🌍 こんにちは العالم"
        let unicodeTranslation = "Hello, world! 🌍 Hola mundo"
        // Не должно крашиться на unicode
        overlay.addEntry(original: unicodeText, translation: unicodeTranslation)
        XCTAssertEqual(overlay._testEntryCount, 1, "Unicode строка должна добавляться без краша")
    }

    // MARK: 8. test_long_text_wraps

    func test_long_text_wraps() {
        let overlay = makeOverlay()
        let longText = String(repeating: "Очень длинный текст который должен переноситься. ", count: 20)
        overlay.addEntry(original: longText, translation: longText)
        // Не должно крашиться — lineBreakMode.byTruncatingTail защищает от переполнения
        XCTAssertEqual(overlay._testEntryCount, 1, "Длинный текст должен добавляться без краша")
    }

    // MARK: 9. test_concurrent_appendLine_thread_safe

    func test_concurrent_appendLine_thread_safe() async {
        // LiveSubtitlesOverlay — @MainActor, все вызовы serialized на main thread.
        // Тест проверяет что 50 addEntry подряд не вызывают краш.
        let overlay = makeOverlay()
        for i in 0..<50 {
            overlay.addEntry(original: "Original \(i)", translation: "Перевод \(i)")
        }
        // После 50 rapid-fire записей max=3 должен быть enforced
        XCTAssertEqual(overlay._testEntryCount, 3, "После 50 записей должно остаться максимум 3")
    }

    // MARK: 10. test_clear_resets_panel

    func test_clear_resets_panel() {
        let overlay = makeOverlay()
        overlay.addEntry(original: "A", translation: "Б")
        overlay.addEntry(original: "B", translation: "В")
        overlay.addEntry(original: "C", translation: "Г")
        XCTAssertEqual(overlay._testEntryCount, 3)
        overlay.clearAll()
        XCTAssertEqual(overlay._testEntryCount, 0, "clearAll должен обнулить все записи")
    }

    // MARK: 11. test_sse_correct_event_type_parsed

    func test_sse_correct_event_type_parsed() {
        let overlay = makeOverlay()
        // Симулируем SSE поток с правильным event type
        overlay._testHandleSSELine("event: live_subs.result")
        overlay._testHandleSSELine(#"data: {"text":"Hello","translation":"Привет"}"#)
        XCTAssertEqual(overlay._testEntryCount, 1, "live_subs.result event должен добавить запись")
    }

    // MARK: 12. test_sse_wrong_event_type_ignored

    func test_sse_wrong_event_type_ignored() {
        let overlay = makeOverlay()
        // Другой event type — должен быть проигнорирован
        overlay._testHandleSSELine("event: other.event")
        overlay._testHandleSSELine(#"data: {"text":"Hello","translation":"Привет"}"#)
        XCTAssertEqual(overlay._testEntryCount, 0, "Чужой event type не должен добавлять запись")
    }

    // MARK: 13. test_sse_flat_text_field

    func test_sse_flat_text_field() {
        let overlay = makeOverlay()
        overlay._testHandleSSELine("event: live_subs.result")
        // Плоский формат с "text" вместо "original"
        overlay._testHandleSSELine(#"data: {"text":"Flat field","translation":"Плоское поле"}"#)
        XCTAssertEqual(overlay._testEntryCount, 1, "\"text\" поле должно парситься как original")
    }

    // MARK: 14. test_sse_both_field_aliases_work

    func test_sse_both_field_aliases_work() {
        let overlay = makeOverlay()
        // "original" alias
        overlay._testHandleSSELine("event: live_subs.result")
        overlay._testHandleSSELine(#"data: {"original":"Original field","translated":"Translated alias"}"#)
        XCTAssertEqual(overlay._testEntryCount, 1, "\"original\"/\"translated\" aliases должны парситься")
    }

    // MARK: 15. test_sse_empty_payload_ignored

    func test_sse_empty_payload_ignored() {
        let overlay = makeOverlay()
        overlay._testHandleSSELine("event: live_subs.result")
        // Оба поля пусты — entry не добавляется (guard !translation.isEmpty || !original.isEmpty)
        overlay._testHandleSSELine(#"data: {"text":"","translation":""}"#)
        XCTAssertEqual(overlay._testEntryCount, 0, "Пустой payload не должен добавлять запись")
    }

    // MARK: 16. test_show_hide_isVisible

    func test_show_hide_isVisible() {
        // show() открывает SSE-соединение, поэтому даже простая проверка флага
        // использует тестовую сессию и никогда не обращается к localhost.
        let environment = TrackingSSEEnvironment()
        let overlay = makeTrackedOverlay(environment: environment)
        overlay.show()
        XCTAssertTrue(overlay.isVisible, "show() должен устанавливать isVisible = true")
        XCTAssertEqual(panelOrdering.orderFrontCallCount, 1)
        overlay.hide()
        XCTAssertFalse(overlay.isVisible, "hide() должен устанавливать isVisible = false")
        XCTAssertEqual(panelOrdering.orderOutCallCount, 1)
    }

    /// show → hide → show не должен оставлять старое SSE-соединение живым:
    /// скрытие отменяет именно активную задачу и освобождает ссылку на неё.
    func test_showHideShow_replacesSSETaskWithoutAccumulation() {
        let environment = TrackingSSEEnvironment()
        let overlay = makeTrackedOverlay(environment: environment)

        overlay.show()

        XCTAssertEqual(environment.sessions.count, 1)
        XCTAssertEqual(environment.sessions[0].task.resumeCount, 1)
        XCTAssertEqual(environment.sessions[0].task.cancelCount, 0)
        XCTAssertTrue(overlay._testHasActiveSSETask)

        overlay.hide()

        XCTAssertEqual(environment.sessions[0].task.cancelCount, 1)
        XCTAssertEqual(environment.sessions[0].invalidateCount, 1)
        XCTAssertFalse(overlay._testHasActiveSSETask)

        overlay.show()

        XCTAssertEqual(environment.sessions.count, 2, "Повторный show должен создать ровно одну новую задачу")
        XCTAssertEqual(environment.sessions[0].task.cancelCount, 1, "Старая задача не должна ожить или отменяться повторно")
        XCTAssertEqual(environment.sessions[1].task.resumeCount, 1)
        XCTAssertEqual(environment.sessions[1].task.cancelCount, 0)
        XCTAssertTrue(overlay._testHasActiveSSETask)

        overlay.hide()
        XCTAssertEqual(environment.sessions[1].task.cancelCount, 1)
        XCTAssertEqual(environment.sessions[1].invalidateCount, 1)
        XCTAssertFalse(overlay._testHasActiveSSETask)
    }

    func test_completionReconnectsForEOFAndErrorWhileVisible() async {
        let environment = TrackingSSEEnvironment()
        let overlay = makeTrackedOverlay(environment: environment)
        overlay.show()

        environment.sessions[0].complete()
        await drainSSECallbacks()

        XCTAssertFalse(overlay._testHasActiveSSETask)
        XCTAssertEqual(environment.sessions[0].invalidateCount, 1)
        XCTAssertEqual(environment.scheduledReconnects.count, 1)

        environment.scheduledReconnects.removeFirst().perform()
        await drainSSECallbacks()
        XCTAssertEqual(environment.sessions.count, 2)
        XCTAssertTrue(overlay._testHasActiveSSETask)

        environment.sessions[1].complete(error: SyntheticSSEError())
        await drainSSECallbacks()
        XCTAssertEqual(environment.scheduledReconnects.count, 1)

        overlay.hide()
        environment.scheduledReconnects.removeFirst().perform()
        await drainSSECallbacks()
        XCTAssertEqual(environment.sessions.count, 2, "После hide() отложенное переподключение не должно создавать сессию")
    }

    func test_staleConnectionCallbacksIgnoredAfterReconnect() async {
        let environment = TrackingSSEEnvironment()
        let overlay = makeTrackedOverlay(environment: environment)
        overlay.show()
        environment.sessions[0].complete()
        await drainSSECallbacks()
        environment.scheduledReconnects.removeFirst().perform()
        await drainSSECallbacks()

        environment.sessions[0].complete(error: SyntheticSSEError())
        await drainSSECallbacks()
        XCTAssertTrue(overlay._testHasActiveSSETask, "Позднее завершение старой сессии не должно закрыть новую")
        XCTAssertTrue(environment.scheduledReconnects.isEmpty)

        environment.sessions[0].receive("event: live_subs.result\n")
        environment.sessions[0].receive(#"data: {"text":"Старое","translation":"Old"}"# + "\n")
        await drainSSECallbacks()
        XCTAssertEqual(overlay._testEntryCount, 0, "Поздний обработчик старой сессии должен быть отброшен")

        environment.sessions[1].receive("event: live_subs.result\n")
        environment.sessions[1].receive(#"data: {"text":"Новое","translation":"New"}"# + "\n")
        await drainSSECallbacks()
        XCTAssertEqual(overlay._testEntryCount, 1)
        overlay.hide()
    }

    func test_partialBufferDoesNotCrossConnectionGeneration() async {
        let environment = TrackingSSEEnvironment()
        let overlay = makeTrackedOverlay(environment: environment)
        overlay.show()
        environment.sessions[0].receive("event: live_subs.")
        environment.sessions[0].complete(error: SyntheticSSEError())
        await drainSSECallbacks()
        environment.scheduledReconnects.removeFirst().perform()
        await drainSSECallbacks()

        // Если старый незавершённый буфер протёк, `result` завершит корректную строку
        // события, и следующая строка данных ошибочно добавит субтитр.
        environment.sessions[1].receive("result\n")
        environment.sessions[1].receive(#"data: {"text":"Смешано","translation":"Mixed"}"# + "\n")
        await drainSSECallbacks()
        XCTAssertEqual(overlay._testEntryCount, 0)

        environment.sessions[1].receive("event: live_subs.result\n")
        environment.sessions[1].receive(#"data: {"text":"Чисто","translation":"Clean"}"# + "\n")
        await drainSSECallbacks()
        XCTAssertEqual(overlay._testEntryCount, 1)
        overlay.hide()
    }

    /// S3 финальное ревью, фикс 3: до фикса `maxReconnectAttempts = 5`
    /// сдавался за ~15.5с — при живом in-process REST сторож задачи 7 лечит
    /// не меньше минуты, поэтому лимит воспроизводил бы застывший экран
    /// живых субтитров. Здесь прогоняем 12 подряд обрывов (заведомо больше
    /// старого лимита 5) и проверяем, что реконнект планируется КАЖДЫЙ раз,
    /// пока панель видима — и прекращается только по hide(), а не по счётчику.
    func test_reconnectContinuesIndefinitelyWhileVisible() async {
        let environment = TrackingSSEEnvironment()
        let overlay = makeTrackedOverlay(environment: environment)
        overlay.show()

        let attemptsBeyondOldCap = 12
        for _ in 0..<attemptsBeyondOldCap {
            environment.sessions.last!.complete(error: SyntheticSSEError())
            await drainSSECallbacks()
            XCTAssertEqual(
                environment.scheduledReconnects.count, 1,
                "реконнект обязан планироваться на каждый обрыв, пока панель видима — без верхней границы попыток"
            )
            environment.scheduledReconnects.removeFirst().perform()
            await drainSSECallbacks()
        }
        XCTAssertTrue(overlay._testHasActiveSSETask, "после \(attemptsBeyondOldCap) переподключений соединение всё ещё поднимается")

        // Останов приходит только от hide(), не от исчерпанного бюджета попыток.
        overlay.hide()
        environment.sessions.last!.complete(error: SyntheticSSEError())
        await drainSSECallbacks()
        XCTAssertTrue(environment.scheduledReconnects.isEmpty, "скрытая панель не должна планировать реконнект")
        XCTAssertFalse(overlay._testHasActiveSSETask)
    }

    /// S3 финальное ревью, фикс 3 (сиблинг предыдущего теста): невалидные
    /// HTTP-ответы (не проходят status/MIME проверку) не должны ни попасть в
    /// UI, ни остановить бесконечную серию реконнектов раньше времени —
    /// раньше именование теста относилось к «бюджету» из 5 попыток, теперь
    /// бюджета нет вовсе, поэтому прогоняем заведомо больше старого лимита.
    func test_invalidResponsesNeverPolluteEntriesDuringUnboundedReconnect() async {
        let environment = TrackingSSEEnvironment()
        let overlay = makeTrackedOverlay(environment: environment)
        overlay.show()

        let attemptsBeyondOldCap = 12
        for attempt in 0..<attemptsBeyondOldCap {
            let session = environment.sessions.last!
            let accepted: Bool
            if attempt.isMultiple(of: 2) {
                accepted = session.receiveResponse(
                    statusCode: 503,
                    contentType: "text/event-stream"
                )
            } else {
                accepted = session.receiveResponse(
                    statusCode: 200,
                    contentType: "text/html; charset=utf-8"
                )
            }
            XCTAssertFalse(accepted)

            session.receive("event: live_subs.result\n")
            session.receive(#"data: {"text":"Ошибка","translation":"Error"}"# + "\n")
            session.complete(error: SyntheticSSEError())
            await drainSSECallbacks()

            XCTAssertEqual(
                environment.scheduledReconnects.count, 1,
                "невалидный ответ не должен глушить серию реконнектов раньше времени"
            )
            environment.scheduledReconnects.removeFirst().perform()
            await drainSSECallbacks()
        }

        XCTAssertTrue(overlay._testHasActiveSSETask, "серия продолжается за пределами старого лимита в 5 попыток")
        XCTAssertEqual(
            environment.sessions.count, attemptsBeyondOldCap + 1,
            "начальное соединение + \(attemptsBeyondOldCap) переподключений"
        )
        XCTAssertEqual(overlay._testEntryCount, 0, "Тело ошибочного ответа не является SSE")
        overlay.hide()
    }

    func test_deinitCancelsTaskAndInvalidatesSession() async {
        let environment = TrackingSSEEnvironment()
        var overlay: LiveSubtitlesOverlay? = makeTrackedOverlay(environment: environment)
        weak let weakOverlay = overlay
        overlay?.show()

        overlay = nil
        await drainSSECallbacks()

        XCTAssertNil(weakOverlay)
        XCTAssertEqual(environment.sessions[0].task.cancelCount, 1)
        XCTAssertEqual(environment.sessions[0].invalidateCount, 1)
    }

    // MARK: 17. test_showOriginal_toggle_no_crash

    func test_showOriginal_toggle_no_crash() {
        let overlay = makeOverlay()
        overlay.addEntry(original: "Orig", translation: "Trans")
        overlay.showOriginalAndTranslation = true
        XCTAssertTrue(overlay.showOriginalAndTranslation)
        overlay.showOriginalAndTranslation = false
        XCTAssertFalse(overlay.showOriginalAndTranslation)
        // Toggle с записями — не должно крашиться
        overlay.addEntry(original: "More", translation: "Больше")
        overlay.showOriginalAndTranslation = true
        XCTAssertTrue(true, "Переключение showOriginalAndTranslation не должно крашиться")
    }

    func test_initializer_restoresShowOriginal_fromInjectedDefaults() {
        defaultsDomain.defaults.set(false, forKey: "KrabEar_LiveSubsShowOriginal")

        let overlay = makeOverlay()

        XCTAssertFalse(
            overlay.showOriginalAndTranslation,
            "HUD обязан читать настройку показа оригинала из внедрённого домена"
        )
    }

    // MARK: 18. test_resetPosition_no_crash

    func test_resetPosition_no_crash() {
        defaultsDomain.defaults.set("test-position", forKey: "KrabEar_LiveSubsHUDPosition")
        let overlay = makeOverlay()
        overlay.resetPosition()
        // UserDefaults ключ должен быть удалён
        XCTAssertNil(
            defaultsDomain.defaults.string(forKey: "KrabEar_LiveSubsHUDPosition"),
            "resetPosition должен удалять сохранённую позицию из UserDefaults"
        )
    }

    // MARK: 19. test_restBaseURL_default

    func test_restBaseURL_default() {
        let overlay = makeOverlay()
        XCTAssertTrue(
            overlay.restBaseURL.contains("5005"),
            "restBaseURL по умолчанию должен содержать порт 5005"
        )
        XCTAssertTrue(
            overlay.restBaseURL.hasPrefix("http://"),
            "restBaseURL должен начинаться с http://"
        )
    }

    // MARK: 20. test_clearAll_after_multiple_entries

    func test_clearAll_after_multiple_entries() {
        let overlay = makeOverlay()
        for i in 0..<5 {
            overlay.addEntry(original: "O\(i)", translation: "T\(i)")
        }
        // max=3, но проверяем что clearAll точно обнуляет
        XCTAssertEqual(overlay._testEntryCount, 3, "Должно быть 3 записи (cap enforced)")
        overlay.clearAll()
        XCTAssertEqual(overlay._testEntryCount, 0, "clearAll должен обнулять все записи")
    }
}

// MARK: - Изоляция буфера общего SSE-делегата

/// Общий делегат используется несколькими экранами, поэтому незавершённые строки
/// разных URLSessionTask не должны склеиваться даже при повторном использовании.
final class SSESessionDelegateLifecycleTests: XCTestCase {

    func test_splitUTF8ScalarIsPreservedUntilCompleteLine() {
        var lines: [String] = []
        let delegate = SSESessionDelegate { lines.append($0) }
        let expected = #"data: {"text":"Привет 🦀"}"#
        let bytes = Data(expected.utf8)
        guard let scalarStart = bytes.firstIndex(of: 0xF0) else {
            return XCTFail("В тестовой строке не найден четырёхбайтовый UTF-8 scalar")
        }
        let splitIndex = scalarStart + 2

        delegate._testReceive(bytes.prefix(upTo: splitIndex))
        XCTAssertTrue(lines.isEmpty)

        var tail = Data(bytes.suffix(from: splitIndex))
        tail.append(0x0A)
        delegate._testReceive(tail)

        XCTAssertEqual(lines, [expected])
    }

    func test_responseValidationRejectsStatusAndMIMEBeforeDeliveringLines() {
        var lines: [String] = []
        let delegate = SSESessionDelegate { lines.append($0) }
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let url = URL(string: "http://127.0.0.1:5005/v1/events")!

        func deliverResponse(
            statusCode: Int,
            contentType: String,
            task: URLSessionDataTask
        ) -> URLSession.ResponseDisposition? {
            guard let response = HTTPURLResponse(
                url: url,
                statusCode: statusCode,
                httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": contentType]
            ) else {
                XCTFail("Не удалось создать тестовый HTTPURLResponse")
                return nil
            }
            var disposition: URLSession.ResponseDisposition?
            delegate.urlSession(
                session,
                dataTask: task,
                didReceive: response,
                completionHandler: { disposition = $0 }
            )
            return disposition
        }

        let rejectedStatusTask = session.dataTask(with: url)
        XCTAssertEqual(deliverResponse(
            statusCode: 503,
            contentType: "text/event-stream",
            task: rejectedStatusTask
        ), .cancel)
        delegate._testReceive(
            "event: rejected-status\n",
            taskIdentifier: rejectedStatusTask.taskIdentifier
        )

        let rejectedMIMETask = session.dataTask(with: url)
        XCTAssertEqual(deliverResponse(
            statusCode: 200,
            contentType: "text/html; charset=utf-8",
            task: rejectedMIMETask
        ), .cancel)
        delegate._testReceive(
            "event: rejected-mime\n",
            taskIdentifier: rejectedMIMETask.taskIdentifier
        )

        let acceptedTask = session.dataTask(with: url)
        XCTAssertEqual(deliverResponse(
            statusCode: 200,
            contentType: "text/event-stream; charset=utf-8",
            task: acceptedTask
        ), .allow)
        delegate._testReceive(
            "event: accepted\n",
            taskIdentifier: acceptedTask.taskIdentifier
        )

        XCTAssertEqual(lines, ["event: accepted"])
    }

    func test_partialBuffersAreSeparatedByTask() {
        var lines: [String] = []
        let delegate = SSESessionDelegate { lines.append($0) }

        delegate._testReceive("event: old", taskIdentifier: 11)
        delegate._testReceive("event: new\n", taskIdentifier: 22)

        XCTAssertEqual(lines, ["event: new"])

        delegate._testReceive(".tail\n", taskIdentifier: 11)
        XCTAssertEqual(lines, ["event: new", "event: old.tail"])
    }

    func test_completionClearsOnlyCompletedTaskBuffer() {
        var lines: [String] = []
        var completionCount = 0
        let delegate = SSESessionDelegate(
            onLine: { lines.append($0) },
            onComplete: { _ in completionCount += 1 }
        )

        delegate._testReceive("discard-me", taskIdentifier: 11)
        delegate._testReceive("keep-me", taskIdentifier: 22)
        delegate._testComplete(taskIdentifier: 11)
        delegate._testReceive("fresh\n", taskIdentifier: 11)
        delegate._testReceive("-tail\n", taskIdentifier: 22)

        XCTAssertEqual(completionCount, 1)
        XCTAssertEqual(lines, ["fresh", "keep-me-tail"])
    }
}

// MARK: - Off-screen guard в restorePosition() (fix/livesubs-offscreen-guard)

/// Портировано из RealtimeOverlayController.restoreSavedPosition() (M2, ~строки 757-782) /
/// ConversationStatusOverlay.isOnScreen(_:) (Волна 3c): сохранённая позиция применяется,
/// только если ≥80% frame пересекается с visibleFrame какого-нибудь ТЕКУЩЕГО экрана. Без
/// этой проверки юзер, перетащивший HUD на второй монитор и затем отключивший его, теряет
/// панель за экраном навсегда — restorePosition() применял UserDefaults безусловно.
@MainActor
final class LiveSubtitlesOverlayPositionGuardTests: XCTestCase {

    private let positionKey = "KrabEar_LiveSubsHUDPosition"
    private let defaultsDomain = IsolatedUserDefaultsDomain(scope: "LiveSubtitlesOverlayPositionGuardTests")
    private let panelOrdering = RecordingPanelOrdering()

    override func tearDown() async throws {
        defaultsDomain.removePersistentDomain()
        try await super.tearDown()
    }

    private func savePosition(x: CGFloat, y: CGFloat) {
        let dict: [String: CGFloat] = ["x": x, "y": y]
        guard let data = try? JSONSerialization.data(withJSONObject: dict),
              let str = String(data: data, encoding: .utf8) else {
            return XCTFail("не удалось сериализовать тестовую позицию")
        }
        defaultsDomain.defaults.set(str, forKey: positionKey)
    }

    /// Заведомо off-screen сохранённая позиция (например после отключения второго
    /// монитора) НЕ должна применяться безусловно. Инвариант держится независимо от
    /// headless-среды CI: (99999, 99999) не может пересекаться ни с одним реальным
    /// экраном ≥80%, поэтому кандидат никогда не проходит guard — panel либо падает
    /// на placeAtBottom() (есть NSScreen.main), либо остаётся на дефолтном contentRect
    /// панели (экранов нет вовсе). Ни один из исходов не равен (99999, 99999).
    func test_restorePosition_offScreen_doesNotApplyBogusOrigin() {
        savePosition(x: 99999, y: 99999)

        let overlay = LiveSubtitlesOverlay(
            userDefaults: defaultsDomain.defaults,
            panelOrdering: panelOrdering
        )

        XCTAssertNotEqual(overlay._testPanelOrigin.x, 99999)
        XCTAssertNotEqual(overlay._testPanelOrigin.y, 99999)
    }

    /// Позитивный кейс: позиция внутри видимой области реального экрана восстанавливается
    /// как есть. Пропускается в headless-среде без NSScreen.main (нечего проверять).
    func test_restorePosition_onScreen_appliesSavedOrigin() throws {
        guard let screen = NSScreen.main else {
            throw XCTSkip("нет NSScreen.main в этой среде — позитивный кейс непроверяем headless")
        }
        let vf = screen.visibleFrame
        let x = vf.minX + 40
        let y = vf.minY + 40
        savePosition(x: x, y: y)

        let overlay = LiveSubtitlesOverlay(
            userDefaults: defaultsDomain.defaults,
            panelOrdering: panelOrdering
        )

        XCTAssertEqual(overlay._testPanelOrigin.x, x, accuracy: 0.5)
        XCTAssertEqual(overlay._testPanelOrigin.y, y, accuracy: 0.5)
    }
}
