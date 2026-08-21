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
    // Не lazy: session нужна СРАЗУ после super.init() (delegate = self), иначе
    // deinit мог бы форсировать создание session только затем, чтобы её же
    // инвалидировать (пустая работа при объекте, который так и не подключался).
    private var session: URLSession!
    private var task: URLSessionWebSocketTask?
    private var pingTimer: DispatchSourceTimer?
    private var reconnectAttempt = 0
    private var stopped = false
    /// Последнее ЭМИТИРОВАННОЕ наружу состояние — единая точка правды для
    /// onStateChange, чтобы не слать false (или true) дважды подряд.
    private var lastReportedState: Bool?
    /// Гасит спам NSLog при затяжной серии ретраев одного и того же обрыва:
    /// один лог на смену состояния, не один на каждый backoff-цикл.
    private var loggedFailureSinceLastSuccess = false

    #if DEBUG
    // Наблюдаемый инвариант для тестов: сколько ping-таймеров сейчас живо
    // среди ВСЕХ инстансов. Мутации из deinit происходят не на `queue`,
    // поэтому счётчик защищён отдельным локом, а не serial-очередью.
    private static let liveTimerCountLock = NSLock()
    private static var _liveTimerCount = 0
    static var liveTimerCount: Int {
        liveTimerCountLock.lock(); defer { liveTimerCountLock.unlock() }
        return _liveTimerCount
    }
    private static func bumpLiveTimerCount(_ delta: Int) {
        liveTimerCountLock.lock(); _liveTimerCount += delta; liveTimerCountLock.unlock()
    }
    #endif

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
        self.session = URLSession(configuration: .ephemeral, delegate: self, delegateQueue: nil)
    }

    deinit {
        if pingTimer != nil {
            #if DEBUG
            Self.bumpLiveTimerCount(-1)
            #endif
        }
        pingTimer?.cancel()
        task?.cancel(with: .goingAway, reason: nil)
        session.invalidateAndCancel()
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
        // wss остаётся wss (никогда не даунгрейдить уже-защищённую схему);
        // всё прочее (http и любая другая) → обычный ws.
        comps.scheme = ["https", "wss"].contains(comps.scheme ?? "") ? "wss" : "ws"
        comps.path = path
        return comps.url
    }

    func connect() { queue.async { self.openLocked() } }

    /// Терминал поколения / call.closed: больше НИКОГДА не реконнектится.
    /// Объект одноразовый per-generation — переиспользование после
    /// permanentStop не поддерживается (contract).
    func permanentStop() {
        queue.async {
            let hadTask = self.task != nil
            self.stopped = true
            self.teardownLocked()
            self.session.invalidateAndCancel()
            if hadTask, self.lastReportedState != false {
                self.lastReportedState = false
                self.onStateChange?(false, self.generation)
            }
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
        // onStateChange(true) больше НЕ эмитится здесь — только из
        // urlSession(_:webSocketTask:didOpenWithProtocol:) после реального
        // хендшейка (см. extension ниже).
        receiveLoop(t)
    }

    private func receiveLoop(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                guard t === self.task, !self.stopped else { return }
                switch result {
                case .success(let msg):
                    // Сброс счётчика ретраев — на подтверждённом СООБЩЕНИИ,
                    // а не на didOpen: иначе connect-and-instant-close шторм
                    // (открылось и тут же легло ещё до первого сообщения)
                    // обнулял бы backoff на каждой итерации.
                    self.reconnectAttempt = 0
                    self.loggedFailureSinceLastSuccess = false
                    switch msg {
                    case .string(let s): self.onMessage(.text(s), self.generation)
                    case .data(let d): self.onMessage(.binary(d), self.generation)
                    @unknown default: break
                    }
                    self.receiveLoop(t)
                case .failure(let error):
                    let code = t.closeCode.rawValue
                    if !self.loggedFailureSinceLastSuccess {
                        self.loggedFailureSinceLastSuccess = true
                        NSLog("VGWS[\(self.generation)] receive failed: \(error.localizedDescription), closeCode=\(code)")
                    }
                    self.onClose?(code, self.generation)
                    if self.lastReportedState != false {
                        self.lastReportedState = false
                        self.onStateChange?(false, self.generation)
                    }
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
        #if DEBUG
        Self.bumpLiveTimerCount(1)
        #endif
    }

    private func teardownLocked() {
        if pingTimer != nil {
            #if DEBUG
            Self.bumpLiveTimerCount(-1)
            #endif
        }
        pingTimer?.cancel()
        pingTimer = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    #if DEBUG
    // MARK: - Test hooks (только для unit-тестов; исключены из release-сборки)
    func testHook_onQueue(_ block: @escaping () -> Void) { queue.async(execute: block) }
    var testHook_isStopped: Bool { stopped }
    #endif
}

extension VGWebSocketConnection: URLSessionWebSocketDelegate {
    /// Единственный источник правды для onStateChange(true, ...) — только
    /// после реального хендшейка сервера, не сразу после resume().
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                     didOpenWithProtocol protocol: String?) {
        queue.async {
            guard webSocketTask === self.task, !self.stopped else { return }
            guard self.lastReportedState != true else { return }
            self.lastReportedState = true
            self.loggedFailureSinceLastSuccess = false
            self.onStateChange?(true, self.generation)
        }
    }
}
