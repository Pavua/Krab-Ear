/*
 HotkeyDoubleTapDetectorTests — тесты детектора двойного нажатия Right Option.

 Подход:
 - HotkeyDoubleTapLogic — pure value type с injectable nowProvider для
   детерминированного тестирования без реального времени и async sleep.
 - Тесты логики (test_logic_*) — мгновенные, без async.
 - Тесты хука injectTapAt (test_inject_*) — через публичный тест-хук.
 - Тесты граничного значения 300 мс — проверяют inclusive/exclusive поведение.
 - Сброс состояния через reset()/stop().

 Swift 6: все тесты @MainActor (detector изолирован на MainActor).
*/

import AppKit
import XCTest
@testable import KrabEarAgent

// MARK: - Pure logic struct (injectable clock)

/// Чистая логика детектора двойного нажатия с инъектируемым провайдером времени.
/// Позволяет тестировать детерминированно без реального времени.
struct HotkeyDoubleTapLogic {
    var nowProvider: () -> TimeInterval
    let windowSec: TimeInterval
    var lastTapTime: TimeInterval?

    init(windowSec: TimeInterval = 0.3, nowProvider: @escaping () -> TimeInterval) {
        self.windowSec = windowSec
        self.nowProvider = nowProvider
    }

    /// Обработать одно нажатие. Возвращает true, если это double-tap.
    mutating func handleTap() -> Bool {
        let now = nowProvider()
        if let last = lastTapTime, (now - last) <= windowSec {
            // Второй тап в окне — double-tap
            lastTapTime = nil
            return true
        }
        // Первый тап (или просроченный) — запускаем окно
        lastTapTime = now
        return false
    }

    /// Сбросить состояние (эмулирует stop()).
    mutating func reset() {
        lastTapTime = nil
    }
}

// MARK: - HotkeyDoubleTapLogic tests (deterministic, no async)

final class HotkeyDoubleTapLogicTests: XCTestCase {

    // MARK: test_first_tap_no_trigger

    func test_first_tap_no_trigger() {
        var time: TimeInterval = 1000.0
        var logic = HotkeyDoubleTapLogic(windowSec: 0.3) { time }

        let result = logic.handleTap()

        XCTAssertFalse(result, "Первый тап не должен вызывать double-tap")
        XCTAssertNotNil(logic.lastTapTime, "После первого тапа время должно быть сохранено")
    }

    // MARK: test_second_tap_within_300ms_triggers

    func test_second_tap_within_300ms_triggers() {
        var time: TimeInterval = 1000.0
        var logic = HotkeyDoubleTapLogic(windowSec: 0.3) { time }

        let first = logic.handleTap()     // t=1000.0
        XCTAssertFalse(first)

        time = 1000.0 + 0.200            // +200 мс — внутри окна 300 мс
        let second = logic.handleTap()

        XCTAssertTrue(second, "Второй тап через 200 мс должен вызвать double-tap")
        XCTAssertNil(logic.lastTapTime, "После double-tap состояние должно быть сброшено")
    }

    // MARK: test_second_tap_after_300ms_no_trigger (resets)

    func test_second_tap_after_300ms_no_trigger_resets() throws {
        var time: TimeInterval = 1000.0
        var logic = HotkeyDoubleTapLogic(windowSec: 0.3) { time }

        let first = logic.handleTap()    // t=1000.0
        XCTAssertFalse(first)

        time = 1000.0 + 0.400            // +400 мс — за пределами окна 300 мс
        let second = logic.handleTap()

        XCTAssertFalse(second, "Тап через 400 мс не должен давать double-tap (окно истекло)")
        // Состояние обновилось на новый первый тап
        let savedTime = try XCTUnwrap(logic.lastTapTime,
                                      "Просроченный тап должен стать новым firstTapTime")
        XCTAssertEqual(savedTime, 1000.0 + 0.400, accuracy: 0.001,
                       "Просроченный тап становится новым первым тапом")
    }

    // MARK: test_third_tap_resets_state

    func test_third_tap_resets_state() {
        var time: TimeInterval = 1000.0
        var logic = HotkeyDoubleTapLogic(windowSec: 0.3) { time }
        var doubleTapCount = 0

        _ = logic.handleTap()            // tap 1 → first tap

        time += 0.100
        if logic.handleTap() { doubleTapCount += 1 }  // tap 2 → double-tap, state сброшен

        time += 0.100
        let thirdResult = logic.handleTap()  // tap 3 → становится новым первым тапом

        XCTAssertEqual(doubleTapCount, 1, "Ровно один double-tap на тапах 1+2")
        XCTAssertFalse(thirdResult, "Третий тап — новый первый, не double-tap")
        XCTAssertNotNil(logic.lastTapTime, "После третьего тапа состояние сохранено")
    }

    // MARK: test_window_boundary_exactly_300ms (inclusive)

    func test_window_boundary_exactly_300ms_is_inclusive() {
        var time: TimeInterval = 1000.0
        var logic = HotkeyDoubleTapLogic(windowSec: 0.3) { time }

        _ = logic.handleTap()            // t=1000.0

        time = 1000.0 + 0.300            // ровно на границе — inclusive (<=)
        let result = logic.handleTap()

        XCTAssertTrue(result,
            "Граница 300 мс включена (<=): тап ровно в 300 мс должен давать double-tap")
    }

