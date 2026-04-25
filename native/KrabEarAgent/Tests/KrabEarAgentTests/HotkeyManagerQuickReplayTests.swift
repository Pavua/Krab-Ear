/*
 HotkeyManagerQuickReplayTests.swift
 Тесты для быстрого повтора вставки в HotkeyManager (Cmd+Option+V).
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class HotkeyManagerQuickReplayTests: XCTestCase {

    var manager: HotkeyManager!

    override func setUp() async throws {
        try await super.setUp()
        manager = HotkeyManager(variant: "right_option", onToggle: {})
    }

    override func tearDown() async throws {
        manager.stop()
        manager = nil
        try await super.tearDown()
    }

    // MARK: - isQuickReplayHotkey

    func testCmdOptionVIsQuickReplayHotkey() throws {
        guard let event = NSEvent.keyEvent(
            with: .keyDown,
            location: .zero,
            modifierFlags: [.command, .option],
            timestamp: 0,
            windowNumber: 0,
            context: nil,
            characters: "v",
            charactersIgnoringModifiers: "v",
            isARepeat: false,
            keyCode: UInt16(Keycode.v.rawValue)
        ) else {
            XCTFail("Не удалось создать NSEvent")
            return
        }
        XCTAssertTrue(manager.isQuickReplayHotkey(event))
    }

    func testCmdShiftVIsNotQuickReplayHotkey() throws {
        guard let event = NSEvent.keyEvent(
            with: .keyDown,
            location: .zero,
            modifierFlags: [.command, .shift],
            timestamp: 0,
            windowNumber: 0,
            context: nil,
            characters: "V",
            charactersIgnoringModifiers: "v",
            isARepeat: false,
            keyCode: UInt16(Keycode.v.rawValue)
        ) else {
            XCTFail("Не удалось создать NSEvent")
            return
        }
        XCTAssertFalse(manager.isQuickReplayHotkey(event))
    }

    func testCmdVAloneIsNotQuickReplayHotkey() throws {
        guard let event = NSEvent.keyEvent(
            with: .keyDown,
            location: .zero,
            modifierFlags: [.command],
            timestamp: 0,
            windowNumber: 0,
            context: nil,
            characters: "v",
            charactersIgnoringModifiers: "v",
            isARepeat: false,
            keyCode: UInt16(Keycode.v.rawValue)
        ) else {
            XCTFail("Не удалось создать NSEvent")
            return
        }
        XCTAssertFalse(manager.isQuickReplayHotkey(event))
    }

    // MARK: - injectKeyDownLogic

    func testInjectCmdOptionVFiresOnQuickReplay() {
        var fired = false
        manager.onQuickReplay = { fired = true }
        manager.injectKeyDownLogic(
            keyCode: UInt16(Keycode.v.rawValue),
            flags: [.command, .option]
        )
        XCTAssertTrue(fired, "onQuickReplay должен сработать для Cmd+Option+V")
    }

    func testInjectCmdShiftVDoesNotFireOnQuickReplay() {
        var fired = false
        manager.onQuickReplay = { fired = true }
        manager.injectKeyDownLogic(
            keyCode: UInt16(Keycode.v.rawValue),
            flags: [.command, .shift]
        )
        XCTAssertFalse(fired, "onQuickReplay не должен срабатывать для Cmd+Shift+V")
    }

    // MARK: - Независимость toggle и replay

    func testToggleAndReplayCallbacksDontInterfere() {
        var toggleFired = false
        var replayFired = false
        let toggleManager = HotkeyManager(variant: "right_option", onToggle: {
            toggleFired = true
        })
        toggleManager.onQuickReplay = { replayFired = true }

        // Нажать Right Option (toggle)
        toggleManager.injectEventLogic(
            keyCode: UInt16(Keycode.rightOption.rawValue),
            isOptionDown: true
        )
        XCTAssertTrue(toggleFired)
        XCTAssertFalse(replayFired)

        // Нажать Cmd+Option+V (replay)
        toggleFired = false
        toggleManager.injectKeyDownLogic(
            keyCode: UInt16(Keycode.v.rawValue),
            flags: [.command, .option]
        )
        XCTAssertFalse(toggleFired)
        XCTAssertTrue(replayFired)

        toggleManager.stop()
    }
}
