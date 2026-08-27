/*
 Поле «Модель» облачного рерайтера — одно на всех провайдеров.

 До 2026-08-27 модель можно было задать ТОЛЬКО для self-hosted (custom):
 у OpenAI и Anthropic имена моделей были константами в Python
 (`gpt-4o-mini`, `claude-haiku-4-5-20251001`) — пользователь не мог ни сменить
 их, ни даже увидеть, какая используется. Линейки провайдеров обновляются
 чаще, чем выходят наши релизы.
*/

import XCTest
import Foundation
@testable import KrabEarAgent

@MainActor
final class CloudRewriterModelFieldTests: XCTestCase {

    func test_setting_key_differs_per_provider() {
        // Значения не переиспользуются между провайдерами: имена моделей из
        // разных линеек несовместимы.
        XCTAssertEqual(
            HistoryPanelController.cloudRewriterModelSettingKey(for: "openai"),
            "cloud_rewriter_openai_model")
        XCTAssertEqual(
            HistoryPanelController.cloudRewriterModelSettingKey(for: "anthropic"),
            "cloud_rewriter_anthropic_model")
        XCTAssertEqual(
            HistoryPanelController.cloudRewriterModelSettingKey(for: "custom"),
            "cloud_rewriter_custom_model")
    }

    func test_unknown_provider_falls_back_to_openai_key() {
        XCTAssertEqual(
            HistoryPanelController.cloudRewriterModelSettingKey(for: "нечто"),
            "cloud_rewriter_openai_model",
            "неизвестный провайдер не должен ронять запись настройки")
    }

    func test_value_picked_matches_provider() {
        var settings = AgentSettings.default
        settings.cloudRewriterOpenaiModel = "gpt-4.1-mini"
        settings.cloudRewriterAnthropicModel = "claude-sonnet-5"
        settings.cloudRewriterCustomModel = "qwen2.5:7b"

        XCTAssertEqual(
            HistoryPanelController.cloudRewriterModelValue(for: "openai", settings: settings),
            "gpt-4.1-mini")
        XCTAssertEqual(
            HistoryPanelController.cloudRewriterModelValue(for: "anthropic", settings: settings),
            "claude-sonnet-5")
        XCTAssertEqual(
            HistoryPanelController.cloudRewriterModelValue(for: "custom", settings: settings),
            "qwen2.5:7b")
    }

    func test_settings_payload_carries_both_new_models() {
        var settings = AgentSettings.default
        settings.cloudRewriterOpenaiModel = "gpt-4.1-mini"
        settings.cloudRewriterAnthropicModel = "claude-sonnet-5"
        let payload = settings.toPayload()
        XCTAssertEqual(payload["cloud_rewriter_openai_model"] as? String, "gpt-4.1-mini")
        XCTAssertEqual(payload["cloud_rewriter_anthropic_model"] as? String, "claude-sonnet-5")
    }

    func test_model_row_is_no_longer_custom_only() throws {
        let source = try String(contentsOf: Self.sourceURL, encoding: .utf8)
        XCTAssertFalse(
            source.contains("modelRow.isHidden = !isCustom"),
            "строка «Модель» обязана быть видимой для всех провайдеров")
    }

    private static var sourceURL: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/HistoryPanelController+CloudRewriter.swift")
    }
}
