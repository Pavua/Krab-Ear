/*
 ConversationVCWebSocketTests — XCTest для WebSocket-клиента ConversationViewController.

 Стратегия (без живого сокета):
 1. ConversationEvent.decode — парсинг всех 5 downlink-типов + невалидный JSON.
 2. ConversationControlMessage.jsonData — структура uplink JSON.
 3. ConversationState equality — все комбинации == / !=.
 4. URL-сборка — query params движка/мозга/языка через _buildWSRequest (DEBUG hook).
 5. Заголовок Authorization через _buildWSRequest.
 6. State transitions: startConversation → .connecting, stopConversation → .idle (@MainActor).
 7. sendControlMessage не крашит при nil task (guard wsHolder.task).
*/

import XCTest
@testable import KrabEarAgent

// MARK: - ConversationEvent.decode

final class ConversationEventDecodeTests: XCTestCase {

    // MARK: stt.partial (partial)

    func test_decode_sttPartial_partial() {
        let json = #"{"type":"stt.partial","text":"Привет","lang":"ru","is_final":false}"#
        let data = json.data(using: .utf8)!
        let event = ConversationEvent.decode(from: data)
        guard case .sttPartial(let text, let lang, let isFinal) = event else {
            return XCTFail("Ожидался .sttPartial")
        }
        XCTAssertEqual(text, "Привет")
        XCTAssertEqual(lang, "ru")
        XCTAssertFalse(isFinal)
    }

    // MARK: stt.partial (final)

    func test_decode_sttPartial_final() {
        let json = #"{"type":"stt.partial","text":"Hola","lang":"es","is_final":true}"#
        let data = json.data(using: .utf8)!
        guard case .sttPartial(let text, _, let isFinal) = ConversationEvent.decode(from: data) else {
            return XCTFail("Ожидался .sttPartial")
        }
        XCTAssertEqual(text, "Hola")
        XCTAssertTrue(isFinal)
    }

    // MARK: engine.loaded

    func test_decode_engineLoaded() {
        let json = #"{"type":"engine.loaded","name":"moshi","elapsed_sec":2.5}"#
        let data = json.data(using: .utf8)!
        guard case .engineLoaded(let name, let elapsed) = ConversationEvent.decode(from: data) else {
            return XCTFail("Ожидался .engineLoaded")
        }
        XCTAssertEqual(name, "moshi")
        XCTAssertEqual(elapsed, 2.5, accuracy: 0.001)
    }

    // MARK: conv.reply_final (заменяет tool.invoked + summary.ready)

    func test_decode_convReplyFinal() {
        let json = #"{"type":"conv.reply_final","ts":1718880002,"session_id":"vs_abc123","data":{"text":"Привет от AI"}}"#
        let data = json.data(using: .utf8)!
        guard case .replyFinal(let text) = ConversationEvent.decode(from: data) else {
            return XCTFail("Ожидался .replyFinal")
        }
        XCTAssertEqual(text, "Привет от AI")
    }

    // MARK: conv.recycled

    func test_decode_convRecycled() {
        let json = #"{"type":"conv.recycled","data":{"reason":"5min_cap","recycled_count":1}}"#
        let data = json.data(using: .utf8)!
        guard case .recycled(let reason) = ConversationEvent.decode(from: data) else {
            return XCTFail("Ожидался .recycled")
        }
        XCTAssertEqual(reason, "5min_cap")
    }

    // MARK: error

    func test_decode_error() {
        let json = #"{"type":"error","code":"E_AUTH","message":"Unauthorized"}"#
        let data = json.data(using: .utf8)!
        guard case .error(let code, let message) = ConversationEvent.decode(from: data) else {
            return XCTFail("Ожидался .error")
        }
        XCTAssertEqual(code, "E_AUTH")
        XCTAssertEqual(message, "Unauthorized")
    }

    // MARK: unknown type — forward compat

