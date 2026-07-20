/*
 ConversationHotkeyLifecycleTests — детерминированные тесты жизненного цикла
 двойного Right Option для «Разговора с AI».

 Проверяют без запуска живого приложения:
 - сохранённое выключенное состояние действительно запрещает callback на старте;
 - отсутствующая настройка сохраняет исторический дефолт «включено»;
 - idle-сессия запускается, активная — останавливается;
 - wake-word по-прежнему умеет только запускать разговор;
 - пересоздание HotkeyManager не теряет режим и общие callback'и.
*/

import Foundation
import XCTest
@testable import KrabEarAgent

final class ConversationHotkeyLifecycleTests: XCTestCase {
    private var defaults: UserDefaults!

    override func setUp() {
        super.setUp()
        defaults = UserDefaults(suiteName: "ConversationHotkeyLifecycleTests")
        defaults.removePersistentDomain(forName: "ConversationHotkeyLifecycleTests")
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: "ConversationHotkeyLifecycleTests")
        defaults = nil
        super.tearDown()
    }

    func test_missingPreference_keepsHotkeyEnabledByDefault() {
        XCTAssertTrue(ConversationHotkeyPolicy.isEnabled(in: defaults))
    }

    func test_savedFalse_disablesHotkeyAtStartup() {
        defaults.set(false, forKey: ConversationHotkeyPolicy.defaultsKey)

        XCTAssertFalse(
            ConversationHotkeyPolicy.isEnabled(in: defaults),
            "Явно сохранённый false нельзя подменять дефолтом true после перезапуска"
        )
    }

    func test_savedTrue_enablesHotkeyAtStartup() {
        defaults.set(true, forKey: ConversationHotkeyPolicy.defaultsKey)

        XCTAssertTrue(ConversationHotkeyPolicy.isEnabled(in: defaults))
    }

    func test_idleSession_routesDoubleTapToStartOnly() {
        var starts = 0
        var stops = 0

        ConversationHotkeyPolicy.performToggle(
            isSessionActive: false,
            onStart: { starts += 1 },
            onStop: { stops += 1 }
        )

        XCTAssertEqual(starts, 1)
        XCTAssertEqual(stops, 0)
    }

    func test_activeSession_routesDoubleTapToStopOnly() {
        var starts = 0
        var stops = 0

        ConversationHotkeyPolicy.performToggle(
            isSessionActive: true,
            onStart: { starts += 1 },
            onStop: { stops += 1 }
        )

        XCTAssertEqual(starts, 0)
        XCTAssertEqual(stops, 1)
    }
}

final class ConversationHotkeyWiringSourceContractTests: XCTestCase {
    func test_factory_usesSavedPreferenceAndToggleEntryPoint() throws {
        let body = try Self.functionBody(named: "makeHotkeyManager", in: Self.mainSwiftURL)

        XCTAssertTrue(body.contains("ConversationHotkeyPolicy.isEnabled"))
        XCTAssertTrue(body.contains("triggerConversationToggle"))
        XCTAssertFalse(
            body.contains("triggerConversationStart()"),
            "Фабрика не должна превращать double-tap в безусловный старт"
        )
    }

    func test_manualDoubleTapEntryPoint_hasBothLifecycleBranches() throws {
        let body = try Self.functionBody(
            named: "triggerConversationToggle",
            in: Self.voiceTabSwiftURL
        )

        XCTAssertTrue(body.contains("ConversationHotkeyPolicy.performToggle"))
        XCTAssertTrue(body.contains("triggerConversationStart()"))
        XCTAssertTrue(body.contains("stopConversation()"))
    }

    func test_wakeWordRemainsStartOnly() throws {
        let body = try Self.functionBody(
            named: "triggerConversationFromWakeWord",
            in: Self.voiceTabSwiftURL
        )

        XCTAssertTrue(body.contains("conversationVC?.startConversation()"))
        XCTAssertFalse(body.contains("triggerConversationToggle"))
        XCTAssertFalse(body.contains("stopConversation()"))
    }

    func test_hotkeyReinstallUsesFactoryAndTracksModeChanges() throws {
        let body = try Self.functionBody(
            named: "applySettingsSideEffects",
            in: Self.statusMenuSwiftURL
        )

        XCTAssertTrue(body.contains("previous.hotkeyMode != current.hotkeyMode"))
        XCTAssertTrue(body.contains("makeHotkeyManager(settings: current)"))
        XCTAssertFalse(
            body.contains("HotkeyManager(variant:"),
            "Параллельный bare-init снова потеряет callback'и и выбранный hold/toggle-режим"
        )
    }

    private static func functionBody(named name: String, in url: URL) throws -> String {
        let source = try String(contentsOf: url, encoding: .utf8)
        guard let signature = source.range(of: "func \(name)(") else {
            XCTFail("Функция \(name) не найдена в \(url.lastPathComponent)")
            return ""
        }
        guard let openingBrace = source.range(
            of: "{",
            range: signature.upperBound..<source.endIndex
        ) else {
            XCTFail("Открывающая скобка функции \(name) не найдена")
            return ""
        }

        var depth = 0
        var index = openingBrace.lowerBound
        while index < source.endIndex {
            let character = source[index]
            if character == "{" {
                depth += 1
            } else if character == "}" {
                depth -= 1
                if depth == 0 {
                    return String(source[openingBrace.lowerBound...index])
                }
            }
            index = source.index(after: index)
        }

        XCTFail("Закрывающая скобка функции \(name) не найдена")
        return ""
    }

    private static var sourcesDirectory: URL {
        var url = URL(fileURLWithPath: #filePath)
        url.deleteLastPathComponent()
        url.deleteLastPathComponent()
        url.deleteLastPathComponent()
        return url.appendingPathComponent("Sources/KrabEarAgent")
    }

    private static var mainSwiftURL: URL {
        sourcesDirectory.appendingPathComponent("main.swift")
    }

    private static var voiceTabSwiftURL: URL {
        sourcesDirectory.appendingPathComponent("HistoryPanelController+VoiceTab.swift")
    }

    private static var statusMenuSwiftURL: URL {
        sourcesDirectory.appendingPathComponent("main+StatusMenu.swift")
    }
}
