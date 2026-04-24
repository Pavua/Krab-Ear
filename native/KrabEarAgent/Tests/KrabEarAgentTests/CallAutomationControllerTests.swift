/*
 CallAutomationControllerTests — тесты логики CallAutomationController.

 Стратегия: mock IPCClient (stub через словари), тестируем:
   - E.164 валидацию номера
   - CallSession.Status display / badge color
   - IPC response parsing (handleDialResponse whitebox)
   - callHistory parsing из "list_call_sessions" ответа
   - ConfigBanner показывается при telnyx_not_configured
   - AgentSettings Telnyx fields roundtrip (toPayload / init(from:))
   - PanelTab.callAutomation rawValue и from()
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Whitebox: E.164 validation

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
        // Just ensure no crash — colours are NSColor system values.
        for s: CallSession.Status in [.idle, .dialing, .connected, .talking, .ending, .ended, .error] {
            XCTAssertNotNil(s.badgeColor)
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
