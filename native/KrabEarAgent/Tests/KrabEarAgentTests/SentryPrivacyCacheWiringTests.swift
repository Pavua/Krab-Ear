import XCTest
@testable import KrabEarAgent

@MainActor
final class SentryPrivacyCacheWiringTests: XCTestCase {
    private let prefs = IsolatedUserDefaultsDomain(scope: "SentryPrivacyCache")
    private let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)

    override func tearDown() async throws {
        prefs.removePersistentDomain()
        try? FileManager.default.removeItem(at: root)
        try await super.tearDown()
    }

    private func cache() -> SentryPrivacyCache { SentryPrivacyCache(defaults: prefs.defaults, root: root) }

    private func instance(_ start: @escaping (SentryPrivacyCache.Access) -> Void) -> SentryPrivacyLifecycle {
        SentryPrivacyLifecycle(cache: cache(), start: { _, _, access in start(access) }, close: {})
    }

    private func confirm(_ life: SentryPrivacyLifecycle, epoch: String?) {
        let lease = ConfirmedPrivacyLease(until: .now + .seconds(10), liveSubsEpoch: epoch)
        life.reconcile(privacy: false, dsn: "synthetic-destination", lease: lease)
    }

    func testNormalProcessRestartReopensConfirmedCrashCacheOnlyAfterOFFRead() throws {
        var first: SentryPrivacyCache.Access?
        confirm(instance { first = $0 }, epoch: "same-off")
        let initial = try XCTUnwrap(first)
        let sentinel = initial.directory.appendingPathComponent("synthetic-crash")
        try Data([1]).write(to: sentinel)

        var second: SentryPrivacyCache.Access?
        let next = instance { second = $0 }
        XCTAssertFalse(next.isActive)
        XCTAssertNil(second)
        XCTAssertTrue(FileManager.default.fileExists(atPath: sentinel.path))
        confirm(next, epoch: "same-off")
        XCTAssertEqual(second?.directory, initial.directory)
        XCTAssertTrue(FileManager.default.fileExists(atPath: sentinel.path))
        // Запоздалая отмена от предыдущего владельца не трогает текущий SDK.
        cache().discardExpired(initial)
        XCTAssertTrue(FileManager.default.fileExists(atPath: sentinel.path))
    }

    func testDelegateBootKeepsBoundCacheAndUnconfirmedWriteSuppressesReplay() async throws {
        struct Backend: IPCSocketProviding {
            func send(payload: Data, timeoutSec: Int) async throws -> Data {
                let request = try JSONSerialization.jsonObject(with: payload) as? [String: Any]
                guard request?["method"] as? String == "get_settings" else { throw IPCError.timeout }
                return Data(#"{"ok":true,"result":{"privacy_mode_enabled":false,"live_subs_policy_epoch":"off","sentry_dsn_agent":"synthetic-destination"}}"#.utf8)
            }
        }

        var first: SentryPrivacyCache.Access?
        confirm(instance { first = $0 }, epoch: "off")
        let old = try XCTUnwrap(first)
        var starts = [URL]()
        func boot() -> AgentAppDelegate {
            let telemetry = instance { starts.append($0.directory) }
            let agent = AgentAppDelegate(options: LaunchOptions(arguments: []), userDefaults: prefs.defaults,
                                         sentryPrivacy: telemetry)
            agent.ipcClient = IPCClient(socketProvider: Backend())
            return agent
        }
        let agent = boot()
        XCTAssertTrue(agent.privacyModeEnabled)
        XCTAssertTrue(starts.isEmpty)
        XCTAssertTrue(FileManager.default.fileExists(atPath: old.directory.path))
        _ = await agent.tickPrivacyMonitor()?.value
        XCTAssertEqual(starts, [old.directory])

        let write = agent.performSettingsMutation("set_settings", params: ["privacy_mode_enabled": true])
        let saved = await write.value
        XCTAssertFalse(saved)
        XCTAssertFalse(FileManager.default.fileExists(atPath: old.directory.path))
        let restarted = boot()
        _ = await restarted.tickPrivacyMonitor()?.value
        XCTAssertTrue(restarted.privacyModeEnabled)
        XCTAssertEqual(starts, [old.directory])
    }

    func testGracefulExitLeavesOnlyStillValidLeaseReusable() throws {
        for expired in [false, true] {
            let store = cache()
            var opened: SentryPrivacyCache.Access?
            let exiting = SentryPrivacyLifecycle(cache: store,
                start: { _, _, access in opened = access }, close: {})
            let lease = ConfirmedPrivacyLease(until: .now + .seconds(10), liveSubsEpoch: "same")
            exiting.reconcile(privacy: false, dsn: "synthetic-destination", lease: lease)
            let prior = try XCTUnwrap(opened)
            if expired { lease.revoke() }
            exiting.finishProcess()
            confirm(instance { opened = $0 }, epoch: "same")
            let current = try XCTUnwrap(opened)
            XCTAssertEqual(current.directory == prior.directory, !expired)
            store.discardExpired(prior)
            XCTAssertTrue(FileManager.default.fileExists(atPath: current.directory.path))
        }
    }

    func testOfflinePrivacyOnThenOffRefusesPriorCache() throws {
        var opened: SentryPrivacyCache.Access?
        confirm(instance { opened = $0 }, epoch: "before-on")
        let old = try XCTUnwrap(opened)
        confirm(instance { opened = $0 }, epoch: "after-on")
        XCTAssertNotEqual(opened?.directory, old.directory)
        XCTAssertFalse(FileManager.default.fileExists(atPath: old.directory.path))
    }

    func testExplicitRestrictionOrUnknownBeforeFirstOFFDiscardsCache() throws {
        for restricted in [true, nil] as [Bool?] {
            var opened: SentryPrivacyCache.Access?
            confirm(instance { opened = $0 }, epoch: "unchanged")
            let old = try XCTUnwrap(opened)
            let next = instance { opened = $0 }
            next.reconcile(privacy: restricted, dsn: "synthetic-destination", lease: nil)
            XCTAssertFalse(next.isActive)
            confirm(next, epoch: "unchanged")
            XCTAssertNotEqual(opened?.directory, old.directory)
            XCTAssertFalse(FileManager.default.fileExists(atPath: old.directory.path))
        }
    }

    func testMissingCorruptAndLegacyBindingCannotSelectOldDirectory() throws {
        for bogus in [nil, Data("{}".utf8)] as [Data?] {
            let id = UUID()
            prefs.defaults.set(id.uuidString, forKey: SentryPrivacyCache.pointerKey)
            let old = root.appendingPathComponent(id.uuidString)
            try FileManager.default.createDirectory(at: old, withIntermediateDirectories: true)
            if let bogus { try bogus.write(to: old.appendingPathComponent(".privacy-binding-v1.json")) }
            var opened: SentryPrivacyCache.Access?
            confirm(instance { opened = $0 }, epoch: "confirmed-off")
            XCTAssertNotEqual(opened?.directory.lastPathComponent, id.uuidString)
            XCTAssertFalse(FileManager.default.fileExists(atPath: old.path))
        }
    }

    func testDifferentDestinationAndAbsentEpochCannotReuseBoundCache() throws {
        var opened: SentryPrivacyCache.Access?
        confirm(instance { opened = $0 }, epoch: "off")
        let old = try XCTUnwrap(opened)
        let lease = ConfirmedPrivacyLease(until: .now + .seconds(10), liveSubsEpoch: "off")
        instance { opened = $0 }.reconcile(privacy: false, dsn: "other-destination", lease: lease)
        let other = try XCTUnwrap(opened)
        XCTAssertNotEqual(other.directory, old.directory)
        confirm(instance { opened = $0 }, epoch: nil)
        let unbound = try XCTUnwrap(opened)
        confirm(instance { opened = $0 }, epoch: nil)
        XCTAssertNotEqual(opened?.directory, unbound.directory)
    }

    func testBackgroundLeaseExpiryInvalidatesBindingBeforeNextProcess() async throws {
        let store = cache()
        let expired = expectation(description: "binding revoked without UI tick")
        let lease = ConfirmedPrivacyLease(until: .now + .milliseconds(250), liveSubsEpoch: "same-off")
        let old = try XCTUnwrap(store.open(lease: lease, dsn: "synthetic-destination"))
        let transport = SentryPrivacySession(lease: lease, onExpiry: {
            store.discardExpired(old)
            expired.fulfill()
        })
        await fulfillment(of: [expired], timeout: 2)
        XCTAssertFalse(transport.allowsTelemetry)
        var reopened: SentryPrivacyCache.Access?
        confirm(instance { reopened = $0 }, epoch: "same-off")
        XCTAssertNotEqual(reopened?.directory, old.directory)
        XCTAssertFalse(FileManager.default.fileExists(atPath: old.directory.path))
    }
}
