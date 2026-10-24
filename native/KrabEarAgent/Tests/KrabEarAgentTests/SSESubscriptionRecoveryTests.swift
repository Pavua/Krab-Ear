import XCTest
import Foundation
@testable import KrabEarAgent

final class SSESubscriptionRecoveryTests: XCTestCase {
    @MainActor
    func testCancellationMarkerRejectsQueuedLinesBeforeActorCleanup() async {
        let factory = RecoverySessionFactory()
        let cancelled = ProbeSSECancellationGate()
        let owner = SSESubscriptionTransport(sessionFactory: { factory.make($0) })
        var lines: [String] = []
        owner.start(request: URLRequest(url: URL(string: "http://127.0.0.1/events")!),
                    isCancelled: { cancelled.cancelled },
                    onConnectionReset: {}, onLine: { lines.append($0) })
        factory.delegate(at: 0)._testReceive("data: stale\n")
        cancelled.cancel()
        await flushMain()
        XCTAssertTrue(lines.isEmpty)
        owner.stop()
        XCTAssertEqual(factory.sessions[0].task.cancellations, 1)
        XCTAssertEqual(factory.sessions[0].invalidations, 1)
    }

    @MainActor
    func testCleanAndRepeatedEOFCreateOneRetry() async {
        let h = TransportHarness()
        h.start()
        let first = h.factory.sessions[0]
        first.delegate._testComplete()
        first.delegate._testComplete()
        await flushMain()
        XCTAssertEqual(h.delays, [0.5])
        XCTAssertEqual(first.task.cancellations, 1)
        XCTAssertEqual(first.invalidations, 1)
        h.fireRetry()
        await flushMain()
        XCTAssertEqual(h.factory.sessions.count, 2)
        h.owner.stop()
    }

    @MainActor
    func testErrorEOFReconnects() async {
        let h = TransportHarness()
        h.start()
        h.factory.delegate(at: 0)._testComplete(error: URLError(.networkConnectionLost))
        await flushMain()
        h.fireRetry()
        await flushMain()
        XCTAssertEqual(h.factory.sessions.count, 2)
        h.owner.stop()
    }

    @MainActor
    func testStopDuringBackoffRejectsEvenPerformedOldWork() async {
        let h = TransportHarness()
        h.start()
        h.factory.delegate(at: 0)._testComplete()
        await flushMain()
        h.owner.stop()
        h.fireRetry()
        await flushMain()
        XCTAssertEqual(h.factory.sessions.count, 1)
    }

    @MainActor
    func testLateG1CallbacksDoNotTouchG2() async {
        let h = TransportHarness()
        h.start()
        let old = h.factory.delegate(at: 0)
        old._testComplete()
        await flushMain()
        h.fireRetry()
        await flushMain()
        old._testReceive("event: stale\ndata: OLD\n")
        old._testComplete()
        h.factory.delegate(at: 1)._testReceive("data: CURRENT\n")
        await flushMain()
        XCTAssertEqual(h.lines, ["data: CURRENT"])
        XCTAssertEqual(h.delays, [0.5])
        XCTAssertEqual(h.factory.sessions[1].task.cancellations, 0)
        h.owner.stop()
    }

    @MainActor
    func testResetClearsEventTypeAndPartialBytesBetweenConnections() async {
        let h = TransportHarness()
        var eventType = ""
        var types: [String] = []
        h.owner.start(request: h.request, onConnectionReset: { eventType = "" }, onLine: {
            if $0.hasPrefix("event: ") { eventType = String($0.dropFirst(7)) }
            if $0.hasPrefix("data: ") { types.append(eventType) }
        })
        h.factory.delegate(at: 0)._testReceive("event: stale\ndata: incom")
        h.factory.delegate(at: 0)._testComplete()
        await flushMain()
        h.fireRetry()
        await flushMain()
        h.factory.delegate(at: 1)._testReceive("data: CURRENT\n")
        await flushMain()
        XCTAssertEqual(types, [""])
        h.owner.stop()
    }

    @MainActor
    func testLineEOFOrderingWhenMainActorIsDelayed() async {
        let h = TransportHarness()
        h.start()
        let delegate = h.factory.delegate(at: 0)
        // Всё enqueue до передачи исполнения main queue: порядок должен сохраниться.
        delegate._testReceive("event: first\ndata: SECOND\n\n")
        delegate._testComplete()
        await flushMain()
        XCTAssertEqual(h.events, ["reset", "event: first", "data: SECOND", "", "reset"])
        h.owner.stop()
    }

