// BookmarkResponseTests.swift — классификация ответа add_bookmark.
// Бэкенд при privacy/лимите отвечает {"ok": false, "reason": ...}; раньше агент
// считал любой непустой ответ успехом и показывал «📌».

import XCTest
@testable import KrabEarAgent

final class BookmarkResponseTests: XCTestCase {

    func test_flatSuccess() {
        let r: [String: Any] = ["bookmark": ["id": "b1", "offset_sec": 3.0]]
        XCTAssertEqual(AgentAppDelegate.classifyBookmarkResponse(r), .success)
    }

    func test_wrappedSuccess() {
        let r: [String: Any] = ["ok": true, "result": ["bookmark": ["id": "b1"]]]
        XCTAssertEqual(AgentAppDelegate.classifyBookmarkResponse(r), .success)
    }

    func test_privacyRefusalFlat() {
        let r: [String: Any] = ["ok": false, "reason": "privacy_mode_active"]
        XCTAssertEqual(AgentAppDelegate.classifyBookmarkResponse(r), .refused("privacy_mode_active"))
    }

    func test_limitRefusalWrapped() {
        let r: [String: Any] = ["ok": true, "result": ["ok": false, "reason": "limit_exceeded"]]
        XCTAssertEqual(AgentAppDelegate.classifyBookmarkResponse(r), .refused("limit_exceeded"))
    }

    func test_refusalWithoutReason() {
        let r: [String: Any] = ["ok": false]
        XCTAssertEqual(AgentAppDelegate.classifyBookmarkResponse(r), .refused("refused"))
    }

    func test_emptyDictIsNotSuccess() {
        XCTAssertEqual(AgentAppDelegate.classifyBookmarkResponse([String: Any]()), .refused("invalid_response"))
    }

    func test_emptyBookmarkIsNotSuccess() {
        let r: [String: Any] = ["bookmark": [String: Any]()]
        XCTAssertEqual(AgentAppDelegate.classifyBookmarkResponse(r), .refused("invalid_response"))
    }

    func test_nonDictIsNotSuccess() {
        XCTAssertEqual(AgentAppDelegate.classifyBookmarkResponse(nil), .refused("invalid_response"))
        XCTAssertEqual(AgentAppDelegate.classifyBookmarkResponse("ok"), .refused("invalid_response"))
    }
}
