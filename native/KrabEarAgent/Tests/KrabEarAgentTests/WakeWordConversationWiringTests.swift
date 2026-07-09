/*
 WakeWordConversationWiringTests — Волна 3c, секция 6 спеки.

 Пин существующей (корректной) проводки паузы wake-поллера вокруг
 conversation-lifecycle. Класс «test-validates-the-hole» дважды кусал проект
 (setupErrorBus, setupHealthMonitor — оба были определены, но не вызваны).
 Эти тесты грепают РЕАЛЬНЫЙ source, чтобы рефакторинг, молча выронивший вызов
 или одну из двух подписок, упал в CI.
*/

import XCTest
@testable import KrabEarAgent

final class WakeWordConversationWiringTests: XCTestCase {

    // MARK: 1. setupWakeWordConversationObservers реально ВЫЗЫВАЕТСЯ (не только определён)

    func test_setupWakeWordConversationObservers_is_actually_called() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        let callSites = src.components(separatedBy: "setupWakeWordConversationObservers()").count - 1
        // ≥2 вхождения: определение (func ...) даёт 1, вызов — ещё ≥1.
        XCTAssertGreaterThanOrEqual(callSites, 2,
            "setupWakeWordConversationObservers() должен быть и определён, и вызван в main.swift")
    }

    // MARK: 2. Обе подписки живы и дергают правильные методы

    func test_conversationStarted_pausesPoller() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains(".krabConversationStarted"),
                      "подписка на .krabConversationStarted обязана существовать")
        XCTAssertTrue(src.contains("pause(.conversation)"),
                      "обработчик started обязан вызывать pause(.conversation)")
    }

    func test_conversationStopped_resumesPoller() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains(".krabConversationStopped"),
                      "подписка на .krabConversationStopped обязана существовать")
        XCTAssertTrue(src.contains("resume(.conversation)"),
                      "обработчик stopped обязан вызывать resume(.conversation)")
    }

    // MARK: 3. Обе нотификации реально постятся из единой воронки start/stop

    func test_conversationVC_posts_bothNotifications() throws {
        let src = try String(contentsOf: Self.vcSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains("post(name: .krabConversationStarted"),
                      "startConversation обязан постить .krabConversationStarted")
        XCTAssertTrue(src.contains("post(name: .krabConversationStopped"),
                      "stopConversation обязан постить .krabConversationStopped")
    }

    // MARK: - Source URLs (#filePath walk-up, паттерн MainErrorsWiringTests)

    private static var sourcesDir: URL {
        var url = URL(fileURLWithPath: #filePath)  // .../Tests/KrabEarAgentTests/<файл>
        url.deleteLastPathComponent()              // Tests/KrabEarAgentTests
        url.deleteLastPathComponent()              // Tests
        url.deleteLastPathComponent()              // native/KrabEarAgent
        return url.appendingPathComponent("Sources/KrabEarAgent")
    }

    private static var mainSwiftURL: URL { sourcesDir.appendingPathComponent("main.swift") }
    private static var vcSwiftURL: URL { sourcesDir.appendingPathComponent("ConversationViewController.swift") }
}
