/*
 HotkeyDoubleTapDetectorTests — тесты детектора двойного нажатия Right Option.

 Подход: синтетические потоки событий через прямой вызов внутреннего метода.
 NSEvent нельзя создать программно в unit-тестах → тестируем логику таймингов
 через testable-интерфейс (whitebox через метод simulateTap).
*/

import XCTest
@testable import KrabEarAgent

final class HotkeyDoubleTapDetectorTests: XCTestCase {

    // MARK: - Helpers

    /// Создаёт detector и добавляет синтетический метод simulateTap для тестирования
    /// без реальных NSEvent.
    private func makeDetector(
        windowMs: TimeInterval = 0.3,
        onDoubleTap: @escaping () -> Void
    ) -> HotkeyDoubleTapDetectorTestable {
        HotkeyDoubleTapDetectorTestable(windowMs: windowMs, onDoubleTap: onDoubleTap)
    }

    // MARK: - Tests

    func test_singleTap_doesNotTriggerCallback() {
        var triggered = false
        let detector = makeDetector { triggered = true }

        detector.simulateTap()

        // Ждём чуть дольше окна — callback не должен сработать
        let exp = expectation(description: "no double-tap")
        exp.isInverted = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            exp.fulfill()
        }
        wait(for: [exp], timeout: 0.5)
        XCTAssertFalse(triggered, "Одиночный тап не должен вызывать callback")
    }

    func test_doubleTapWithinWindow_triggersCallback() {
        var tapCount = 0
        let exp = expectation(description: "double-tap callback")
        let detector = makeDetector(windowMs: 0.3) {
            tapCount += 1
            exp.fulfill()
        }

        detector.simulateTap()
        // Второй тап через 100 мс (в пределах окна 300 мс)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
            detector.simulateTap()
        }

        wait(for: [exp], timeout: 1.0)
        XCTAssertEqual(tapCount, 1, "Double-tap должен вызывать callback ровно 1 раз")
    }

    func test_doubleTapOutsideWindow_doesNotTrigger() {
        var triggered = false
        let detector = makeDetector(windowMs: 0.15) { triggered = true }

        detector.simulateTap()
        // Второй тап через 250 мс — за пределами окна 150 мс
        let exp = expectation(description: "outside window - no callback")
        exp.isInverted = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
            detector.simulateTap()
            exp.fulfill()
        }
        wait(for: [exp], timeout: 0.5)
        XCTAssertFalse(triggered, "Второй тап за пределами окна не должен вызывать callback")
    }

    func test_tripleRapidTaps_triggersOnlyOnce() {
        var tapCount = 0
        let exp = expectation(description: "triggered once")
        let detector = makeDetector(windowMs: 0.3) {
            tapCount += 1
            if tapCount == 1 { exp.fulfill() }
        }

        detector.simulateTap()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
            detector.simulateTap()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.10) {
            detector.simulateTap()
        }

        wait(for: [exp], timeout: 1.0)
        // Подождём ещё немного, убедимся что не двойной вызов
        let pause = expectation(description: "wait for potential second call")
        pause.isInverted = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
            pause.fulfill()
        }
        wait(for: [pause], timeout: 0.6)
        XCTAssertEqual(tapCount, 1, "Тройной тап должен давать ровно 1 callback")
    }

    func test_windowMs_isRespected() {
        // Проверяем что окно 50 мс работает независимо от дефолта 300 мс
        var triggered = false
        let exp = expectation(description: "tiny window double-tap")
        let detector = makeDetector(windowMs: 0.05) {
            triggered = true
            exp.fulfill()
        }

        detector.simulateTap()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.03) {
            detector.simulateTap()
        }

        wait(for: [exp], timeout: 1.0)
        XCTAssertTrue(triggered)
    }

    func test_stopClearsState() {
        var triggered = false
        let detector = makeDetector { triggered = true }

        detector.simulateTap()
        detector.stop()  // Остановили — state сброшен

        // Второй тап — но state уже сброшен stop(), первый тап исчез
        let exp = expectation(description: "no callback after stop")
        exp.isInverted = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
            detector.simulateTap()
            exp.fulfill()
        }
        wait(for: [exp], timeout: 0.3)
        XCTAssertFalse(triggered)
    }
}

// MARK: - HotkeyDoubleTapDetectorTestable

/// Testable subclass с синтетическим методом simulateTap для unit-тестов.
/// В prod-коде используется только HotkeyDoubleTapDetector (без simulateTap).
final class HotkeyDoubleTapDetectorTestable: HotkeyDoubleTapDetector {

    /// Имитирует одно нажатие Right Option (без реального NSEvent).
    func simulateTap() {
        // Напрямую вызываем логику через reflected method.
        // Используем Selector-based тест-хук через extension в тест-таргете.
        performTap()
    }
}

extension HotkeyDoubleTapDetector {
    /// Тест-хук: инжектировать синтетическое press-событие в логику детектора.
    /// Вызывается только из testable subclass в тестовом таргете.
    func performTap() {
        let now = Date().timeIntervalSinceReferenceDate
        injectTapAt(time: now)
    }
}