    func test_decode_unknownType_returnsUnknown() {
        let json = #"{"type":"future.event","foo":"bar"}"#
        let data = json.data(using: .utf8)!
        guard case .unknown(let type, _) = ConversationEvent.decode(from: data) else {
            return XCTFail("Ожидался .unknown для нераспознанного типа")
        }
        XCTAssertEqual(type, "future.event")
    }

    // MARK: невалидный JSON

    func test_decode_invalidJSON_returnsNil() {
        let data = "not json at all".data(using: .utf8)!
        XCTAssertNil(ConversationEvent.decode(from: data),
                     "Невалидный JSON должен давать nil")
    }

    // MARK: пустой JSON-объект (нет поля type)

    func test_decode_missingTypeField_returnsNil() {
        let data = "{}".data(using: .utf8)!
        XCTAssertNil(ConversationEvent.decode(from: data),
                     "JSON без поля type должен давать nil")
    }

    // MARK: stt.partial с отсутствующими полями — defaults

    func test_decode_sttPartial_missingFields_usesDefaults() {
        let json = #"{"type":"stt.partial"}"#
        let data = json.data(using: .utf8)!
        guard case .sttPartial(let text, let lang, let isFinal) = ConversationEvent.decode(from: data) else {
            return XCTFail("Ожидался .sttPartial даже без полей text/lang/is_final")
        }
        XCTAssertEqual(text, "")
        XCTAssertEqual(lang, "")
        XCTAssertFalse(isFinal)
    }
}

// MARK: - ConversationControlMessage JSON encoding

final class ConversationControlMessageTests: XCTestCase {

    private func decode(_ data: Data) -> [String: Any]? {
        try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    func test_jsonData_interrupt_hasTypeAndAction() {
        let msg = ConversationControlMessage(action: .interrupt)
        guard let data = msg.jsonData, let dict = decode(data) else {
            return XCTFail("jsonData не должен быть nil")
        }
        XCTAssertEqual(dict["type"] as? String, "control")
        XCTAssertEqual(dict["action"] as? String, "interrupt")
    }

    func test_jsonData_end_hasTypeAndAction() {
        let msg = ConversationControlMessage(action: .end)
        guard let data = msg.jsonData, let dict = decode(data) else {
            return XCTFail("jsonData не должен быть nil")
        }
        XCTAssertEqual(dict["type"] as? String, "control")
        XCTAssertEqual(dict["action"] as? String, "end")
    }

    func test_jsonData_pushToTalkOff_hasCorrectActionString() {
        let msg = ConversationControlMessage(action: .pushToTalkOff)
        guard let data = msg.jsonData, let dict = decode(data) else {
            return XCTFail("jsonData не должен быть nil")
        }
        XCTAssertEqual(dict["action"] as? String, "push_to_talk_off",
                       "action должен быть push_to_talk_off (со snake_case)")
    }

    func test_jsonData_isValidUTF8String() {
        let msg = ConversationControlMessage(action: .interrupt)
        guard let data = msg.jsonData else { return XCTFail("jsonData nil") }
        XCTAssertNotNil(String(data: data, encoding: .utf8),
                        "jsonData должен декодироваться как UTF-8")
    }
}

// MARK: - ConversationState equality

final class ConversationStateTests: XCTestCase {

    func test_sameStates_areEqual() {
        XCTAssertEqual(ConversationState.idle, .idle)
        XCTAssertEqual(ConversationState.connecting, .connecting)
        XCTAssertEqual(ConversationState.listening, .listening)
        XCTAssertEqual(ConversationState.thinking, .thinking)
        XCTAssertEqual(ConversationState.speaking, .speaking)
        XCTAssertEqual(ConversationState.error("oops"), .error("oops"))
    }

    func test_differentStates_areNotEqual() {
        XCTAssertNotEqual(ConversationState.idle, .connecting)
        XCTAssertNotEqual(ConversationState.listening, .thinking)
        XCTAssertNotEqual(ConversationState.error("a"), .error("b"))
        XCTAssertNotEqual(ConversationState.error("x"), .idle)
    }

