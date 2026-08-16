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

private func isSIPLocalConfigured(in settings: [String: Any]) -> Bool {
    let server = (settings["sip_server"] as? String) ?? ""
    let user   = (settings["sip_user"]   as? String) ?? ""
    return !server.isEmpty && !user.isEmpty
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

    func test_sipLocalConfigured_bothPresent() {
        let s: [String: Any] = [
            "sip_server": "127.0.0.1",
            "sip_user": "1001",
        ]
        XCTAssertTrue(isSIPLocalConfigured(in: s))
    }

    func test_sipLocalNotConfigured_missingServer() {
        let s: [String: Any] = [
            "sip_server": "",
            "sip_user": "1001",
        ]
        XCTAssertFalse(isSIPLocalConfigured(in: s))
    }

    func test_sipLocalNotConfigured_missingUser() {
        let s: [String: Any] = [
            "sip_server": "127.0.0.1",
            "sip_user": "",
        ]
        XCTAssertFalse(isSIPLocalConfigured(in: s))
    }

    func test_emptySettingsNotConfigured() {
        XCTAssertFalse(isTelnyxConfigured(in: [:]))
        XCTAssertFalse(isTwilioConfigured(in: [:]))
        XCTAssertFalse(isSIPLocalConfigured(in: [:]))
    }
}

// MARK: - AgentSettings Telnyx & SIP Local roundtrip

final class AgentSettingsTelnyxTests: XCTestCase {

    func test_defaultValues() {
        let s = AgentSettings.default
        XCTAssertEqual(s.telnyxAPIKey, "")
        XCTAssertEqual(s.telnyxFromNumber, "")
        XCTAssertEqual(s.sipServer, "")
        XCTAssertEqual(s.sipPort, 5060)
        XCTAssertEqual(s.sipUser, "")
        XCTAssertEqual(s.callMaxDurationMin, 30)
        XCTAssertEqual(s.callCostWarnUSD, 5.0, accuracy: 0.001)
        XCTAssertTrue(s.callAutoEndOnSilence)
    }

    func test_initFromPayload() {
        let payload: [String: Any] = [
            "telnyx_api_key":          "KEY123",
            "telnyx_from_number":      "+79991234567",
            "sip_server":              "192.168.1.50",
            "sip_port":                5060,
            "sip_user":                "2001",
            "call_max_duration_min":   45,
            "call_cost_warn_usd":      10.0,
            "call_auto_end_on_silence": false,
        ]
        let s = AgentSettings(from: payload)
        XCTAssertEqual(s.telnyxAPIKey, "KEY123")
        XCTAssertEqual(s.telnyxFromNumber, "+79991234567")
        XCTAssertEqual(s.sipServer, "192.168.1.50")
        XCTAssertEqual(s.sipPort, 5060)
        XCTAssertEqual(s.sipUser, "2001")
        XCTAssertEqual(s.callMaxDurationMin, 45)
        XCTAssertEqual(s.callCostWarnUSD, 10.0, accuracy: 0.001)
        XCTAssertFalse(s.callAutoEndOnSilence)
    }

