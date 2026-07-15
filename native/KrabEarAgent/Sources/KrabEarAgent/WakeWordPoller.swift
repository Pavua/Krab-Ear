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
    case ttsPlayback     // собственный TTS звучит через колонки — эхо триггерит детекцию (T5b)
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

// MARK: - Решение об эскалации wedged (чистая логика, без таймеров/IPC)

/// Backend сообщил wedged:true (wake-word поток заклинил, мягкое лечение
/// невозможно/не помогло — спека 2026-07-15). Разрешаем принудительный
/// рестарт backend не чаще раза в minGapSec.
struct WedgedEscalationTracker {
    static let minGapSec: TimeInterval = 1800  // 30 минут

    private var lastEscalationAt: TimeInterval?

    mutating func shouldEscalate(wedged: Bool, now: TimeInterval) -> Bool {
        guard wedged else { return false }
        if let last = lastEscalationAt, now - last < Self.minGapSec { return false }
        lastEscalationAt = now
        return true
    }

    mutating func reset() { lastEscalationAt = nil }
}

// MARK: - Поллер

@MainActor
final class WakeWordPoller {
    static let pollInterval: TimeInterval = 0.75
    /// Мин. пауза между self-heal попытками wake_word_start (backend мог
    /// перезапуститься launchd'ом — сессия адаптера пропадает).
    static let restartMinGapSec: TimeInterval = 10.0
    /// После стольких подряд ответов engine_available=false поллинг
    /// останавливается (openWakeWord не установлен — жечь IPC бессмысленно).
    static let maxEngineUnavailablePolls = 3
    /// После стольких подряд неудачных wake_word_start self-heal замолкает
    /// до следующего activate()/resume() (иначе лог-спам каждые 10s, если
    /// backend отвергает старт, например по privacy mode).
    static let maxFailedStartAttempts = 3

    /// Все wake word IPC идут через ОДНУ серийную очередь: конкурентная
    /// global не гарантирует порядок ЗАВЕРШЕНИЯ независимых async-блоков,
    /// а порядок stop/start критичен — инверсия при быстрых pause/resume
    /// или toggle оставляла адаптер выключенным при «включённом» Swift-
    /// состоянии (ревью-находки волны wake word).
    private static let ipcQueue = DispatchQueue(label: "com.krabear.agent.wakeword-ipc", qos: .utility)

    private let ipcProvider: () -> IPCClient?
    private let isToggleEnabled: () -> Bool
    private let onDetection: () -> Void
    private let onWedgedEscalation: (() -> Void)?

    private let tracker = WakeWordDetectionTracker()
    private var wedgedTracker = WedgedEscalationTracker()
    private var timer: Timer?
    private var pausedReasons: Set<WakeWordPauseReason> = []
    private var inFlight = false
    private var lastStartAttempt: TimeInterval = 0
    private var consecutiveEngineUnavailable = 0
    private var failedStartAttempts = 0
    /// Последний известный engine_available (для Settings-статуса).
    private(set) var lastEngineAvailable: Bool?

    init(
        ipcProvider: @escaping () -> IPCClient?,
        isToggleEnabled: @escaping () -> Bool,
        onDetection: @escaping () -> Void,
        onWedgedEscalation: (() -> Void)? = nil
    ) {
        self.ipcProvider = ipcProvider
        self.isToggleEnabled = isToggleEnabled
        self.onDetection = onDetection
        self.onWedgedEscalation = onWedgedEscalation
    }

    var isActive: Bool { timer != nil }

    /// Включить: wake_word_start в backend + периодический поллинг статуса.
    func activate() {
        guard timer == nil else { return }
        tracker.reset()
        wedgedTracker.reset()
        consecutiveEngineUnavailable = 0
        failedStartAttempts = 0
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
        failedStartAttempts = 0
        sendStart(force: true)
        AgentLogger.shared.info("[WakeWord] Возобновлён после: \(reason.rawValue)")
    }

    // MARK: - Внутренние

