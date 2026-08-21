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

    // M1: wss не должен даунгрейдиться до ws (сиблинг-баг к https→wss).
    func test_wsURL_does_not_downgrade_existing_wss() {
        let wss = URL(string: "wss://vg.local:8443")!
        XCTAssertEqual(VGWebSocketConnection.wsURL(httpBase: wss, path: "/x")?.scheme, "wss")
    }

    // M3 (renamed from test_permanentStop_prevents_reconnect_flag — этот тест
    // проверяет ровно установку internal-флага `stopped`, поведение самого
    // сокета (реконнект не срабатывает) — это отдельная e2e-проверка T10.
    func test_permanentStop_sets_stopped_flag() {
        let conn = VGWebSocketConnection(
            url: URL(string: "ws://127.0.0.1:1/dead")!, generation: 7,
            autoReconnect: true, tokenProvider: { "" },
            onMessage: { _, _ in }, onStateChange: nil, onClose: nil)
        conn.permanentStop()
        let exp = expectation(description: "queue drained")
        conn.testHook_onQueue { XCTAssertTrue(conn.testHook_isStopped); exp.fulfill() }
        wait(for: [exp], timeout: 2)
    }

    // I1: onStateChange обязан говорить правду — permanentStop эмитит false
    // РОВНО один раз, даже если позвать его дважды подряд (idempotent).
    func test_permanentStop_emits_false_exactly_once_even_when_called_twice() {
        let lock = NSLock()
        var states: [(Bool, UInt64)] = []
        let conn = VGWebSocketConnection(
            url: URL(string: "ws://127.0.0.1:1/dead")!, generation: 7,
            autoReconnect: true, tokenProvider: { "" },
            onMessage: { _, _ in },
            onStateChange: { connected, gen in
                lock.lock(); states.append((connected, gen)); lock.unlock()
            },
            onClose: nil)
        conn.connect()
        conn.permanentStop()
        conn.permanentStop()

        let exp = expectation(description: "queue drained")
        conn.testHook_onQueue { exp.fulfill() }
        wait(for: [exp], timeout: 2)

        lock.lock()
        let falseCount = states.filter { $0.0 == false }.count
        let trueCount = states.filter { $0.0 == true }.count
        lock.unlock()
        // Ровно один false (permanentStop нашёл активный task); true мог
        // не случиться вовсе (дохлый порт — didOpen никогда не срабатывает),
        // но никогда не эмитится СИНХРОННО до реального хендшейка (I1).
        XCTAssertEqual(falseCount, 1)
        XCTAssertEqual(trueCount, 0)
    }

    // I2: deinit не копит session/timer — после permanentStop (контрактный
    // терминал объекта) и снятия последней сильной ссылки инстанс реально
    // освобождается, а счётчик живых ping-таймеров возвращается в 0.
    // Note: без permanentStop объект НЕ деаллоцируется — URLSession держит
    // delegate сильной ссылкой до invalidate, это ожидаемый контракт (см.
    // doc-comment у permanentStop), поэтому тест обязательно зовёт его.
    func test_permanentStop_then_release_deallocates_and_resets_timer_count() {
        weak var weakConn: VGWebSocketConnection?
        autoreleasepool {
            let conn = VGWebSocketConnection(
                url: URL(string: "ws://127.0.0.1:1/dead")!, generation: 42,
                autoReconnect: false, tokenProvider: { "" },
                onMessage: { _, _ in }, onStateChange: nil, onClose: nil)
            weakConn = conn
            conn.connect()
            let expConnected = expectation(description: "connect queued")
            conn.testHook_onQueue { expConnected.fulfill() }
            wait(for: [expConnected], timeout: 2)

            conn.permanentStop()
            let expStopped = expectation(description: "stop queued")
            conn.testHook_onQueue { expStopped.fulfill() }
            wait(for: [expStopped], timeout: 2)
        }
        // Даём ARC/URLSession-инвалидации время осесть после выхода из скоупа.
        let expSettle = expectation(description: "settle")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { expSettle.fulfill() }
        wait(for: [expSettle], timeout: 2)

        XCTAssertNil(weakConn, "инстанс обязан деаллоцироваться после permanentStop + снятия сильной ссылки")
        XCTAssertEqual(VGWebSocketConnection.liveTimerCount, 0)
    }
}
