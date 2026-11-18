import Foundation
import os
import XCTest
@testable import KrabEarAgent

@MainActor
final class E8PrivacyLifecycleTests: XCTestCase {
    func testTelemetryStartsOnlyAfterConfirmedOptOutOfPrivacy() {
        var calls: [String] = []
        let lifecycle = SentryPrivacyLifecycle(start: { calls.append($0) }, close: { calls.append("close") })

        for flag in [nil, true] as [Bool?] {
            lifecycle.reconcile(privacy: flag, dsn: "configured")
        }
        lifecycle.reconcile(privacy: false, dsn: nil)
        XCTAssertTrue(calls.isEmpty)

        lifecycle.reconcile(privacy: false, dsn: "configured")
        lifecycle.reconcile(privacy: false, dsn: "configured")
        XCTAssertTrue(lifecycle.isActive)
        XCTAssertEqual(calls, ["configured"])

        lifecycle.reconcile(privacy: true, dsn: "configured")
        lifecycle.reconcile(privacy: nil, dsn: nil)
        XCTAssertFalse(lifecycle.isActive)
        XCTAssertEqual(calls, ["configured", "close"])

        lifecycle.reconcile(privacy: false, dsn: "configured")
        XCTAssertEqual(calls, ["configured", "close", "configured"])
    }

    func testRevokedTelemetrySessionCancelsActiveAndRejectsLateTasks() async {
        let started = expectation(description: "request intercepted locally")
        let cancelled = expectation(description: "transport cancelled")
        let late = expectation(description: "late task completed locally")
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [BlockingTelemetryProtocol.self]
        BlockingTelemetryProtocol.onStart.withLock { $0 = { started.fulfill() } }
        defer { BlockingTelemetryProtocol.onStart.withLock { $0 = nil } }
        let lease = ConfirmedPrivacyLease(until: .now + .seconds(10))
        let session = SentryPrivacySession(lease: lease, ephemeralConfiguration: config)
        let request = URLRequest(url: URL(string: "https://telemetry.invalid/synthetic")!)
        session.dataTask(with: request) { _, _, error in
            XCTAssertEqual((error as? URLError)?.code, .cancelled)
            cancelled.fulfill()
        }.resume()
        await fulfillment(of: [started], timeout: 2)

        session.revoke()
        let rejected = session.dataTask(with: request) { _, _, error in
            XCTAssertNotNil(error)
            late.fulfill()
        }
        XCTAssertNotEqual(rejected.originalRequest?.url, request.url)
        rejected.resume()
        await fulfillment(of: [cancelled, late], timeout: 2)
    }

