import Foundation

/// Событие realtime-канала VG `/v1/sessions/{id}/stream` (spec §2.2).
/// Диспетчеризация — ТОЧНОЕ совпадение type (prefix-матч съел бы
/// auto_spoken игнорируемым agent.suggestion). Все поля кроме text
/// опциональны: у VG до 4 publish-сайтов на событие с разными наборами.
enum VGCallEvent: Equatable {
    case sttFinal(text: String, language: String?, confidence: Double?)
    case translationFinal(text: String, sourceText: String?, srcLang: String?, tgtLang: String?)
    case agentResponse(text: String, textRu: String?, utteranceTs: String?, action: String?)
    case agentAutoSpoken(text: String, textRu: String?, action: String?, digits: String?)
    case agentInterrupted(utteranceTs: String?, spokenFraction: Double?, spokenText: String?)
    case callState(status: String, muted: Bool?, held: Bool?)
    case callRinging
    case callAnswered
    case callEnded(reason: String?)
    case callClosed
    case diagnosticError(message: String?)
    case screeningStarted
    case costAlert(level: String?, currentUsd: Double?, message: String?)
    case ignored(type: String)

    static func decode(_ data: Data) -> VGCallEvent? {
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let type = obj["type"] as? String else { return nil }
        let d = obj["data"] as? [String: Any] ?? [:]
        func s(_ k: String) -> String? { d[k] as? String }
        func dbl(_ k: String) -> Double? {
            if let v = d[k] as? Double { return v }
            if let v = d[k] as? Int { return Double(v) }
            return nil
        }
        switch type {
        case "stt.final":
            guard let text = s("text") else { return .ignored(type: type) }
            return .sttFinal(text: text, language: s("language"), confidence: dbl("confidence"))
        case "translation.final":
            guard let text = s("text") else { return .ignored(type: type) }
            return .translationFinal(text: text, sourceText: s("source_text"),
                                     srcLang: s("src_lang"), tgtLang: s("tgt_lang"))
        case "agent.response":
            guard let text = s("text") else { return .ignored(type: type) }
            return .agentResponse(text: text, textRu: s("text_ru"),
                                  utteranceTs: s("utterance_ts"), action: s("action"))
        case "agent.suggestion.auto_spoken":
            guard let text = s("text") else { return .ignored(type: type) }
            return .agentAutoSpoken(text: text, textRu: s("text_ru"),
                                    action: s("action"), digits: s("digits"))
        case "agent.interrupted":
            return .agentInterrupted(utteranceTs: s("utterance_ts"),
                                     spokenFraction: dbl("spoken_fraction"),
                                     spokenText: s("spoken_text"))
        case "call.state":
            return .callState(status: s("status") ?? "",
                              muted: d["muted"] as? Bool, held: d["held"] as? Bool)
        case "call.ringing": return .callRinging
        case "call.answered": return .callAnswered
        case "call.ended": return .callEnded(reason: s("reason"))
        case "call.closed": return .callClosed
        // T2 (w1 final): реальные VG publish-сайты (app/main.py, engines/realtime.py,
        // telephony/telegram.py) шлют РОВНО {"msg": "..."} — "message"/"detail"
        // остаются лишь бэккомпат-фоллбэком, ни один живой сайт их не несёт.
        case "diagnostic.error": return .diagnosticError(message: s("msg") ?? s("message") ?? s("detail"))
        case "screening.started": return .screeningStarted
        case "cost.alert":
            return .costAlert(level: s("level"), currentUsd: dbl("current_usd"), message: s("message"))
        default:
            return .ignored(type: type)
        }
    }
}
