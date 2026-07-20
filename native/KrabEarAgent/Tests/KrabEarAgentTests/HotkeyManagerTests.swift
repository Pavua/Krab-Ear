/*
 HotkeyManagerTests — тесты логики фильтрации событий HotkeyManager.

 Подход: NSEvent нельзя создать программно в unit-тестах → тестируем логику
 через тест-хук injectEventLogic(keyCode:isOptionDown:), который повторяет
 ту же фильтрацию что и handle(event:), без реального NSEvent/CGEventTap.

 Проверяем: вариантный enum, фильтрацию keyCodes, toggle-callback,
 debounce isPressed, start/stop lifecycle.
*/

import AppKit
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

    /// Агрегатный флаг `.option` остаётся включённым, пока удерживается левая
    /// Option. Отпускание правой клавиши обязано смотреть на её аппаратную маску, иначе
    /// менеджер навсегда оставляет `isPressed=true`.
    func test_rightOptionReleaseWhileLeftHeld_rearmsNextRightPress() {
        var toggleCount = 0
        let manager = makeManager(variant: "right_option") { toggleCount += 1 }
        let option = NSEvent.ModifierFlags.option.rawValue

        manager.injectFlagsChangedLogic(
            keyCode: Keycode.rightOption.rawValue,
            modifierFlagsRawValue: option | OptionKeyPhysicalState.rightOptionMask
        )
        manager.injectFlagsChangedLogic(
            keyCode: Keycode.rightOption.rawValue,
            modifierFlagsRawValue: option | OptionKeyPhysicalState.leftOptionMask
        )
        manager.injectFlagsChangedLogic(
            keyCode: Keycode.rightOption.rawValue,
            modifierFlagsRawValue: option
                | OptionKeyPhysicalState.leftOptionMask
                | OptionKeyPhysicalState.rightOptionMask
        )

        XCTAssertEqual(toggleCount, 2, "Следующий физический Right Option должен снова сработать")
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

    // MARK: - HotkeyMode enum

    func test_hotkeyMode_toggle_isDefault() {
        let manager = HotkeyManager(variant: "right_option", onToggle: {})
        XCTAssertEqual(manager.mode, .toggle)
    }

    func test_hotkeyMode_hold_parsedCorrectly() {
        let manager = HotkeyManager(variant: "right_option", onToggle: {}, mode: "hold")
        XCTAssertEqual(manager.mode, .hold)
    }

    func test_hotkeyMode_unknown_fallsBackToToggle() {
        let manager = HotkeyManager(variant: "right_option", onToggle: {}, mode: "unknown_mode")
        XCTAssertEqual(manager.mode, .toggle)
    }

    // MARK: - Toggle mode: не вызывает onHoldStart/onHoldStop

    func test_toggleMode_doesNotFireHoldCallbacks() {
        var startCount = 0
        var stopCount = 0
        var toggleCount = 0
        let manager = HotkeyManager(variant: "right_option", onToggle: { toggleCount += 1 }, mode: "toggle")
        manager.onHoldStart = { startCount += 1 }
        manager.onHoldStop = { stopCount += 1 }

        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: true)
        manager.injectEventLogic(keyCode: Keycode.rightOption.rawValue, isOptionDown: false)

        XCTAssertEqual(toggleCount, 1, "Toggle mode должен вызывать onToggle при DOWN")
        XCTAssertEqual(startCount, 0, "Toggle mode не должен вызывать onHoldStart")
        XCTAssertEqual(stopCount, 0, "Toggle mode не должен вызывать onHoldStop")
    }

    // MARK: - Совместимость вариантов с conversation double-tap

    func test_rightOption_supportsConversationDoubleTap() {
        XCTAssertTrue(HotkeyManager.supportsConversationDoubleTap(variant: "right_option"))
        XCTAssertTrue(HotkeyManager.supportsConversationDoubleTap(variant: "right_option_toggle"))
    }

    func test_leftAndAnyOption_doNotSupportConversationDoubleTap() {
        XCTAssertFalse(HotkeyManager.supportsConversationDoubleTap(variant: "left_option"))
        XCTAssertFalse(HotkeyManager.supportsConversationDoubleTap(variant: "any_option"))
    }

    func test_leftOptionConversationCallback_doesNotDelaySingleTap() {
        var toggleCount = 0
        let manager = HotkeyManager(
            variant: "left_option",
            onToggle: { toggleCount += 1 },
            mode: "toggle"
        )
        // Защита в самом менеджере обязательна: даже ошибочно назначенный
        // callback не должен задерживать вариант без double-tap detector.
        manager.onConversationDoubleTap = {}

        manager.injectEventLogic(
            keyCode: Keycode.leftOption.rawValue,
            isOptionDown: true
        )

        XCTAssertEqual(toggleCount, 1)
        XCTAssertFalse(manager.hasPendingSingleTapForTests)
    }

    func test_anyOptionConversationCallback_doesNotDelaySingleTap() {
        var toggleCount = 0
        let manager = HotkeyManager(
            variant: "any_option",
            onToggle: { toggleCount += 1 },
            mode: "toggle"
        )
        manager.onConversationDoubleTap = {}

        manager.injectEventLogic(
            keyCode: Keycode.rightOption.rawValue,
            isOptionDown: true
        )

        XCTAssertEqual(toggleCount, 1)
        XCTAssertFalse(manager.hasPendingSingleTapForTests)
    }

    func test_startAfterStop_doesNotDebounceFirstTapAsConsumedDoubleTap() {
        var toggleCount = 0
        let manager = HotkeyManager(
            variant: "right_option",
            onToggle: { toggleCount += 1 },
            mode: "toggle"
        )
        manager.onConversationDoubleTap = {}

        // start() внутри вызывает stop(); раньше stop() записывал recentDoubleTapAt,
        // поэтому первое реальное нажатие после startup/reinstall терялось на 500 мс.
        manager.start()
        manager.injectEventLogic(
            keyCode: Keycode.rightOption.rawValue,
            isOptionDown: true
        )

        XCTAssertTrue(manager.hasPendingSingleTapForTests)
        manager.firePendingSingleTapForTests()
        XCTAssertEqual(toggleCount, 1)
        manager.stop()
    }

    // MARK: - Hold mode: DOWN → onHoldStart, UP (≥200ms) → onHoldStop

    func test_holdMode_downDefersStartUntilThreshold() {
        var startCount = 0
        let manager = HotkeyManager(variant: "right_option", onToggle: {}, mode: "hold", holdMinDurationMs: 200)
        manager.onHoldStart = { startCount += 1 }

        manager.simulateHoldDown(keyCode: Keycode.rightOption.rawValue)
        XCTAssertEqual(startCount, 0, "DOWN не должен начинать запись до порога удержания")
        XCTAssertTrue(manager.hasPendingHoldStartForTests)

        manager.firePendingHoldStartForTests()
        XCTAssertEqual(startCount, 1, "Hold DOWN должен вызывать onHoldStart")
    }

    func test_holdMode_upAfterSufficientDuration_firesHoldStop() {
        var stopCount = 0
        let manager = HotkeyManager(variant: "right_option", onToggle: {}, mode: "hold", holdMinDurationMs: 200)
        manager.onHoldStop = { stopCount += 1 }

        manager.simulateHoldDown(keyCode: Keycode.rightOption.rawValue)
        manager.firePendingHoldStartForTests()
        manager.simulateHoldUp(keyCode: Keycode.rightOption.rawValue)

        XCTAssertEqual(stopCount, 1, "Удержание ≥200ms должно вызывать onHoldStop")
    }

    func test_holdMode_upAfterTooShortDuration_ignoresRelease() {
        var stopCount = 0
        let manager = HotkeyManager(variant: "right_option", onToggle: {}, mode: "hold", holdMinDurationMs: 200)
        manager.onHoldStop = { stopCount += 1 }

        var startCount = 0
        manager.onHoldStart = { startCount += 1 }
        manager.simulateHoldDown(keyCode: Keycode.rightOption.rawValue)
        manager.simulateHoldUp(keyCode: Keycode.rightOption.rawValue)

        XCTAssertEqual(startCount, 0, "Короткий тап не должен даже начинать запись")
        XCTAssertEqual(stopCount, 0, "Удержание <200ms должно игнорироваться (не вызывать onHoldStop)")
        XCTAssertFalse(manager.hasPendingHoldStartForTests)
    }

    func test_holdMode_doesNotFireOnToggle() {
        var toggleCount = 0
        let manager = HotkeyManager(variant: "right_option", onToggle: { toggleCount += 1 }, mode: "hold")

        manager.simulateHoldDown(keyCode: Keycode.rightOption.rawValue)
        manager.firePendingHoldStartForTests()
        manager.simulateHoldUp(keyCode: Keycode.rightOption.rawValue)

        XCTAssertEqual(toggleCount, 0, "Hold mode не должен вызывать onToggle")
    }

    func test_holdMode_secondDownIgnoredWhileHeld() {
        var startCount = 0
        let manager = HotkeyManager(variant: "right_option", onToggle: {}, mode: "hold")
        manager.onHoldStart = { startCount += 1 }

        manager.simulateHoldDown(keyCode: Keycode.rightOption.rawValue)
        manager.simulateHoldDown(keyCode: Keycode.rightOption.rawValue)  // второй DOWN — должен игнорироваться
        manager.firePendingHoldStartForTests()

        XCTAssertEqual(startCount, 1, "Повторный DOWN во время удержания должен игнорироваться")
    }

    func test_holdMode_doubleTapCancelsDictationBeforeConversationStarts() {
        var startCount = 0
        var stopCount = 0
        var conversationCount = 0
        let manager = HotkeyManager(
            variant: "right_option",
            onToggle: {},
            mode: "hold",
            holdMinDurationMs: 200
        )
        manager.onHoldStart = { startCount += 1 }
        manager.onHoldStop = { stopCount += 1 }
        manager.onConversationDoubleTap = { conversationCount += 1 }

        // Первый короткий тап только вооружает hold, но не начинает диктовку.
        manager.injectEventLogic(
            keyCode: Keycode.rightOption.rawValue,
            isOptionDown: true
        )
        manager.injectEventLogic(
            keyCode: Keycode.rightOption.rawValue,
            isOptionDown: false
        )

        // Второй DOWN снова создаёт pending hold; double-tap обязан поглотить его
        // до запуска conversation, независимо от порядка NSEvent-мониторов.
        manager.injectEventLogic(
            keyCode: Keycode.rightOption.rawValue,
            isOptionDown: true
        )
        XCTAssertTrue(manager.hasPendingHoldStartForTests)
        manager.injectConversationDoubleTapLogic()
        manager.injectEventLogic(
            keyCode: Keycode.rightOption.rawValue,
            isOptionDown: false
        )

        XCTAssertEqual(startCount, 0, "Double-tap не должен параллельно запускать диктовку")
        XCTAssertEqual(stopCount, 0, "Несуществующую hold-запись не нужно останавливать")
        XCTAssertEqual(conversationCount, 1)
        XCTAssertFalse(manager.hasPendingHoldStartForTests)
    }

    func test_holdMode_detectorFirstOrder_doesNotRearmDictationOnSecondDown() {
        var startCount = 0
        var conversationCount = 0
        let manager = HotkeyManager(
            variant: "right_option",
            onToggle: {},
            mode: "hold",
            holdMinDurationMs: 200
        )
        manager.onHoldStart = { startCount += 1 }
        manager.onConversationDoubleTap = { conversationCount += 1 }

        manager.injectEventLogic(
            keyCode: Keycode.rightOption.rawValue,
            isOptionDown: true
        )
        manager.injectEventLogic(
            keyCode: Keycode.rightOption.rawValue,
            isOptionDown: false
        )

        // Реальные NSEvent-мониторы независимы: detector может первым увидеть
        // второй DOWN. Последующий вызов manager не должен заново вооружить hold.
        manager.injectConversationDoubleTapLogic()
        manager.injectEventLogic(
            keyCode: Keycode.rightOption.rawValue,
            isOptionDown: true
        )
        manager.injectEventLogic(
            keyCode: Keycode.rightOption.rawValue,
            isOptionDown: false
        )

        XCTAssertEqual(conversationCount, 1)
        XCTAssertEqual(startCount, 0)
        XCTAssertFalse(manager.hasPendingHoldStartForTests)
    }
}
