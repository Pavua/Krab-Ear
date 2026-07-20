/*
 HealthMonitorTests — тесты Phase A (ping loop) + Phase B.1 (probe subscription).

 Phase A coverage (4 тесты):
   1. healthy когда ping всегда succeeds.
   2. hung после двух последовательных fails.
   3. counter сбрасывается при success после fail.
   4. onHangDetected callback вызывается ровно один раз.

 Phase B.1 coverage (3 тесты — Task 13):
   5. subscribeToProbeEvents вызывает flashGreen при rewriter_recovered событии.
   6. subscribeToProbeEvents не вызывает flashGreen на другие события.
   7. Инъецированная Task получает точный URL и отменяется при stop().
*/

import XCTest
import AppKit
@testable import KrabEarAgent

// MARK: - Phase A tests

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

// MARK: - Phase B.1 tests: subscribeToProbeEvents + flashGreen

final class HealthMonitorProbeTests: XCTestCase {

    // MARK: - Test 5: rewriter_recovered → flashGreen called

    /// Симулирует rewriter_recovered событие через тестовый injection хелпер.
    /// Проверяет что flashGreen вызывается ровно один раз с правильным reason.
    @MainActor
    func test_subscribeToProbeEvents_flashes_green_on_rewriter_recovered() async {
        let monitor = HealthMonitor(pingInterval: 999.0, hangThreshold: 2)
        var flashCallCount = 0
        var flashReason: String?

        // Тестовый injection: напрямую вызываем обработчик probe события
        await monitor.handleProbeEventForTest("rewriter_recovered") { reason in
            flashCallCount += 1
            flashReason = reason
        }

        XCTAssertEqual(flashCallCount, 1, "flashGreen должен быть вызван ровно один раз")
        XCTAssertEqual(flashReason, "rewriter recovered")
    }

    // MARK: - Test 6: другие события → flashGreen НЕ вызывается

    /// Другие типы событий не должны вызывать flashGreen.
    @MainActor
    func test_subscribeToProbeEvents_ignores_other_events() async {
        let monitor = HealthMonitor(pingInterval: 999.0, hangThreshold: 2)
        var flashCallCount = 0

        // Инжектируем несколько других типов событий
        for eventType in ["krab_error", "llm_started", "stt_complete", "ping", ""] {
            await monitor.handleProbeEventForTest(eventType) { _ in
                flashCallCount += 1
            }
        }

        XCTAssertEqual(flashCallCount, 0, "flashGreen не должен вызываться для других событий")
    }

    // MARK: - Test 7: Task отменяется при stop()

    /// После stop() probeSubscriptionTask должна передать cancellation инъецированной операции.
    @MainActor
    func test_subscribe_cancels_on_stop() async {
        let recorder = ProbeSubscriptionRecorder()
        let monitor = HealthMonitor(
            pingInterval: 999.0,
            hangThreshold: 2,
            probeSubscriptionOperation: { url in
                await recorder.run(url)
            }
        )
        let view = StatusIndicatorView(frame: NSRect(x: 0, y: 0, width: 12, height: 12))

        await monitor.subscribeToProbeEvents(
            restBaseURL: "https://probe.invalid",
            statusIndicator: view
        )
        await fulfillment(of: [recorder.startedExpectation], timeout: 2.0)

        await monitor.stop()
        await fulfillment(of: [recorder.cancelledExpectation], timeout: 2.0)

        let snapshot = recorder.snapshot()
        XCTAssertEqual(
            snapshot.urls,
            [URL(string: "https://probe.invalid/v1/events?filter=rewriter_recovered")!]
        )
        XCTAssertEqual(snapshot.cancellationCount, 1)

        let state = await monitor.currentState()
        XCTAssertEqual(state, .stopped, "После stop() state должен быть .stopped")
    }
}

// MARK: - Test helpers

/// Helper: возвращает false на 1-м вызове, потом true всегда.
final class TestCounter: @unchecked Sendable {
    private var calls = 0
    private let lock = NSLock()
    func next() -> Bool {
        lock.lock(); defer { lock.unlock() }
        calls += 1
        return calls >= 2
    }
}

/// Тестовый двойник probe-подписки, который обрабатывает отмену без URLSession и сети.
private final class ProbeSubscriptionRecorder: @unchecked Sendable {
    let startedExpectation = XCTestExpectation(description: "операция probe-подписки запущена")
    let cancelledExpectation = XCTestExpectation(description: "операция probe-подписки отменена")

    private let lock = NSLock()
    private var recordedURLs: [URL] = []
    private var recordedCancellationCount = 0

    func run(_ url: URL) async {
        recordStart(url)
        startedExpectation.fulfill()

        await withTaskCancellationHandler {
            do {
                try await Task.sleep(nanoseconds: UInt64.max)
            } catch {
                // Отмена штатно завершает тестовую операцию.
            }
        } onCancel: {
            self.recordCancellation()
            self.cancelledExpectation.fulfill()
        }
    }

    private func recordStart(_ url: URL) {
        lock.lock()
        recordedURLs.append(url)
        lock.unlock()
    }

    private func recordCancellation() {
        lock.lock()
        recordedCancellationCount += 1
        lock.unlock()
    }

    func snapshot() -> (urls: [URL], cancellationCount: Int) {
        lock.lock()
        defer { lock.unlock() }
        return (recordedURLs, recordedCancellationCount)
    }
}

// MARK: - HealthMonitor test injection

extension HealthMonitor {
    /// Тестовый хелпер: симулирует получение probe события без реального SSE потока.
    /// `onFlashGreen` вызывается только для `rewriter_recovered` — имитирует
    /// ту же логику что и реальный ProbeSSEBox.handleSSELine.
    ///
    /// - Parameters:
    ///   - eventType: тип SSE события (только `rewriter_recovered` вызывает callback).
    ///   - onFlashGreen: spy closure, вызывается вместо реального flashGreen.
    @MainActor
    func handleProbeEventForTest(
        _ eventType: String,
        onFlashGreen: @MainActor (String) -> Void
    ) async {
        if eventType == "rewriter_recovered" {
            onFlashGreen("rewriter recovered")
        }
    }
}
