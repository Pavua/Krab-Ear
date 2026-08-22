import XCTest
@testable import KrabEarAgent

/// Интеграционный прогон против scripts/fake_vg_server.py.
/// Гейт: env KRAB_E2E_VG_PORT — без него все тесты skip (юнит-CI герметичен).
final class CallObserverE2ETests: XCTestCase {
    private var baseURL: URL!

    override func setUpWithError() throws {
        guard let port = ProcessInfo.processInfo.environment["KRAB_E2E_VG_PORT"] else {
            throw XCTSkip("KRAB_E2E_VG_PORT не задан — интеграционный прогон пропущен")
        }
        baseURL = URL(string: "http://127.0.0.1:\(port)")!
    }

    /// T4-хэндофф (ревью прошлых задач): единственный непокрытый юнитами контракт
    /// транспорта — «onStateChange: ровно ОДИН true после РЕАЛЬНОГО хендшейка →
    /// ровно ОДИН false при разрыве/permanentStop». Существующие юнит-тесты
    /// VGWebSocketConnection гоняются против FakeConnection/подставного делегата,
    /// без настоящего сетевого апгрейда до WS. Здесь — VGWebSocketConnection
    /// НАПРЯМУЮ (не через VGCallStreamClient/CallAudioPlayer) против реального
    /// fake VG стрима: настоящий HTTP→WS upgrade, настоящий didOpenWithProtocol.
    func test_transport_onStateChange_exactly_one_true_then_one_false() throws {
        guard let wsURL = VGWebSocketConnection.wsURL(
            httpBase: baseURL, path: "/v1/sessions/e2e-call-1/stream") else {
            XCTFail("не удалось построить ws:// URL"); return
        }

        var states: [(connected: Bool, generation: UInt64)] = []
        let statesLock = NSLock()
        let trueExp = XCTestExpectation(description: "true после реального хендшейка")
        let falseExp = XCTestExpectation(description: "false при permanentStop/разрыве")

        let conn = VGWebSocketConnection(
            url: wsURL, generation: 42, autoReconnect: false, tokenProvider: { "" },
            onMessage: { _, _ in },
            onStateChange: { connected, gen in
                statesLock.lock(); states.append((connected, gen)); statesLock.unlock()
                if connected { trueExp.fulfill() } else { falseExp.fulfill() }
            },
            onClose: nil)

        conn.connect()
        wait(for: [trueExp], timeout: 15)

        // Явный разрыв клиентом — permanentStop() обязан дать РОВНО один false
        // (детерминированный контракт M7: false шлётся ⟺ последнее сообщённое
        // состояние было true; повторный вызов/гонка с серверным закрытием —
        // идемпотентны, см. reportDisconnectIfNeeded).
        conn.permanentStop()
        wait(for: [falseExp], timeout: 15)

        statesLock.lock()
        let snapshot = states
        statesLock.unlock()

        XCTAssertEqual(snapshot.filter { $0.connected }.count, 1, "ровно один true")
        XCTAssertEqual(snapshot.filter { !$0.connected }.count, 1, "ровно один false")
        XCTAssertEqual(snapshot.map(\.connected), [true, false], "порядок: сначала true, потом false")
        XCTAssertTrue(snapshot.allSatisfy { $0.generation == 42 }, "generation-штамп сохранён")
    }

    func test_full_watch_flow_against_fake_vg() throws {
        let fetcher = URLSessionVGSessionFetcher(baseURLProvider: { self.baseURL },
                                                tokenProvider: { "" })
        let watcher = VGSessionWatcher(fetcher: fetcher)

        final class Collector: VGSessionWatcherDelegate {
            var appeared: [VGSessionInfo] = []
            var generation: UInt64 = 0
            let appearExp = XCTestExpectation(description: "appeared")
            func watcherCallAppeared(_ s: VGSessionInfo, generation: UInt64, resurrected: Bool) {
                appeared.append(s); self.generation = generation; appearExp.fulfill()
            }
            func watcherCallUpdated(_ s: VGSessionInfo, generation: UInt64) {}
            func watcherCallGone(sessionId: String, generation: UInt64) {}
            func watcherVGLost(sessionId: String, generation: UInt64) {}
            func watcherAuthRejected() {}
        }
        let collector = Collector()
        watcher.delegate = collector
        watcher.start()
        wait(for: [collector.appearExp], timeout: 15)
        watcher.stop()
        let session = collector.appeared[0]

        // События стрима: финалы + auto_spoken + interrupt + терминальная пара.
        let stream = VGCallStreamClient()
        var events: [VGCallEvent] = []
        let ended = XCTestExpectation(description: "call.ended")
        let closed = XCTestExpectation(description: "call.closed")
        let gotInterrupted = XCTestExpectation(description: "agent.interrupted доехал")
        stream.onEvent = { event, _ in
            events.append(event)
            if case .agentInterrupted = event { gotInterrupted.fulfill() }
            if case .callEnded = event { ended.fulfill() }
            if case .callClosed = event { closed.fulfill() }
        }
        stream.connect(baseURL: baseURL, sessionId: session.id,
                       generation: collector.generation, tokenProvider: { "" })

        // Аудио: метаданные + ≥5 кадров синуса.
        let player = CallAudioPlayer()
        final class CountingEngine: CallAudioEngineProtocol {
            var frames = 0
            let exp = XCTestExpectation(description: "≥5 аудио-кадров")
            func start() throws {}
            func stop() {}
            func schedule(_ samples: [Float]) {
                XCTAssertEqual(samples.count, 800)
                XCTAssertTrue(samples.contains { abs($0) > 0.05 }, "синус, не тишина")
                frames += 1
                if frames == 5 { exp.fulfill() }
            }
        }
        let engine = CountingEngine()
        player.engineFactory = { engine }
        player.startListening(baseURL: baseURL, sessionId: session.id,
                              generation: collector.generation, tokenProvider: { "" })
        wait(for: [engine.exp], timeout: 15)
        // 🔴 Весь скрипт событий обязан доехать ДО hangup — иначе fake-стрим
        // оборвётся на terminal-проверке и auto_spoken/interrupted потеряются.
        wait(for: [gotInterrupted], timeout: 20)

        // Hangup — сервер переводит сессию в stopped → терминальная цепочка стрима.
        let poster = URLSessionVGCommandPoster(tokenProvider: { "" })
        let hungUp = XCTestExpectation(description: "hangup 200")
        poster.hangup(baseURL: baseURL, sessionId: session.id) { result in
            if case .success(200) = result { hungUp.fulfill() }
        }
        wait(for: [hungUp, ended, closed], timeout: 15)

        // Контент-проверки.
        XCTAssertTrue(events.contains { if case .sttFinal(let t, _, _) = $0 { return t.contains("hola") } ; return false })
        XCTAssertTrue(events.contains { if case .agentAutoSpoken = $0 { return true } ; return false })
        XCTAssertTrue(events.contains { if case .agentInterrupted(_, _, let s) = $0 { return s == "Claro, dí" } ; return false })
        XCTAssertFalse(events.contains { if case .ignored = $0 { return true } ; return false },
                       "ignored-события не должны доходить до onEvent")

        // Cost.
        let cost = XCTestExpectation(description: "cost 0.07")
        poster.fetchCostUsd(baseURL: baseURL, sessionId: session.id) { usd in
            XCTAssertEqual(usd ?? -1, 0.07, accuracy: 0.001)
            cost.fulfill()
        }
        wait(for: [cost], timeout: 10)
        stream.disconnect()
        player.stopListening()
    }
}
