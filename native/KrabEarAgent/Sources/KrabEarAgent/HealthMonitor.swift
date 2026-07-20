/*
 Continuous health monitor для backend Python процесса.

 Phase A: ping loop каждые 3s, hang detection, onHangDetected callback.
 Phase B.1: subscribeToProbeEvents — подписка на rewriter_recovered SSE событие,
            flash green на StatusIndicatorView при восстановлении rewriter'а.

 Связи модуля:
 1) BackendSupervisor: использует ping и решения о restart.
 2) main.swift: запуск/остановка по lifecycle приложения.
 3) StatusIndicatorView: flashGreen при probe recovery (Phase B.1).
 4) SSESessionDelegate: SSE стриминг через URLSession.
*/

import AppKit
import Foundation

// MARK: - HealthMonitor actor (Phase A)

/// Actor, который раз в `pingInterval` секунд дёргает ping и трекает
/// последовательные fails. После `hangThreshold` подряд fails переключает
/// state в `.hung` и зовёт `onHangDetected` ровно один раз.
actor HealthMonitor {
    private let pingInterval: TimeInterval
    private let hangThreshold: Int

    private var consecutiveFailures: Int = 0
    private var state: HealthState = .stopped
    private var monitorTask: Task<Void, Never>?
    private var pingProvider: (@Sendable () async -> Bool)?
    private var onHangDetected: (@Sendable () async -> Void)?
    private var hangFiredForCurrentEpisode: Bool = false

    // Phase B.1: хранит Task подписки на probe events чтобы можно было отменить.
    private var probeSubscriptionTask: Task<Void, Never>?
    /// Тестовая граница подписки. nil сохраняет рабочий путь через ProbeSSEBox.
    private let probeSubscriptionOperation: (@Sendable (URL) async -> Void)?

    init(
        pingInterval: TimeInterval = 3.0,
        hangThreshold: Int = 2,
        probeSubscriptionOperation: (@Sendable (URL) async -> Void)? = nil
    ) {
        self.pingInterval = pingInterval
        self.hangThreshold = hangThreshold
        self.probeSubscriptionOperation = probeSubscriptionOperation
    }

    // MARK: - Phase A: ping loop

    /// Устанавливает провайдер ping'а — вынесено для тестируемости.
    /// В production это будет вызов `IPCClient.callAsync(method: "ping")`.
    nonisolated func setPingProvider(_ provider: @escaping @Sendable () async -> Bool) {
        Task { await self._setPingProvider(provider) }
    }

    private func _setPingProvider(_ provider: @escaping @Sendable () async -> Bool) {
        self.pingProvider = provider
    }

    func setOnHangDetected(_ callback: @escaping @Sendable () async -> Void) {
        self.onHangDetected = callback
    }

    func currentState() -> HealthState {
        return state
    }

    func start() {
        guard monitorTask == nil else { return }
        state = .healthy
        consecutiveFailures = 0
        hangFiredForCurrentEpisode = false

        monitorTask = Task { [weak self] in
            await self?.runLoop()
        }
    }

    func stop() {
        monitorTask?.cancel()
        monitorTask = nil
        probeSubscriptionTask?.cancel()
        probeSubscriptionTask = nil
        state = .stopped
    }

    private func runLoop() async {
        while !Task.isCancelled {
            let nanos = UInt64(pingInterval * 1_000_000_000)
            try? await Task.sleep(nanoseconds: nanos)
            if Task.isCancelled { break }
            await tick()
        }
    }

    private func tick() async {
        guard let provider = pingProvider else { return }
        let ok = await provider()
        if ok {
            consecutiveFailures = 0
            if state == .hung {
                state = .healthy
                hangFiredForCurrentEpisode = false
            } else if state == .stopped {
                state = .healthy
            }
        } else {
            consecutiveFailures += 1
            if consecutiveFailures >= hangThreshold && state != .hung {
                state = .hung
                if !hangFiredForCurrentEpisode {
                    hangFiredForCurrentEpisode = true
                    if let callback = onHangDetected {
                        await callback()
                    }
                }
            }
        }
    }
}

