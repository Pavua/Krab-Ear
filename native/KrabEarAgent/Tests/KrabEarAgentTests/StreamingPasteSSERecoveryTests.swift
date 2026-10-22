import XCTest
import Foundation
@testable import KrabEarAgent

final class StreamingPasteSSERecoveryTests: XCTestCase {
    @MainActor
    func testActualStreamingOwnerReconnectsAfterCleanEOF() async throws {
        let second = expectation(description: "streaming paste reconnected")
        let server = try SSELoopbackFixture(onConnection: { count in
            if count == 2 { second.fulfill() }
        })
        let serving = Task.detached { server.serveTwoStreams() }
        let controller = StreamingPasteController(pasteService: FakeStreamingPasteTarget())
        controller.isEnabled = true
        controller.recordingDidStart(restBaseURL: "http://127.0.0.1:\(server.port)")
        await fulfillment(of: [second], timeout: 8)
        controller.recordingDidStop()
        let count = await serving.value
        XCTAssertEqual(count, 2)
    }

    @MainActor
    func testRecordingStopDuringBackoffDoesNotReconnect() async {
        let factory = RecoverySessionFactory()
        var pending: [DispatchWorkItem] = []
        let owner = SSESubscriptionTransport(sessionFactory: { factory.make($0) },
                                             scheduler: { _, work in pending.append(work) })
        let target = FakeStreamingPasteTarget()
        let controller = StreamingPasteController(pasteService: target, transport: owner)
        controller.isEnabled = true
        controller.recordingDidStart()
        factory.delegate(at: 0)._testComplete()
        await flushMain()
        XCTAssertEqual(pending.count, 1)
        controller.recordingDidStop()
        pending.forEach { $0.perform() }
        await flushMain()
        XCTAssertEqual(factory.sessions.count, 1)
        XCTAssertTrue(target.calls.isEmpty)
    }

    @MainActor
    func testReconnectRetainsRecordingAndDoesNotDuplicateFinalPaste() async {
        let factory = RecoverySessionFactory()
        var pending: [DispatchWorkItem] = []
        let owner = SSESubscriptionTransport(sessionFactory: { factory.make($0) },
                                             scheduler: { _, work in pending.append(work) })
        let target = FakeStreamingPasteTarget()
        let controller = StreamingPasteController(pasteService: target, transport: owner)
        controller.isEnabled = true
        controller.recordingDidStart()
        let pasted = expectation(description: "SSE dispatched final")
        target.onPaste = { _ in pasted.fulfill() }
        factory.delegate(at: 0)._testReceive("event: realtime.final_transcript\ndata: {\"text\":\"one final\"}\n\n")
        await fulfillment(of: [pasted], timeout: 2)
        factory.delegate(at: 0)._testComplete()
        await flushMain()
        pending[0].perform()
        await flushMain()
        factory.delegate(at: 1)._testReceive("event: realtime.final_transcript\ndata: {\"text\":\"one final\"}\n\n")
        await flushMain()
        await flushMain()
        XCTAssertEqual(target.pasteCalls, ["one final"])
        XCTAssertTrue(controller.didStreamThisRecording)
        controller.recordingDidStop()
    }

    @MainActor
    func testNewRecordingRejectsQueuedAndLateOldFinal() async {
        let factory = RecoverySessionFactory()
        let owner = SSESubscriptionTransport(sessionFactory: { factory.make($0) })
        let target = FakeStreamingPasteTarget()
        let controller = StreamingPasteController(pasteService: target, transport: owner)
        controller.isEnabled = true
        controller.recordingDidStart()
        let pasted = expectation(description: "only new recording final")
        target.onPaste = { text in
            XCTAssertEqual(text, "NEW")
            pasted.fulfill()
        }
        let old = factory.delegate(at: 0)
        old._testReceive("event: realtime.final_transcript\ndata: {\"text\":\"OLD\"}\n\n")
        controller.recordingDidStart()
        old._testReceive("event: realtime.final_transcript\ndata: {\"text\":\"LATE\"}\n\n")
        factory.delegate(at: 1)._testReceive("event: realtime.final_transcript\ndata: {\"text\":\"NEW\"}\n\n")
        await flushMain()
        await fulfillment(of: [pasted], timeout: 2)
        XCTAssertEqual(target.pasteCalls, ["NEW"])
        controller.recordingDidStop()
    }

    @MainActor
    func testOrderedAdmissionDoesNotAcceptPartialAfterFinal() async {
        let factory = RecoverySessionFactory()
        let owner = SSESubscriptionTransport(sessionFactory: { factory.make($0) })
        let target = FakeStreamingPasteTarget()
        let controller = StreamingPasteController(pasteService: target, transport: owner)
        controller.isEnabled = true
        controller.recordingDidStart()
        let pasted = expectation(description: "final before partial")
        target.onPaste = { _ in pasted.fulfill() }
        factory.delegate(at: 0)._testReceive("event: realtime.final_transcript\ndata: {\"text\":\"final\"}\n\nevent: realtime.partial_transcript\ndata: {\"text\":\"stale partial \"}\n\n")
        await flushMain()
        await fulfillment(of: [pasted], timeout: 2)
        XCTAssertEqual(target.pasteCalls, ["final"])
        controller.recordingDidStop()
    }

