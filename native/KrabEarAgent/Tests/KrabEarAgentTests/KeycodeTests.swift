/*
 KeycodeTests — тесты виртуальных keycodes macOS (CGKeyCode / HIToolbox).

 Таблица соответствия (HIToolbox/Events.h, Carbon framework):
   kVK_ANSI_V       = 0x09  (9)  — клавиша V
   kVK_Escape       = 0x35 (53)  — Escape
   kVK_Option       = 0x3A (58)  — Left Option/Alt
   kVK_RightOption  = 0x3D (61)  — Right Option/Alt
   kVK_Command      = 0x37 (55)  — Left Command/Meta
   kVK_RightCommand = 0x36 (54)  — Right Command/Meta

 Источник: https://developer.apple.com/documentation/carbon/1462078-key_codes
           (файл HIToolbox/Events.h в macOS SDK)
*/

import XCTest
@testable import KrabEarAgent

final class KeycodeTests: XCTestCase {

    // MARK: - Alphabetic keys

    func test_vKey_rawValueIs9() {
        // kVK_ANSI_V = 0x09
        XCTAssertEqual(Keycode.v.rawValue, 9,
            "Keycode.v должен быть 9 (kVK_ANSI_V = 0x09)")
    }

    // MARK: - Special keys

    func test_escapeKey_rawValueIs53() {
        // kVK_Escape = 0x35 = 53
        XCTAssertEqual(Keycode.escape.rawValue, 53,
            "Keycode.escape должен быть 53 (kVK_Escape = 0x35)")
    }

    // MARK: - Modifier keys

    func test_leftOption_rawValueIs58() {
        // kVK_Option = 0x3A = 58
        XCTAssertEqual(Keycode.leftOption.rawValue, 58,
            "Keycode.leftOption должен быть 58 (kVK_Option = 0x3A)")
    }

    func test_rightOption_rawValueIs61() {
        // kVK_RightOption = 0x3D = 61  — основная горячая клавиша Krab Ear
        XCTAssertEqual(Keycode.rightOption.rawValue, 61,
            "Keycode.rightOption должен быть 61 (kVK_RightOption = 0x3D)")
    }

    func test_leftCommand_rawValueIs55() {
        // kVK_Command = 0x37 = 55
        XCTAssertEqual(Keycode.leftCommand.rawValue, 55,
            "Keycode.leftCommand должен быть 55 (kVK_Command = 0x37)")
    }

    func test_rightCommand_rawValueIs54() {
        // kVK_RightCommand = 0x36 = 54
        XCTAssertEqual(Keycode.rightCommand.rawValue, 54,
            "Keycode.rightCommand должен быть 54 (kVK_RightCommand = 0x36)")
    }

    // MARK: - Raw value uniqueness

    func test_allRawValues_areUnique() {
        // Каждая case должна иметь уникальный raw value (нет коллизий в HIToolbox таблице)
        let values: [UInt16] = [
            Keycode.v.rawValue,
            Keycode.escape.rawValue,
            Keycode.leftOption.rawValue,
            Keycode.rightOption.rawValue,
            Keycode.leftCommand.rawValue,
            Keycode.rightCommand.rawValue,
        ]
        let unique = Set(values)
        XCTAssertEqual(unique.count, values.count,
            "Каждый Keycode должен иметь уникальный raw value")
    }

    // MARK: - UInt16 conformance

    func test_keycodeRawType_isUInt16() {
        // Enum raw type должен соответствовать CGKeyCode (UInt16)
        let code: UInt16 = Keycode.rightOption.rawValue
        XCTAssertEqual(code, 61)
    }
}
