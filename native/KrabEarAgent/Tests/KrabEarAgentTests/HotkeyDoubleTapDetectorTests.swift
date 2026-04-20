/*
 HotkeyDoubleTapDetectorTests — тесты детектора двойного нажатия Right Option.

 Подход: синтетические потоки событий через прямой вызов внутреннего метода.
 NSEvent нельзя создать программно в unit-тестах → тестируем логику таймингов
 через тест-хук performTap() (extension в тест-таргете, @MainActor).

 Swift 6: все тесты помечены @MainActor (detector изолирован на MainActor).
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class HotkeyDoubleTapDetectorTests: XCTestCase {

    // MARK: - Helpers

    private func makeDetector(
        windowMs: TimeInterval = 0.3,
        onDoubleTap: @escaping @MainActor () -> Void
    ) -> HotkeyDoubleTapDetector {
        HotkeyDoubleTapDetector(windowMs: windowMs, onDoubleTap: onDoubleTap)
    }

    // MARK: - Tests

    func test_singleTap_doesNotTriggerCallback() async {
        var triggered = false
        let detector = makeDetector { triggered = true }

        detector.performTap()

        // Ждём чуть дольше окна — callback не должен сработать
        try? await Task.sleep(nanoseconds: 350_000_000) // 350 ms
        XCTAssertFalse(triggered, "Одиночный тап не должен вызывать callback")
    }

    func test_doubleTapWithinWindow_triggersCallback() async {
        var tapCount = 0
        let detector = makeDetector(windowMs: 0.3) { tapCount += 1 }

        detector.performTap()
        // Второй тап через 100 мс (в пределах окна 300 мс)
        try? await Task.sleep(nanoseconds: 100_000_000)
        detector.performTap()

        // Небольшая пауза чтобы callback выполнился
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(tapCount, 1, "Double-tap должен вызывать callback ровно 1 раз")
    }

    func test_doubleTapOutsideWindow_doesNotTrigger() async {
        var triggered = false
        let detector = makeDetector(windowMs: 0.15) { triggered = true }

        detector.performTap()
        // Второй тап через 250 мс — за пределами окна 150 мс
        try? await Task.sleep(nanoseconds: 250_000_000)
        detector.performTap()

        // Дополнительная пауза
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertFalse(triggered, "Второй тап за пределами окна не должен вызывать callback")
    }

    func test_tripleRapidTaps_triggersOnlyOnce() async {
        var tapCount = 0
        let detector = makeDetector(windowMs: 0.3) { tapCount += 1 }

        detector.performTap()
        try? await Task.sleep(nanoseconds: 50_000_000)
        detector.performTap()
        try? await Task.sleep(nanoseconds: 50_000_000)
        detector.performTap()

        // Ждём, убедимся что нет двойного вызова
        try? await Task.sleep(nanoseconds: 400_000_000)
        XCTAssertEqual(tapCount, 1, "Тройной тап должен давать ровно 1 callback")
    }

    func test_windowMs_isRespected() async {
        var triggered = false
        let detector = makeDetector(windowMs: 0.05) { triggered = true }

        detector.performTap()
        try? await Task.sleep(nanoseconds: 30_000_000) // 30 мс < окно 50 мс
        detector.performTap()

        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertTrue(triggered, "Double-tap в пределах 50 мс окна должен сработать")
    }

    func test_stopClearsState() async {
        var triggered = false
        let detector = makeDetector { triggered = true }

        detector.performTap()
        detector.stop()  // Остановили — state сброшен

        // Второй тап — но state уже сброшен stop(), первый тап исчез
        try? await Task.sleep(nanoseconds: 50_000_000)
        detector.performTap()

        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertFalse(triggered, "После stop() двойной тап не должен срабатывать")
    }
}

// MARK: - Test hook extension

extension HotkeyDoubleTapDetector {
    /// Тест-хук: инжектировать синтетическое press-событие в логику детектора.
    /// Вызывается только из тестового таргета.
    @MainActor
    func performTap() {
        let now = Date().timeIntervalSinceReferenceDate
        injectTapAt(time: now)
    }
}
