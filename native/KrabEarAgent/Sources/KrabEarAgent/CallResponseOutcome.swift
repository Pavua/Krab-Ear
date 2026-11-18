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

    /// Код backend буква в букву: `GatewayCallProvider.ERR_DIAL_UNKNOWN`.
    static let dialUnknownCode = "gateway_dial_unknown"

    /// Текст отказа набора. `gateway_dial_unknown` — не отказ, а неизвестность: шлюз не ответил
    /// вовремя, линия могла подняться, поэтому совет «проверьте настройки» здесь неуместен.
    static func failureMessage(code: String) -> String {
        if code == dialUnknownCode {
            return "Шлюз не ответил вовремя: звонок мог начаться. "
                + "Проверьте активные звонки в Voice Gateway, прежде чем набирать снова."
        }
        return "Не удалось начать звонок (\(code)). Проверьте настройки Voice Gateway."
    }
}

/// Перевод статуса журнала backend (`CallStatus`) в статус UI (`CallSession.Status`).
///
/// 🔴 Журнал заканчивает звонок словами `completed` / `failed`, UI знает `ended` / `error`.
/// Сырое значение не совпадало, и опрос не замечал конца звонка, завершённого не кнопкой.
enum CallBackendStatus {
    private static let liveStatuses: Set<String> = ["idle", "dialing", "connected", "talking", "ending"]

    /// Возвращает rawValue для `CallSession.Status` или `nil` для неизвестного статуса.
    static func uiRawValue(_ backendStatus: String) -> String? {
        let status = backendStatus.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch status {
        case "completed": return "ended"
        case "failed": return "error"
        default: return liveStatuses.contains(status) ? status : nil
        }
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
        let status = (result["status"] as? String)?.lowercased()
        if IPCWire.strictBool(result["ok"]) == false {
            // Повторный end по уже завершённой сессии: backend отвечает invalid_state_transition
            // с терминальным current_state. Звонка больше нет, для UI это «завершён».
            if (result["reason"] as? String) == "invalid_state_transition",
               let current = (result["current_state"] as? String)?.lowercased(),
               terminalStatuses.contains(current) {
                return .ended
            }
            return .failed((result["error"] as? String) ?? (result["reason"] as? String) ?? "call_end_refused")
        }
        // Подтверждение должно быть положительным: пустой result, нестроковый ok и нетерминальный
        // status («active», «talking») завершением не считаются.
        if let status, !terminalStatuses.contains(status) { return .failed("not_terminal:\(status)") }
        if IPCWire.strictBool(result["ok"]) == true || status != nil { return .ended }
        return .failed("no_acknowledgement")
    }

    /// Терминальные статусы backend (`CallStatus.COMPLETED`/`FAILED`).
    static let terminalStatuses: Set<String> = ["completed", "failed"]

    /// Отказ завершить звонок не должен «оживлять» сессию, уже помеченную завершённой
    /// конкурирующим обработчиком (hangup и emergency stop шлют end независимо).
    static func shouldRestoreAfterFailedEnd(sessionAlreadyEnded: Bool) -> Bool {
        !sessionAlreadyEnded
    }
}