// MARK: - Phase B.1: probe event subscription

extension HealthMonitor {

    /// Подписывается на `rewriter_recovered` SSE события от backend EventBus.
    ///
    /// При получении события — вызывает `statusIndicator.flashGreen(reason:)`.
    /// Использует URLSession + SSESessionDelegate (тот же паттерн что RealtimeOverlayController+PartialSSE).
    ///
    /// - Parameters:
    ///   - restBaseURL: базовый URL REST сервера (по умолчанию http://127.0.0.1:5005).
    ///   - statusIndicator: view для отображения flash green эффекта.
    ///
    /// Отменяется автоматически при вызове `stop()`.
    func subscribeToProbeEvents(
        restBaseURL: String = "http://127.0.0.1:5005",
        statusIndicator: StatusIndicatorView
    ) {
        // Отменяем предыдущую подписку если была
        probeSubscriptionTask?.cancel()

        let filterParam = "rewriter_recovered"
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=\(filterParam)") else {
            return
        }

        if let operation = probeSubscriptionOperation {
            // Инъекция оставляет Task под тем же контрактом отмены HealthMonitor,
            // но не открывает URLSession в unit-тестах.
            probeSubscriptionTask = Task.detached {
                await operation(url)
            }
            return
        }

        // Рабочее приложение по-прежнему использует ProbeSSEBox без нарушений Sendable.
        let box = ProbeSSEBox(statusIndicator: statusIndicator)
        // Держим box сильной ссылкой, чтобы он жил на время работы Task
        probeSubscriptionTask = Task.detached {
            await box.startStreaming(url: url)
        }
    }

    /// Cancels the probe subscription (e.g. from tests or stop()).
    func cancelProbeSubscription() {
        probeSubscriptionTask?.cancel()
        probeSubscriptionTask = nil
    }
}

// MARK: - ProbeSSEBox (NSObject wrapper для URLSession delegate)

/// Ref-counted box для SSE подписки probe событий.
/// Изолирован от actor чтобы соответствовать Swift 6 Sendable требованиям.
private final class ProbeSSEBox: @unchecked Sendable {
    private let statusIndicator: StatusIndicatorView
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var sseDelegate: SSESessionDelegate?

    /// Для парсинга SSE event name
    private var pendingEventType = ""

    init(statusIndicator: StatusIndicatorView) {
        self.statusIndicator = statusIndicator
    }

    deinit {
        task?.cancel()
        session?.invalidateAndCancel()
    }

    func startStreaming(url: URL) async {
        let weakSelf = self
        let delegate = SSESessionDelegate { line in
            weakSelf.handleSSELine(line)
        }
        self.sseDelegate = delegate
        let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        self.session = session

        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let dataTask = session.dataTask(with: request)
        self.task = dataTask
        dataTask.resume()

        // Держим alive пока Task не отменён
        await withTaskCancellationHandler {
            // await indefinitely — SSESessionDelegate handles the stream
            // Using continuation to wait for cancellation
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                // Store continuation to resume on cancel
                // We use a polling approach since SSE is callback-based
                Task {
                    while !Task.isCancelled {
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                    }
                    continuation.resume()
                }
            }
        } onCancel: {
            weakSelf.task?.cancel()
            weakSelf.session?.invalidateAndCancel()
        }
    }

    private func handleSSELine(_ line: String) {
        if line.hasPrefix("event: ") {
            pendingEventType = String(line.dropFirst(7)).trimmingCharacters(in: .whitespaces)
        } else if line.hasPrefix("data: ") {
            let eventType = pendingEventType
            if eventType == "rewriter_recovered" {
                let indicator = self.statusIndicator
                Task { @MainActor in
                    indicator.flashGreen(reason: "rewriter recovered")
                }
            }
            pendingEventType = ""
        } else if line.isEmpty {
            pendingEventType = ""
        }
    }
}
