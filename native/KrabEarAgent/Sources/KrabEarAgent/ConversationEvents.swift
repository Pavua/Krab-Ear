/*
 Типы событий WebSocket для режима «Разговор с AI».

 Протокол: Voice Gateway conv.* vocabulary (2026-06-20).
 Uplink JSON: управляющие команды (control).
 Downlink JSON: события от Voice Gateway (конверт {"type","ts","session_id","data"}).

 Все поля контента находятся в подобъекте "data", НЕ на верхнем уровне.
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
    /// conv.transcript_partial (isFinal=false) / conv.transcript_final (isFinal=true).
    case sttPartial(text: String, lang: String, isFinal: Bool)

    /// Движок AI успешно загружен и готов к работе (conv.ready).
    case engineLoaded(name: String, elapsedSec: Double)

    /// Финальный текстовый ответ AI (conv.reply_final).
    case replyFinal(text: String)

    /// Сессия переработана из-за 5-минутного лимита (conv.recycled) — нужно переподключиться.
    case recycled(reason: String)

    /// Сессия закрыта сервером штатно (conv.closed) — завершаем диалог без ошибки.
    case closed

    /// AI прерван (barge-in голосом ИЛИ подтверждение ручного control-interrupt).
    /// conv.interrupted — с Волны 3c означает ПОДТВЕРЖДЁННУЮ осмысленную речь
    /// (VG фильтрует шум/кашель на своей стороне, см. бриф 2026-07-09-vg-barge-in-resume).
    case interrupted(reason: String)

    /// Ошибка от сервера (conv.error / conv.fatal).
    case error(code: String, message: String)

    /// Неизвестный/нераспознанный тип события (для forward-compat).
    case unknown(type: String, raw: [String: Any])
}

// MARK: - Parsing

extension ConversationEvent {

    /// Декодирует JSON-данные WebSocket downlink в ConversationEvent.
    /// Возвращает nil при невалидном JSON (бинарные PCM/Opus-фреймы не затрагиваются).
    static func decode(from data: Data) -> ConversationEvent? {
        guard
            let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let type = raw["type"] as? String
        else { return nil }

        // Подобъект "data" присутствует во всех conv.* событиях.
        let payload = (raw["data"] as? [String: Any]) ?? [:]

        switch type {

        // MARK: conv.* (основной словарь Voice Gateway)

        case "conv.transcript_partial":
            let text = (payload["text"] as? String) ?? ""
            return .sttPartial(text: text, lang: "", isFinal: false)

        case "conv.transcript_final":
            let text = (payload["text"] as? String) ?? ""
            return .sttPartial(text: text, lang: "", isFinal: true)

        case "conv.reply_final":
            let text = (payload["text"] as? String) ?? ""
            return .replyFinal(text: text)

        case "conv.ready":
            let name = (payload["engine"] as? String) ?? ""
            return .engineLoaded(name: name, elapsedSec: 0.0)

        case "conv.error":
            let message = (payload["message"] as? String)
                ?? (payload["error"] as? String)
                ?? (raw["message"] as? String)
                ?? ""
            return .error(code: "conv.error", message: message)

        case "conv.fatal":
            let message = (payload["message"] as? String)
                ?? (payload["error"] as? String)
                ?? (raw["message"] as? String)
                ?? ""
            return .error(code: "conv.fatal", message: message)

        case "conv.recycled":
            let reason = (payload["reason"] as? String) ?? ""
            return .recycled(reason: reason)

        case "conv.closed":
            // Сессия закрыта сервером — завершаем диалог без ошибки.
            return .closed

        case "conv.interrupted":
            let reason = (payload["reason"] as? String) ?? ""
            return .interrupted(reason: reason)

        case "conv.vad_speech", "conv.vad_silence":
            // VAD-маркеры состояния пользователя — обрабатываем молча.
            return .unknown(type: type, raw: raw)

        case "conv.audio_chunk":
            // JSON-вариант аудио-чанка (в продакшне TTS приходит бинарными WS-фреймами).
            return .unknown(type: type, raw: raw)

        // MARK: Обратная совместимость со старым словарём (legacy, безвредно)

        case "stt.partial":
            let text    = (raw["text"] as? String) ?? ""
            let lang    = (raw["lang"] as? String) ?? ""
            let isFinal = (raw["is_final"] as? Bool) ?? false
            return .sttPartial(text: text, lang: lang, isFinal: isFinal)

        case "engine.loaded":
            let name       = (raw["name"] as? String) ?? ""
            let elapsedSec = (raw["elapsed_sec"] as? Double) ?? 0.0
            return .engineLoaded(name: name, elapsedSec: elapsedSec)

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
