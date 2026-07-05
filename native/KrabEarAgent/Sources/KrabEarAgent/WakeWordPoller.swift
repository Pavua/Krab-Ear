/*
 WakeWordPoller.swift — wake word через IPC-поллинг backend'а.

 Архитектура (spec docs/superpowers/specs/2026-07-05-wake-word-openwakeword-design.md):
 - Микрофоном владеет Python-бэкенд (backend/openwakeword_adapter.py, openWakeWord).
 - Агент шлёт wake_word_start/stop по IPC и раз в 0.75с поллит wake_word_status.
 - Рост last_detection.ts → триггер «Разговор с AI».
 - SSE НЕ используется: прод = два процесса (IPC-бэкенд и REST) с раздельными
   EventBus, событие из service.py до SSE на :5005 не доходит.

 WakeWordDetectionTracker — чистая, тестируемая логика дебаунса (без IPC/таймеров).
 WakeWordPoller — тонкая обвязка: Timer на main + sync IPC на global queue
 (идиом main+RealtimeOverlay.refreshRealtimeOverlay, AGENT-3: без sync IPC на main).
*/

import AppKit
import Foundation

// MARK: - Причины паузы (идемпотентны по причине — Set, не счётчик)

enum WakeWordPauseReason: String, CaseIterable, Sendable {
    case recording      // идёт диктовка — слушатель поймал бы её же
    case conversation   // идёт «Разговор с AI» — микрофон занят разговором
    case privacyMode    // privacy mode — микрофон wake word держать нельзя
}

// MARK: - Чистая логика дебаунса

/// Решает «была ли НОВАЯ детекция» по последовательности значений last_detection.ts.
/// Первый вызов только устанавливает baseline (стейл-детекция прошлой сессии
/// или живого бэкенда при перезапуске агента не триггерит). nil re-arm'ит
/// baseline: после рестарта бэкенда monotonic-отсчёт начинается заново и новый
/// ts может быть меньше старого.
final class WakeWordDetectionTracker {
    private var initialized = false
    private var baselineTs: Double?

    /// true ровно один раз на каждую новую детекцию.
    func shouldTrigger(lastDetectionTs ts: Double?) -> Bool {
        if !initialized {
            initialized = true
            baselineTs = ts
            return false
        }
        guard let ts else {
            baselineTs = nil   // backend сбросил состояние (рестарт/новая сессия)
            return false
        }
        if let base = baselineTs, ts <= base { return false }
        baselineTs = ts
        return true
    }

    func reset() {
        initialized = false
        baselineTs = nil
    }
}

// MARK: - Поллер

@MainActor
final class WakeWordPoller {
    static let pollInterval: TimeInterval = 0.75
    /// Мин. пауза между self-heal попытками wake_word_start (backend мог
    /// перезапуститься launchd'ом — сессия адаптера пропадает).
    static let restartMinGapSec: TimeInterval = 10.0

    private let ipcProvider: () -> IPCClient?
    private let isToggleEnabled: () -> Bool
    private let onDetection: () -> Void

    private let tracker = WakeWordDetectionTracker()
    private var timer: Timer?
    private var pausedReasons: Set<WakeWordPauseReason> = []
    private var inFlight = false
    private var lastStartAttempt: TimeInterval = 0
    /// Последний известный engine_available (для Settings-статуса).
    private(set) var lastEngineAvailable: Bool?

    init(
        ipcProvider: @escaping () -> IPCClient?,
        isToggleEnabled: @escaping () -> Bool,
        onDetection: @escaping () -> Void
    ) {
        self.ipcProvider = ipcProvider
        self.isToggleEnabled = isToggleEnabled
        self.onDetection = onDetection
    }

    var isActive: Bool { timer != nil }

    /// Включить: wake_word_start в backend + периодический поллинг статуса.
    func activate() {
        guard timer == nil else { return }
        tracker.reset()
        sendStart(force: true)
        let t = Timer.scheduledTimer(withTimeInterval: Self.pollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        AgentLogger.shared.info("[WakeWord] Поллинг запущен (интервал \(Self.pollInterval)s)")
    }

    /// Выключить: остановить поллинг + wake_word_stop в backend.
    func deactivate() {
        guard timer != nil else { return }
        timer?.invalidate()
        timer = nil
        pausedReasons.removeAll()
        sendStop()
        AgentLogger.shared.info("[WakeWord] Поллинг остановлен")
    }

    /// Пауза по причине (запись/разговор/privacy). Идемпотентна по причине.
    func pause(_ reason: WakeWordPauseReason) {
        guard timer != nil else { return }
        let wasEmpty = pausedReasons.isEmpty
        pausedReasons.insert(reason)
        if wasEmpty {
            sendStop()
            AgentLogger.shared.info("[WakeWord] Пауза: \(reason.rawValue)")
        }
    }

    /// Снять паузу по причине; возобновляет только когда причин не осталось.
    func resume(_ reason: WakeWordPauseReason) {
        pausedReasons.remove(reason)
        guard pausedReasons.isEmpty, timer != nil, isToggleEnabled() else { return }
        tracker.reset()
        sendStart(force: true)
        AgentLogger.shared.info("[WakeWord] Возобновлён после: \(reason.rawValue)")
    }

    // MARK: - Внутренние

    private func tick() {
        guard timer != nil, pausedReasons.isEmpty, !inFlight,
              let ipc = ipcProvider() else { return }
        inFlight = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let resp = try? ipc.call(method: "wake_word_status", params: [:])
            DispatchQueue.main.async {
                guard let self else { return }
                self.inFlight = false
                // Backend down — nil; HealthMonitor чинит сам, мы просто ждём.
                guard let result = resp?["result"] as? [String: Any] else { return }
                let engineAvailable = result["engine_available"] as? Bool ?? false
                self.lastEngineAvailable = engineAvailable
                let running = result["running"] as? Bool ?? false
                let ts = (result["last_detection"] as? [String: Any])?["ts"] as? Double
                if self.tracker.shouldTrigger(lastDetectionTs: ts) {
                    AgentLogger.shared.info("[WakeWord] Детекция — запускаю разговор")
                    self.onDetection()
                    return
                }
                // Self-heal: launchd перезапустил backend — сессия адаптера пропала.
                if !running && engineAvailable {
                    self.sendStart(force: false)
                }
            }
        }
    }

    private func sendStart(force: Bool) {
        let now = ProcessInfo.processInfo.systemUptime
        if !force && now - lastStartAttempt < Self.restartMinGapSec { return }
        lastStartAttempt = now
        guard let ipc = ipcProvider() else { return }
        let model = UserDefaults.standard.string(forKey: "KrabEar_WakeWordModel") ?? "hey_jarvis"
        var threshold = UserDefaults.standard.double(forKey: "KrabEar_WakeWordThreshold")
        if threshold <= 0 { threshold = 0.5 }
        DispatchQueue.global(qos: .utility).async {
            let resp = try? ipc.call(
                method: "wake_word_start",
                params: ["model": model, "threshold": threshold]
            )
            let result = resp?["result"] as? [String: Any]
            let ok = result?["ok"] as? Bool ?? false
            if !ok {
                let why = (result?["error"] as? String)
                    ?? (result?["reason"] as? String) ?? "нет ответа от backend"
                AgentLogger.shared.warn("[WakeWord] wake_word_start не удался: \(why)")
            }
        }
    }

    private func sendStop() {
        guard let ipc = ipcProvider() else { return }
        DispatchQueue.global(qos: .utility).async {
            _ = try? ipc.call(method: "wake_word_stop", params: [:])
        }
    }
}