    func test_localizedLabel_idle() {
        XCTAssertTrue(ConversationState.idle.localizedLabel.contains("Готов"))
    }

    func test_localizedLabel_error_containsMessage() {
        let state = ConversationState.error("тест-ошибка")
        XCTAssertTrue(state.localizedLabel.contains("тест-ошибка"))
    }
}

// MARK: - URL building (DEBUG hook)

@MainActor
final class ConversationVCURLBuildingTests: XCTestCase {

    private func makeVC(config: ConversationConfig) -> ConversationViewController {
        ConversationViewController(config: config)
    }

    // MARK: engine/brain/lang добавляются только если не "auto"

    func test_buildWSRequest_autoValues_onlyBrainModeParam() {
        let config = ConversationConfig(
            wsURLString: "ws://localhost:8090/v1/conversation",
            apiKey: "",
            languageHint: "auto",
            engine: "auto",
            brain: "auto"
        )
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)
        XCTAssertNotNil(req)
        let items = URLComponents(url: req!.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        XCTAssertEqual(items.count, 1,
                       "При auto-значениях engine/brain/lang опускаются, но brain_mode остаётся")
        XCTAssertEqual(items.first?.name, "brain_mode")
        XCTAssertEqual(items.first?.value, "auto")
    }

    func test_buildWSRequest_nonAutoEngine_addsEngineParam() {
        var config = ConversationConfig.default
        config.engine = "moshi"
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let engineParam = items.first(where: { $0.name == "engine" })
        XCTAssertEqual(engineParam?.value, "moshi")
    }

    func test_buildWSRequest_nonAutoBrain_addsBrainParam() {
        var config = ConversationConfig.default
        config.brain = "qwen3-4b"
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let brainParam = items.first(where: { $0.name == "brain" })
        XCTAssertEqual(brainParam?.value, "qwen3-4b")
    }

    func test_buildWSRequest_nonAutoLang_addsLangParam() {
        var config = ConversationConfig.default
        config.languageHint = "ru"
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let langParam = items.first(where: { $0.name == "lang" })
        XCTAssertEqual(langParam?.value, "ru")
    }

    func test_buildWSRequest_allNonAuto_allParamsPresent() {
        let config = ConversationConfig(
            wsURLString: "ws://localhost:8090/v1/conversation",
            apiKey: "",
            languageHint: "es",
            engine: "seamless",
            brain: "llama-3.2-3b"
        )
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let names = Set(items.map(\.name))
        XCTAssertTrue(names.contains("engine"), "engine param должен присутствовать")
        XCTAssertTrue(names.contains("brain"),  "brain param должен присутствовать")
        XCTAssertTrue(names.contains("lang"),   "lang param должен присутствовать")
    }

    // MARK: brain_mode — ВСЕГДА присутствует (в отличие от engine/brain/lang)

    func test_buildWSRequest_brainModeAuto_stillIncludesParam() {
        var config = ConversationConfig.default
        config.brainMode = "auto"
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let brainModeParam = items.first(where: { $0.name == "brain_mode" })
        XCTAssertEqual(brainModeParam?.value, "auto",
                       "brain_mode должен передаваться явно, даже если равен auto")
    }

    func test_buildWSRequest_brainModeKrab_includesParam() {
        var config = ConversationConfig.default
        config.brainMode = "krab"
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let brainModeParam = items.first(where: { $0.name == "brain_mode" })
        XCTAssertEqual(brainModeParam?.value, "krab")
    }

    func test_buildWSRequest_brainModeFast_includesParam() {
        var config = ConversationConfig.default
        config.brainMode = "fast"
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let brainModeParam = items.first(where: { $0.name == "brain_mode" })
        XCTAssertEqual(brainModeParam?.value, "fast")
    }

    // MARK: Authorization header

    func test_buildWSRequest_withApiKey_setsAuthorizationHeader() {
        var config = ConversationConfig.default
        config.apiKey = "tok-secret"
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer tok-secret")
    }

