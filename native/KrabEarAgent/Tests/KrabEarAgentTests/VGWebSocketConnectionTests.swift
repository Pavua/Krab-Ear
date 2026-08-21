import XCTest
@testable import KrabEarAgent

final class VGWebSocketConnectionTests: XCTestCase {
    func test_backoff_bounds_exponential_capped_with_jitter_band() {
        let b0 = VGWebSocketConnection.backoffBounds(attempt: 0)
        XCTAssertEqual(b0.min, 0.75, accuracy: 0.001)   // 1s −25%
        XCTAssertEqual(b0.max, 1.25, accuracy: 0.001)   // 1s +25%
        let b5 = VGWebSocketConnection.backoffBounds(attempt: 5)
        XCTAssertEqual(b5.min, 22.5, accuracy: 0.001)   // 30s cap −25%
        XCTAssertEqual(b5.max, 37.5, accuracy: 0.001)
        let b99 = VGWebSocketConnection.backoffBounds(attempt: 99)
        XCTAssertEqual(b99.max, 37.5, accuracy: 0.001)  // cap держится
    }

    func test_request_carries_bearer_only_when_token_nonempty() {
        let url = URL(string: "ws://127.0.0.1:8090/v1/sessions/s1/stream")!
        let with = VGWebSocketConnection.makeRequest(url: url, token: "sek")
        XCTAssertEqual(with.value(forHTTPHeaderField: "Authorization"), "Bearer sek")
        let without = VGWebSocketConnection.makeRequest(url: url, token: "")
        XCTAssertNil(without.value(forHTTPHeaderField: "Authorization"))
    }

    func test_wsURL_scheme_swap() {
        let base = URL(string: "http://127.0.0.1:8090")!
        XCTAssertEqual(VGWebSocketConnection.wsURL(httpBase: base, path: "/v1/sessions/a b/stream")?.absoluteString,
                       "ws://127.0.0.1:8090/v1/sessions/a%20b/stream")
        let https = URL(string: "https://vg.local")!
        XCTAssertEqual(VGWebSocketConnection.wsURL(httpBase: https, path: "/x")?.scheme, "wss")
    }

    func test_permanentStop_prevents_reconnect_flag() {
        let conn = VGWebSocketConnection(
            url: URL(string: "ws://127.0.0.1:1/dead")!, generation: 7,
            autoReconnect: true, tokenProvider: { "" },
            onMessage: { _, _ in }, onStateChange: nil, onClose: nil)
        conn.permanentStop()
        let exp = expectation(description: "queue drained")
        conn.testHook_onQueue { XCTAssertTrue(conn.testHook_isStopped); exp.fulfill() }
        wait(for: [exp], timeout: 2)
    }
}
