/*
 IPCClientReconnectE2ETests — E2E integration tests for Phase C C.2 IPC reconnect.

 Verifies that `callWithReconnect` exercises real exponential-backoff retry logic
 using a `FlakyMockSocketProvider` that fails N times then succeeds.  No Python
 backend process required — the mock implements `IPCSocketProviding` in-process.

 Test cases:
   1. recovers after 2 failures — 3 total attempts, elapsed ~750 ms
   2. fails after max 5 retries — 6 total attempts, throws on exhaustion
   3. non-transient errors rethrown immediately — 1 attempt, no retry
*/

import XCTest
@testable import KrabEarAgent

// MARK: - FlakyMockSocketProvider

/// In-process mock that fails the first `failuresBeforeSuccess` attempts with a
/// transient `socketConnectFailed` error, then returns a canned success response.
///
/// Pass `raiseFatalError: true` to simulate a non-transient backend error on the
/// first (and only) attempt — verifying that `callWithReconnect` does NOT retry.
final class FlakyMockSocketProvider: IPCSocketProviding, @unchecked Sendable {

    /// Number of calls made so far (read after the test to verify attempt count).
    private(set) var attemptCount: Int = 0

    private let failuresBeforeSuccess: Int
    private let raiseFatalError: Bool

    /// Lock protecting `attemptCount` across concurrent calls (Swift 6 strict concurrency).
    private let lock = NSLock()

    init(failuresBeforeSuccess: Int, raiseFatalError: Bool = false) {
        self.failuresBeforeSuccess = failuresBeforeSuccess
        self.raiseFatalError = raiseFatalError
    }

    func send(payload: Data, timeoutSec: Int) async throws -> Data {
        let myAttempt: Int = lock.withLock {
            attemptCount += 1
            return attemptCount
        }

        // Fatal (non-transient) error — rethrown immediately by callWithReconnect.
        if raiseFatalError {
            throw IPCError.backendError("method_not_found")
        }

        // Transient error — callWithReconnect will sleep and retry.
        if myAttempt <= failuresBeforeSuccess {
            throw IPCError.socketConnectFailed("mock: backend not ready (attempt \(myAttempt))")
        }

        // Success — return a minimal valid IPC response.
        let response = #"{"id":"mock-1","ok":true,"result":{}}"# + "\n"
        return Data(response.utf8)
    }
}

// MARK: - IPCClientReconnectE2ETests

final class IPCClientReconnectE2ETests: XCTestCase {

    // MARK: 1. Recovers after 2 transient failures

    /// `callWithReconnect` must succeed after failing twice and sleeping the first two
    /// backoff intervals (250 ms + 500 ms = 750 ms total minimum elapsed time).
    func testCallWithReconnect_recoversAfter2Failures() async throws {
        let mockProvider = FlakyMockSocketProvider(failuresBeforeSuccess: 2)
        let client = IPCClient(socketProvider: mockProvider)

        let start = ContinuousClock.now
        let result = try await client.callWithReconnect(method: "ping", params: [:])
        let elapsed = start.duration(to: ContinuousClock.now)

        // Should have received the success response.
        XCTAssertEqual(result["ok"] as? Bool, true, "Expected ok:true in success response")

        // 2 failures → slept 250 ms before attempt 2, 500 ms before attempt 3 → ~750 ms total.
        XCTAssertGreaterThan(
            elapsed,
            .milliseconds(700),
            "Expected at least ~750 ms elapsed for 2 backoff sleeps (actual: \(elapsed))"
        )
        XCTAssertLessThan(
            elapsed,
            .milliseconds(3000),
            "Should not exhaust all 5 retries — expected < 3 s (actual: \(elapsed))"
        )

        // 2 failures + 1 success = 3 total attempts.
        XCTAssertEqual(
            mockProvider.attemptCount,
            3,
            "Expected 3 provider calls (2 failures + 1 success)"
        )
    }

    // MARK: 2. Fails after exhausting max retries

    /// When the backend never recovers, `callWithReconnect` must exhaust all 5 retries
    /// (6 total attempts: 1 initial + 5 backoff) and then throw.
    func testCallWithReconnect_failsAfterMaxRetries() async throws {
        // 999 > 6 total attempts — backend never comes back.
        let mockProvider = FlakyMockSocketProvider(failuresBeforeSuccess: 999)
        let client = IPCClient(socketProvider: mockProvider)

        var caughtError: Error?
        do {
            _ = try await client.callWithReconnect(method: "ping", params: [:])
            XCTFail("Expected an error after exhausting all retries")
        } catch {
            caughtError = error
        }

        // Must have thrown an IPCError (transient variant on exhaustion).
        XCTAssertNotNil(caughtError, "Expected error on exhaustion")
        if let ipcErr = caughtError as? IPCError {
            XCTAssertTrue(
                ipcErr.isTransient,
                "Exhausted transient retries — last error should be transient, got: \(ipcErr)"
            )
        } else {
            XCTFail("Expected IPCError, got: \(String(describing: caughtError))")
        }

        // 1 initial + 5 retries = 6 total attempts.
        XCTAssertEqual(
            mockProvider.attemptCount,
            6,
            "Expected 6 provider calls (1 initial + 5 retries)"
        )
    }

    // MARK: 3. Non-transient errors are rethrown immediately (no retry)

    /// A fatal backend error (non-transient `IPCError.backendError`) must NOT trigger
    /// exponential-backoff retry — it should be rethrown after the very first attempt.
    func testCallWithReconnect_nonTransientErrors_rethrownImmediately() async throws {
        let mockProvider = FlakyMockSocketProvider(failuresBeforeSuccess: 0, raiseFatalError: true)
        let client = IPCClient(socketProvider: mockProvider)

        var caughtError: Error?
        do {
            _ = try await client.callWithReconnect(method: "ping", params: [:])
            XCTFail("Expected a fatal error to be rethrown")
        } catch {
            caughtError = error
        }

        // Must have thrown immediately.
        XCTAssertNotNil(caughtError, "Expected error on fatal backend response")

        // Non-transient error — should NOT have been retried.
        if let ipcErr = caughtError as? IPCError {
            XCTAssertFalse(
                ipcErr.isTransient,
                "Non-transient errors must not trigger retries: \(ipcErr)"
            )
        } else {
            XCTFail("Expected IPCError, got: \(String(describing: caughtError))")
        }

        // Exactly 1 attempt — no retries on fatal errors.
        XCTAssertEqual(
            mockProvider.attemptCount,
            1,
            "Expected exactly 1 provider call — non-transient errors must not be retried"
        )
    }
}
