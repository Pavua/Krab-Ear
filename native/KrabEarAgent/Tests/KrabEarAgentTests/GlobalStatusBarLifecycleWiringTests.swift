import AppKit
import XCTest
import Foundation
@testable import KrabEarAgent

@MainActor
final class GlobalStatusBarLifecycleWiringTests: XCTestCase {
    func testHiddenPanelDoesNotStartDecorativeSubscription() {
        let factory = RecoverySessionFactory()
        let bar = GlobalStatusBar(frame: .zero, sessionFactory: { factory.make($0) })
        bar.start()
        XCTAssertTrue(factory.sessions.isEmpty, "Скрытая панель не владеет app.status stream")
        bar.stop()
    }

    func testVisibilityNotificationsStopAndReopenExactlyOneStream() async {
        let factory = RecoverySessionFactory()
        let window = StatusVisibilityWindow()
        let bar = GlobalStatusBar(frame: .zero, sessionFactory: { factory.make($0) })
        window.contentView = bar
        defer { bar.stop(); window.contentView = nil }
        window.reportVisible(true)
        XCTAssertEqual(factory.sessions.count, 1)
        guard factory.sessions.count == 1 else { return }
        window.reportVisible(true)
        XCTAssertEqual(factory.sessions.count, 1, "Повторное уведомление не перезапускает stream")
        factory.delegate(at: 0)._testReceive(Data("data: {\"op\":\"transcribe_job\"}\n".utf8))
        await drainCallbacks()
        XCTAssertFalse(bar.isHidden)
        factory.delegate(at: 0)._testReceive(Data("data: {\"op\":\"idle\"}\n".utf8))
        await drainCallbacks()
        XCTAssertTrue(bar.isHidden, "Idle скрывает статус без EOF/reset")
        XCTAssertEqual(factory.sessions[0].invalidations, 0)
        factory.delegate(at: 0)._testReceive(Data("data: {\"op\":\"transcribe_job\"}\n".utf8))
        await drainCallbacks()
        XCTAssertFalse(bar.isHidden)

        window.reportVisible(false) // orderOut/Escape, без уничтожения owner.
        XCTAssertTrue(bar.isHidden)
        XCTAssertEqual(factory.sessions[0].invalidations, 1)
        XCTAssertEqual(factory.sessions[0].task.cancellations, 1)
        factory.delegate(at: 0)._testReceive(Data("data: {\"op\":\"transcribe_job\"}\n".utf8))
        await drainCallbacks()
        XCTAssertTrue(bar.isHidden, "Старый callback не оживляет скрытую панель")

        window.reportVisible(true)
        XCTAssertEqual(factory.sessions.count, 2)
        NotificationCenter.default.post(name: NSWindow.willCloseNotification, object: window)
        XCTAssertEqual(factory.sessions[1].invalidations, 1)
        XCTAssertTrue(bar.isHidden)
    }

