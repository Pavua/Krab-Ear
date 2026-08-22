import Foundation

/// Клиент событий VG WS `/v1/sessions/{id}/stream` (spec §3 комп. 2).
/// Подключается на callAppeared выбранной сессии; авто-реконнект внутри
/// транспорта; call.closed → permanentStop (терминал ЛЮБОГО звонка).
/// Каждое событие — на main со generation-штампом; чужое поколение
/// отбрасывается ДО UI. onConnectionState → бейдж «переподключение…».
final class VGCallStreamClient {
    var onEvent: ((VGCallEvent, UInt64) -> Void)?
    var onConnectionState: ((Bool, UInt64) -> Void)?

    /// Только для тестов; nil → реальный транспорт с autoReconnect.
    var connectionFactoryForTests: ((URL, UInt64,
        @escaping (VGWebSocketConnection.Message, UInt64) -> Void) -> VGWebSocketConnecting)?

    private var connection: VGWebSocketConnecting?
    private(set) var generation: UInt64 = 0

    func connect(baseURL: URL, sessionId: String, generation: UInt64,
                 tokenProvider: @escaping () -> String) {
        disconnect()
        self.generation = generation
        guard let url = VGWebSocketConnection.wsURL(httpBase: baseURL,
                                                    path: "/v1/sessions/\(sessionId)/stream") else { return }
        let handler: (VGWebSocketConnection.Message, UInt64) -> Void = { [weak self] msg, gen in
            guard case .text(let s) = msg, let data = s.data(using: .utf8),
                  let event = VGCallEvent.decode(data) else { return }
            DispatchQueue.main.async {
                guard let self, gen == self.generation else { return }
                if case .callClosed = event { self.connection?.permanentStop() }
                if case .ignored = event { return }
                self.onEvent?(event, gen)
            }
        }
        let conn: VGWebSocketConnecting
        if let factory = connectionFactoryForTests {
            conn = factory(url, generation, handler)
        } else {
            conn = VGWebSocketConnection(
                url: url, generation: generation, autoReconnect: true,
                tokenProvider: tokenProvider, onMessage: handler,
                onStateChange: { [weak self] connected, gen in
                    DispatchQueue.main.async {
                        guard let self, gen == self.generation else { return }
                        self.onConnectionState?(connected, gen)
                    }
                },
                onClose: nil)
        }
        connection = conn
        conn.connect()
    }

    func disconnect() {
        generation &+= 1  // Screen out dead generation: in-flight stamps carry old values and are rejected by the guard
        connection?.permanentStop()
        connection = nil
    }
}
