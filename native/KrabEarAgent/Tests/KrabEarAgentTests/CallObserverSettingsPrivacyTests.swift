import XCTest
@testable import KrabEarAgent

/// Privacy fail-closed в `IPCCallObserverSettings`: провал IPC `get_settings`
/// не равен privacy OFF — иначе HUD/autoplay звонка открываются при таймауте backend.
final class CallObserverSettingsPrivacyTests: XCTestCase {
    func test_ipc_failure_is_privacy_on_and_no_autoplay() {
        let r = IPCCallObserverSettings.resolveSettings(response: nil)
        XCTAssertTrue(r.privacy)
        XCTAssertFalse(r.autoplay)
    }

    func test_response_without_result_dict_is_privacy_on() {
        let r = IPCCallObserverSettings.resolveSettings(response: ["ok": false])
        XCTAssertTrue(r.privacy)
        XCTAssertFalse(r.autoplay)
    }

    func test_privacy_flag_of_wrong_type_is_privacy_on() {
        let r = IPCCallObserverSettings.resolveSettings(
            response: ["result": ["privacy_mode_enabled": "false"]])
        XCTAssertTrue(r.privacy)
    }

    func test_explicit_privacy_off_is_honoured() {
        let r = IPCCallObserverSettings.resolveSettings(response: ["result": [
            "privacy_mode_enabled": false,
            "call_observer_hud_enabled": false,
            "call_observer_autoplay_audio": true,
        ]])
        XCTAssertFalse(r.privacy)
        XCTAssertFalse(r.hud)
        XCTAssertTrue(r.autoplay)
    }

    func test_explicit_privacy_on_is_honoured() {
        let r = IPCCallObserverSettings.resolveSettings(
            response: ["result": ["privacy_mode_enabled": true]])
        XCTAssertTrue(r.privacy)
    }

    func test_absent_privacy_key_keeps_new_profile_default_off() {
        let r = IPCCallObserverSettings.resolveSettings(response: ["result": [:] as [String: Any]])
        XCTAssertFalse(r.privacy)
        XCTAssertTrue(r.hud)
        XCTAssertFalse(r.autoplay)
    }

    /// refresh() обязан идти через resolveSettings, а не держать локальный `?? false`.
    func test_refresh_uses_resolveSettings() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/main+CallObserver.swift")
        let src = try String(contentsOf: root, encoding: .utf8)
        XCTAssertTrue(src.contains("resolveSettings(response:"))
        XCTAssertFalse(src.contains("privacy = s[\"privacy_mode_enabled\"] as? Bool ?? false"))
    }
}
