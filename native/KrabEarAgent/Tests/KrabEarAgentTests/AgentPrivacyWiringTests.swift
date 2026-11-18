import AppKit
import XCTest
@testable import KrabEarAgent

@MainActor
final class AgentPrivacyWiringTests: XCTestCase {
    private actor Backend: IPCSocketProviding {
        var privacy = false
        var timeoutWrite = false
        var timeoutRead = false
        var holdNext: XCTestExpectation?
        var continuation: CheckedContinuation<Void, Never>?
        private(set) var reads = 0

        func setTimeoutRead(_ value: Bool) { timeoutRead = value }
        func holdNextRead(_ started: XCTestExpectation) { holdNext = started }
        func releaseRead() { continuation?.resume(); continuation = nil }
        func setTimeoutWrite(_ value: Bool) { timeoutWrite = value }
        func setExternalPrivacy(_ value: Bool) { privacy = value }

        func send(payload: Data, timeoutSec: Int) async throws -> Data {
            let request = try JSONSerialization.jsonObject(with: payload) as? [String: Any]
            var observed = privacy
            switch request?["method"] as? String {
            case "get_settings":
                reads += 1
                if timeoutRead { throw IPCError.timeout }
                if let started = holdNext {
                    holdNext = nil
                    await withCheckedContinuation { continuation in
                        self.continuation = continuation
                        started.fulfill()
                    }
                }
            case "set_settings", "apply_config_preset", "restore_settings_backup":
                if timeoutWrite { throw IPCError.timeout }
                let patch = request?["params"] as? [String: Any]
                if let flag = patch?["privacy_mode_enabled"] as? Bool { privacy = flag }
                observed = privacy
            default:
                throw IPCError.invalidResponse
            }
            return try JSONSerialization.data(withJSONObject: [
                "ok": true, "result": ["privacy_mode_enabled": observed, "overlay_opacity_percent": 12]
            ])
        }
    }

    private let defaults = IsolatedUserDefaultsDomain(scope: "AgentPrivacyWiringTests")
    override func tearDown() async throws {
        defaults.removePersistentDomain()
        try await super.tearDown()
    }