    private func tick() {
        guard timer != nil, pausedReasons.isEmpty, !inFlight,
              let ipc = ipcProvider() else { return }
        inFlight = true
        Self.ipcQueue.async { [weak self] in
            let resp = try? ipc.call(method: "wake_word_status", params: [:])
            DispatchQueue.main.async {
                guard let self else { return }
                self.inFlight = false
                // Гонка in-flight (ревью-находка): за время блокирующего IPC
                // пользователь мог выключить тумблер (deactivate) или началась
                // запись/разговор/privacy (pause) — ответ уже неактуален,
                // триггерить разговор или self-heal по нему нельзя.
                guard self.timer != nil, self.pausedReasons.isEmpty else { return }
                // Backend down — nil; HealthMonitor чинит сам, мы просто ждём.
                guard let result = resp?["result"] as? [String: Any] else { return }
                let engineAvailable = result["engine_available"] as? Bool ?? false
                self.lastEngineAvailable = engineAvailable
                // openWakeWord не установлен: после N подряд пустых ответов
                // останавливаем поллинг целиком (спека: не поллить без движка).
                if !engineAvailable {
                    self.consecutiveEngineUnavailable += 1
                    if self.consecutiveEngineUnavailable >= Self.maxEngineUnavailablePolls {
                        AgentLogger.shared.warn(
                            "[WakeWord] openWakeWord не установлен в backend — поллинг остановлен. " +
                            "Установка: pip install -r KrabEar/requirements-wakeword.txt, затем включите тумблер заново")
                        self.deactivate()
                    }
                    return
                }
                self.consecutiveEngineUnavailable = 0
                let running = result["running"] as? Bool ?? false
                let ts = (result["last_detection"] as? [String: Any])?["ts"] as? Double
                if self.tracker.shouldTrigger(lastDetectionTs: ts) {
                    AgentLogger.shared.info("[WakeWord] Детекция — запускаю разговор")
                    self.onDetection()
                    return
                }
                // Эскалация wedged: backend сам не смог вылечить wake-word
                // поток (спека 2026-07-15) — просим принудительный рестарт.
                let wedged = result["wedged"] as? Bool ?? false
                if self.wedgedTracker.shouldEscalate(
                    wedged: wedged, now: ProcessInfo.processInfo.systemUptime
                ) {
                    AgentLogger.shared.warn(
                        "[WakeWord] backend сообщил wedged — эскалация: принудительный рестарт backend")
                    self.onWedgedEscalation?()
                    return
                }
                // Self-heal: launchd перезапустил backend — сессия адаптера пропала.
                if !running {
                    self.sendStart(force: false)
                }
            }
        }
    }

    private func sendStart(force: Bool) {
        let now = ProcessInfo.processInfo.systemUptime
        if !force {
            if now - lastStartAttempt < Self.restartMinGapSec { return }
            // Бюджет self-heal исчерпан (backend стабильно отвергает старт,
            // например privacy включён только на стороне backend) — молчим
            // до следующего activate()/resume(), не спамим каждые 10s.
            if failedStartAttempts >= Self.maxFailedStartAttempts { return }
        }
        lastStartAttempt = now
        guard let ipc = ipcProvider() else { return }
        let model = UserDefaults.standard.string(forKey: "KrabEar_WakeWordModel") ?? "hey_jarvis"
        var threshold = UserDefaults.standard.double(forKey: "KrabEar_WakeWordThreshold")
        if threshold <= 0 { threshold = 0.5 }
        Self.ipcQueue.async { [weak self] in
            let resp = try? ipc.call(
                method: "wake_word_start",
                params: ["model": model, "threshold": threshold]
            )
            let result = resp?["result"] as? [String: Any]
            let ok = result?["ok"] as? Bool ?? false
            let why = ok ? "" : ((result?["error"] as? String)
                ?? (result?["reason"] as? String) ?? "нет ответа от backend")
            DispatchQueue.main.async {
                guard let self else { return }
                if ok {
                    self.failedStartAttempts = 0
                } else {
                    self.failedStartAttempts += 1
                    AgentLogger.shared.warn(
                        "[WakeWord] wake_word_start не удался (\(self.failedStartAttempts)/\(Self.maxFailedStartAttempts)): \(why)")
                }
            }
        }
    }

    private func sendStop() {
        guard let ipc = ipcProvider() else { return }
        Self.ipcQueue.async {
            _ = try? ipc.call(method: "wake_word_stop", params: [:])
        }
    }

}
