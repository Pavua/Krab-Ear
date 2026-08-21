import Foundation

protocol VGWebSocketConnecting: AnyObject {
    func connect()
    func permanentStop()
}

/// Общий WS-транспорт двух клиентов VG (events-stream + audio-monitor).
/// Bearer-заголовок из tokenProvider (кэш креденшела W1892, читается на
/// каждом коннекте), exp backoff 1→30с ±25% джиттера, ping каждые 25с.
/// Каждое сообщение доставляется со generation-штампом; отмена ВСЕГДА
/// инвалидирует ping-таймер (§4 спеки: таймеры не копятся между реконнектами).
final class VGWebSocketConnection: NSObject, VGWebSocketConnecting {
    enum Message { case text(String); case binary(Data) }

    private let url: URL
    let generation: UInt64
    private let autoReconnect: Bool
    private let tokenProvider: () -> String
    private let onMessage: (Message, UInt64) -> Void
    private let onStateChange: ((Bool, UInt64) -> Void)?
    private let onClose: ((Int, UInt64) -> Void)?

    private let queue = DispatchQueue(label: "krab.vg.ws")
    private lazy var session = URLSession(configuration: .ephemeral)
    private var task: URLSessionWebSocketTask?
    private var pingTimer: DispatchSourceTimer?
    private var reconnectAttempt = 0
    private var stopped = false

    init(url: URL, generation: UInt64, autoReconnect: Bool,
         tokenProvider: @escaping () -> String,
         onMessage: @escaping (Message, UInt64) -> Void,
         onStateChange: ((Bool, UInt64) -> Void)?,
         onClose: ((Int, UInt64) -> Void)?) {
        self.url = url
        self.generation = generation
        self.autoReconnect = autoReconnect
        self.tokenProvider = tokenProvider
        self.onMessage = onMessage
        self.onStateChange = onStateChange
        self.onClose = onClose
        super.init()
    }

    static func backoffBounds(attempt: Int) -> (min: Double, max: Double) {
        let base = Swift.min(30.0, pow(2.0, Double(Swift.min(attempt, 30))))
        return (base * 0.75, base * 1.25)
    }

    static func makeRequest(url: URL, token: String) -> URLRequest {
        var req = URLRequest(url: url)
        if !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return req
    }

    static func wsURL(httpBase: URL, path: String) -> URL? {
        guard var comps = URLComponents(url: httpBase, resolvingAgainstBaseURL: false) else { return nil }
        comps.scheme = (comps.scheme == "https") ? "wss" : "ws"
        comps.path = path
        return comps.url
    }

    func connect() { queue.async { self.openLocked() } }

    /// Терминал поколения / call.closed: больше НИКОГДА не реконнектится.
    func permanentStop() {
        queue.async {
            self.stopped = true
            self.teardownLocked()
        }
    }

    private func openLocked() {
        guard !stopped else { return }
        teardownLocked()
        let req = Self.makeRequest(url: url, token: tokenProvider())
        let t = session.webSocketTask(with: req)
        task = t
        t.resume()
        startPingLocked(for: t)
        onStateChange?(true, generation)
        receiveLoop(t)
    }

    private func receiveLoop(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                guard t === self.task, !self.stopped else { return }
                switch result {
                case .success(let msg):
                    self.reconnectAttempt = 0
                    switch msg {
                    case .string(let s): self.onMessage(.text(s), self.generation)
                    case .data(let d): self.onMessage(.binary(d), self.generation)
                    @unknown default: break
                    }
                    self.receiveLoop(t)
                case .failure:
                    let code = t.closeCode.rawValue
                    self.onClose?(code, self.generation)
                    self.onStateChange?(false, self.generation)
                    self.teardownLocked()
                    guard self.autoReconnect, !self.stopped else { return }
                    let bounds = Self.backoffBounds(attempt: self.reconnectAttempt)
                    self.reconnectAttempt += 1
                    let delay = Double.random(in: bounds.min...bounds.max)
                    self.queue.asyncAfter(deadline: .now() + delay) { [weak self] in
                        self?.openLocked()
                    }
                }
            }
        }
    }

    private func startPingLocked(for t: URLSessionWebSocketTask) {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 25, repeating: 25)
        timer.setEventHandler { [weak self, weak t] in
            guard let self, let t, t === self.task, !self.stopped else { return }
            t.sendPing { _ in }
        }
        timer.resume()
        pingTimer = timer
    }

    private func teardownLocked() {
        pingTimer?.cancel()
        pingTimer = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    // MARK: - Test hooks (только для unit-тестов)
    func testHook_onQueue(_ block: @escaping () -> Void) { queue.async(execute: block) }
    var testHook_isStopped: Bool { stopped }
}
