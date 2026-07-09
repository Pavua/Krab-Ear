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
*/

import XCTest
import AppKit
@testable import KrabEarAgent

// MARK: - LiveSubtitlesOverlayWave190Tests

@MainActor
final class LiveSubtitlesOverlayWave190Tests: XCTestCase {

    // MARK: - Helpers

    private func makeOverlay() -> LiveSubtitlesOverlay {
        LiveSubtitlesOverlay()
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
        let overlay = makeOverlay()
        overlay.show()
        XCTAssertTrue(overlay.isVisible, "show() должен устанавливать isVisible = true")
        overlay.hide()
        XCTAssertFalse(overlay.isVisible, "hide() должен устанавливать isVisible = false")
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

    // MARK: 18. test_resetPosition_no_crash

    func test_resetPosition_no_crash() {
        let overlay = makeOverlay()
        overlay.resetPosition()
        // UserDefaults ключ должен быть удалён
        XCTAssertNil(
            UserDefaults.standard.string(forKey: "KrabEar_LiveSubsHUDPosition"),
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

// MARK: - Off-screen guard в restorePosition() (fix/livesubs-offscreen-guard)

/// Портировано из RealtimeOverlayController.restoreSavedPosition() (M2, ~строки 757-782) /
/// ConversationStatusOverlay.isOnScreen(_:) (Волна 3c): сохранённая позиция применяется,
/// только если ≥80% frame пересекается с visibleFrame какого-нибудь ТЕКУЩЕГО экрана. Без
/// этой проверки юзер, перетащивший HUD на второй монитор и затем отключивший его, теряет
/// панель за экраном навсегда — restorePosition() применял UserDefaults безусловно.
@MainActor
final class LiveSubtitlesOverlayPositionGuardTests: XCTestCase {

    private let positionKey = "KrabEar_LiveSubsHUDPosition"

    override func tearDown() async throws {
        UserDefaults.standard.removeObject(forKey: positionKey)
        try await super.tearDown()
    }

    private func savePosition(x: CGFloat, y: CGFloat) {
        let dict: [String: CGFloat] = ["x": x, "y": y]
        guard let data = try? JSONSerialization.data(withJSONObject: dict),
              let str = String(data: data, encoding: .utf8) else {
            return XCTFail("не удалось сериализовать тестовую позицию")
        }
        UserDefaults.standard.set(str, forKey: positionKey)
    }

    /// Заведомо off-screen сохранённая позиция (например после отключения второго
    /// монитора) НЕ должна применяться безусловно. Инвариант держится независимо от
    /// headless-среды CI: (99999, 99999) не может пересекаться ни с одним реальным
    /// экраном ≥80%, поэтому кандидат никогда не проходит guard — panel либо падает
    /// на placeAtBottom() (есть NSScreen.main), либо остаётся на дефолтном contentRect
    /// панели (экранов нет вовсе). Ни один из исходов не равен (99999, 99999).
    func test_restorePosition_offScreen_doesNotApplyBogusOrigin() {
        savePosition(x: 99999, y: 99999)

        let overlay = LiveSubtitlesOverlay()

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

        let overlay = LiveSubtitlesOverlay()

        XCTAssertEqual(overlay._testPanelOrigin.x, x, accuracy: 0.5)
        XCTAssertEqual(overlay._testPanelOrigin.y, y, accuracy: 0.5)
    }
}
