import XCTest
import AppKit
@testable import KrabEarAgent

final class ProbeSSERecoveryTests: XCTestCase {
    @MainActor
    func testCleanEOFReconnectsWhileSubscriptionIsActive() async {
        let factory = RecoverySessionFactory()
        let view = StatusIndicatorView(frame: .zero)
        let box = ProbeSSEBox(statusIndicator: view, sessionFactory: { factory.make($0) })
        let stream = Task { await box.startStreaming(url: URL(string: "https://probe.invalid/v1/events")!) }
        await fulfillment(of: [factory.firstStarted], timeout: 2)
        factory.delegate(at: 0)._testComplete()
        await fulfillment(of: [factory.secondStarted], timeout: 2)
        stream.cancel()
        await stream.value
        XCTAssertEqual(factory.sessions.count, 2)
    }
    @MainActor
    func testCancellationBeforeStartCreatesNoTransport() async {
        let factory = RecoverySessionFactory()
        let box = ProbeSSEBox(statusIndicator: StatusIndicatorView(frame: .zero), sessionFactory: { factory.make($0) })
        let stream = Task { await box.startStreaming(url: URL(string: "https://probe.invalid/v1/events")!) }
        stream.cancel()
        await stream.value
        XCTAssertTrue(factory.sessions.isEmpty)
    }

    @MainActor
    func testCancellationDuringBackoffDoesNotReconnect() async {
        let factory = RecoverySessionFactory()
        var pending: [DispatchWorkItem] = []
        let box = ProbeSSEBox(statusIndicator: StatusIndicatorView(frame: .zero),
                              sessionFactory: { factory.make($0) }, scheduler: { _, work in pending.append(work) })
        let stream = Task { await box.startStreaming(url: URL(string: "https://probe.invalid/v1/events")!) }
        await fulfillment(of: [factory.firstStarted], timeout: 2)
        factory.delegate(at: 0)._testComplete()
        await withCheckedContinuation { c in DispatchQueue.main.async { c.resume() } }
        XCTAssertEqual(pending.count, 1)
        stream.cancel()
        await stream.value
        pending.forEach { $0.perform() }
        await withCheckedContinuation { c in DispatchQueue.main.async { c.resume() } }
        XCTAssertEqual(factory.sessions.count, 1)
        XCTAssertEqual(factory.sessions[0].invalidations, 1)
    }

    @MainActor
    func testQueuedRetryCannotStartAfterCancellationBeforeCleanupResumes() async {
        let factory = RecoverySessionFactory()
        var pending: [DispatchWorkItem] = []
        let box = ProbeSSEBox(statusIndicator: StatusIndicatorView(frame: .zero),
                              sessionFactory: { factory.make($0) }, scheduler: { _, work in pending.append(work) })
        let stream = Task { await box.startStreaming(url: URL(string: "https://probe.invalid/v1/events")!) }
        await fulfillment(of: [factory.firstStarted], timeout: 2)
        factory.delegate(at: 0)._testComplete()
        await withCheckedContinuation { c in DispatchQueue.main.async { c.resume() } }
        pending[0].perform() // Retry уже в main queue, cleanup task ещё не запланирован.
        stream.cancel()
        await withCheckedContinuation { c in DispatchQueue.main.async { c.resume() } }
        XCTAssertEqual(factory.sessions.count, 1, "cancel обязан закрыть admission до async cleanup")
        await stream.value
    }

}

final class RecoverySessionFactory: @unchecked Sendable {
    let firstStarted = XCTestExpectation(description: "first connection")
    let secondStarted = XCTestExpectation(description: "reconnected")
    private let lock = NSLock()
    private var stored: [RecoverySession] = []
    var sessions: [RecoverySession] { lock.lock(); defer { lock.unlock() }; return stored }
    func delegate(at index: Int) -> SSESessionDelegate { sessions[index].delegate }
    func make(_ delegate: SSESessionDelegate) -> RecoverySession {
        lock.lock()
        let started = stored.isEmpty ? firstStarted : (stored.count == 1 ? secondStarted : XCTestExpectation(description: "later connection"))
        let session = RecoverySession(delegate: delegate, started: started)
        stored.append(session)
        lock.unlock()
        return session
    }
}

final class RecoverySession: ProbeSSESession, @unchecked Sendable {
    let delegate: SSESessionDelegate
    let task: RecoveryTask
    private(set) var invalidations = 0
    init(delegate: SSESessionDelegate, started: XCTestExpectation) {
        self.delegate = delegate
        self.task = RecoveryTask(started: started)
    }
    func makeDataTask(with request: URLRequest) -> any ProbeSSETask { task }
    func invalidateAndCancel() { invalidations += 1 }
}

final class RecoveryTask: ProbeSSETask, @unchecked Sendable {
    let started: XCTestExpectation
    private(set) var cancellations = 0
    init(started: XCTestExpectation) { self.started = started }
    func resume() { started.fulfill() }
    func cancel() { cancellations += 1 }
}
