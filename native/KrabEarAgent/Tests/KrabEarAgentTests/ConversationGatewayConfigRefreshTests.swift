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

    func test_settingsHandlers_refreshConversationConfig() throws {
        let src = try String(contentsOf: Self.sourceURL("HistoryPanelController+Settings.swift"), encoding: .utf8)
        for handler in ["func onVoiceGatewayURLChanged()", "func onVoiceGatewayAPIKeyChanged()"] {
            let start = try XCTUnwrap(src.range(of: handler), handler)
            let tail = String(src[start.upperBound...])
            let end = tail.range(of: "@objc func")?.lowerBound ?? tail.endIndex
            let body = String(tail[..<end])
            XCTAssertTrue(body.contains("applySettingsPatch("), "\(handler): настройка должна сохраняться")
            XCTAssertTrue(body.contains("refreshConversationGatewayConfig()"),
                          "\(handler): после сохранения конфиг разговора обязан обновиться")
            XCTAssertLessThan(try XCTUnwrap(body.range(of: "applySettingsPatch(")).lowerBound,
                              try XCTUnwrap(body.range(of: "refreshConversationGatewayConfig()")).lowerBound,
                              "\(handler): сначала сохранить, потом обновлять конфиг")
        }
    }

    func test_refresh_fetchesRealCredentialOffMain_andUpdatesConfig() throws {
        let src = try String(contentsOf: Self.sourceURL("HistoryPanelController+VoiceTab.swift"), encoding: .utf8)
        let start = try XCTUnwrap(src.range(of: "func refreshConversationGatewayConfig()"))
        let tail = String(src[start.upperBound...])
        let end = tail.range(of: "// MARK:")?.lowerBound ?? tail.endIndex
        let body = String(tail[..<end])
        XCTAssertTrue(body.contains("get_voice_gateway_credential"))
        XCTAssertTrue(body.contains("callAsync"), "ключ тянем off-main (AGENT-3), не синхронным IPC")
        XCTAssertFalse(body.contains("ipcClient.call("), "sync IPC на main запрещён")
        XCTAssertTrue(body.contains("config.apiKey = realKey"))
        XCTAssertTrue(body.contains("updatingGateway("))
    }

    private static func sourceURL(_ name: String) -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
    }
}
