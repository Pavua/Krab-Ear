/*
 ModelsTests — тесты структур данных нативного агента Krab Ear.

 Покрываемые типы:
 - ConversationConfig: инициализация + static default
 - AgentSettings: init(from payload:), static default, toPayload() roundtrip
 - HistoryItem: init?(payload:) — happy path + missing required fields
*/

import XCTest
@testable import KrabEarAgent

final class ModelsTests: XCTestCase {

    // MARK: - ConversationConfig

    func test_conversationConfig_defaultValues() {
        let config = ConversationConfig.default
        XCTAssertEqual(config.wsURLString, "ws://127.0.0.1:8090/v1/conversation")
        XCTAssertEqual(config.apiKey, "")
        XCTAssertEqual(config.languageHint, "auto")
        XCTAssertEqual(config.engine, "auto")
        XCTAssertEqual(config.brain, "auto")
    }

    func test_conversationConfig_customInit() {
        let config = ConversationConfig(
            wsURLString: "ws://example.com/v1/conv",
            apiKey: "secret",
            languageHint: "ru",
            engine: "moshi",
            brain: "qwen3-4b"
        )
        XCTAssertEqual(config.wsURLString, "ws://example.com/v1/conv")
        XCTAssertEqual(config.apiKey, "secret")
        XCTAssertEqual(config.languageHint, "ru")
        XCTAssertEqual(config.engine, "moshi")
        XCTAssertEqual(config.brain, "qwen3-4b")
    }

    func test_conversationConfig_defaultValues_includesBrainModeAndHttpBase() {
        let config = ConversationConfig.default
        XCTAssertEqual(config.brainMode, "auto")
        XCTAssertEqual(config.httpBaseURLString, "http://127.0.0.1:8090")
    }

    func test_conversationConfig_customInit_brainModeAndHttpBase() {
        var config = ConversationConfig.default
        config.brainMode = "krab"
        config.httpBaseURLString = "http://127.0.0.1:9090"
        XCTAssertEqual(config.brainMode, "krab")
        XCTAssertEqual(config.httpBaseURLString, "http://127.0.0.1:9090")
    }

    // MARK: - AgentSettings.init(from payload:)

    func test_agentSettings_initFromPayload_happyPath() {
        let payload: [String: Any] = [
            "mode": "headless",
            "show_dock_icon": false,
            "auto_start_enabled": true,
            "auto_paste": false,
            "quality_profile": "max",
            "network_mode": "offline_only",
            "history_page_size": 100,
            "llm_model": "qwen3-4b",
        ]
        let settings = AgentSettings(from: payload)
        XCTAssertEqual(settings.mode, "headless")
        XCTAssertEqual(settings.showDockIcon, false)
        XCTAssertEqual(settings.autoStartEnabled, true)
        XCTAssertEqual(settings.autoPaste, false)
        XCTAssertEqual(settings.qualityProfile, "max")
        XCTAssertEqual(settings.networkMode, "offline_only")
        XCTAssertEqual(settings.historyPageSize, 100)
        XCTAssertEqual(settings.llmModel, "qwen3-4b")
    }

    func test_agentSettings_initFromEmptyPayload_usesDefaults() {
        // Пустой payload — все поля должны получить значения из AgentSettings.default
        let settings = AgentSettings(from: [:])
        let defaults = AgentSettings.default

        XCTAssertEqual(settings.mode, defaults.mode)
        XCTAssertEqual(settings.qualityProfile, defaults.qualityProfile)
        XCTAssertEqual(settings.historyPageSize, defaults.historyPageSize)
        XCTAssertEqual(settings.llmModel, defaults.llmModel)
        XCTAssertEqual(settings.translationMode, defaults.translationMode)
    }

    func test_agentSettings_toPayload_roundtrip() {
        // toPayload() → init(from:) должен воспроизводить исходные значения
        let original = AgentSettings.default
        let payload = original.toPayload()
        let restored = AgentSettings(from: payload)

        XCTAssertEqual(restored.mode, original.mode)
        XCTAssertEqual(restored.showDockIcon, original.showDockIcon)
        XCTAssertEqual(restored.autoPaste, original.autoPaste)
        XCTAssertEqual(restored.qualityProfile, original.qualityProfile)
        XCTAssertEqual(restored.historyPageSize, original.historyPageSize)
        XCTAssertEqual(restored.llmModel, original.llmModel)
        XCTAssertEqual(restored.diarizationEnabled, original.diarizationEnabled)
        XCTAssertEqual(restored.llmRewriteEnabled, original.llmRewriteEnabled)
    }

    func test_agentSettings_toPayload_containsExpectedKeys() {
        let payload = AgentSettings.default.toPayload()
        let expectedKeys = ["mode", "quality_profile", "llm_model", "history_page_size",
                            "auto_paste", "diarization_enabled", "translation_mode"]
        for key in expectedKeys {
            XCTAssertNotNil(payload[key], "toPayload() должен содержать ключ '\(key)'")
        }
    }

    func test_agentSettings_glossaryRoundtrip() {
        // translationGlossary [String:String] проходит через payload без потерь
        let glossary = ["API": "АПИ", "тест": "test"]
        var payload = AgentSettings.default.toPayload()
        payload["translation_glossary"] = glossary
        let settings = AgentSettings(from: payload)
        XCTAssertEqual(settings.translationGlossary, glossary)
    }

    // MARK: - HistoryItem

    func test_historyItem_initFromPayload_happyPath() {
        let payload: [String: Any] = [
            "id": "item-001",
            "ts": "2026-04-19T12:00:00Z",
            "text": "Привет, мир!",
            "paste_status": "ok",
            "source_text": "original",
            "translated_text": "Hello, world!",
            "translation_mode": "ru_es",
            "translation_status": "done",
        ]
        let item = HistoryItem(payload: payload)
        XCTAssertNotNil(item)
        XCTAssertEqual(item?.id, "item-001")
        XCTAssertEqual(item?.ts, "2026-04-19T12:00:00Z")
        XCTAssertEqual(item?.text, "Привет, мир!")
        XCTAssertEqual(item?.pasteStatus, "ok")
        XCTAssertEqual(item?.translatedText, "Hello, world!")
        XCTAssertEqual(item?.translationMode, "ru_es")
        XCTAssertEqual(item?.translationStatus, "done")
    }

    func test_historyItem_initFromPayload_missingRequiredFields_returnsNil() {
        // Отсутствие "id", "ts" или "text" → init? возвращает nil
        let noId: [String: Any] = ["ts": "2026-01-01", "text": "hello"]
        XCTAssertNil(HistoryItem(payload: noId), "Без 'id' должен вернуть nil")

        let noTs: [String: Any] = ["id": "x", "text": "hello"]
        XCTAssertNil(HistoryItem(payload: noTs), "Без 'ts' должен вернуть nil")

        let noText: [String: Any] = ["id": "x", "ts": "2026-01-01"]
        XCTAssertNil(HistoryItem(payload: noText), "Без 'text' должен вернуть nil")
    }

    func test_historyItem_optionalFields_fallbackToDefaults() {
        // Только обязательные поля — опциональные получают default-значения
        let minimal: [String: Any] = [
            "id": "min-001",
            "ts": "2026-04-19",
            "text": "test",
        ]
        let item = HistoryItem(payload: minimal)
        XCTAssertNotNil(item)
        XCTAssertEqual(item?.pasteStatus, "failed")
        XCTAssertEqual(item?.sourceText, "")
        XCTAssertEqual(item?.translatedText, "")
        XCTAssertEqual(item?.translationMode, "off")
        XCTAssertEqual(item?.translationStatus, "not_requested")
    }
}