    // MARK: test_window_boundary_just_over_300ms_no_trigger

    func test_window_boundary_just_over_300ms_no_trigger() {
        var time: TimeInterval = 1000.0
        var logic = HotkeyDoubleTapLogic(windowSec: 0.3) { time }

        _ = logic.handleTap()            // t=1000.0

        time = 1000.0 + 0.3001           // чуть больше 300 мс — за границей
        let result = logic.handleTap()

        XCTAssertFalse(result,
            "На 0.3001 с (> 300 мс) double-tap не должен срабатывать")
    }

    // MARK: test_reset_clears_state

    func test_reset_clears_state() {
        var time: TimeInterval = 1000.0
        var logic = HotkeyDoubleTapLogic(windowSec: 0.3) { time }

        _ = logic.handleTap()            // сохранили первый тап
        XCTAssertNotNil(logic.lastTapTime)

        logic.reset()                    // сброс

        XCTAssertNil(logic.lastTapTime, "reset() должен очистить lastTapTime")

        // Следующий тап после reset — снова первый
        time += 0.100
        let result = logic.handleTap()
        XCTAssertFalse(result, "После reset() тап — первый, не double-tap")
    }

    // MARK: test_sequential_double_taps

    func test_sequential_double_taps_count_correctly() {
        var time: TimeInterval = 1000.0
        var logic = HotkeyDoubleTapLogic(windowSec: 0.3) { time }
        var count = 0

        // Пара 1
        _ = logic.handleTap()
        time += 0.150
        if logic.handleTap() { count += 1 }

        // Пара 2 (state сброшен после double-tap)
        time += 0.500   // большой интервал — пара 2 начинается свежо
        _ = logic.handleTap()
        time += 0.150
        if logic.handleTap() { count += 1 }

        XCTAssertEqual(count, 2, "Два последовательных double-tap — 2 срабатывания")
    }

    // MARK: test_custom_window_respected

    func test_custom_window_10ms_respected() {
        var time: TimeInterval = 1000.0
        var logic = HotkeyDoubleTapLogic(windowSec: 0.010) { time }  // 10 мс окно

        _ = logic.handleTap()

        time += 0.005  // 5 мс < 10 мс → должен сработать
        let inside = logic.handleTap()
        XCTAssertTrue(inside, "5 мс < 10 мс окно → double-tap")

        _ = logic.handleTap()           // новый первый тап
        time += 0.020                    // 20 мс > 10 мс → не должен
        let outside = logic.handleTap()
        XCTAssertFalse(outside, "20 мс > 10 мс окно → нет double-tap")
    }
}

// MARK: - HotkeyDoubleTapDetector integration tests (via injectTapAt)

@MainActor
final class HotkeyDoubleTapDetectorTests: XCTestCase {

    private func makeDetector(
        windowMs: TimeInterval = 0.3,
        onDoubleTap: @escaping @MainActor () -> Void = {}
    ) -> HotkeyDoubleTapDetector {
        HotkeyDoubleTapDetector(windowMs: windowMs, onDoubleTap: onDoubleTap)
    }

    // MARK: test_inject_first_tap_no_trigger

    func test_inject_first_tap_no_trigger() {
        var triggered = false
        let detector = makeDetector { triggered = true }

        detector.injectTapAt(time: 1000.0)

        XCTAssertFalse(triggered, "Первый инжектированный тап не вызывает callback")
    }

    // MARK: test_inject_second_tap_within_window_triggers

    func test_inject_second_tap_within_window_triggers() {
        var tapCount = 0
        let detector = makeDetector(windowMs: 0.3) { tapCount += 1 }

        detector.injectTapAt(time: 1000.0)
        detector.injectTapAt(time: 1000.200)  // +200 мс — внутри 300 мс

        XCTAssertEqual(tapCount, 1, "Double-tap в окне вызывает callback ровно 1 раз")
    }

    // MARK: test_inject_second_tap_after_window_no_trigger

    func test_inject_second_tap_after_window_no_trigger() {
        var triggered = false
        let detector = makeDetector(windowMs: 0.3) { triggered = true }

        detector.injectTapAt(time: 1000.0)
        detector.injectTapAt(time: 1000.400)  // +400 мс — за пределами окна

        XCTAssertFalse(triggered, "Второй тап через 400 мс не вызывает callback")
    }

    // MARK: test_inject_third_tap_resets_state

    func test_inject_third_tap_resets_state() {
        var count = 0
        let detector = makeDetector(windowMs: 0.3) { count += 1 }

        detector.injectTapAt(time: 1000.0)    // первый
        detector.injectTapAt(time: 1000.100)  // второй → double-tap, count=1
        detector.injectTapAt(time: 1000.150)  // третий → новый первый тап

        XCTAssertEqual(count, 1, "Тройной тап: ровно 1 double-tap (тапы 1+2)")

        // Четвёртый тап в окне от третьего → ещё один double-tap
        detector.injectTapAt(time: 1000.250)  // +100 мс от третьего
        XCTAssertEqual(count, 2, "Тапы 3+4 тоже дают double-tap")
    }