    func testOnlyJSONBooleansConfirmPrivacyState() throws {
        let decoded = try XCTUnwrap(JSONSerialization.jsonObject(
            with: Data(#"{"bool":false,"number":0,"text":"false"}"#.utf8)
        ) as? [String: Any])
        XCTAssertEqual(confirmedPrivacyFlag(decoded["bool"]), false)
        XCTAssertEqual(confirmedPrivacyFlag(true), true)
        XCTAssertNil(confirmedPrivacyFlag(decoded["number"]))
        XCTAssertNil(confirmedPrivacyFlag(decoded["text"]))
        XCTAssertNil(confirmedPrivacyFlag(decoded["missing"]))
    }

    func testSettingsMutationsWaitForPriorAcknowledgment() async {
        let queue = AgentSettingsOperationQueue()
        let firstStarted = expectation(description: "first IPC sent")
        var firstReply: CheckedContinuation<Void, Never>?
        var events: [String] = []

        let first = queue.enqueue {
            events.append("first write")
            await withCheckedContinuation {
                firstReply = $0
                firstStarted.fulfill()
            }
            events.append("first acknowledgment")
            return true
        }
        let second = queue.enqueue {
            events.append("second write")
            return true
        }

        await fulfillment(of: [firstStarted], timeout: 2)
        XCTAssertEqual(events, ["first write"])
        firstReply?.resume()
        let firstSucceeded = await first.value
        let secondSucceeded = await second.value
        XCTAssertTrue(firstSucceeded)
        XCTAssertTrue(secondSucceeded)
        XCTAssertEqual(events, ["first write", "first acknowledgment", "second write"])
    }

    func testStopDuringStartClosesLateStreamAndBlocksSecondStart() async {
        let startCalled = expectation(description: "start entered")
        let stopCalled = expectation(description: "late stream stopped")
        let identity = NSObject()
        var finishStart: CheckedContinuation<Void, Never>?
        let capture = SystemAudioCapture(ipcClient: IPCClient(socketPath: "/unused-e8-test.sock"), makeStream: { _ in
            SystemAudioStream(
                identity: ObjectIdentifier(identity),
                start: {
                    await withCheckedContinuation {
                        finishStart = $0
                        startCalled.fulfill()
                    }
                },
                stop: { stopCalled.fulfill() }
            )
        })

        let lease = ConfirmedPrivacyLease(until: .now + .seconds(10), liveSubsEpoch: "test")
        XCTAssertTrue(capture.start(lease: lease))
        XCTAssertFalse(capture.start(lease: lease))
        await fulfillment(of: [startCalled], timeout: 2)
        capture.stop(flush: false)
        XCTAssertFalse(capture.isCapturing)
        XCTAssertFalse(capture.start(lease: lease))
        finishStart?.resume()
        await fulfillment(of: [stopCalled], timeout: 2)
        XCTAssertFalse(capture.isCapturing)
    }
}

@MainActor
final class E8PrivacyDeadlineWiringTests: XCTestCase {
    func testCaptureWithoutObservedEpochCannotStart() {
        let capture = SystemAudioCapture(ipcClient: IPCClient(socketPath: "/unused-e8-test.sock"), makeStream: { _ in nil })
        XCTAssertFalse(capture.start(lease: nil))
        XCTAssertFalse(capture.start(lease: ConfirmedPrivacyLease(until: .now + .seconds(10))))
    }

    func testSuspendedTelemetryTaskCannotBeginAfterDeadline() async throws {
        let leaked = expectation(description: "transport probe")
        leaked.isInverted = true
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [BlockingTelemetryProtocol.self]
        BlockingTelemetryProtocol.onStart.withLock { $0 = { leaked.fulfill() } }
        defer { BlockingTelemetryProtocol.onStart.withLock { $0 = nil } }
        let lease = ConfirmedPrivacyLease(until: .now + .milliseconds(100))
        let session = SentryPrivacySession(lease: lease, ephemeralConfiguration: config)
        let task = session.dataTask(with: URLRequest(url: URL(string: "https://telemetry.invalid/synthetic")!)) { _, _, _ in }
        XCTAssertTrue(session.allowsTelemetry)
        // MainActor намеренно занят: ни Timer, ни agent settings tick здесь не работают.
        occupyPrivacyTestMainThread(for: 0.15)
        XCTAssertFalse(session.allowsTelemetry)
        XCTAssertFalse(lease.renew(until: .now + .seconds(10)))
        task.resume()
        await fulfillment(of: [leaked], timeout: 0.2)
        session.revoke()
    }

    func testDeadlineCancelsInflightTelemetryWithoutAgentTick() async {
        let started = expectation(description: "request intercepted")
        let cancelled = expectation(description: "request cancelled on expiry")
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [BlockingTelemetryProtocol.self]
        BlockingTelemetryProtocol.onStart.withLock { $0 = { started.fulfill() } }
        defer { BlockingTelemetryProtocol.onStart.withLock { $0 = nil } }
        let lease = ConfirmedPrivacyLease(until: .now + .milliseconds(400))
        let transport = SentryPrivacySession(lease: lease, ephemeralConfiguration: config)
        let request = URLRequest(url: URL(string: "https://telemetry.invalid/synthetic")!)
        transport.dataTask(with: request) { _, _, error in
            XCTAssertEqual((error as? URLError)?.code, .cancelled)
            cancelled.fulfill()
        }.resume()
        await fulfillment(of: [started, cancelled], timeout: 2, enforceOrder: true)
        XCTAssertFalse(transport.allowsTelemetry)
    }
}

private final class BlockingTelemetryProtocol: URLProtocol, @unchecked Sendable {
    static let onStart = OSAllocatedUnfairLock<(@Sendable () -> Void)?>(initialState: nil)

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { Self.onStart.withLock { $0 }?() }
    override func stopLoading() {}
}