    func test_buildWSRequest_emptyApiKey_noAuthorizationHeader() {
        var config = ConversationConfig.default
        config.apiKey = ""
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        XCTAssertNil(req.value(forHTTPHeaderField: "Authorization"),
                     "Пустой apiKey не должен добавлять заголовок Authorization")
    }

    // MARK: Невалидный URL (пустая строка → URL(string: "") == nil)

    func test_buildWSRequest_invalidURL_returnsNil() {
        let vc = makeVC(config: .default)
        let req = vc._buildWSRequest(for: "")
        XCTAssertNil(req, "Пустой URL должен давать nil из URL(string:)")
    }
}

// MARK: - State transitions via startConversation / stopConversation

@MainActor
final class ConversationVCStateTransitionTests: XCTestCase {

    private func makeVC(wsURL: String = "ws://127.0.0.1:9999/v1/conversation") -> ConversationViewController {
        let config = ConversationConfig(
            wsURLString: wsURL,
            apiKey: "",
            languageHint: "auto",
            engine: "auto",
            brain: "auto"
        )
        return ConversationViewController(config: config)
    }

    /// startConversation → устанавливает isSessionActive = true и state = .connecting.
    /// (Сокет запускается async; синхронно проверяем только немедленные мутации.)
    func test_startConversation_setsConnectingAndActive() {
        let vc = makeVC()
        XCTAssertFalse(vc.isSessionActive)
        XCTAssertEqual(vc.conversationState, .idle)

        vc.startConversation()

        XCTAssertTrue(vc.isSessionActive, "isSessionActive должен стать true")
        // Состояние .connecting устанавливается до открытия сокета.
        XCTAssertEqual(vc.conversationState, .connecting,
                       "startConversation должен немедленно перевести в .connecting")
    }

    /// startConversation при пустом URL (URL(string: "") == nil) → state = .error.
    func test_startConversation_emptyURL_setsErrorState() {
        let vc = makeVC(wsURL: "")
        vc.startConversation()
        if case .error(_) = vc.conversationState {
            // ожидаемое поведение
        } else {
            XCTFail("Ожидался .error при пустом URL, получено: \(vc.conversationState)")
        }
    }

    /// stopConversation сбрасывает isSessionActive и возвращает state в .idle.
    func test_stopConversation_resetsState() {
        let vc = makeVC()
        // Принудительно выставляем активную сессию.
        vc.isSessionActive = true
        vc.conversationState = .listening

        vc.stopConversation()

        XCTAssertFalse(vc.isSessionActive, "isSessionActive должен стать false")
        XCTAssertEqual(vc.conversationState, .idle)
    }

    /// Повторный startConversation при уже активной сессии игнорируется (guard !isSessionActive).
    func test_startConversation_whenAlreadyActive_isNoop() {
        let vc = makeVC()
        vc.isSessionActive = true
        vc.conversationState = .listening

        // Второй start не должен менять состояние
        vc.startConversation()

        XCTAssertEqual(vc.conversationState, .listening,
                       "Повторный startConversation при активной сессии не должен менять состояние")
    }

    /// stopConversation при неактивной сессии — no-op, не крашит.
    func test_stopConversation_whenNotActive_isNoop() {
        let vc = makeVC()
        XCTAssertFalse(vc.isSessionActive)
        vc.stopConversation() // Не должен крашить
        XCTAssertEqual(vc.conversationState, .idle)
    }

    /// interruptAI при активной сессии переводит в .listening.
    func test_interruptAI_whenActive_setsListening() {
        let vc = makeVC()
        vc.isSessionActive = true
        vc.conversationState = .speaking

        vc.interruptAI()

        XCTAssertEqual(vc.conversationState, .listening)
    }

    /// sendAudioFrame при неактивной сессии не крашит (guard isSessionActive).
    func test_sendAudioFrame_whenNotActive_doesNotCrash() {
        let vc = makeVC()
        vc.isSessionActive = false
        let data = Data([0x01, 0x02, 0x03])
        vc.sendAudioFrame(data) // guard isSessionActive → return — не должен крашить
    }
}
