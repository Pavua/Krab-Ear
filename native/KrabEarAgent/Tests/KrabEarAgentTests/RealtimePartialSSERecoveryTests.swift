import XCTest
import Foundation
@testable import KrabEarAgent

final class RealtimePartialSSERecoveryTests: XCTestCase {
    @MainActor
    func testActualPartialOwnerReconnectsAfterCleanEOF() async throws {
        let second = expectation(description: "partial preview reconnected")
        let server = try SSELoopbackFixture(onConnection: { count in
            if count == 2 { second.fulfill() }
        })
        let serving = Task.detached { server.serveTwoStreams() }
        let overlay = RealtimeOverlayController()
        overlay.startPartialSSE(restBaseURL: "http://127.0.0.1:\(server.port)")
        await fulfillment(of: [second], timeout: 8)
        overlay.stopPartialSSE()
        let count = await serving.value
        XCTAssertEqual(count, 2)
    }

    @MainActor
    func testStopDuringBackoffRejectsPendingRetry() async {
        let factory = RecoverySessionFactory()
        var pending: [DispatchWorkItem] = []
        let owner = SSESubscriptionTransport(sessionFactory: { factory.make($0) },
                                             scheduler: { _, work in pending.append(work) })
        let overlay = RealtimeOverlayController()
        overlay.startPartialSSE(transport: owner)
        factory.delegate(at: 0)._testComplete()
        await flushMain()
        XCTAssertEqual(pending.count, 1)
        overlay.stopPartialSSE()
        pending.forEach { $0.perform() }
        await flushMain()
        XCTAssertEqual(factory.sessions.count, 1)
        XCTAssertEqual(factory.sessions[0].invalidations, 1)
    }

    @MainActor
    func testReconnectResetsEventTypeAndRejectsOldCallbacks() async {
        let factory = RecoverySessionFactory()
        var pending: [DispatchWorkItem] = []
        let owner = SSESubscriptionTransport(sessionFactory: { factory.make($0) },
                                             scheduler: { _, work in pending.append(work) })
        let overlay = RealtimeOverlayController()
        overlay.primaryLabel.stringValue = "unchanged"
        overlay.startPartialSSE(transport: owner)
        let old = factory.delegate(at: 0)
        old._testReceive("event: realtime.final_transcript\n")
        old._testComplete()
        await flushMain()
        pending[0].perform()
        await flushMain()
        old._testReceive("data: {\"text\":\"stale\"}\n")
        factory.delegate(at: 1)._testReceive("data: {\"text\":\"no event type\"}\n")
        await flushMain()
        XCTAssertEqual(overlay.primaryLabel.stringValue, "unchanged")
        factory.delegate(at: 1)._testReceive("event: realtime.final_transcript\ndata: {\"text\":\"current\"}\n\n")
        await flushMain()
        XCTAssertEqual(overlay.primaryLabel.stringValue, "current")
        overlay.stopPartialSSE()
    }

    @MainActor
    func testRestartRejectsQueuedLinesFromPreviousSubscription() async {
        let factory = RecoverySessionFactory()
        let owner = SSESubscriptionTransport(sessionFactory: { factory.make($0) })
        let overlay = RealtimeOverlayController()
        overlay.primaryLabel.stringValue = "unchanged"
        overlay.startPartialSSE(transport: owner)
        factory.delegate(at: 0)._testReceive("event: realtime.final_transcript\ndata: {\"text\":\"stale\"}\n")
        overlay.startPartialSSE(transport: owner)
        await flushMain()
        XCTAssertEqual(overlay.primaryLabel.stringValue, "unchanged")
        XCTAssertEqual(factory.sessions.count, 2)
        XCTAssertEqual(factory.sessions[0].invalidations, 1)
        overlay.stopPartialSSE()
        XCTAssertEqual(factory.sessions[1].invalidations, 1)
    }

    @MainActor private func flushMain() async {
        await withCheckedContinuation { c in DispatchQueue.main.async { c.resume() } }
    }

}