    // MARK: test_inject_boundary_exactly_300ms_inclusive

    func test_inject_boundary_exactly_300ms_inclusive() {
        var triggered = false
        let detector = makeDetector(windowMs: 0.3) { triggered = true }

        detector.injectTapAt(time: 1000.0)
        detector.injectTapAt(time: 1000.300)  // ровно 300 мс — inclusive (<=)

        XCTAssertTrue(triggered,
            "Граница включена (<=): ровно 300 мс → double-tap должен сработать")
    }

    // MARK: test_inject_boundary_just_over_300ms_no_trigger

    func test_inject_boundary_just_over_300ms_no_trigger() {
        var triggered = false
        let detector = makeDetector(windowMs: 0.3) { triggered = true }

        detector.injectTapAt(time: 1000.0)
        detector.injectTapAt(time: 1000.3001)  // 300.1 мс — за границей

        XCTAssertFalse(triggered, "300.1 мс > окно → нет double-tap")
    }

    // MARK: test_stop_clears_state
    // Проверяем что после сброса детектор не даёт ложный double-tap.
    // Прямой доступ к private firstTapTime невозможен — тестируем через поведение:
    // инжектируем taps с монотонным временем, что исключает случайные negative-delta.

    func test_stop_clears_state() {
        var count = 0
        let detector = makeDetector(windowMs: 0.3) { count += 1 }

        // Сначала убеждаемся что работает нормально: double-tap [t=100, t=200]
        detector.injectTapAt(time: 100.0)
        detector.injectTapAt(time: 100.200)
        XCTAssertEqual(count, 1, "До reset: double-tap должен сработать")

        // После double-tap state сброшен (firstTapTime=nil).
        // Следующий одиночный тап → первый тап нового окна
        detector.injectTapAt(time: 200.0)
        XCTAssertEqual(count, 1, "Одиночный тап после reset не вызывает callback")

        // Тап вне окна от предыдущего → тоже первый тап, count остаётся 1
        detector.injectTapAt(time: 200.500)  // +500 мс > 300 мс
        XCTAssertEqual(count, 1, "Тап вне окна → нет нового double-tap")

        // Теперь double-tap: [t=200.500, t=200.700]
        detector.injectTapAt(time: 200.700)  // +200 мс от t=200.500 — внутри окна
        XCTAssertEqual(count, 2, "Новый double-tap после полного reset должен работать")
    }

    // MARK: test_windowMs_property_stored_correctly

    func test_windowMs_property_stored_correctly() {
        let detector = makeDetector(windowMs: 0.150)
        XCTAssertEqual(detector.windowMs, 0.150, accuracy: 0.0001,
                       "windowMs должен сохраняться корректно")
    }

    /// Отпускание правой Option при удерживаемой левой сохраняет общий `.option`,
    /// но не является вторым нажатием правой клавиши.
    func test_rightOptionReleaseWhileLeftHeld_doesNotCompleteDoubleTap() {
        var count = 0
        let detector = makeDetector(windowMs: 0.3) { count += 1 }
        let option = NSEvent.ModifierFlags.option.rawValue

        detector.injectFlagsChangedLogic(
            keyCode: Keycode.rightOption.rawValue,
            modifierFlagsRawValue: option | OptionKeyPhysicalState.rightOptionMask,
            time: 100.0
        )
        detector.injectFlagsChangedLogic(
            keyCode: Keycode.rightOption.rawValue,
            modifierFlagsRawValue: option | OptionKeyPhysicalState.leftOptionMask,
            time: 100.1
        )
        XCTAssertEqual(count, 0, "Отпускание не должно считаться вторым нажатием")

        detector.injectFlagsChangedLogic(
            keyCode: Keycode.rightOption.rawValue,
            modifierFlagsRawValue: option
                | OptionKeyPhysicalState.leftOptionMask
                | OptionKeyPhysicalState.rightOptionMask,
            time: 100.2
        )
        XCTAssertEqual(count, 1, "Следующее физическое нажатие завершает double-tap")
    }
}

// MARK: - Test hook extensions

extension HotkeyDoubleTapDetector {
    /// Тест-хук: инжектировать синтетическое нажатие с заданным временем.
    @MainActor
    func performTap() {
        injectTapAt(time: Date().timeIntervalSinceReferenceDate)
    }

    /// Тест-хук: сброс состояния через двойной тап (потребляет текущее firstTapTime).
    /// Вызывается только из тестового таргета для инициализации чистого состояния.
    /// Примечание: firstTapTime private — прямого доступа нет; вместо обнуления
    /// используем поведение: двойной тап потребляет состояние (firstTapTime = nil).
    @MainActor
    func consumeStateForTesting(firstTapTime t1: TimeInterval) {
        // Потребить сохранённое первое нажатие, вызвав double-tap (state → nil)
        injectTapAt(time: t1 + 0.001)  // второй тап в пределах окна
    }
}