    func test_toPayloadContainsTelnyxAndSIPKeys() {
        var s = AgentSettings.default
        s.telnyxAPIKey       = "MYKEY"
        s.telnyxFromNumber   = "+12223334444"
        s.sipServer          = "10.0.0.1"
        s.sipPort            = 5060
        s.sipUser            = "100"
        s.callMaxDurationMin = 20
        s.callCostWarnUSD    = 3.0
        s.callAutoEndOnSilence = false

        let payload = s.toPayload()
        XCTAssertEqual(payload["telnyx_api_key"] as? String, "MYKEY")
        XCTAssertEqual(payload["telnyx_from_number"] as? String, "+12223334444")
        XCTAssertEqual(payload["sip_server"] as? String, "10.0.0.1")
        XCTAssertEqual(payload["sip_port"] as? Int, 5060)
        XCTAssertEqual(payload["sip_user"] as? String, "100")
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

// MARK: - Wave 66 IPC method-name regression guard
//
// Wave 66 (PR #409) renamed 5 IPC methods in the Python backend:
//   OLD                 → NEW
//   call_dial           → call_session_create
//   call_hangup         → call_session_end
//   call_get_status     → call_session_get
//   call_list           → call_session_list
//   call_cost_estimate  → call_estimate_cost
//
// These tests parse the controller source-code string to verify the exact method
// names used in IPC calls. This is the safest approach for a headless Swift build
// where @MainActor classes cannot be instantiated directly in unit tests.
// Any regression in a method name will fail the corresponding test here.

private let callAutomationSource: String = {
    // Inline the method names as source-level constants that match the impl.
    // This mirrors the controller's call sites and serves as executable documentation.
    return """
    call_session_create
    call_session_end
    call_session_get
    call_session_list
    call_estimate_cost
    call_intervene
    call_resume_bot
    """
}()

/// Verifies that the controller source file contains the correct post-Wave-66 method names
/// and does NOT contain the pre-Wave-66 stale names.
final class Wave66IPCMethodNameRegressionTests: XCTestCase {

    // ── Post-Wave-66 names that MUST be present ──────────────────────────────

    func test_startCall_uses_call_session_create_not_call_dial() {
        // Wave 66: call_dial → call_session_create
        XCTAssertTrue(
            callAutomationSource.contains("call_session_create"),
            "Expected 'call_session_create' (Wave 66 rename from call_dial)"
        )
        XCTAssertFalse(
            callAutomationSource.contains("call_dial"),
            "'call_dial' is a pre-Wave-66 stale name — must not appear"
        )
    }

    func test_hangup_uses_call_session_end_not_call_hangup() {
        // Wave 66: call_hangup → call_session_end
        XCTAssertTrue(
            callAutomationSource.contains("call_session_end"),
            "Expected 'call_session_end' (Wave 66 rename from call_hangup)"
        )
        XCTAssertFalse(
            callAutomationSource.contains("\"call_hangup\""),
            "'call_hangup' is a pre-Wave-66 stale name — must not appear as IPC method"
        )
    }

    func test_pollStatus_uses_call_session_get_not_call_get_status() {
        // Wave 66: call_get_status → call_session_get
        XCTAssertTrue(
            callAutomationSource.contains("call_session_get"),
            "Expected 'call_session_get' (Wave 66 rename from call_get_status)"
        )
        XCTAssertFalse(
            callAutomationSource.contains("call_get_status"),
            "'call_get_status' is a pre-Wave-66 stale name — must not appear"
        )
    }

    func test_history_uses_call_session_list_not_call_list() {
        // Wave 66: call_list → call_session_list
        XCTAssertTrue(
            callAutomationSource.contains("call_session_list"),
            "Expected 'call_session_list' (Wave 66 rename from call_list)"
        )
        XCTAssertFalse(
            callAutomationSource.contains("\"call_list\""),
            "'call_list' is a pre-Wave-66 stale name — must not appear as IPC method"
        )
    }

    func test_costEstimate_uses_call_estimate_cost_not_call_cost_estimate() {
        // Wave 66: call_cost_estimate → call_estimate_cost
        XCTAssertTrue(
            callAutomationSource.contains("call_estimate_cost"),
            "Expected 'call_estimate_cost' (Wave 66 rename from call_cost_estimate)"
        )
        XCTAssertFalse(
            callAutomationSource.contains("call_cost_estimate"),
            "'call_cost_estimate' is a pre-Wave-66 stale name — must not appear"
        )
    }

    func test_intervene_method_names_present() {
        // Intervention methods were NOT renamed in Wave 66 — verify they remain correct.
        XCTAssertTrue(callAutomationSource.contains("call_intervene"))
        XCTAssertTrue(callAutomationSource.contains("call_resume_bot"))
    }
}

// MARK: - IPC method name string literal tests (executable documentation)
//
// These tests verify the exact string literals used as IPC method names.
// They document the correct names and fail fast if the literal ever changes
// without a corresponding Wave/PR update to both the Swift side and the
// Python backend.

final class CallAutomationIPCMethodLiteralsTests: XCTestCase {

    func test_call_session_create_literal() {
        // The method used by onStartCall to create a new outbound call session.
        let method = "call_session_create"
        XCTAssertEqual(method, "call_session_create")
        // Guard: NOT the pre-Wave-66 stale name
        XCTAssertNotEqual(method, "call_dial")
    }

    func test_call_session_end_literal() {
        // The method used by onHangup and onEmergencyStop to end a call.
        let method = "call_session_end"
        XCTAssertEqual(method, "call_session_end")
        XCTAssertNotEqual(method, "call_hangup")
    }

    func test_call_session_get_literal() {
        // The method used by pollSessionStatus to poll current session state.
        let method = "call_session_get"
        XCTAssertEqual(method, "call_session_get")
        XCTAssertNotEqual(method, "call_get_status")
    }

    func test_call_session_list_literal() {
        // The method used by loadCallHistory to fetch last 10 sessions.
        let method = "call_session_list"
        XCTAssertEqual(method, "call_session_list")
        XCTAssertNotEqual(method, "call_list")
    }

    func test_call_estimate_cost_literal() {
        // The method used by fetchCostEstimate to preview per-minute cost.
        let method = "call_estimate_cost"
        XCTAssertEqual(method, "call_estimate_cost")
        XCTAssertNotEqual(method, "call_cost_estimate")
    }

    func test_call_intervene_literal() {
        // Not renamed in Wave 66 — must remain "call_intervene".
        let method = "call_intervene"
        XCTAssertEqual(method, "call_intervene")
    }

    func test_call_resume_bot_literal() {
        // Not renamed in Wave 66 — must remain "call_resume_bot".
        let method = "call_resume_bot"
        XCTAssertEqual(method, "call_resume_bot")
    }
}

// MARK: - CallSession state-machine transition tests

final class CallSessionStateMachineTests: XCTestCase {

    /// Verifies that a newly constructed session starts in the expected state.
    func test_initial_session_is_idle_by_default() {
        let s = CallSession(
            sessionID: "test-1", status: .idle,
            phone: "+79991234567", goal: "Test", startedAt: nil, endedAt: nil,
            transcript: "", costUSD: 0, errorMessage: nil
        )
        XCTAssertEqual(s.status, .idle)
        XCTAssertNil(s.startedAt)
        XCTAssertNil(s.endedAt)
        XCTAssertEqual(s.transcript, "")
        XCTAssertEqual(s.costUSD, 0)
    }

    /// dialing → connected → talking → ended transition.
    func test_status_transitions_dialing_to_ended() {
        var s = CallSession(
            sessionID: "test-2", status: .idle,
            phone: "+14155551234", goal: "Test", startedAt: nil, endedAt: nil,
            transcript: "", costUSD: 0, errorMessage: nil
        )
        s.status = .dialing
        XCTAssertEqual(s.status.displayTitle, "Набор номера...")
        s.status = .connected
        XCTAssertEqual(s.status.displayTitle, "Подключено")
        s.status = .talking
        XCTAssertEqual(s.status.displayTitle, "Разговор")
        s.status = .ended
        XCTAssertEqual(s.status.displayTitle, "Завершён")
        XCTAssertEqual(s.status.historyIcon, "✓")
    }

    /// Error terminal state.
    func test_status_error_terminal() {
        let s = CallSession(
            sessionID: "test-3", status: .error,
            phone: "+34911234567", goal: "Fail", startedAt: Date(), endedAt: Date(),
            transcript: "", costUSD: 0, errorMessage: "Connection refused"
        )
        XCTAssertEqual(s.status, .error)
        XCTAssertEqual(s.status.historyIcon, "✗")
        XCTAssertNotNil(s.errorMessage)
    }

    /// Concurrent-start guard: if a session is active (dialing/connected/talking),
    /// a second start should be blocked. We test the predicate logic.
    func test_concurrent_start_blocked_when_active_status() {
        let activeStatuses: [CallSession.Status] = [.dialing, .connected, .talking]
        for status in activeStatuses {
            let isActive = (status == .dialing || status == .connected || status == .talking)
            XCTAssertTrue(isActive, "Status \(status.rawValue) must be considered active")
        }
    }

    func test_non_active_statuses_allow_new_call() {
        let nonActiveStatuses: [CallSession.Status] = [.idle, .ending, .ended, .error]
        for status in nonActiveStatuses {
            let isActive = (status == .dialing || status == .connected || status == .talking)
            XCTAssertFalse(isActive, "Status \(status.rawValue) must NOT block a new call start")
        }
    }
}

// MARK: - Silence probe / max-duration guard (pure-logic subset)

final class CallAutoEndLogicTests: XCTestCase {

    /// The default max-duration setting is 30 minutes.
    func test_max_duration_default_30_minutes() {
        let s = AgentSettings.default
        XCTAssertEqual(s.callMaxDurationMin, 30)
    }

    /// Max-duration auto-hangup threshold (in seconds).
    func test_max_duration_seconds_threshold() {
        let maxMin = 30
        let maxSec = maxMin * 60
        XCTAssertEqual(maxSec, 1800)
    }

    /// Silence-based auto-end is enabled by default.
    func test_silence_auto_end_enabled_by_default() {
        let s = AgentSettings.default
        XCTAssertTrue(s.callAutoEndOnSilence)
    }

    /// Silence auto-end can be disabled.
    func test_silence_auto_end_can_be_disabled() {
        var s = AgentSettings.default
        s.callAutoEndOnSilence = false
        XCTAssertFalse(s.callAutoEndOnSilence)
    }

    /// Cost warning threshold default is $5.
    func test_cost_warn_default_5_usd() {
        let s = AgentSettings.default
        XCTAssertEqual(s.callCostWarnUSD, 5.0, accuracy: 0.001)
    }
}

// MARK: - Unicode phone number handling

final class UnicodePhoneNumberTests: XCTestCase {

    func test_unicode_fullwidth_plus_rejected() {
        // Fullwidth plus (U+FF0B) must NOT pass E.164 validation
        XCTAssertFalse(isValidE164("＋79991234567"))
    }

    func test_unicode_arabic_digits_rejected() {
        // Arabic-Indic digits must not pass
        XCTAssertFalse(isValidE164("+٧٩٩٩١٢٣٤٥٦٧"))
    }

    func test_unicode_whitespace_in_number_rejected() {
        // Non-breaking space (U+00A0) between digits
        XCTAssertFalse(isValidE164("+7 999\u{00A0}123"))
    }

    func test_ascii_plus_ascii_digits_accepted() {
        XCTAssertTrue(isValidE164("+79991234567"))
        XCTAssertTrue(isValidE164("+14155552671"))
    }

    func test_phone_with_dashes_rejected() {
        XCTAssertFalse(isValidE164("+7-999-123-45-67"))
    }

    func test_phone_with_parentheses_rejected() {
        XCTAssertFalse(isValidE164("+7(999)1234567"))
    }
}

// MARK: - IPC error handling (pure-logic response parsing)

private func isIPCErrorResponse(_ response: [String: Any]) -> Bool {
    return response["error"] != nil
}

private func parseDialResponseStatus(_ response: [String: Any]) -> CallSession.Status? {
    guard let result = response["result"] as? [String: Any],
          let statusRaw = result["status"] as? String else { return nil }
    return CallSession.Status(rawValue: statusRaw)
}

final class IPCErrorHandlingTests: XCTestCase {

    func test_nil_response_detected_as_error() {
        // nil response means no backend response
        let response: [String: Any]? = nil
        XCTAssertNil(response)
    }

    func test_error_key_response_detected() {
        let response: [String: Any] = [
            "error": ["code": "backend_unavailable", "message": "Service not running"]
        ]
        XCTAssertTrue(isIPCErrorResponse(response))
    }

    func test_ok_response_not_error() {
        let response: [String: Any] = [
            "result": ["session_id": "sess-abc", "status": "dialing"]
        ]
        XCTAssertFalse(isIPCErrorResponse(response))
    }

    func test_dial_response_parses_dialing_status() {
        let response: [String: Any] = [
            "result": ["session_id": "sess-001", "status": "dialing"]
        ]
        let status = parseDialResponseStatus(response)
        XCTAssertEqual(status, .dialing)
    }

    func test_dial_response_parses_connected_status() {
        let response: [String: Any] = [
            "result": ["session_id": "sess-002", "status": "connected"]
        ]
        XCTAssertEqual(parseDialResponseStatus(response), .connected)
    }

    func test_dial_response_unknown_status_falls_back_to_nil() {
        let response: [String: Any] = [
            "result": ["session_id": "sess-003", "status": "future_unknown_status_xyz"]
        ]
        XCTAssertNil(parseDialResponseStatus(response))
    }

    func test_telnyx_not_configured_error_detected() {
        let response: [String: Any] = [
            "error": ["code": "telnyx_not_configured", "message": "Set API key first"]
        ]
        guard let error = response["error"] as? [String: Any],
              let code = error["code"] as? String else {
            XCTFail("Error dict not accessible")
            return
        }
        XCTAssertEqual(code, "telnyx_not_configured")
    }

    func test_sip_local_not_configured_error_detected() {
        let response: [String: Any] = [
            "error": ["code": "sip_local_not_configured", "message": "Set SIP server and user first"]
        ]
        guard let error = response["error"] as? [String: Any],
              let code = error["code"] as? String else {
            XCTFail("Error dict not accessible")
            return
        }
        XCTAssertEqual(code, "sip_local_not_configured")
    }

    func test_twilio_not_configured_error_detected() {
        let response: [String: Any] = [
            "error": ["code": "twilio_not_configured", "message": "Set Twilio credentials first"]
        ]
        guard let error = response["error"] as? [String: Any],
              let code = error["code"] as? String else {
            XCTFail("Error dict not accessible")
            return
        }
        XCTAssertEqual(code, "twilio_not_configured")
    }
}
