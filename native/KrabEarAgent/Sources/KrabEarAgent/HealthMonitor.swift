/*
 Continuous health monitor для backend Python процесса.

 Связи модуля:
 1) BackendSupervisor: использует ping и решения о restart.
 2) main.swift: запуск/остановка по lifecycle приложения.
 3) StatusIndicator: подписка на изменения состояния для UI.
*/

import Foundation

/// Наблюдаемое состояние backend'а.
enum HealthState: Sendable, Equatable {
    /// Backend жив, последние ping'и проходят.
    case healthy
    /// Backend завис: 2+ ping'а подряд не ответили.
    case hung
    /// Backend остановлен (явно через `stop()`).
    case stopped
}

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

    init(pingInterval: TimeInterval = 3.0, hangThreshold: Int = 2) {
        self.pingInterval = pingInterval
        self.hangThreshold = hangThreshold
    }

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