    /// Настоящее AppKit окно: порядок show/orderOut/reopen/close, без key window
    /// или активации приложения, без clipboard/микрофона/production REST.
    func testActualWindowOrderOutAndReopenControlSubscription() async {
        _ = NSApplication.shared
        let factory = RecoverySessionFactory()
        let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 200, height: 100),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let bar = GlobalStatusBar(frame: .zero, sessionFactory: { factory.make($0) })
        window.contentView = bar
        defer { bar.stop(); window.contentView = nil; window.orderOut(nil) }
        window.orderFront(nil)
        await waitUntil { factory.sessions.count == 1 }
        XCTAssertEqual(factory.sessions.count, 1)
        guard factory.sessions.count == 1 else { return }
        window.orderOut(nil)
        await waitUntil { factory.sessions[0].invalidations == 1 }
        XCTAssertEqual(factory.sessions[0].invalidations, 1)
        window.orderFront(nil)
        await waitUntil { factory.sessions.count == 2 }
        XCTAssertEqual(factory.sessions.count, 2)
        guard factory.sessions.count == 2 else { return }
        window.close()
        XCTAssertEqual(factory.sessions[1].invalidations, 1)
    }

    func testMiniaturizingStopsAndRestoringResumesSubscription() {
        let factory = RecoverySessionFactory()
        let window = StatusVisibilityWindow()
        let bar = GlobalStatusBar(frame: .zero, sessionFactory: { factory.make($0) })
        window.contentView = bar
        defer { bar.stop(); window.contentView = nil }
        window.reportVisible(true)
        XCTAssertEqual(factory.sessions.count, 1)
        guard factory.sessions.count == 1 else { return }
        window.testMiniaturized = true
        NotificationCenter.default.post(name: NSWindow.didMiniaturizeNotification, object: window)
        XCTAssertEqual(factory.sessions[0].invalidations, 1)
        window.testMiniaturized = false
        NotificationCenter.default.post(name: NSWindow.didDeminiaturizeNotification, object: window)
        XCTAssertEqual(factory.sessions.count, 2)
    }

    func testEOFReconnectsOnlyWhileWindowIsVisible() async {
        let factory = RecoverySessionFactory()
        let window = StatusVisibilityWindow()
        window.testVisible = true
        let bar = GlobalStatusBar(frame: .zero, sessionFactory: { factory.make($0) })
        window.contentView = bar
        bar.start()
        defer { bar.stop(); window.contentView = nil }
        guard factory.sessions.count == 1 else { XCTFail("Один активный stream"); return }
        factory.delegate(at: 0)._testComplete()
        await fulfillment(of: [factory.secondStarted], timeout: 10)
        XCTAssertEqual(factory.sessions.count, 2)
        window.reportVisible(false)
        XCTAssertEqual(factory.sessions.last?.invalidations, 1)
    }

    func testRemovingOwnerInvalidatesTimerAndSession() async throws {
        let factory = RecoverySessionFactory()
        let window = StatusVisibilityWindow()
        window.testVisible = true
        var bar: GlobalStatusBar? = GlobalStatusBar(frame: .zero, sessionFactory: { factory.make($0) })
        weak var released = bar
        window.contentView = bar
        bar?.start()
        let timer = try XCTUnwrap(Mirror(reflecting: bar!).children.first { $0.label == "heartbeatTimer" }?.value as? Timer)
        defer { timer.invalidate() }
        XCTAssertTrue(timer.isValid)
        window.contentView = nil
        bar = nil
        await drainCallbacks()
        XCTAssertNil(released)
        XCTAssertFalse(timer.isValid, "RunLoop не должен удерживать забытый repeating Timer")
        XCTAssertEqual(factory.sessions.first?.invalidations, 1)
    }

    func testWindowObservationDoesNotRetainWindowOrBar() async {
        let factory = RecoverySessionFactory()
        weak var releasedWindow: NSWindow?
        weak var releasedBar: GlobalStatusBar?
        autoreleasepool {
            let window = StatusVisibilityWindow()
            window.testVisible = true
            let bar = GlobalStatusBar(frame: .zero, sessionFactory: { factory.make($0) })
            releasedWindow = window
            releasedBar = bar
            window.contentView = bar
        }
        await drainCallbacks()
        XCTAssertNil(releasedWindow, "Observation не создаёт цикл bar→window→bar")
        XCTAssertNil(releasedBar)
        XCTAssertEqual(factory.sessions.first?.invalidations, 1)
    }

    /// Реальные HTTP/URLSession/парсер/GlobalStatusBar, без production endpoint.
    func testActualBarRecoversAfterLoopbackPeerClosesStream() async throws {
        let server = try SSELoopbackFixture(bodies: [
            "data: {\"op\":\"transcribe_job\"}\n\n",
            "data: {\"op\":\"obsidian_sync\"}\n\n",
        ])
        let serving = Task.detached { server.serveTwoStreams() }
        let window = StatusVisibilityWindow()
        window.testVisible = true
        let bar = GlobalStatusBar(frame: .zero, sessionFactory: { delegate in
            StatusLoopbackSession(delegate: delegate, port: server.port)
        })
        window.contentView = bar
        bar.start()
        defer { bar.stop(); window.contentView = nil }
        let deadline = ContinuousClock.now + .seconds(8)
        func containsStatus(_ view: NSView) -> Bool {
            if let label = view as? NSTextField, label.stringValue == "Obsidian sync" { return true }
            return view.subviews.contains { containsStatus($0) }
        }
        while !containsStatus(bar), ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertTrue(containsStatus(bar), "Второй payload должен пройти настоящий owner/parser/UI после reconnect")
        let streams = await serving.value
        XCTAssertEqual(streams, 2, "EOF обязан переподключить видимый статус")
    }

    private func waitUntil(_ condition: @MainActor () -> Bool) async {
        let deadline = ContinuousClock.now + .seconds(5)
        while !condition(), ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(20))
        }
    }

    private func drainCallbacks() async {
        for _ in 0..<2 {
            await withCheckedContinuation { continuation in
                DispatchQueue.main.async { continuation.resume() }
            }
        }
    }
}

@MainActor
private final class StatusVisibilityWindow: NSWindow {
    var testVisible = false
    var testMiniaturized = false
    override var isMiniaturized: Bool { testMiniaturized }
    override var isVisible: Bool { testVisible }
    init() {
        _ = NSApplication.shared
        super.init(contentRect: NSRect(x: -10000, y: -10000, width: 200, height: 100),
                   styleMask: [.borderless], backing: .buffered, defer: false)
        isReleasedWhenClosed = false
    }
    func reportVisible(_ visible: Bool) {
        testVisible = visible
        NotificationCenter.default.post(name: NSWindow.didChangeOcclusionStateNotification, object: self)
    }
}

private final class StatusLoopbackSession: ProbeSSESession, @unchecked Sendable {
    private let session: URLSession
    private let port: UInt16
    init(delegate: SSESessionDelegate, port: UInt16) {
        self.port = port
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        session = URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: queue)
    }
    func makeDataTask(with request: URLRequest) -> any ProbeSSETask {
        var loopback = request
        loopback.url = URL(string: "http://127.0.0.1:\(port)/events")!
        return session.dataTask(with: loopback)
    }
    func invalidateAndCancel() { session.invalidateAndCancel() }
}
