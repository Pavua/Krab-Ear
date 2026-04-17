/*
 Типы событий WebSocket для режима «Разговор с AI».

 Протокол: Section 4.1 из спецификации Voice Assistant Mode.
 Uplink JSON: управляющие команды (control).
 Downlink JSON: события от Voice Gateway.
*/

import Foundation

// MARK: - Uplink (Swift → Voice Gateway)

/// Управляющее сообщение, отправляемое на сервер.
struct ConversationControlMessage: Encodable {
    let type: String = "control"
    let action: ConversationControlAction
}

enum ConversationControlAction: String, Encodable {
    case interrupt         = "interrupt"
    case end               = "end"
    case pushToTalkOff     = "push_to_talk_off"
}

// MARK: - Downlink (Voice Gateway → Swift)

/// Все типы событий, получаемых от Voice Gateway по WebSocket.
enum ConversationEvent {
    /// Частичная или финальная расшифровка речи пользователя.
    case sttPartial(text: String, lang: String, isFinal: Bool)

    /// Движок AI успешно загружен и готов к работе.
    case engineLoaded(name: String, elapsedSec: Double)

    /// AI вызвал инструмент (поиск, LLM-агент и т.д.).
    case toolInvoked(tool: String, args: [String: Any])

    /// Готово краткое резюме разговора.
    case summaryReady(text: String, lang: String)

    /// Ошибка от сервера.
    case error(code: String, message: String)

    /// Неизвестный/нераспознанный тип события (для forward-compat).
    case unknown(type: String, raw: [String: Any])
}

// MARK: - Parsing

extension ConversationEvent {

    /// Декодирует JSON-данные WebSocket downlink в ConversationEvent.
    /// Возвращает nil при невалидном JSON (бинарные Opus-фреймы не затрагиваются).
    static func decode(from data: Data) -> ConversationEvent? {
        guard
            let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let type = raw["type"] as? String
        else { return nil }

        switch type {
        case "stt.partial":
            let text    = (raw["text"] as? String) ?? ""
            let lang    = (raw["lang"] as? String) ?? ""
            let isFinal = (raw["is_final"] as? Bool) ?? false
            return .sttPartial(text: text, lang: lang, isFinal: isFinal)

        case "engine.loaded":
            let name        = (raw["name"] as? String) ?? ""
            let elapsedSec  = (raw["elapsed_sec"] as? Double) ?? 0.0
            return .engineLoaded(name: name, elapsedSec: elapsedSec)

        case "tool.invoked":
            let tool = (raw["tool"] as? String) ?? ""
            let args = (raw["args"] as? [String: Any]) ?? [:]
            return .toolInvoked(tool: tool, args: args)

        case "summary.ready":
            let text = (raw["text"] as? String) ?? ""
            let lang = (raw["lang"] as? String) ?? ""
            return .summaryReady(text: text, lang: lang)

        case "error":
            let code    = (raw["code"] as? String) ?? "unknown"
            let message = (raw["message"] as? String) ?? ""
            return .error(code: code, message: message)

        default:
            return .unknown(type: type, raw: raw)
        }
    }
}

// MARK: - Uplink helpers

extension ConversationControlMessage {
    var jsonData: Data? {
        try? JSONEncoder().encode(self)
    }
}
