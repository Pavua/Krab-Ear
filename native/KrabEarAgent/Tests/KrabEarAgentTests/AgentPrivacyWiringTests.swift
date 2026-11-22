import AppKit
import os
import XCTest
@testable import KrabEarAgent

@MainActor
final class AgentPrivacyWiringTests: XCTestCase {
    private actor Backend: IPCSocketProviding {
        var privacy = false
        var epoch = "epoch-A"
        func setEpoch(_ value: String) { epoch = value }
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
                "ok": true, "result": ["privacy_mode_enabled": observed, "live_subs_policy_epoch": epoch, "overlay_opacity_percent": 12]
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

    func testEpochChangeStopsCaptureDropsQueuedPCMAndRequiresExplicitStart() async throws {
        let backend = Backend()
        let agent = delegate(backend)
        let queue = DispatchQueue(label: "test.epoch.delivery")
        let epochs = OSAllocatedUnfairLock(initialState: [String]())
        let identity = NSObject()
        let streamID = ObjectIdentifier(identity)
        let stopped = expectation(description: "old stream stopped")
        let capture = SystemAudioCapture(
            ipcClient: agent.ipcClient, deliveryQueue: queue,
            deliver: { method, params in
                if method == "live_subs_ingest" {
                    let epoch = params["live_subs_policy_epoch"] as? String ?? "missing"
                    epochs.withLock { $0.append(epoch) }
                }
            }, makeStream: { _ in
                SystemAudioStream(identity: streamID, start: {}, stop: { stopped.fulfill() })
            }
        )
        agent.systemAudioCapture = capture
        var now = ContinuousClock.now
        agent.privacyNow = { now }
        _ = await agent.tickPrivacyMonitor()?.value
        let oldLease = try XCTUnwrap(agent.privacyLease)
        XCTAssertTrue(capture.start(lease: oldLease))
        let startDeadline = ContinuousClock.now + .seconds(1)
        while !capture.isCapturing && .now < startDeadline { await Task.yield() }
        let samples = [Int16](repeating: 1, count: 48_000)
        let accepted = try XCTUnwrap(capture.receiveSamples(from: streamID) { (samples, 48_000) })
        await accepted.value
        queue.sync {}
        XCTAssertEqual(epochs.withLock { $0 }, ["epoch-A"])

        now += .seconds(5)
        _ = await agent.tickPrivacyMonitor()?.value
        XCTAssertTrue(agent.privacyLease === oldLease)
        XCTAssertTrue(capture.isCapturing)

        queue.suspend()
        _ = await capture.receiveSamples(from: streamID) { (samples, 48_000) }?.value
        _ = await capture.receiveSamples(from: streamID) { ([1], 48_000) }?.value
        await backend.setEpoch("epoch-B")
        now += .seconds(5)
        _ = await agent.tickPrivacyMonitor()?.value
        queue.resume()
        await fulfillment(of: [stopped], timeout: 2)
        queue.sync {}
        XCTAssertFalse(capture.isCapturing)
        XCTAssertFalse(oldLease.isValid)
        XCTAssertEqual(agent.privacyLease?.liveSubsEpoch, "epoch-B")
        XCTAssertEqual(epochs.withLock { $0 }, ["epoch-A"])
        XCTAssertNil(capture.receiveSamples(from: streamID) { XCTFail("старый PCM копируется"); return nil })
    }

    func testExpiredLeaseRejectsPCMAtIntakeAndOnBackgroundDelivery() async throws {
        let id = NSObject()
        let identity = ObjectIdentifier(id)
        let queue = DispatchQueue(label: "test.expired.pcm")
        let ingests = OSAllocatedUnfairLock(initialState: 0)
        let capture = SystemAudioCapture(
            ipcClient: IPCClient(socketPath: "/unused-e8-test.sock"), deliveryQueue: queue,
            deliver: { method, _ in if method == "live_subs_ingest" { ingests.withLock { $0 += 1 } } },
            makeStream: { _ in SystemAudioStream(identity: identity, start: {}, stop: {}) }
        )
        let lease = ConfirmedPrivacyLease(until: .now + .milliseconds(300), liveSubsEpoch: "old")
        XCTAssertTrue(capture.start(lease: lease))
        let startDeadline = ContinuousClock.now + .seconds(1)
        while !capture.isCapturing && .now < startDeadline { await Task.yield() }
        queue.suspend()
        let pending = capture.receiveSamples(from: identity) { ([Int16](repeating: 1, count: 48_000), 48_000) }
        await pending?.value
        // Ни refresh, ни main-loop stop не могут помочь очереди доставки.
        occupyPrivacyTestMainThread(for: 0.35)
        queue.resume()
        queue.sync {}
        XCTAssertNotNil(pending)
        XCTAssertEqual(ingests.withLock { $0 }, 0)
        XCTAssertNil(capture.receiveSamples(from: identity) { XCTFail("просроченный PCM копируется"); return nil })
        XCTAssertFalse(lease.renew(until: .now + .seconds(10)))
        capture.stop(flush: false)
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

@MainActor
func occupyPrivacyTestMainThread(for seconds: TimeInterval) {
    Thread.sleep(forTimeInterval: seconds)
}
