/*
 HotkeyManagerTests — тесты логики фильтрации событий HotkeyManager.

 Подход: NSEvent нельзя создать программно в unit-тестах → тестируем логику
 через тест-хук injectEventLogic(keyCode:isOptionDown:), который повторяет
 ту же фильтрацию что и handle(event:), без реального NSEvent/CGEventTap.

 Проверяем: вариантный enum, фильтрацию keyCodes, toggle-callback,
 debounce isPressed, start/stop lifecycle.
*/

import XCTest
@testable import KrabEarAgent

// MARK: - HotkeyManagerTests

@MainActor
final class HotkeyManagerTests: XCTestCase {

    // MARK: - Helpers

    private func makeManager(
        variant: String,
        onToggle: @escaping @MainActor () -> Void
    ) -> HotkeyManager {
        HotkeyManager(variant: variant, onToggle: onToggle)
    }

    // MARK: - HotkeyVariant enum

    func test_variant_rightOption_rawValue() {
        XCTAssertEqual(HotkeyVariant.rightOption.rawValue, "right_option")
    }

    func test_variant_leftOption_rawValue() {
        XCTAssertEqual(HotkeyVariant.leftOption.rawValue, "left_option")
    }

    func test_variant_anyOption_rawValue() {
        XCTAssertEqual(HotkeyVariant.anyOption.rawValue, "any_option")
    }

    func test_unknownVariant_fallsBackToRightOption() {
        // Неизвестный rawValue → HotkeyVariant(rawValue:) == nil → init использует .rightOption
        // Проверяем через поведение: rightOption keyCode (61) должен вызвать callback
        var called = false
        let manager = makeManager(variant: "unknown_variant") { called = true }
        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: true)
        XCTAssertTrue(called, "Неизвестный вариант должен вести себя как rightOption (fallback)")
    }

    // MARK: - Event filtering: rightOption variant

    func test_rightOption_rightOptionKeyCode_triggersToggle() {
        var toggleCount = 0
        let manager = makeManager(variant: "right_option") { toggleCount += 1 }

        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: true)
        XCTAssertEqual(toggleCount, 1, "Right Option key down должен вызвать onToggle")
    }

    func test_rightOption_leftOptionKeyCode_doesNotTrigger() {
        var called = false
        let manager = makeManager(variant: "right_option") { called = true }

        manager.injectEventLogic(keyCode: Keycode.leftOption.rawValue, isOptionDown: true)
        XCTAssertFalse(called, "Левая Option не должна срабатывать для варианта rightOption")
    }

    func test_leftOption_leftOptionKeyCode_triggersToggle() {
        var called = false
        let manager = makeManager(variant: "left_option") { called = true }

        manager.injectEventLogic(keyCode: Keycode.leftOption.rawValue, isOptionDown: true)
        XCTAssertTrue(called, "Left Option key down должен вызвать onToggle в leftOption режиме")
    }

    func test_leftOption_rightOptionKeyCode_doesNotTrigger() {
        var called = false
        let manager = makeManager(variant: "left_option") { called = true }

        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: true)
        XCTAssertFalse(called, "Правая Option не должна срабатывать для варианта leftOption")
    }

    func test_anyOption_rightOptionKeyCode_triggers() {
        var called = false
        let manager = makeManager(variant: "any_option") { called = true }

        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: true)
        XCTAssertTrue(called, "anyOption должен реагировать на правую Option")
    }

    func test_anyOption_leftOptionKeyCode_triggers() {
        var called = false
        let manager = makeManager(variant: "any_option") { called = true }

        manager.injectEventLogic(keyCode: Keycode.leftOption.rawValue, isOptionDown: true)
        XCTAssertTrue(called, "anyOption должен реагировать на левую Option")
    }

    // MARK: - isPressed debounce (toggle fire-once-per-press)

    func test_keyDown_keyDown_togglesOnlyOnce() {
        // Второй keyDown без промежуточного keyUp — должен быть проигнорирован (isPressed guard)
        var toggleCount = 0
        let manager = makeManager(variant: "right_option") { toggleCount += 1 }

        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: true)  // isPressed = true, toggle
        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: true)  // уже pressed → ignored

        XCTAssertEqual(toggleCount, 1, "Повторный keyDown без keyUp не должен вызывать toggle снова")
    }

    func test_keyDown_keyUp_keyDown_togglesTwice() {
        // Полный цикл: down → up → down = два toggle
        var toggleCount = 0
        let manager = makeManager(variant: "right_option") { toggleCount += 1 }

        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: true)   // toggle #1
        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: false)  // keyUp, isPressed = false
        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: true)   // toggle #2

        XCTAssertEqual(toggleCount, 2, "Два полных нажатия должны вызывать toggle дважды")
    }

    func test_keyUp_withoutPriorDown_doesNotToggle() {
        // keyUp без предшествующего keyDown — ничего не должно произойти
        var called = false
        let manager = makeManager(variant: "right_option") { called = true }

        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: false)

        XCTAssertFalse(called, "keyUp без keyDown не должен вызывать toggle")
    }

    // MARK: - Lifecycle: start / stop

    func test_stop_afterStart_doesNotCrash() {
        // start() устанавливает NSEvent monitors; stop() их удаляет — не должно падать
        let manager = makeManager(variant: "right_option") {}
        manager.start()
        manager.stop()
    }

    func test_stopCalledTwice_doesNotCrash() {
        let manager = makeManager(variant: "right_option") {}
        manager.stop()
        manager.stop() // double stop safe
    }

    func test_startCalledTwice_doesNotCrash() {
        // start() вызывает stop() внутри — повторный вызов безопасен
        let manager = makeManager(variant: "right_option") {}
        manager.start()
        manager.start()
        manager.stop()
    }
}
