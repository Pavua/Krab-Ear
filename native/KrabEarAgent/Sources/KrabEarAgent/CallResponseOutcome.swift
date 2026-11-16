import Foundation

/// Строгий разбор ответа backend на `call_start`.
///
/// 🔴 Раньше любой `result` без отказа «не настроен» порождал сессию: `ok == false` или отсутствие
/// `session_id` давали фантомный звонок со случайным UUID и polling несуществующей сессии.
/// Теперь успех = непустой строковый `session_id` и `ok != false`.
enum CallDialOutcome: Equatable {
    case missingResponse
    case needsConfiguration
    case started(sessionID: String, status: String)
    case failed(String)

    /// Коды сверены с backend буква в букву: `gateway_not_configured` возвращает
    /// GatewayCallProvider, `call_provider_unavailable` — CallSessionService.
    static let notConfiguredCodes: Set<String> = ["gateway_not_configured", "call_provider_unavailable"]

    static func parse(_ response: [String: Any]?) -> CallDialOutcome {
        guard let response else { return .missingResponse }
        if let error = response["error"] as? [String: Any] {
            let code = (error["code"] as? String) ?? "unknown_error"
            return notConfiguredCodes.contains(code) ? .needsConfiguration : .failed(code)
        }
        guard let result = response["result"] as? [String: Any] else { return .failed("no_result") }
        if (result["ok"] as? Bool) == false {
            // call_start отвечает отказом В PAYLOAD, а не конвертом ошибки.
            let code = (result["error"] as? String) ?? "call_start_refused"
            return notConfiguredCodes.contains(code) ? .needsConfiguration : .failed(code)
        }
        guard let sessionID = (result["session_id"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !sessionID.isEmpty else { return .failed("no_session_id") }
        return .started(sessionID: sessionID, status: (result["status"] as? String) ?? "dialing")
    }
}

/// Строгий разбор ответа на `call_session_end`.
///
/// 🔴 Раньше `try?` проглатывал ошибку, а UI всё равно помечал сессию `.ended`: живой звонок
/// выглядел завершённым. `.ended` только при подтверждённом ответе без ошибки.
enum CallEndOutcome: Equatable {
    case ended
    case failed(String)

    static func parse(_ response: [String: Any]?) -> CallEndOutcome {
        guard let response else { return .failed("no_response") }
        if let error = response["error"] as? [String: Any] {
            return .failed((error["code"] as? String) ?? "unknown_error")
        }
        guard let result = response["result"] as? [String: Any] else { return .failed("no_result") }
        if (result["ok"] as? Bool) == false {
            return .failed((result["error"] as? String) ?? "call_end_refused")
        }
        return .ended
    }
}