    @MainActor
    func testReconnectDoesNotCarryEventTypeIntoNewConnection() async {
        let factory = RecoverySessionFactory()
        var pending: [DispatchWorkItem] = []
        let owner = SSESubscriptionTransport(sessionFactory: { factory.make($0) },
                                             scheduler: { _, work in pending.append(work) })
        let target = FakeStreamingPasteTarget()
        let controller = StreamingPasteController(pasteService: target, transport: owner)
        controller.isEnabled = true
        controller.recordingDidStart()
        let pasted = expectation(description: "new connection typed final")
        target.onPaste = { _ in pasted.fulfill() }
        factory.delegate(at: 0)._testReceive("event: realtime.final_transcript\n")
        factory.delegate(at: 0)._testComplete()
        await flushMain()
        pending[0].perform()
        await flushMain()
        factory.delegate(at: 1)._testReceive("data: {\"text\":\"BAD inherited type\"}\n\nevent: realtime.final_transcript\ndata: {\"text\":\"GOOD\"}\n\n")
        await flushMain()
        await fulfillment(of: [pasted], timeout: 2)
        XCTAssertEqual(target.pasteCalls, ["GOOD"])
        controller.recordingDidStop()
    }

    @MainActor
    func testSSEAdmissionUsesBoundedRealQueueBeforeAwaitAndPreservesFinal() async {
        let factory = RecoverySessionFactory()
        var pending: [DispatchWorkItem] = []
        let owner = SSESubscriptionTransport(sessionFactory: { factory.make($0) },
                                             scheduler: { _, work in pending.append(work) })
        let target = SSEQueuePasteTarget()
        let controller = StreamingPasteController(pasteService: target, transport: owner)
        controller.isEnabled = true
        controller.recordingDidStart()
        let started = expectation(description: "first real queued input")
        let finished = expectation(description: "first input released")
        target.firstStarted = started
        target.firstFinished = finished
        let packet = ["hello ", "hello world ", "hello world again ", "hello world again now "]
            .map { "event: realtime.partial_transcript\ndata: {\"text\":\"\($0)\"}\n\n" }.joined()
        factory.delegate(at: 0)._testReceive(packet)
        await fulfillment(of: [started], timeout: 2)
        XCTAssertEqual(target.admitted, [true, true, false, false], "admission must happen before async input drains")
        XCTAssertEqual(target.calls, ["hello "])
        target.releaseFirst?()
        await fulfillment(of: [finished], timeout: 2)
        let final = expectation(description: "final reached fake paste through real queue")
        target.finalPasted = final
        factory.delegate(at: 0)._testReceive("event: realtime.final_transcript\ndata: {\"text\":\"hello world\"}\n\n")
        await fulfillment(of: [final], timeout: 2)
        factory.delegate(at: 0)._testComplete()
        await flushMain()
        pending[0].perform()
        await flushMain()
        factory.delegate(at: 1)._testReceive("event: realtime.final_transcript\ndata: {\"text\":\"hello world\"}\n\n")
        await flushMain()
        await flushMain()
        XCTAssertEqual(target.calls, ["hello ", "world"])
        XCTAssertEqual(target.admitted, [true, true, false, false, true])
        XCTAssertEqual(target.queue.pendingCount, 0)
        controller.recordingDidStop()
    }

    @MainActor private func flushMain() async {
        await withCheckedContinuation { c in DispatchQueue.main.async { c.resume() } }
    }

}


/// Настоящие reservations/FIFO; только физическая вставка заменена spy.
@MainActor
private final class SSEQueuePasteTarget: StreamingPasteTarget {
    let queue = InputAutomationQueue(capacity: 1)
    var admitted: [Bool] = []
    var calls: [String] = []
    var firstStarted: XCTestExpectation?
    var firstFinished: XCTestExpectation?
    var finalPasted: XCTestExpectation?
    var releaseFirst: (() -> Void)?
    func captureTarget() -> pid_t? { 42 }
    func reserveInput(target: pid_t?, maximumDeleteCount: Int) -> PasteInputReservation? {
        switch queue.reserve(deadline: .now.advanced(by: .seconds(10))) {
        case .success(let ticket):
            admitted.append(true)
            return PasteInputReservation(queue: queue, ticket: ticket)
        case .failure(let failure):
            admitted.append(false)
            return PasteInputReservation(queue: queue, failure: PasteAttemptResult(ok: false, reason: failure.rawValue))
        }
    }
    func paste(_ text: String, target: pid_t?, reservation: PasteInputReservation?) async -> PasteAttemptResult {
        guard let ticket = reservation?.ticket else {
            XCTFail("SSE input lost reservation")
            return PasteAttemptResult(ok: false, reason: "missing_reservation")
        }
        guard case .success = await queue.awaitReady(ticket) else {
            XCTFail("accepted input failed FIFO")
            return PasteAttemptResult(ok: false, reason: "queue_failed")
        }
        calls.append(text)
        if calls.count == 1 {
            await withCheckedContinuation { c in
                releaseFirst = { c.resume() }
                firstStarted?.fulfill()
            }
            releaseFirst = nil
            firstFinished?.fulfill()
        } else {
            finalPasted?.fulfill()
        }
        return PasteAttemptResult(ok: true, reason: "ok")
    }
    func pasteToFrontmostApp(_ text: String) async -> PasteAttemptResult {
        XCTFail("input bypassed reservation")
        return PasteAttemptResult(ok: false, reason: "unreserved")
    }
    func deleteBackward(count: Int) async -> PasteAttemptResult {
        XCTFail("unexpected native deletion")
        return PasteAttemptResult(ok: false, reason: "unexpected_delete")
    }
}
