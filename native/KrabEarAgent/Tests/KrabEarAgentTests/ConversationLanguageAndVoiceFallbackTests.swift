/*
 ConversationLanguageAndVoiceFallbackTests — язык разговора уходит в Voice Gateway явно, а смена голоса не молчит.

 Живой случай 24.11: посреди разговора голос ответа сменился с клона на женский с акцентом. По данным VG, TTS
 вызывается без языка (`auto`), а клон при сбое тихо уступает другому движку. Ear: (1) не оставляет `lang` пустым —
 сохранённый выбор, по умолчанию RU; (2) разбирает `conv.voice_fallback` и показывает строку в расшифровке.
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class ConversationLanguageAndVoiceFallbackTests: XCTestCase {

    private var defaults: UserDefaults!

    override func setUp() async throws {
        try await super.setUp()
        let suite = "ConversationLanguageTests-\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)
        defaults.removePersistentDomain(forName: suite)
    }

    // MARK: Язык

    func test_savedLanguageHint_defaultsToRU_notAuto() {
        XCTAssertEqual(ConversationViewController.savedLanguageHint(in: defaults), "ru")
    }

    func test_saveAndRestoreLanguageHint_roundTrip() {
        for value in ["auto", "ru", "en", "es"] {
            ConversationViewController.saveLanguageHint(value, in: defaults)
            XCTAssertEqual(ConversationViewController.savedLanguageHint(in: defaults), value)
        }
    }

    func test_unknownStoredValueFallsBackToRU() {
        defaults.set("de", forKey: "KrabEar_ConversationLanguageHint")
        XCTAssertEqual(ConversationViewController.savedLanguageHint(in: defaults), "ru")
        defaults.set("", forKey: "KrabEar_ConversationLanguageHint")
        XCTAssertEqual(ConversationViewController.savedLanguageHint(in: defaults), "ru")
    }

    func test_languageChange_isPersistedAndSelectorShowsSavedValue() {
        ConversationViewController.saveLanguageHint("es", in: defaults)
        var config = ConversationConfig.default
        config.languageHint = ConversationViewController.savedLanguageHint(in: defaults)
        let vc = ConversationViewController(config: config, userDefaults: defaults)
        vc.loadView()
        vc.viewDidLoad()
        XCTAssertEqual(vc.langHintSelector.indexOfSelectedItem, 3, "ES — четвёртый пункт селектора")
        vc.langHintSelector.selectItem(at: 2)
        vc.onLangHintChanged()
        XCTAssertEqual(vc.config.languageHint, "en")
        XCTAssertEqual(ConversationViewController.savedLanguageHint(in: defaults), "en")
    }

    func test_websocketRequest_sendsLangWhenNotAuto() throws {
        var config = ConversationConfig.default
        config.languageHint = "ru"
        let vc = ConversationViewController(config: config, userDefaults: defaults)
        let request = try XCTUnwrap(vc.makeWebSocketRequest(for: "ws://127.0.0.1:8090/v1/sessions/vs_a/conversation"))
        XCTAssertTrue(request.url?.absoluteString.contains("lang=ru") == true)
    }

    func test_setupUsesSavedLanguageHint() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/HistoryPanelController+VoiceTab.swift")
        let src = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(src.contains("languageHint: ConversationViewController.savedLanguageHint"))
        XCTAssertFalse(src.contains("languageHint: \"auto\""), "язык не должен жёстко возвращаться к auto")
    }

    // MARK: conv.voice_fallback

    func test_decode_voiceFallback_fields() throws {
        let json = #"{"type":"conv.voice_fallback","data":{"reason":"clone_timeout","from_engine":"local_clone","to_engine":"kokoro"}}"#
        let event = try XCTUnwrap(ConversationEvent.decode(from: Data(json.utf8)))
        guard case .voiceFallback(let reason, let from, let to) = event else {
            return XCTFail("ожидался .voiceFallback, получено \(event)")
        }
        XCTAssertEqual(reason, "clone_timeout")
        XCTAssertEqual(from, "local_clone")
        XCTAssertEqual(to, "kokoro")
    }

    func test_decode_voiceFallback_missingFieldsAreEmptyNotCrash() throws {
        let event = try XCTUnwrap(ConversationEvent.decode(from: Data(#"{"type":"conv.voice_fallback"}"#.utf8)))
        guard case .voiceFallback(let reason, let from, let to) = event else { return XCTFail() }
        XCTAssertEqual([reason, from, to], ["", "", ""])
    }

    func test_voiceFallback_appendsTranscriptLine_keepsState_noSpeech() {
        let vc = ConversationViewController(config: .default)
        vc.loadView()
        vc.viewDidLoad()
        var spoken: [String] = []
        vc.errorAnnouncer.speak = { spoken.append($0) }
        vc.isSessionActive = true
        vc.conversationState = .speaking
        vc.handleDownlinkEvent(.voiceFallback(reason: "clone_timeout", fromEngine: "local_clone", toEngine: "kokoro"))
        XCTAssertTrue(vc.transcriptBuffer.contains("Голос временно упрощён"), vc.transcriptBuffer)
        XCTAssertTrue(vc.transcriptBuffer.contains("clone_timeout"))
        XCTAssertEqual(vc.conversationState, .speaking, "смена голоса не обрывает разговор")
        XCTAssertTrue(spoken.isEmpty, "не озвучиваем, иначе фраза сама ломает голос ответа")
    }
}
