/*
 ConversationGatewayConfigRefreshTests — смена ключа/адреса Voice Gateway в настройках должна доходить
 до конфига «Разговора с AI» сразу, а не после перезапуска агента.

 Баг 24.11.2026: `setupConversationTab` кладёт ключ и адрес в `ConversationConfig` ОДИН раз при запуске;
 `onVoiceGatewayAPIKeyChanged`/`onVoiceGatewayURLChanged` только сохраняли настройку в бэкенде. Владелец ввёл
 верный ключ, а разговор продолжал слать прежний (пустой) и получал 401 «шлюз недоступен».
*/

import XCTest
@testable import KrabEarAgent

final class ConversationGatewayConfigRefreshTests: XCTestCase {

    func test_updatingGateway_replacesBothURLs_andKeepsEverythingElse() {
        let original = ConversationConfig(
            wsURLString: "ws://old/v1/sessions/vs_x/conversation", apiKey: "k-old",
            languageHint: "ru", engine: "moshi", brain: "qwen3-4b", brainMode: "fast",
            httpBaseURLString: "http://old")
        let updated = original.updatingGateway(
            wsURLString: "ws://new/v1/sessions/vs_y/conversation", httpBaseURLString: "http://new")
        XCTAssertEqual(updated.wsURLString, "ws://new/v1/sessions/vs_y/conversation")
        XCTAssertEqual(updated.httpBaseURLString, "http://new")
        XCTAssertEqual(updated.apiKey, "k-old", "ключ обновляется отдельно (реальное значение — через IPC)")
        XCTAssertEqual(updated.languageHint, "ru")
        XCTAssertEqual(updated.engine, "moshi")
        XCTAssertEqual(updated.brain, "qwen3-4b")
        XCTAssertEqual(updated.brainMode, "fast")
    }

    func test_settingsHandlers_passTypedValuesDirectly() throws {
        let src = try String(contentsOf: Self.sourceURL("HistoryPanelController+Settings.swift"), encoding: .utf8)
        let cases = [("func onVoiceGatewayURLChanged()", "refreshConversationGatewayConfig(gatewayURL: raw)"),
                     ("func onVoiceGatewayAPIKeyChanged()", "refreshConversationGatewayConfig(apiKey: raw)")]
        for (handler, call) in cases {
            let start = try XCTUnwrap(src.range(of: handler), handler)
            let tail = String(src[start.upperBound...])
            let end = tail.range(of: "@objc func")?.lowerBound ?? tail.endIndex
            let body = String(tail[..<end])
            XCTAssertTrue(body.contains("applySettingsPatch("), "\(handler): настройка должна сохраняться")
            XCTAssertTrue(body.contains(call), "\(handler): конфиг разговора обязан получить введённое значение")
            XCTAssertLessThan(try XCTUnwrap(body.range(of: "applySettingsPatch(")).lowerBound,
                              try XCTUnwrap(body.range(of: call)).lowerBound)
        }
    }

    func test_refresh_doesNotReadSettingsOrDoIPC() throws {
        // Гейт Opus: чтение settingsProvider()/IPC сразу после сохранения может вернуть прежнее значение.
        let src = try String(contentsOf: Self.sourceURL("HistoryPanelController+VoiceTab.swift"), encoding: .utf8)
        let start = try XCTUnwrap(src.range(of: "func refreshConversationGatewayConfig("))
        let tail = String(src[start.upperBound...])
        let end = tail.range(of: "// MARK:")?.lowerBound ?? tail.endIndex
        let body = String(tail[..<end])
        XCTAssertFalse(body.contains("settingsProvider()"))
        XCTAssertFalse(body.contains("callAsync"))
        XCTAssertFalse(body.contains("ipcClient"))
        XCTAssertTrue(body.contains("conversationGatewayConfig(current: vc.config"))
    }

    // MARK: Поведение чистого правила (не по тексту исходника)

