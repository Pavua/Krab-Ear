import XCTest
@testable import KrabEarAgent

final class HealthMonitorTests: XCTestCase {

    /// При успешном ping HealthMonitor остаётся в .healthy.
    func testHealthyWhenPingSucceeds() async {
        let monitor = HealthMonitor(pingInterval: 0.05, hangThreshold: 2)
        monitor.setPingProvider { return true }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 200_000_000) // ~4 пинга
        let state = await monitor.currentState()
        await monitor.stop()

        XCTAssertEqual(state, .healthy)
    }

    /// 2 fail подряд → состояние .hung.
    func testHungAfterTwoConsecutiveFailures() async {
        let monitor = HealthMonitor(pingInterval: 0.05, hangThreshold: 2)
        monitor.setPingProvider { return false }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 300_000_000)
        let state = await monitor.currentState()
        await monitor.stop()

        XCTAssertEqual(state, .hung)
    }

    /// Один fail + один success → .healthy (счётчик сбрасывается).
    func testCounterResetsOnSuccess() async {
        let monitor = HealthMonitor(pingInterval: 0.05, hangThreshold: 2)
        let counter = TestCounter()
        monitor.setPingProvider { return counter.next() }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 400_000_000)
        let state = await monitor.currentState()
        await monitor.stop()

        XCTAssertEqual(state, .healthy)
    }

    /// onHangDetected callback вызывается ровно один раз при переходе → .hung.
    func testOnHangCallbackFiresOnce() async {
        let monitor = HealthMonitor(pingInterval: 0.05, hangThreshold: 2)
        monitor.setPingProvider { return false }

        let expectation = XCTestExpectation(description: "hang detected")
        await monitor.setOnHangDetected {
            expectation.fulfill()
        }

        await monitor.start()
        await fulfillment(of: [expectation], timeout: 2.0)
        await monitor.stop()
    }
}

/// Helper: возвращает false на 1-м вызове, потом true всегда.
/// Цель: один fail, потом успехи — counter сбрасывается, state остаётся .healthy.
final class TestCounter: @unchecked Sendable {
    private var calls = 0
    private let lock = NSLock()
    func next() -> Bool {
        lock.lock(); defer { lock.unlock() }
        calls += 1
        // 1st: fail; 2nd and after: success — resets consecutive counter
        return calls >= 2
    }
}
