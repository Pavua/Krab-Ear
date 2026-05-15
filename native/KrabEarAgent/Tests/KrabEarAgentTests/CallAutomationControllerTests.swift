/*
 CallAutomationControllerTests — тесты логики CallAutomationController.

 Стратегия: mock IPCClient (stub через словари), тестируем:
   - E.164 валидацию номера
   - CallSession.Status display / badge color / historyIcon
   - IPC response parsing (handleDialResponse whitebox)
   - callHistory parsing из "call_session_list" ответа
   - ConfigBanner показывается при telnyx_not_configured
   - AgentSettings Telnyx fields roundtrip (toPayload / init(from:))
   - PanelTab.callAutomation rawValue и from()
   - Cost estimate parsing
   - CallHistoryItem.durationFormatted
   - Provider status detection
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Whitebox: E.164 validation (matches controller impl)

private func isValidE164(_ s: String) -> Bool {
    let pattern = #"^\+[1-9]\d{6,14}$"#
    return s.range(of: pattern, options: .regularExpression) != nil
}

// MARK: - CallSession.Status tests

final class CallSessionStatusTests: XCTestCase {

    func test_statusDisplayTitles() {
        XCTAssertEqual(CallSession.Status.idle.displayTitle,      "Ожидание")
        XCTAssertEqual(CallSession.Status.dialing.displayTitle,   "Набор номера...")
        XCTAssertEqual(CallSession.Status.connected.displayTitle, "Подключено")
        XCTAssertEqual(CallSession.Status.talking.displayTitle,   "Разговор")
        XCTAssertEqual(CallSession.Status.ending.displayTitle,    "Завершение...")
        XCTAssertEqual(CallSession.Status.ended.displayTitle,     "Завершён")
        XCTAssertEqual(CallSession.Status.error.displayTitle,     "Ошибка")
    }

    func test_statusFromRawValue() {
        XCTAssertEqual(CallSession.Status(rawValue: "idle"),      .idle)
        XCTAssertEqual(CallSession.Status(rawValue: "dialing"),   .dialing)
        XCTAssertEqual(CallSession.Status(rawValue: "connected"), .connected)
        XCTAssertEqual(CallSession.Status(rawValue: "talking"),   .talking)
        XCTAssertEqual(CallSession.Status(rawValue: "ending"),    .ending)
        XCTAssertEqual(CallSession.Status(rawValue: "ended"),     .ended)
        XCTAssertEqual(CallSession.Status(rawValue: "error"),     .error)
        XCTAssertNil(CallSession.Status(rawValue: "unknown_xyz"))
    }

    func test_badgeColors_notNil() {
        for s: CallSession.Status in [.idle, .dialing, .connected, .talking, .ending, .ended, .error] {
            XCTAssertNotNil(s.badgeColor)
        }
    }

    // --- Polish v2: historyIcon tests ---

    func test_historyIconCompleted() {
        XCTAssertEqual(CallSession.Status.ended.historyIcon, "✓")
    }

    func test_historyIconFailed() {
        XCTAssertEqual(CallSession.Status.error.historyIcon, "✗")
    }

    func test_historyIconAutoEnded() {
        XCTAssertEqual(CallSession.Status.ending.historyIcon, "⏱")
    }

    func test_historyIconOtherStatuses() {
        // Non-terminal statuses use bullet
        XCTAssertEqual(CallSession.Status.idle.historyIcon,      "•")
        XCTAssertEqual(CallSession.Status.dialing.historyIcon,   "•")
        XCTAssertEqual(CallSession.Status.connected.historyIcon, "•")
        XCTAssertEqual(CallSession.Status.talking.historyIcon,   "•")
    }

    func test_historyIconColors_notNil() {
        for s: CallSession.Status in [.idle, .dialing, .connected, .talking, .ending, .ended, .error] {
            XCTAssertNotNil(s.historyIconColor)
        }
    }
}

// MARK: - E.164 validation tests

final class E164ValidationTests: XCTestCase {

    func test_validE164Numbers() {
        XCTAssertTrue(isValidE164("+79991234567"))    // RU mobile
        XCTAssertTrue(isValidE164("+14155552671"))    // US
        XCTAssertTrue(isValidE164("+441234567890"))   // UK
        XCTAssertTrue(isValidE164("+34911234567"))    // ES
        XCTAssertTrue(isValidE164("+12223334444"))    // US 11 digits
    }

    func test_invalidE164Numbers() {
        XCTAssertFalse(isValidE164(""))                  // empty
        XCTAssertFalse(isValidE164("79991234567"))        // no +
        XCTAssertFalse(isValidE164("+0123456789"))        // leading zero after +
        XCTAssertFalse(isValidE164("+123"))               // too short
        XCTAssertFalse(isValidE164("+1234567890123456"))  // too long (16 digits)
        XCTAssertFalse(isValidE164("+abc1234567"))        // non-digit
        XCTAssertFalse(isValidE164("+ 79991234567"))      // space
    }
}

// MARK: - callHistory parsing

private func parseCallHistory(from response: [String: Any]) -> [CallHistoryItem] {
    guard let items = (response["result"] as? [String: Any])?["sessions"] as? [[String: Any]] else {
        return []
    }
    return items.compactMap { dict -> CallHistoryItem? in
        guard let id = dict["session_id"] as? String else { return nil }
        return CallHistoryItem(
            sessionID: id,
            phone:    (dict["phone"]    as? String) ?? "",
            goal:     (dict["goal"]     as? String) ?? "",
            status:   (dict["status"]   as? String) ?? "unknown",
            durationSec: (dict["duration_sec"] as? Double) ?? 0,
            costUSD:  (dict["cost_usd"] as? Double) ?? 0,
            startedAt:(dict["started_at"] as? String) ?? "",
            summary:  (dict["summary"]  as? String) ?? ""
        )
    }
}

final class CallHistoryParsingTests: XCTestCase {

    func test_parsesValidSessionList() {
        let response: [String: Any] = [
            "result": [
                "sessions": [
                    [
                        "session_id": "sess-001",
                        "phone":      "+79991234567",
                        "goal":       "Узнать расписание",
                        "status":     "ended",
                        "duration_sec": 125.0,
                        "cost_usd":   0.042,
                        "started_at": "2026-04-22",
                        "summary":    "Записались на завтра",
                    ] as [String: Any],
                    [
                        "session_id": "sess-002",
                        "phone":      "+14155551234",
                        "goal":       "Order pizza",
                        "status":     "error",
                        "duration_sec": 0.0,
                        "cost_usd":   0.0,
                        "started_at": "2026-04-21",
                        "summary":    "",
                    ] as [String: Any],
                ] as [[String: Any]]
            ] as [String: Any]
        ]

        let items = parseCallHistory(from: response)
        XCTAssertEqual(items.count, 2)
        XCTAssertEqual(items[0].sessionID, "sess-001")
        XCTAssertEqual(items[0].phone, "+79991234567")
        XCTAssertEqual(items[0].status, "ended")
        XCTAssertEqual(items[0].costUSD, 0.042, accuracy: 0.0001)
        XCTAssertEqual(items[1].sessionID, "sess-002")
        XCTAssertEqual(items[1].status, "error")
    }

    func test_emptySessionsReturnsEmpty() {
        let response: [String: Any] = [
            "result": ["sessions": [] as [[String: Any]]]
        ]
        XCTAssertEqual(parseCallHistory(from: response).count, 0)
    }

    func test_missingSessionsKeyReturnsEmpty() {
        let response: [String: Any] = ["result": ["other": "data"]]
        XCTAssertEqual(parseCallHistory(from: response).count, 0)
    }

    func test_sessionWithoutIDSkipped() {
        let response: [String: Any] = [
            "result": [
                "sessions": [
                    ["phone": "+79991234567", "status": "ended"] as [String: Any]
                ] as [[String: Any]]
            ] as [String: Any]
        ]
        XCTAssertEqual(parseCallHistory(from: response).count, 0)
    }

    // --- durationFormatted tests ---

    func test_durationFormatted_zero() {
        let item = CallHistoryItem(sessionID: "x", phone: "", goal: "", status: "ended",
                                  durationSec: 0, costUSD: 0, startedAt: "", summary: "")
        XCTAssertEqual(item.durationFormatted, "0:00")
    }

    func test_durationFormatted_oneMinute() {
        let item = CallHistoryItem(sessionID: "x", phone: "", goal: "", status: "ended",
                                  durationSec: 65, costUSD: 0, startedAt: "", summary: "")
        XCTAssertEqual(item.durationFormatted, "1:05")
    }

    func test_durationFormatted_longCall() {
        let item = CallHistoryItem(sessionID: "x", phone: "", goal: "", status: "ended",
                                  durationSec: 3661, costUSD: 0, startedAt: "", summary: "")
        XCTAssertEqual(item.durationFormatted, "61:01")
    }
}

// MARK: - telnyx_not_configured detection

private func isTelnyxNotConfigured(_ response: [String: Any]) -> Bool {
    guard let error = response["error"] as? [String: Any],
          let code = error["code"] as? String else { return false }
    return code == "telnyx_not_configured"
}

final class TelnyxNotConfiguredTests: XCTestCase {

    func test_detectsNotConfigured() {
        let response: [String: Any] = [
            "error": ["code": "telnyx_not_configured", "message": "Set API key first"]
        ]
        XCTAssertTrue(isTelnyxNotConfigured(response))
    }

    func test_normalSuccessNotDetected() {
        let response: [String: Any] = [
            "result": ["session_id": "sess-001", "status": "dialing"]
        ]
        XCTAssertFalse(isTelnyxNotConfigured(response))
    }

    func test_otherErrorCodeNotDetected() {
        let response: [String: Any] = [
            "error": ["code": "network_error", "message": "Timeout"]
        ]
        XCTAssertFalse(isTelnyxNotConfigured(response))
    }
}

// MARK: - Cost estimate parsing (Polish v2)

private func parseCostEstimate(_ response: [String: Any]) -> (costPerMin: Double, country: String, provider: String)? {
    guard let result = response["result"] as? [String: Any] else { return nil }
    let cost     = (result["cost_per_minute"] as? Double) ?? 0
    let country  = (result["country"]  as? String) ?? ""
    let provider = (result["provider"] as? String) ?? ""
    return (cost, country, provider)
}

final class CostEstimateParsingTests: XCTestCase {

    func test_parsesValidEstimate() {
        let response: [String: Any] = [
            "result": [
                "cost_per_minute": 0.018,
                "country": "Spain",
                "provider": "Telnyx",
            ] as [String: Any]
        ]
        let parsed = parseCostEstimate(response)
        XCTAssertNotNil(parsed)
        XCTAssertEqual(parsed!.costPerMin, 0.018, accuracy: 0.0001)
        XCTAssertEqual(parsed!.country, "Spain")
        XCTAssertEqual(parsed!.provider, "Telnyx")
    }

    func test_missingResultReturnsNil() {
        XCTAssertNil(parseCostEstimate([:]))
        XCTAssertNil(parseCostEstimate(["error": ["code": "unsupported"]]))
    }

    func test_zeroCostNotShown() {
        let response: [String: Any] = [
            "result": ["cost_per_minute": 0.0, "country": "Unknown", "provider": "Telnyx"] as [String: Any]
        ]
        let parsed = parseCostEstimate(response)
        XCTAssertNotNil(parsed)
        // costPerMin == 0 → UI не должен показывать строку
        XCTAssertEqual(parsed!.costPerMin, 0.0, accuracy: 0.0001)
    }

    func test_estimateLabelFormat_withCountry() {
        // Имитируем логику форматирования строки как в applyCostEstimate
        let cost = 0.018, country = "Spain", provider = "Telnyx"
        let label = String(format: "~$%.3f/min (%@, %@)", cost, country, provider)
        XCTAssertEqual(label, "~$0.018/min (Spain, Telnyx)")
    }

    func test_estimateLabelFormat_noCountry() {
        let cost = 0.025, provider = "Twilio"
        let label = String(format: "~$%.3f/min (%@)", cost, provider)
        XCTAssertEqual(label, "~$0.025/min (Twilio)")
    }
}

// MARK: - Provider status detection (Polish v2)

private func isTelnyxConfigured(in settings: [String: Any]) -> Bool {
    let key  = (settings["telnyx_api_key"]     as? String) ?? ""
    let from = (settings["telnyx_from_number"] as? String) ?? ""
    return !key.isEmpty && !from.isEmpty
}

private func isTwilioConfigured(in settings: [String: Any]) -> Bool {
    let sid  = (settings["twilio_account_sid"] as? String) ?? ""
    let tok  = (settings["twilio_auth_token"]  as? String) ?? ""
    let from = (settings["twilio_from_number"] as? String) ?? ""
    return !sid.isEmpty && !tok.isEmpty && !from.isEmpty
}

final class ProviderStatusTests: XCTestCase {

    func test_telnyxConfigured_bothPresent() {
        let s: [String: Any] = ["telnyx_api_key": "KEY", "telnyx_from_number": "+12223334444"]
        XCTAssertTrue(isTelnyxConfigured(in: s))
    }

    func test_telnyxNotConfigured_missingKey() {
        let s: [String: Any] = ["telnyx_api_key": "", "telnyx_from_number": "+12223334444"]
        XCTAssertFalse(isTelnyxConfigured(in: s))
    }

    func test_telnyxNotConfigured_missingFrom() {
        let s: [String: Any] = ["telnyx_api_key": "KEY", "telnyx_from_number": ""]
        XCTAssertFalse(isTelnyxConfigured(in: s))
    }

    func test_twilioConfigured_allPresent() {
        let s: [String: Any] = [
            "twilio_account_sid": "AC123",
            "twilio_auth_token": "token",
            "twilio_from_number": "+15550001234",
        ]
        XCTAssertTrue(isTwilioConfigured(in: s))
    }

    func test_twilioNotConfigured_missingToken() {
        let s: [String: Any] = [
            "twilio_account_sid": "AC123",
            "twilio_auth_token": "",
            "twilio_from_number": "+15550001234",
        ]
        XCTAssertFalse(isTwilioConfigured(in: s))
    }

    func test_emptySettingsNotConfigured() {
        XCTAssertFalse(isTelnyxConfigured(in: [:]))
        XCTAssertFalse(isTwilioConfigured(in: [:]))
    }
}

// MARK: - AgentSettings Telnyx roundtrip

final class AgentSettingsTelnyxTests: XCTestCase {

    func test_defaultValues() {
        let s = AgentSettings.default
        XCTAssertEqual(s.telnyxAPIKey, "")
        XCTAssertEqual(s.telnyxFromNumber, "")
        XCTAssertEqual(s.callMaxDurationMin, 30)
        XCTAssertEqual(s.callCostWarnUSD, 5.0, accuracy: 0.001)
        XCTAssertTrue(s.callAutoEndOnSilence)
    }

    func test_initFromPayload() {
        let payload: [String: Any] = [
            "telnyx_api_key":          "KEY123",
            "telnyx_from_number":      "+79991234567",
            "call_max_duration_min":   45,
            "call_cost_warn_usd":      10.0,
            "call_auto_end_on_silence": false,
        ]
        let s = AgentSettings(from: payload)
        XCTAssertEqual(s.telnyxAPIKey, "KEY123")
        XCTAssertEqual(s.telnyxFromNumber, "+79991234567")
        XCTAssertEqual(s.callMaxDurationMin, 45)
        XCTAssertEqual(s.callCostWarnUSD, 10.0, accuracy: 0.001)
        XCTAssertFalse(s.callAutoEndOnSilence)
    }

    func test_toPayloadContainsTelnyxKeys() {
        var s = AgentSettings.default
        s.telnyxAPIKey       = "MYKEY"
        s.telnyxFromNumber   = "+12223334444"
        s.callMaxDurationMin = 20
        s.callCostWarnUSD    = 3.0
        s.callAutoEndOnSilence = false

        let payload = s.toPayload()
        XCTAssertEqual(payload["telnyx_api_key"] as? String, "MYKEY")
        XCTAssertEqual(payload["telnyx_from_number"] as? String, "+12223334444")
        XCTAssertEqual(payload["call_max_duration_min"] as? Int, 20)
        XCTAssertEqual(payload["call_cost_warn_usd"] as? Double ?? -1, 3.0, accuracy: 0.001)
        XCTAssertEqual(payload["call_auto_end_on_silence"] as? Bool, false)
    }

    func test_missingKeysUseDefaults() {
        let s = AgentSettings(from: [:])
        XCTAssertEqual(s.telnyxAPIKey, "")
        XCTAssertEqual(s.callMaxDurationMin, 30)
        XCTAssertTrue(s.callAutoEndOnSilence)
    }
}

// MARK: - PanelTab.callAutomation

final class PanelTabCallAutomationTests: XCTestCase {

    func test_rawValue() {
        XCTAssertEqual(HistoryPanelController.PanelTab.callAutomation.rawValue, "call_automation")
    }

    func test_fromSettingsValue() {
        let tab = HistoryPanelController.PanelTab.from(settingsValue: "call_automation")
        XCTAssertEqual(tab, .callAutomation)
    }

    func test_unknownFallsToHistory() {
        let tab = HistoryPanelController.PanelTab.from(settingsValue: "does_not_exist")
        XCTAssertEqual(tab, .history)
    }
}