    private func base() -> ConversationConfig {
        ConversationConfig(wsURLString: "ws://old/v1/sessions/vs_x/conversation", apiKey: "old-key",
                           languageHint: "ru", engine: "moshi", brain: "b", brainMode: "fast",
                           httpBaseURLString: "http://old")
    }

    @MainActor
    func test_newKeyAppliedImmediately_urlUntouched() {
        let out = HistoryPanelController.conversationGatewayConfig(current: base(), urlInput: nil, keyInput: "new-key")
        XCTAssertEqual(out.apiKey, "new-key")
        XCTAssertEqual(out.wsURLString, base().wsURLString)
        XCTAssertEqual(out.httpBaseURLString, "http://old")
    }

    @MainActor
    func test_redactedPlaceholderNeverOverwritesKey() {
        let out = HistoryPanelController.conversationGatewayConfig(
            current: base(), urlInput: nil, keyInput: HistoryPanelController.redactedSecretPlaceholder)
        XCTAssertEqual(out.apiKey, "old-key", "Enter в нетронутом поле не должен затирать ключ плейсхолдером")
    }

    @MainActor
    func test_emptyKeyClearsDeliberately() {
        XCTAssertEqual(HistoryPanelController.conversationGatewayConfig(
            current: base(), urlInput: nil, keyInput: "").apiKey, "")
    }

    @MainActor
    func test_nilInputsChangeNothing() {
        let out = HistoryPanelController.conversationGatewayConfig(current: base(), urlInput: nil, keyInput: nil)
        XCTAssertEqual(out.apiKey, "old-key")
        XCTAssertEqual(out.wsURLString, base().wsURLString)
        XCTAssertEqual(out.httpBaseURLString, "http://old")
    }

    @MainActor
    func test_newURLRebuildsBothAddresses_keepsKeyAndOtherFields() {
        let out = HistoryPanelController.conversationGatewayConfig(
            current: base(), urlInput: " https://gw.example/ ", keyInput: nil)
        XCTAssertTrue(out.wsURLString.hasPrefix("wss://gw.example/v1/sessions/vs_"))
        XCTAssertEqual(out.httpBaseURLString, "https://gw.example")
        XCTAssertEqual(out.apiKey, "old-key")
        XCTAssertEqual(out.brainMode, "fast")
        XCTAssertEqual(out.languageHint, "ru")
        XCTAssertEqual(out.engine, "moshi")
    }

    @MainActor
    func test_bothInputsApplyTogether() {
        let out = HistoryPanelController.conversationGatewayConfig(
            current: base(), urlInput: "http://127.0.0.1:9000", keyInput: "k2")
        XCTAssertEqual(out.apiKey, "k2")
        XCTAssertEqual(out.httpBaseURLString, "http://127.0.0.1:9000")
        XCTAssertTrue(out.wsURLString.hasPrefix("ws://127.0.0.1:9000/"))
    }

    @MainActor
    func test_urlBuilders() {
        typealias P = HistoryPanelController
        XCTAssertEqual(P.conversationHTTPBase(fromGatewayURL: " http://127.0.0.1:8090/ "), "http://127.0.0.1:8090")
        XCTAssertTrue(P.conversationWSURL(fromGatewayURL: "http://127.0.0.1:8090/").hasPrefix("ws://127.0.0.1:8090/v1/sessions/vs_"))
        XCTAssertTrue(P.conversationWSURL(fromGatewayURL: "https://gw.example").hasPrefix("wss://gw.example/v1/sessions/vs_"))
        XCTAssertTrue(P.conversationWSURL(fromGatewayURL: "10.0.0.5:8090").hasPrefix("ws://10.0.0.5:8090/v1/sessions/vs_"))
        XCTAssertTrue(P.conversationWSURL(fromGatewayURL: "ws://h:1").hasSuffix("/conversation"))
        XCTAssertNotEqual(P.conversationWSURL(fromGatewayURL: "http://h"), P.conversationWSURL(fromGatewayURL: "http://h"),
                          "session id свой на каждый вызов")
    }

    private static func sourceURL(_ name: String) -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
    }
}
