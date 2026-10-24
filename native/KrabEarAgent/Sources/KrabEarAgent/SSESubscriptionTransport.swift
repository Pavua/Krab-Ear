import Foundation

/// Узкая подменяемая граница транспорта, без модели или UI.
protocol ProbeSSETask: Sendable {
    func resume()
    func cancel()
}
extension URLSessionDataTask: ProbeSSETask {}
protocol ProbeSSESession: Sendable {
    func makeDataTask(with request: URLRequest) -> any ProbeSSETask
    func invalidateAndCancel()
}
extension URLSession: ProbeSSESession {
    func makeDataTask(with request: URLRequest) -> any ProbeSSETask { dataTask(with: request) }
}
typealias ProbeSSESessionFactory = @Sendable (SSESessionDelegate) -> any ProbeSSESession

/// Переносит последовательные delegate callbacks на MainActor одним FIFO drain.
/// При переполнении не продолжает повреждённое событие: завершает соединение.
private final class SSECallbackMailbox: @unchecked Sendable {
    enum Event { case line(String), complete }
    private let lock = NSLock()
    private var events: [Event] = []
    private var scheduled = false
    private var closed = false
    private let receive: @MainActor (Event) -> Void

    init(receive: @escaping @MainActor (Event) -> Void) { self.receive = receive }

    func enqueue(_ event: Event) {
        lock.lock()
        guard !closed else { lock.unlock(); return }
        if events.count >= 256 {
            events = [.complete]
            closed = true
        } else {
            events.append(event)
            if case .complete = event { closed = true }
        }
        let needsDrain = !scheduled
        scheduled = true
        lock.unlock()
        if needsDrain { DispatchQueue.main.async { self.drain() } }
    }

    func invalidate() {
        lock.lock()
        closed = true
        events.removeAll()
        lock.unlock()
    }

    @MainActor private func drain() {
        lock.lock()
        let batch = events
        events.removeAll()
        scheduled = false
        lock.unlock()
        for event in batch { receive(event) }
    }
}

/// Один активный SSE stream и один retry; stop отменяет и будущие попытки.
@MainActor
final class SSESubscriptionTransport {
    typealias Scheduler = (TimeInterval, DispatchWorkItem) -> Void
    private struct Connection {
        let id: UUID
        let session: any ProbeSSESession
        let task: any ProbeSSETask
        let mailbox: SSECallbackMailbox
    }
    private let factory: ProbeSSESessionFactory
    private let scheduler: Scheduler
    private var connection: Connection?
    private var retry: DispatchWorkItem?
    private var retryID: UUID?
    private var generation: UInt64 = 0
    private var failures = 0
    private var isCancelled: @Sendable () -> Bool = { false }
    private var request: URLRequest?
    private var onReset: (() -> Void)?
    private var onLine: ((String) -> Void)?

    init(sessionFactory: @escaping ProbeSSESessionFactory = { delegate in
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        return URLSession(configuration: .default, delegate: delegate, delegateQueue: queue)
    }, scheduler: @escaping Scheduler = { delay, work in
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }) {
        factory = sessionFactory
        self.scheduler = scheduler
    }

    deinit {
        retry?.cancel()
        connection?.mailbox.invalidate()
        connection?.task.cancel()
        connection?.session.invalidateAndCancel()
    }

    func start(request: URLRequest, isCancelled: @escaping @Sendable () -> Bool = { false },
               onConnectionReset: @escaping () -> Void,
               onLine: @escaping (String) -> Void) {
        generation &+= 1
        let expected = generation
        let previousReset = onReset
        clear()
        previousReset?()
        guard generation == expected else { return }
        self.isCancelled = isCancelled
        self.request = request
        self.onReset = onConnectionReset
        self.onLine = onLine
        failures = 0
        connect(expected)
    }

    func stop() {
        generation &+= 1
        let reset = onReset
        clear()
        reset?()
    }

    private func clear() {
        request = nil
        isCancelled = { false }
        onReset = nil
        onLine = nil
        retryID = nil
        retry?.cancel()
        retry = nil
        closeConnection()
    }

    private func closeConnection() {
        let previous = connection
        connection = nil
        previous?.mailbox.invalidate()
        previous?.task.cancel()
        previous?.session.invalidateAndCancel()
    }

    private func isActive(_ expected: UInt64) -> Bool {
        generation == expected && request != nil && !isCancelled()
    }

    private func connect(_ expected: UInt64) {
        guard isActive(expected), let request, connection == nil else { return }
        onReset?()
        guard isActive(expected) else { return }
        let id = UUID()
        let mailbox = SSECallbackMailbox { [weak self] event in
            guard let self, self.isActive(expected), self.connection?.id == id else { return }
            switch event {
            case .line(let line):
                self.failures = 0
                self.onLine?(line)
            case .complete:
                self.closeConnection()
                self.onReset?()
                guard self.isActive(expected) else { return }
                self.scheduleRetry(expected)
            }
        }
        let delegate = SSESessionDelegate(onLine: { mailbox.enqueue(.line($0)) },
                                          onComplete: { _ in mailbox.enqueue(.complete) })
        let session = factory(delegate)
        guard isActive(expected) else {
            mailbox.invalidate()
            session.invalidateAndCancel()
            return
        }
        let task = session.makeDataTask(with: request)
        guard isActive(expected) else {
            mailbox.invalidate()
            task.cancel()
            session.invalidateAndCancel()
            return
        }
        connection = Connection(id: id, session: session, task: task, mailbox: mailbox)
        task.resume()
    }

    private func scheduleRetry(_ expected: UInt64) {
        guard isActive(expected), retry == nil else { return }
        let delay = min(0.5 * pow(2.0, Double(min(failures, 6))), 30)
        failures = min(failures + 1, 6)
        let id = UUID()
        retryID = id
        let work = DispatchWorkItem { [weak self] in
            DispatchQueue.main.async { [weak self] in
                guard let self, self.isActive(expected), self.retryID == id,
                      self.connection == nil else { return }
                self.retry = nil
                self.retryID = nil
                self.connect(expected)
            }
        }
        retry = work
        scheduler(delay, work)
    }
}
