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
        let session = SentryPrivacySession(ephemeralConfiguration: config)
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
        let capture = SystemAudioCapture(ipcClient: IPCClient(socketPath: "/unused-e8-test.sock")) { _ in
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
        }

        XCTAssertTrue(capture.start())
        XCTAssertFalse(capture.start())
        await fulfillment(of: [startCalled], timeout: 2)
        capture.stop(flush: false)
        XCTAssertFalse(capture.isCapturing)
        XCTAssertFalse(capture.start())
        finishStart?.resume()
        await fulfillment(of: [stopCalled], timeout: 2)
        XCTAssertFalse(capture.isCapturing)
    }
}

private final class BlockingTelemetryProtocol: URLProtocol, @unchecked Sendable {
    static let onStart = OSAllocatedUnfairLock<(@Sendable () -> Void)?>(initialState: nil)

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { Self.onStart.withLock { $0 }?() }
    override func stopLoading() {}
}
