/*
 ErrorBusPoller.swift — доставка krab_error toast'ов через IPC-поллинг backend'а.

 Root cause (найдено 2026-07-05, сиблинг-фикс к wake-word волне): прод =
 два раздельных OS-процесса — IPC-бэкенд (service.py, Unix-сокет) и REST-
 сервер (rest_server.py, :5005) — каждый со своим экземпляром
 backend.event_bus.bus (module-level singleton, но per-process). ErrorBus
 (backend/error_bus.py) эмиттит krab_error ТОЛЬКО в EventBus IPC-процесса;
 событие никогда не доходит до SSE /v1/events REST-процесса, на который
 был подписан старый ErrorSSEBox (main+Errors.swift). Тосты об ошибках
 были декоративны в проде: событие эмиттилось, subscriber на другом конце
 никогда его не видел (класс ошибки: audit_decorative_wiring).

 Фикс: как и wake word (spec 2026-07-05-wake-word-openwakeword-design.md),
 транспорт — IPC-поллинг. list_recent_errors {since_seq} возвращает только
 НОВЫЕ ошибки (backend.error_bus.ErrorBus.list_recent_since) + latest_seq
 для следующего опроса.

 ErrorBusTracker — чистая, тестируемая логика "что уже показано" (без IPC).
 ErrorBusPoller — тонкая обвязка: Timer на main + sync IPC на serial
 background queue (тот же идиом, что WakeWordPoller.tick — sync call на
 отдельной очереди, guard на self.timer в completion от гонки in-flight
 деактивации).
*/

import AppKit
import Foundation

// MARK: - Чистая логика "какие ошибки новые"

/// Решает какие элементы ответа list_recent_errors показывать как toast.
/// Backend уже фильтрует по since_seq, трекер лишь подстраховывается от
/// рассинхрона: если backend перезапустился, его seq-счётчик обнулился и
/// latestSeq может внезапно оказаться МЕНЬШЕ уже виденного — это re-arm
/// (не показывать backlog новой сессии backend'а), не показ старой ошибки
/// как новой.
final class ErrorBusTracker {
    private var initialized = false
    private(set) var lastSeenSeq: Int = 0

    /// Первый вызов — baseline (backlog при старте агента не показываем).
    /// Возвращает элементы, которые стоит показать как toast (может быть
    /// пустым списком).
    func newItemsToShow(_ items: [KrabErrorPayload], latestSeq: Int) -> [KrabErrorPayload] {
        defer { lastSeenSeq = latestSeq }
        if !initialized {
            initialized = true
            return []
        }
        if latestSeq < lastSeenSeq {
            return []
        }
        return items
    }

    func reset() {
        initialized = false
        lastSeenSeq = 0
    }
}

// MARK: - Поллер

@MainActor
final class ErrorBusPoller {
    /// Toast-латентность 1-2s некритична (в отличие от wake word) — реже
    /// wake-word-поллинга ради меньшей IPC/CPU нагрузки.
    static let pollInterval: TimeInterval = 2.0

    private static let ipcQueue = DispatchQueue(label: "com.krabear.agent.errorbus-ipc", qos: .utility)

    private let ipcProvider: () -> IPCClient?
    /// Вызывается максимум раз в тик, с уже упорядоченным (oldest-first)
    /// непустым батчем — вызывающая сторона сама решает, как их доставить
    /// (например, одним Task с последовательным await, чтобы не терять
    /// порядок тостов при нескольких ошибках за один тик).
    private let onNewErrors: ([KrabErrorPayload]) -> Void

    private let tracker = ErrorBusTracker()
    private var timer: Timer?
    private var inFlight = false

    init(
        ipcProvider: @escaping () -> IPCClient?,
        onNewErrors: @escaping ([KrabErrorPayload]) -> Void
    ) {
        self.ipcProvider = ipcProvider
        self.onNewErrors = onNewErrors
    }

    var isActive: Bool { timer != nil }

    func activate() {
        guard timer == nil else { return }
        tracker.reset()
        let t = Timer.scheduledTimer(withTimeInterval: Self.pollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        AgentLogger.shared.info("[ErrorBus] Поллинг запущен (интервал \(Self.pollInterval)s)")
    }

    func deactivate() {
        guard timer != nil else { return }
        timer?.invalidate()
        timer = nil
        AgentLogger.shared.info("[ErrorBus] Поллинг остановлен")
    }

    // MARK: - Внутренние

    private func tick() {
        guard timer != nil, !inFlight, let ipc = ipcProvider() else { return }
        inFlight = true
        let sinceSeq = tracker.lastSeenSeq
        Self.ipcQueue.async { [weak self] in
            let resp = try? ipc.call(method: "list_recent_errors", params: ["since_seq": sinceSeq])
            DispatchQueue.main.async {
                guard let self else { return }
                self.inFlight = false
                // Гонка in-flight: пока IPC блокировался, поллер могли
                // деактивировать (agent завершается) — ответ уже неактуален.
                guard self.timer != nil else { return }
                // Backend down — nil; HealthMonitor чинит сам, мы просто ждём.
                guard let result = resp?["result"] as? [String: Any] else { return }
                let latestSeq = result["latest_seq"] as? Int ?? sinceSeq
                let rawItems = result["errors"] as? [[String: Any]] ?? []
                let payloads: [KrabErrorPayload] = rawItems.compactMap { dict in
                    guard let data = try? JSONSerialization.data(withJSONObject: dict) else { return nil }
                    return try? JSONDecoder().decode(KrabErrorPayload.self, from: data)
                }
                let toShow = self.tracker.newItemsToShow(payloads, latestSeq: latestSeq)
                if !toShow.isEmpty {
                    self.onNewErrors(toShow)
                }
            }
        }
    }
}
