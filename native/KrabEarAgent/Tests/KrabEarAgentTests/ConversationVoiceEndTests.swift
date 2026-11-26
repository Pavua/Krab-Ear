import AppKit
import XCTest
@testable import KrabEarAgent

/// Живая проверка владельца 26.11: «отключись» → ассистент говорит «хорошо», но разговор не закрывается.
@MainActor
final class ConversationVoiceEndTests: XCTestCase {

    func test_phrases_endRequests() {
        for text in ["Отключись", "отключись, пожалуйста.", "Заверши разговор!", "давай закончим разговор",
                     "Ну всё, до свидания", "Пока", "Пока-пока", "Хватит", "Выключайся", "Всё, хватит"] {
            XCTAssertTrue(ConversationVoiceEndPhrases.isEndRequest(text), text)
        }
    }

    func test_phrases_ordinaryTalkDoesNotEnd() {
        for text in ["Расскажи про отключение света", "Пока я готовлю, расскажи новости", "Хватит ли мне денег до зарплаты",
                     "Как завершить разговор с банком, если он не отвечает и всё время переключает меня", "",
                     "Отключи звук", "Какая сегодня погода"] {
            XCTAssertFalse(ConversationVoiceEndPhrases.isEndRequest(text), text)
        }
    }

    /// Ревью Opus-пары 26.11 (блок 1): отрицание переворачивает смысл.
    func test_phrases_negationDoesNotEnd() {
        for text in ["не отключайся", "Не отключайся, я сейчас вернусь", "пожалуйста не выключайся",
                     "нет, не заканчивай разговор", "нельзя завершать разговор", "не заверши разговор"] {
            XCTAssertFalse(ConversationVoiceEndPhrases.isEndRequest(text), text)
        }
    }

    /// Ревью Opus-пары 26.11 (блок 2): «до свидания» внутри просьбы о переводе — не конец разговора.
    func test_phrases_goodbyeInsideQuotingDoesNotEnd() {
        for text in ["как по-испански до свидания", "переведи до свидания на английский", "скажи до свидания по-итальянски",
                     "что означает до свидания"] {
            XCTAssertFalse(ConversationVoiceEndPhrases.isEndRequest(text), text)
        }
        for text in ["до свидания", "ну всё, до свидания", "до свидания, спасибо"] {
            XCTAssertTrue(ConversationVoiceEndPhrases.isEndRequest(text), text)
        }
    }

    func test_settings_defaultOn_explicitOff() {
        let defaults = UserDefaults(suiteName: "vend-\(UUID().uuidString)")!
        XCTAssertTrue(ConversationVoiceEndSettings.isEnabled(in: defaults))
        defaults.set(false, forKey: ConversationVoiceEndSettings.enabledKey)
        XCTAssertFalse(ConversationVoiceEndSettings.isEnabled(in: defaults))
    }

    private func makeVC(defaults: UserDefaults) -> ConversationViewController {
        let config = ConversationConfig(wsURLString: "ws://127.0.0.1:19999/v1/conversation", apiKey: "",
                                        languageHint: "auto", engine: "auto", brain: "auto")
        let vc = ConversationViewController(config: config, runtimeOptions: .isolatedTests, userDefaults: defaults)
        vc.loadView()
        vc.viewDidLoad()
        return vc
    }

    func test_vc_voiceEndRequest_closesSession() {
        let vc = makeVC(defaults: UserDefaults(suiteName: "vend-a-\(UUID().uuidString)")!)
        vc.startConversation()
        vc.conversationState = .listening
        vc.handleDownlinkEvent(.sttPartial(text: "Отключись", lang: "ru", isFinal: true))
        XCTAssertFalse(vc.isSessionActive, "голосовая просьба закрывает разговор")
        XCTAssertEqual(vc.conversationState, .idle)
    }

    func test_vc_partialTranscript_doesNotEnd() {
        let vc = makeVC(defaults: UserDefaults(suiteName: "vend-b-\(UUID().uuidString)")!)
        vc.startConversation()
        vc.conversationState = .listening
        vc.handleDownlinkEvent(.sttPartial(text: "Отключись", lang: "ru", isFinal: false))
        XCTAssertTrue(vc.isSessionActive, "частичная расшифровка не закрывает разговор")
        vc.stopConversation()
    }

    func test_vc_voiceEndDisabled_keepsSession() {
        let defaults = UserDefaults(suiteName: "vend-c-\(UUID().uuidString)")!
        defaults.set(false, forKey: ConversationVoiceEndSettings.enabledKey)
        let vc = makeVC(defaults: defaults)
        vc.startConversation()
        vc.conversationState = .listening
        vc.handleDownlinkEvent(.sttPartial(text: "Отключись", lang: "ru", isFinal: true))
        XCTAssertTrue(vc.isSessionActive, "при выключенном тумблере разговор не закрывается")
        vc.stopConversation()
    }

    func test_vc_ordinaryFinal_keepsSession() {
        let vc = makeVC(defaults: UserDefaults(suiteName: "vend-d-\(UUID().uuidString)")!)
        vc.startConversation()
        vc.conversationState = .listening
        vc.handleDownlinkEvent(.sttPartial(text: "Расскажи про отключение света", lang: "ru", isFinal: true))
        XCTAssertTrue(vc.isSessionActive)
        XCTAssertEqual(vc.conversationState, .thinking)
        vc.stopConversation()
    }
}