    @MainActor
    func testRejectedHTTPOrMIMEDoesNotEmit() async {
        for (status, mime) in [(401, "text/event-stream"), (403, "text/event-stream"), (200, "application/json")] {
            let h = TransportHarness()
            h.start()
            let delegate = h.factory.delegate(at: 0)
            XCTAssertFalse(delegate._testReceiveResponse(statusCode: status, contentType: mime))
            delegate._testReceive("data: must_not_emit\n")
            delegate._testComplete()
            await flushMain()
            XCTAssertTrue(h.lines.isEmpty)
            XCTAssertEqual(h.delays, [0.5])
            h.owner.stop()
        }
    }

    @MainActor
    func testExponentialCapAndAcceptedLineReset() async {
        let h = TransportHarness()
        h.start()
        for _ in 0..<8 {
            h.factory.sessions.last!.delegate._testComplete()
            await flushMain()
            h.fireRetry()
            await flushMain()
        }
        XCTAssertEqual(h.delays, [0.5, 1, 2, 4, 8, 16, 30, 30])
        h.factory.sessions.last!.delegate._testReceive(": heartbeat\n")
        await flushMain()
        h.factory.sessions.last!.delegate._testComplete()
        await flushMain()
        XCTAssertEqual(h.delays.last, 0.5)
        h.owner.stop()
    }

    @MainActor
    func testStopFromLineRejectsRemainingQueuedLines() async {
        let h = TransportHarness()
        var lines: [String] = []
        h.owner.start(request: h.request, onConnectionReset: {}, onLine: {
            lines.append($0)
            h.owner.stop()
        })
        h.factory.delegate(at: 0)._testReceive("first\nsecond\n")
        h.factory.delegate(at: 0)._testComplete()
        await flushMain()
        XCTAssertEqual(lines, ["first"])
        XCTAssertTrue(h.delays.isEmpty)
        XCTAssertEqual(h.factory.sessions[0].invalidations, 1)
    }

    @MainActor
    func testStopFromResetPreventsFactoryAndResume() {
        let h = TransportHarness()
        h.owner.start(request: h.request, onConnectionReset: { h.owner.stop() }, onLine: { _ in })
        XCTAssertTrue(h.factory.sessions.isEmpty)
    }

    @MainActor
    func testMailboxOverflowReconnectsWithoutDeliveringDroppedEvents() async {
        let h = TransportHarness()
        h.start()
        h.factory.delegate(at: 0)._testReceive(String(repeating: "data: X\n", count: 300))
        await flushMain()
        XCTAssertTrue(h.lines.isEmpty)
        XCTAssertEqual(h.delays, [0.5])
        h.owner.stop()
    }

    @MainActor
    func testTransportDeinitClosesActiveConnection() {
        let factory = RecoverySessionFactory()
        var owner: SSESubscriptionTransport? = SSESubscriptionTransport(sessionFactory: { factory.make($0) })
        owner!.start(request: URLRequest(url: URL(string: "https://probe.invalid/v1/events")!),
                     onConnectionReset: {}, onLine: { _ in })
        owner = nil
        XCTAssertEqual(factory.sessions[0].task.cancellations, 1)
        XCTAssertEqual(factory.sessions[0].invalidations, 1)
    }

    @MainActor private func flushMain() async {
        await withCheckedContinuation { continuation in
            DispatchQueue.main.async { continuation.resume() }
        }
    }
}

@MainActor
private final class TransportHarness {
    let factory = RecoverySessionFactory()
    let request = URLRequest(url: URL(string: "https://probe.invalid/v1/events")!)
    var pending: [DispatchWorkItem] = []
    var delays: [Double] = []
    var lines: [String] = []
    var events: [String] = []
    lazy var owner = SSESubscriptionTransport(sessionFactory: { [factory] in factory.make($0) },
        scheduler: { [weak self] delay, work in
            self?.delays.append(delay)
            self?.pending.append(work)
        })
    func start() {
        owner.start(request: request, onConnectionReset: { [weak self] in self?.events.append("reset") },
                    onLine: { [weak self] in self?.lines.append($0); self?.events.append($0) })
    }
    func fireRetry() { pending.removeFirst().perform() }
}
