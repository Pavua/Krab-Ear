import XCTest
@testable import KrabEarAgent

private final class SpyEngine: CallAudioEngineProtocol {
    var started = 0, stoppedCount = 0
    var scheduled: [[Float]] = []
    func start() throws { started += 1 }
    func stop() { stoppedCount += 1 }
    func schedule(_ samples: [Float]) { scheduled.append(samples) }
}

private final class FakeConn: VGWebSocketConnecting {
    var connects = 0, stops = 0
    func connect() { connects += 1 }
    func permanentStop() { stops += 1 }
}

final class CallAudioPlayerTests: XCTestCase {
    private var engine = SpyEngine()
    private var conn = FakeConn()
    private var onMessage: ((VGWebSocketConnection.Message, UInt64) -> Void)?
    private var onClose: ((Int, UInt64) -> Void)?
    private var states: [(CallAudioPlayer.ListenState, UInt64)] = []

    private func makePlayer() -> CallAudioPlayer {
        let p = CallAudioPlayer()
        p.engineFactory = { self.engine }
        p.connectionFactoryForTests = { _, _, msg, close in
            self.onMessage = msg; self.onClose = close
            return self.conn
        }
        p.onStateChange = { self.states.append(($0, $1)) }
        return p
    }

    private func start(_ p: CallAudioPlayer, gen: UInt64 = 1) {
        p.startListening(baseURL: URL(string: "http://127.0.0.1:8090")!,
                         sessionId: "s1", generation: gen, tokenProvider: { "" })
        drain()
    }

    private func drain() { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }

    func test_metadata_then_frames_reach_engine() {
        let p = makePlayer()
        start(p)
        XCTAssertEqual(conn.connects, 1)
        onMessage?(.text(#"{"format":"mulaw_8k","frame_ms":100}"#), 1); drain()
        XCTAssertEqual(states.last?.0, .listening)
        onMessage?(.binary(Data(repeating: 0xFF, count: 800)), 1); drain()
        XCTAssertEqual(engine.scheduled.count, 1)
        XCTAssertEqual(engine.scheduled[0].count, 800)
        XCTAssertEqual(engine.scheduled[0][0], 0.0)  // 0xFF → 0
    }

    func test_wrong_metadata_fails_closed() {
        let p = makePlayer()
        start(p)
        onMessage?(.text(#"{"format":"opus_48k","frame_ms":20}"#), 1); drain()
        XCTAssertEqual(states.last?.0, .failed)
        XCTAssertEqual(conn.stops, 1)
    }

    func test_single_flight_double_start_one_connection() {
        let p = makePlayer()
        start(p); start(p)
        XCTAssertEqual(conn.connects, 1, "двойной клик 🔊 не смеет съесть оба subscriber-слота")
    }

    func test_new_generation_tears_old_connection_first() {
        let p = makePlayer()
        start(p, gen: 1)
        start(p, gen: 2)
        XCTAssertEqual(conn.stops, 1)
        XCTAssertEqual(conn.connects, 2)
    }

    func test_close_1013_is_subscriberLimit_no_retry() {
        let p = makePlayer()
        start(p)
        onClose?(1013, 1); drain()
        XCTAssertEqual(states.last?.0, .subscriberLimit)
        XCTAssertEqual(conn.connects, 1, "retry только по явному повторному клику")
    }

    func test_close_1000_returns_idle_and_stops_engine() {
        let p = makePlayer()
        start(p)
        onMessage?(.text(#"{"format":"mulaw_8k","frame_ms":100}"#), 1); drain()
        onClose?(1000, 1); drain()
        XCTAssertEqual(states.last?.0, .idle)
        XCTAssertEqual(engine.stoppedCount, 1)
    }

    func test_stale_generation_frames_dropped() {
        let p = makePlayer()
        start(p, gen: 1)
        onMessage?(.text(#"{"format":"mulaw_8k","frame_ms":100}"#), 1); drain()
        let oldMessage = onMessage
        start(p, gen: 2)
        oldMessage?(.binary(Data(repeating: 0x00, count: 800)), 1); drain()
        XCTAssertTrue(engine.scheduled.isEmpty, "кадр поколения 1 после старта поколения 2")
    }

    func test_stopListening_idempotent() {
        let p = makePlayer()
        start(p)
        p.stopListening(); p.stopListening(); drain()
        XCTAssertEqual(conn.stops, 1)
        XCTAssertEqual(states.last?.0, .idle)
    }

    func test_empty_binary_frame_does_not_crash() {
        let p = makePlayer()
        start(p)
        onMessage?(.text(#"{"format":"mulaw_8k","frame_ms":100}"#), 1); drain()
        XCTAssertEqual(states.last?.0, .listening)
        // Сеть: пустой кадр не должен вызвать краш (force-unwrap guard)
        onMessage?(.binary(Data()), 1); drain()
        XCTAssertTrue(engine.scheduled.isEmpty, "пустой frame не должен запланирован")
        XCTAssertEqual(states.last?.0, .listening, "состояние остаётся .listening")
        // Процесс не упал (иначе тест не дошёл бы сюда)
    }
}
