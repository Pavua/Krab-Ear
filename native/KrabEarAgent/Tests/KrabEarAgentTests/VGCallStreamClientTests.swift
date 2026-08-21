import XCTest
@testable import KrabEarAgent

private final class FakeConnection: VGWebSocketConnecting {
    var connected = false
    var permanentlyStopped = false
    func connect() { connected = true }
    func permanentStop() { permanentlyStopped = true }
}

final class VGCallStreamClientTests: XCTestCase {
    private func makeClient() -> (VGCallStreamClient, FakeConnection, capture: () -> ((VGWebSocketConnection.Message, UInt64) -> Void)?) {
        let client = VGCallStreamClient()
        let fake = FakeConnection()
        var handler: ((VGWebSocketConnection.Message, UInt64) -> Void)?
        client.connectionFactoryForTests = { _, _, onMessage in
            handler = onMessage
            return fake
        }
        return (client, fake, { handler })
    }

    private func drainMain() { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }

    func test_decodes_and_delivers_on_main_with_generation() {
        let (client, fake, capture) = makeClient()
        var got: [(VGCallEvent, UInt64)] = []
        client.onEvent = { got.append(($0, $1)) }
        client.connect(baseURL: URL(string: "http://127.0.0.1:8090")!, sessionId: "s1", generation: 5, tokenProvider: { "" })
        XCTAssertTrue(fake.connected)
        capture()?(.text(#"{"type":"stt.final","ts":"t","data":{"text":"hola"}}"#), 5)
        drainMain()
        XCTAssertEqual(got.count, 1)
        XCTAssertEqual(got[0].1, 5)
        XCTAssertEqual(got[0].0, .sttFinal(text: "hola", language: nil, confidence: nil))
    }

    func test_stale_generation_dropped_before_render() {
        let (client, _, capture) = makeClient()
        var got = 0
        client.onEvent = { _, _ in got += 1 }
        client.connect(baseURL: URL(string: "http://127.0.0.1:8090")!, sessionId: "s1", generation: 5, tokenProvider: { "" })
        let oldHandler = capture()!
        client.connect(baseURL: URL(string: "http://127.0.0.1:8090")!, sessionId: "s2", generation: 6, tokenProvider: { "" })
        oldHandler(.text(#"{"type":"stt.final","ts":"t","data":{"text":"stale"}}"#), 5)  // событие A в полёте
        drainMain()
        XCTAssertEqual(got, 0, "stt.final чужого поколения не должен дойти до UI")
    }

    func test_callClosed_permanently_stops_connection_and_still_delivers() {
        let (client, fake, capture) = makeClient()
        var got: [VGCallEvent] = []
        client.onEvent = { e, _ in got.append(e) }
        client.connect(baseURL: URL(string: "http://127.0.0.1:8090")!, sessionId: "s1", generation: 7, tokenProvider: { "" })
        capture()?(.text(#"{"type":"call.closed","ts":"t","data":{"session_id":"s1"}}"#), 7)
        drainMain()
        XCTAssertTrue(fake.permanentlyStopped)
        XCTAssertEqual(got, [.callClosed])
    }

    func test_binary_and_malformed_ignored() {
        let (client, _, capture) = makeClient()
        var got = 0
        client.onEvent = { _, _ in got += 1 }
        client.connect(baseURL: URL(string: "http://127.0.0.1:8090")!, sessionId: "s1", generation: 8, tokenProvider: { "" })
        capture()?(.binary(Data([1, 2, 3])), 8)
        capture()?(.text("not json"), 8)
        drainMain()
        XCTAssertEqual(got, 0)
    }

    func test_disconnect_screens_out_old_generation_in_flight() {
        let (client, _, capture) = makeClient()
        var got = 0
        client.onEvent = { _, _ in got += 1 }
        client.connect(baseURL: URL(string: "http://127.0.0.1:8090")!, sessionId: "s1", generation: 9, tokenProvider: { "" })
        let oldHandler = capture()!
        client.disconnect()  // bumps generation from 9 → 10
        // Deliver event with old generation (9) after disconnect
        oldHandler(.text(#"{"type":"stt.final","ts":"t","data":{"text":"dead"}}"#), 9)
        drainMain()
        XCTAssertEqual(got, 0, "stt.final from old generation after disconnect should not reach onEvent")
    }
}