    private func delegate(_ backend: Backend) -> AgentAppDelegate {
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: []), userDefaults: defaults.defaults)
        delegate.ipcClient = IPCClient(socketProvider: backend)
        return delegate
    }

    func testDashboardPreservesUnknownFlagsAndShowsMixedUnavailableToggle() throws {
        for value: Any in [NSNull(), NSNumber(value: 0), "false"] {
            let data = try XCTUnwrap(PrivacyDashboardData(result: [
                "ok": true, "privacy_mode": value, "encryption_enabled": value,
            ]))
            XCTAssertNil(data.privacyMode)
            XCTAssertNil(data.encryptionEnabled)
            let toggle = HistoryPanelController.pdPrivacyModeToggle(on: data.privacyMode, target: nil)
            XCTAssertEqual(toggle.state, .mixed)
            XCTAssertFalse(toggle.isEnabled)
        }
        let known = try XCTUnwrap(PrivacyDashboardData(result: [
            "ok": true, "privacy_mode": false, "encryption_enabled": true,
        ]))
        XCTAssertEqual(known.encryptionEnabled, true)
        let toggle = HistoryPanelController.pdPrivacyModeToggle(on: known.privacyMode, target: nil)
        XCTAssertEqual(toggle.state, .off)
        XCTAssertTrue(toggle.isEnabled)
    }

    func testTimeoutThenStaleFalseDoesNotReleaseLocalPrivacyIntent() async {
        let backend = Backend()
        let agent = delegate(backend)
        await backend.setTimeoutWrite(true)
        let write = agent.performSettingsMutation("set_settings", params: ["privacy_mode_enabled": true])
        XCTAssertTrue(agent.privacyModeEnabled)
        let success = await write.value
        XCTAssertFalse(success)
        XCTAssertTrue(agent.privacyModeEnabled)
        XCTAssertTrue(agent.settings.privacyModeEnabled)
    }

    func testRestartAndOrdinaryReadsKeepUncertainPresetClosedUntilExplicitOff() async {
        let backend = Backend()
        await backend.setTimeoutWrite(true)
        let first = delegate(backend)
        let uncertain = await first.performSettingsMutation("apply_config_preset", params: ["name": "custom"]).value
        XCTAssertFalse(uncertain)
        let restarted = delegate(backend)
        _ = await restarted.tickPrivacyMonitor()?.value
        XCTAssertTrue(restarted.privacyModeEnabled)
        await backend.setTimeoutWrite(false)
        _ = await restarted.performSettingsMutation("set_settings", params: ["quality_profile": "max"]).value
        XCTAssertTrue(restarted.privacyModeEnabled)
        let disabled = await restarted.performSettingsMutation("set_settings", params: ["privacy_mode_enabled": false]).value
        XCTAssertTrue(disabled)
        XCTAssertFalse(restarted.privacyModeEnabled)
    }

    func testExternalChangeAndReadFailureRestrictThenFreshReconnectAllows() async {
        let backend = Backend()
        let agent = delegate(backend)
        var now = ContinuousClock.now
        agent.privacyNow = { now }
        let opacity = agent.settings.overlayOpacityPercent
        XCTAssertTrue(agent.privacyModeEnabled)
        _ = await agent.tickPrivacyMonitor()?.value
        XCTAssertFalse(agent.privacyModeEnabled)
        XCTAssertEqual(agent.settings.overlayOpacityPercent, opacity, "Фоновый poll не применяет чужие UI-настройки")
        await backend.setExternalPrivacy(true)
        now += .seconds(5)
        _ = await agent.tickPrivacyMonitor()?.value
        XCTAssertTrue(agent.privacyModeEnabled)
        await backend.setExternalPrivacy(false)
        now += .seconds(5)
        _ = await agent.tickPrivacyMonitor()?.value
        XCTAssertFalse(agent.privacyModeEnabled)
        await backend.setTimeoutRead(true)
        now += .seconds(5)
        _ = await agent.tickPrivacyMonitor()?.value
        XCTAssertTrue(agent.privacyModeEnabled)
        await backend.setTimeoutRead(false)
        agent.invalidatePrivacyConnection()
        _ = await agent.tickPrivacyMonitor()?.value
        XCTAssertFalse(agent.privacyModeEnabled)
    }

    func testExpiredLeaseRevokesWhileReadIsInFlightAndLateResponseCannotRenewIt() async {
        let backend = Backend()
        let agent = delegate(backend)
        var now = ContinuousClock.now
        agent.privacyNow = { now }
        _ = await agent.tickPrivacyMonitor()?.value
        XCTAssertFalse(agent.privacyModeEnabled)
        let started = expectation(description: "get_settings in flight")
        await backend.holdNextRead(started)
        now += .seconds(5)
        let read = agent.tickPrivacyMonitor()
        await fulfillment(of: [started], timeout: 2)
        now += .seconds(10)
        agent.tickPrivacyMonitor()
        XCTAssertTrue(agent.privacyModeEnabled)
        await backend.releaseRead()
        _ = await read?.value
        XCTAssertTrue(agent.privacyModeEnabled)
        let reads = await backend.reads
        XCTAssertEqual(reads, 2)
    }

    func testOldReadCannotOverrideNewLocalIntentOrReconnect() async {
        let backend = Backend()
        let agent = delegate(backend)
        var now = ContinuousClock.now
        agent.privacyNow = { now }
        _ = await agent.tickPrivacyMonitor()?.value
        let started = expectation(description: "read started before disconnect")
        await backend.holdNextRead(started)
        now += .seconds(5)
        let old = agent.tickPrivacyMonitor()
        await fulfillment(of: [started], timeout: 2)
        agent.invalidatePrivacyConnection()
        XCTAssertTrue(agent.privacyModeEnabled)
        await backend.releaseRead()
        let accepted = await old?.value
        XCTAssertEqual(accepted, false)
        XCTAssertTrue(agent.privacyModeEnabled)

        let next = expectation(description: "read started before write")
        await backend.holdNextRead(next)
        let stale = agent.tickPrivacyMonitor()
        await fulfillment(of: [next], timeout: 2)
        let enable = agent.performSettingsMutation("set_settings", params: ["privacy_mode_enabled": true])
        await backend.releaseRead()
        let acceptedAfterWrite = await stale?.value
        XCTAssertEqual(acceptedAfterWrite, false)
        let enabled = await enable.value
        XCTAssertTrue(enabled)
        XCTAssertTrue(agent.privacyModeEnabled)
    }
}
