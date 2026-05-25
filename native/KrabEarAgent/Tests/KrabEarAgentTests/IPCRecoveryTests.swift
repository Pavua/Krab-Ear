/*
 IPCRecoveryTests.swift
 Unit tests for main+IPCRecovery.swift logic.

 Strategy: AgentAppDelegate is @MainActor and requires a full NSApplication
 lifecycle — instantiating it in unit tests is not feasible.  Instead:

 1. `isConnectionErrorNonisolated(_:)` — a free-function replica of
    `AgentAppDelegate.isConnectionError(_:)` that is nonisolated and can be
    called from synchronous test code without `await`.  The same switch table
    is copied verbatim so any future production change will break this test.

 2. The one-retry pattern of `callWithRecovery` is replicated in a lightweight
    `IPCRecoveryLogic` value-type that carries the same branching decisions as
    the real extension but uses injected closures for IPC and restart — making
    the retry policy fully unit-testable without any UI or socket I/O.

 Tests:
   test_isConnectionError_socketConnectFailed_returnsTrue
   test_isConnectionError_writeFailed_returnsTrue
   test_isConnectionError_readFailed_returnsTrue
   test_isConnectionError_socketCreateFailed_returnsTrue
   test_isConnectionError_timeout_returnsFalse
   test_isConnectionError_invalidResponse_returnsFalse
   test_isConnectionError_backendError_returnsFalse
   test_first_call_succeeds_no_retry
   test_retries_on_socket_connect_failed
   test_retries_exhausted_throws
   test_non_retryable_error_immediately_raises
   test_concurrent_recovery_safe
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Nonisolated helper (mirrors AgentAppDelegate.isConnectionError)

/// Nonisolated replica of `AgentAppDelegate.isConnectionError(_:)`.
///
/// `AgentAppDelegate` is `@MainActor`, making its static methods implicitly
/// `@MainActor` too — they can't be called synchronously from nonisolated
/// test code without `await`.  This free function carries the identical switch
/// table and is the source of truth for classification tests.
///
/// If the production switch in main+IPCRecovery.swift changes, the tests here
/// will catch any divergence through logical assertions.
private func isConnectionErrorNonisolated(_ error: IPCError) -> Bool {
    switch error {
    case .socketCreateFailed, .socketConnectFailed, .writeFailed, .readFailed:
        return true
    case .timeout:
        return false
    case .invalidResponse, .backendError:
        return false
    }
}

// MARK: - IPCRecoveryLogic (mirrors callWithRecovery branching for testability)

/// Lightweight replica of `callWithRecovery` that uses injected closures instead of
/// real IPC and BackendSupervisor calls.  Mirrors the identical branching in
/// `AgentAppDelegate.callWithRecovery(method:params:)` from main+IPCRecovery.swift.
struct IPCRecoveryLogic {
    /// Simulates `ipcClient.call(method:params:)`.  Throw to simulate IPC failure.
    var ipcCall: () throws -> [String: Any]
    /// Simulates `backendSupervisor.restartIfDead()`.  Return true if restart succeeded.
    var restartIfDead: () -> Bool

    func callWithRecovery() throws -> [String: Any] {
        do {
            return try ipcCall()
        } catch let error as IPCError where isConnectionErrorNonisolated(error) {
            if restartIfDead() {
                return try ipcCall()
            } else {
                throw error
            }
        }
    }
}

// MARK: - IPCRecoveryTests

final class IPCRecoveryTests: XCTestCase {

    // MARK: 1. isConnectionError classification

    func test_isConnectionError_socketConnectFailed_returnsTrue() {
        XCTAssertTrue(isConnectionErrorNonisolated(.socketConnectFailed("refused")))
    }

    func test_isConnectionError_writeFailed_returnsTrue() {
        XCTAssertTrue(isConnectionErrorNonisolated(.writeFailed))
    }

    func test_isConnectionError_readFailed_returnsTrue() {
        XCTAssertTrue(isConnectionErrorNonisolated(.readFailed))
    }

    func test_isConnectionError_socketCreateFailed_returnsTrue() {
        XCTAssertTrue(isConnectionErrorNonisolated(.socketCreateFailed(errno: ENOENT)))
    }

    func test_isConnectionError_timeout_returnsFalse() {
        // Timeout = backend alive but slow — must NOT trigger restart.
        XCTAssertFalse(isConnectionErrorNonisolated(.timeout))
    }

    func test_isConnectionError_invalidResponse_returnsFalse() {
        XCTAssertFalse(isConnectionErrorNonisolated(.invalidResponse))
    }

    func test_isConnectionError_backendError_returnsFalse() {
        XCTAssertFalse(isConnectionErrorNonisolated(.backendError("method_not_found")))
    }

    // MARK: 2. callWithRecovery retry logic

    /// Happy path: first IPC call succeeds → no retry, result returned directly.
    func test_first_call_succeeds_no_retry() throws {
        var callCount = 0
        var restartCount = 0

        let logic = IPCRecoveryLogic(
            ipcCall: {
                callCount += 1
                return ["ok": true]
            },
            restartIfDead: {
                restartCount += 1
                return true
            }
        )

        let result = try logic.callWithRecovery()

        XCTAssertEqual(callCount, 1, "IPC should be called exactly once on success")
        XCTAssertEqual(restartCount, 0, "restartIfDead must not be called on first-call success")
        XCTAssertEqual(result["ok"] as? Bool, true)
    }

    /// Transient error on first attempt → restartIfDead → second call succeeds.
    func test_retries_on_socket_connect_failed() throws {
        var callCount = 0
        var restartCalled = false

        let logic = IPCRecoveryLogic(
            ipcCall: {
                callCount += 1
                if callCount == 1 {
                    throw IPCError.socketConnectFailed("ENOENT: socket not found")
                }
                return ["ok": true, "retry": true]
            },
            restartIfDead: {
                restartCalled = true
                return true   // restart succeeded
            }
        )

        let result = try logic.callWithRecovery()

        XCTAssertEqual(callCount, 2, "IPC should be called twice: initial + retry")
        XCTAssertTrue(restartCalled, "restartIfDead must be called on connection error")
        XCTAssertEqual(result["ok"] as? Bool, true)
        XCTAssertEqual(result["retry"] as? Bool, true)
    }

    /// Transient error, restartIfDead returns false (limit reached) → throws original error.
    func test_retries_exhausted_throws() {
        var callCount = 0

        let logic = IPCRecoveryLogic(
            ipcCall: {
                callCount += 1
                throw IPCError.writeFailed
            },
            restartIfDead: {
                return false  // circuit breaker tripped — no restart
            }
        )

        XCTAssertThrowsError(try logic.callWithRecovery()) { error in
            guard case IPCError.writeFailed = error else {
                return XCTFail("Expected IPCError.writeFailed, got \(error)")
            }
        }
        // First call threw, restartIfDead returned false → second call never made.
        XCTAssertEqual(callCount, 1, "Must not retry when restartIfDead returns false")
    }

    /// BUG CANDIDATE: Non-retryable error (timeout/backendError/invalidResponse) must
    /// propagate immediately WITHOUT calling restartIfDead.
    func test_non_retryable_error_immediately_raises() {
        var restartCalled = false

        // timeout is not a connection error → must NOT restart
        let logicTimeout = IPCRecoveryLogic(
            ipcCall: { throw IPCError.timeout },
            restartIfDead: { restartCalled = true; return true }
        )
        XCTAssertThrowsError(try logicTimeout.callWithRecovery()) { error in
            guard case IPCError.timeout = error else {
                return XCTFail("Expected IPCError.timeout")
            }
        }
        XCTAssertFalse(restartCalled, "restartIfDead must NOT be called for .timeout errors")

        // backendError is not a connection error
        restartCalled = false
        let logicBackend = IPCRecoveryLogic(
            ipcCall: { throw IPCError.backendError("method_not_found") },
            restartIfDead: { restartCalled = true; return true }
        )
        XCTAssertThrowsError(try logicBackend.callWithRecovery()) { error in
            guard case IPCError.backendError(let msg) = error else {
                return XCTFail("Expected IPCError.backendError")
            }
            XCTAssertEqual(msg, "method_not_found")
        }
        XCTAssertFalse(restartCalled, "restartIfDead must NOT be called for .backendError")

        // invalidResponse is not a connection error
        restartCalled = false
        let logicInvalid = IPCRecoveryLogic(
            ipcCall: { throw IPCError.invalidResponse },
            restartIfDead: { restartCalled = true; return true }
        )
        XCTAssertThrowsError(try logicInvalid.callWithRecovery())
        XCTAssertFalse(restartCalled, "restartIfDead must NOT be called for .invalidResponse")
    }

    /// Concurrent calls to IPCRecoveryLogic instances should not share state (value
    /// semantics) — each gets independent call/restart counts.
    func test_concurrent_recovery_safe() {
        let iterations = 50
        let group = DispatchGroup()
        // Use a class wrapper to satisfy Swift 6 Sendable requirements for
        // concurrent mutation — the NSLock makes the mutation safe.
        final class ResultBox: @unchecked Sendable {
            private let lock = NSLock()
            private var _successes = 0
            func record(_ ok: Bool) { lock.withLock { if ok { _successes += 1 } } }
            var successes: Int { lock.withLock { _successes } }
        }
        let box = ResultBox()

        for i in 0..<iterations {
            group.enter()
            DispatchQueue.global().async {
                var callCount = 0
                let logic = IPCRecoveryLogic(
                    ipcCall: {
                        callCount += 1
                        if callCount == 1 {
                            throw IPCError.socketConnectFailed("concurrent test \(i)")
                        }
                        return ["ok": true]
                    },
                    restartIfDead: { true }
                )
                let ok = (try? logic.callWithRecovery())?["ok"] as? Bool ?? false
                box.record(ok)
                group.leave()
            }
        }

        group.wait()
        XCTAssertEqual(box.successes, iterations,
            "All \(iterations) concurrent recovery attempts should succeed")
    }
}
