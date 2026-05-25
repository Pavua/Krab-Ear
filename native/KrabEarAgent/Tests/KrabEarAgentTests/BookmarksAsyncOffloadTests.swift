/*
 BookmarksAsyncOffloadTests.swift
 Wave 188 — unit tests for main+Bookmarks.swift async IPC offload (AGENT-3 fix).

 Tests cover:
 1. formatOffsetSec pure logic — no IPC, no AppHang risk
 2. handleBookmarkHotkey guard: IPC is NOT called when isRecording == false
 3. createBookmarkDuringRecording: both IPC calls reach backend via mock provider
    (verifies the Task.detached path is wired, not the old sync callWithRecovery path)
*/

import XCTest
@testable import KrabEarAgent

// MARK: - BookmarksFormatOffsetSecTests

/// Tests for the pure static helper `AgentAppDelegate.formatOffsetSec(_:)`.
/// No main-thread concern — just arithmetic.
@MainActor
final class BookmarksFormatOffsetSecTests: XCTestCase {

    func test_zero() {
        XCTAssertEqual(AgentAppDelegate.formatOffsetSec(0), "0:00")
    }

    func test_59Secs() {
        XCTAssertEqual(AgentAppDelegate.formatOffsetSec(59), "0:59")
    }

    func test_oneMinute() {
        XCTAssertEqual(AgentAppDelegate.formatOffsetSec(60), "1:00")
    }

    func test_oneMinute30Secs() {
        XCTAssertEqual(AgentAppDelegate.formatOffsetSec(90), "1:30")
    }

    func test_59Minutes59Secs_noHoursComponent() {
        // 59*60+59 = 3599 — must NOT show hours
        XCTAssertEqual(AgentAppDelegate.formatOffsetSec(3599), "59:59")
    }

    func test_oneHour() {
        XCTAssertEqual(AgentAppDelegate.formatOffsetSec(3600), "1:00:00")
    }

    func test_twoHours45Mins30Secs() {
        // 2*3600 + 45*60 + 30 = 9930
        XCTAssertEqual(AgentAppDelegate.formatOffsetSec(9930), "2:45:30")
    }

    func test_fractionalSecondsTruncated() {
        // 90.9 → Int(90.9) = 90 → 1:30
        XCTAssertEqual(AgentAppDelegate.formatOffsetSec(90.9), "1:30")
    }
}

// MARK: - BookmarksIPCOffloadTests

/// Verifies that IPC calls in createBookmarkDuringRecording reach the backend
/// through the async path (no sync block on main thread).
///
/// Uses the same TooltipMockSocketProvider-style provider already established
/// in StatusBarTooltipTests.swift — defined here independently to keep tests
/// self-contained.

private final class BookmarkMockSocketProvider: IPCSocketProviding, @unchecked Sendable {
    var responses: [String: String]

    init(responses: [String: String] = [:]) {
        self.responses = responses
    }

    func send(payload: Data, timeoutSec: Int) async throws -> Data {
        let method = Self.extractMethod(from: payload)
        let responseJSON: String
        if let m = method, let r = responses[m] {
            responseJSON = r
        } else {
            responseJSON = #"{"id":"x","ok":true,"result":{}}"#
        }
        return Data((responseJSON + "\n").utf8)
    }

    private static func extractMethod(from payload: Data) -> String? {
        guard
            let dict = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
            let method = dict["method"] as? String
        else { return nil }
        return method
    }
}

@MainActor
final class BookmarksIPCOffloadTests: XCTestCase {

    private func makeDelegate(responses: [String: String] = [:]) -> AgentAppDelegate {
        let provider = BookmarkMockSocketProvider(responses: responses)
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]))
        delegate.ipcClient = IPCClient(socketProvider: provider)
        return delegate
    }

    // MARK: - Guard: hotkey ignored when not recording

    func test_handleBookmarkHotkey_noIpcWhenNotRecording() async throws {
        actor CallCounter {
            var count = 0
            func increment() { count += 1 }
        }
        let counter = CallCounter()

        final class CountingProvider: IPCSocketProviding, @unchecked Sendable {
            let counter: CallCounter
            init(counter: CallCounter) { self.counter = counter }
            func send(payload: Data, timeoutSec: Int) async throws -> Data {
                await counter.increment()
                return Data(#"{"id":"x","ok":true,"result":{}}"#.utf8)
            }
        }

        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]))
        delegate.ipcClient = IPCClient(socketProvider: CountingProvider(counter: counter))

        // isRecording defaults to false on a fresh delegate
        delegate.handleBookmarkHotkey()

        // Give any potential async work a moment to land (it should not)
        try await Task.sleep(for: .milliseconds(100))

        let callCount = await counter.count
        XCTAssertEqual(callCount, 0,
            "No IPC call should be issued when isRecording == false (guard short-circuits)")
    }

    // MARK: - IPC calls are dispatched asynchronously

    func test_createBookmarkDuringRecording_sendsGetRecordingStateThenAddBookmark() async throws {
        let stateResponse = #"{"id":"x","ok":true,"result":{"session_id":"sess-42","elapsed_sec":90.0}}"#
        let bookmarkResponse = #"{"id":"x","ok":true,"result":{"id":"bm-1"}}"#

        // Use an actor to safely accumulate method names from the async send() call.
        actor MethodRecorder {
            var methods: [String] = []
            func append(_ m: String) { methods.append(m) }
        }
        let recorder = MethodRecorder()

        final class OrderedProvider: IPCSocketProviding, @unchecked Sendable {
            let recorder: MethodRecorder
            let stateResponse: String
            let bookmarkResponse: String

            init(recorder: MethodRecorder, stateResponse: String, bookmarkResponse: String) {
                self.recorder = recorder
                self.stateResponse = stateResponse
                self.bookmarkResponse = bookmarkResponse
            }

            func send(payload: Data, timeoutSec: Int) async throws -> Data {
                guard
                    let dict = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
                    let method = dict["method"] as? String
                else { return Data(#"{"id":"x","ok":true,"result":{}}"#.utf8) }

                await recorder.append(method)

                let response = method == "get_recording_state" ? stateResponse : bookmarkResponse
                return Data((response + "\n").utf8)
            }
        }

        let provider = OrderedProvider(
            recorder: recorder,
            stateResponse: stateResponse,
            bookmarkResponse: bookmarkResponse
        )
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]))
        delegate.ipcClient = IPCClient(socketProvider: provider)

        // Trigger async work (isRecording guard bypassed by calling createBookmarkDuringRecording directly)
        delegate.createBookmarkDuringRecording()

        // Allow the Task.detached work to complete
        try await Task.sleep(for: .milliseconds(300))

        let methods = await recorder.methods
        XCTAssertEqual(methods.first, "get_recording_state",
            "First IPC call should be get_recording_state")
        XCTAssertEqual(methods.last, "add_bookmark",
            "Second IPC call should be add_bookmark")
        XCTAssertEqual(methods.count, 2,
            "Exactly 2 IPC calls should be issued: get_recording_state + add_bookmark")
    }
}
