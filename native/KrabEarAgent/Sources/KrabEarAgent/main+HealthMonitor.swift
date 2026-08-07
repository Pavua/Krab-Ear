/*
 main+HealthMonitor.swift
 AgentAppDelegate extension: Phase A auto-heal wiring.

 Связывает:
 - HealthMonitor actor (continuous ping каждые 3s)
 - BackendSupervisor.restartIfDead (max consecutive restarts guard)
 - BackendToast (UI feedback на restart)
 - StatusIndicatorImage (menu bar dot)

 Phase B.1:
 - setupHealthMonitor вызывает subscribeToProbeEvents после старта.
 - applyHealthStateToStatusItem обновляет StatusIndicatorView и menu bar dot.
*/

import AppKit
import Foundation
import ObjectiveC.runtime

// MARK: - Associated objects (Swift не позволяет stored properties в extensions)
// Уникальные ключи — pointer identity. Не shared mutable state.

private nonisolated(unsafe) var healthMonitorKey: UInt8 = 0
private nonisolated(unsafe) var statusUpdateTimerKey: UInt8 = 0
private nonisolated(unsafe) var statusIndicatorViewKey: UInt8 = 0
private nonisolated(unsafe) var privacyModeEnabledKey: UInt8 = 0
private nonisolated(unsafe) var lastHealthStateKey: UInt8 = 0

/// Считать ли ошибку IPC доказательством того, что backend ЗАКЛИНИЛ —
/// то есть процесс жив, но его accept-loop мёртв (инцидент 2026-08-07).
///
/// Решение безопасности: `true` здесь ведёт к `launchctl kickstart -k`, а
/// рестарт под активной диктовкой уничтожает её безвозвратно (инцидент
/// 2026-07-22). Поэтому заклиниванием считается ТОЛЬКО отказ на этапе
/// `connect()`: у заклинившего backend'а accept-loop не работает, backlog
/// переполняется и connect() возвращает ECONNREFUSED — ровно это наблюдалось
/// живьём.
///
/// 🔴 Намеренно УЖЕ, чем `AgentAppDelegate.isConnectionError`. Та считает
/// отказом ещё и `readFailed`/`writeFailed`, и для СВОЕЙ задачи
/// (переподключение IPC) это правильно — но здесь дало бы ложный
/// принудительный рестарт: при исчерпании лимита коннектов
/// (`ipc_server.py`: «лимит 64 коннектов исчерпан») сервер делает `accept()` и
/// сразу `conn.close()`, значит клиент СОЕДИНЯЕТСЯ успешно и падает уже на
/// чтении. Перегруженный, но совершенно здоровый backend выглядел бы
/// заклинившим — такое реально было в логах 2026-08-07.
///
/// Таймаут заклиниванием тоже не считается: под свопом живой RTT доходил до
/// 2.9 с при таймауте ping'а 2 с.
func isBackendWedgeEvidence(_ error: Error) -> Bool {
    guard let ipcError = error as? IPCError else { return false }
    switch ipcError {
    case .socketConnectFailed:
        return true
    case .socketCreateFailed, .writeFailed, .readFailed, .timeout,
         .invalidResponse, .backendError:
        return false
    }
}

/// Разбирает `ps -o etime=` (формат `[[dd-]hh:]mm:ss`) в секунды.
///
/// 🔴 На macOS нет `ps -o etimes=` (это GNU): запрос такого поля печатает
/// список допустимых ключей и «возраст» молча превращается в мусор — ровно тот
/// класс BSD-vs-GNU ловушек, что уже стоил проекту двух мёртвых инструментов.
func parseProcessElapsedSeconds(_ raw: String) -> Int? {
    let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !text.isEmpty else { return nil }

    var days = 0
    var rest = text
    if let dash = text.firstIndex(of: "-") {
        guard let d = Int(text[text.startIndex..<dash]) else { return nil }
        days = d
        rest = String(text[text.index(after: dash)...])
    }

    let parts = rest.split(separator: ":").map(String.init)
    guard parts.count == 2 || parts.count == 3 else { return nil }
    let numbers = parts.compactMap { Int($0) }
    guard numbers.count == parts.count else { return nil }

    let hours = parts.count == 3 ? numbers[0] : 0
    let minutes = parts.count == 3 ? numbers[1] : numbers[0]
    let seconds = parts.count == 3 ? numbers[2] : numbers[1]
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds
}

/// Возраст процесса backend'а по данным launchd, в секундах. `nil` — узнать не
/// удалось (юнит не загружен, процесса нет, ps не отдал разборчивый ответ).
func backendProcessAgeSeconds() -> Int? {
    func run(_ path: String, _ args: [String]) -> String? {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: path)
        task.arguments = args
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        guard (try? task.run()) != nil else { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        guard task.terminationStatus == 0 else { return nil }
        return String(data: data, encoding: .utf8)
    }

    guard let printOut = run("/bin/launchctl",
                             ["print", "gui/\(getuid())/ai.krab.ear.backend"]) else { return nil }
    let pid = printOut
        .split(separator: "\n")
        .first { $0.contains("pid = ") }
        .flatMap { $0.split(separator: "=").last }
        .flatMap { Int($0.trimmingCharacters(in: .whitespaces)) }
    guard let pid else { return nil }

    guard let etime = run("/bin/ps", ["-o", "etime=", "-p", String(pid)]) else { return nil }
    return parseProcessElapsedSeconds(etime)
}

/// Владелец рейт-лимита принудительных рестартов заклинившего backend'а.
///
/// `WedgedEscalationTracker` — struct с mutating-методами, а колбэк детектора
/// `@Sendable` и зовётся из актора HealthMonitor. Актор-обёртка даёт безопасную
/// разделяемую мутацию без второго набора порогов: пороги (30 минут между
/// попытками, кап 3 подряд) остаются едиными с wake-word-эскалацией.
actor WedgeEscalationGate {
    private var tracker = WedgedEscalationTracker()

    /// `true` — принудительный рестарт разрешён прямо сейчас.
    func shouldEscalate() -> Bool {
        tracker.shouldEscalate(wedged: true, now: Date().timeIntervalSince1970)
    }

    /// Backend снова отвечает — кап подряд-эскалаций перевзводится.
    func noteHealthy() { tracker.noteHealthy() }
}

@MainActor
extension AgentAppDelegate {
    var healthMonitor: HealthMonitor? {
        get { objc_getAssociatedObject(self, &healthMonitorKey) as? HealthMonitor }
        set { objc_setAssociatedObject(self, &healthMonitorKey, newValue, .OBJC_ASSOCIATION_RETAIN) }
    }

    var privacyModeEnabled: Bool {
        get { objc_getAssociatedObject(self, &privacyModeEnabledKey) as? Bool ?? false }
        set { objc_setAssociatedObject(self, &privacyModeEnabledKey, newValue, .OBJC_ASSOCIATION_RETAIN) }
    }

    var lastHealthState: HealthState {
        get { objc_getAssociatedObject(self, &lastHealthStateKey) as? HealthState ?? .stopped }
        set { objc_setAssociatedObject(self, &lastHealthStateKey, newValue, .OBJC_ASSOCIATION_RETAIN) }
    }

    func setPrivacyMode(_ on: Bool) {
        self.privacyModeEnabled = on
        self.statusIndicatorView.setPrivacyMode(on)
        self.applyHealthStateToStatusItem(self.lastHealthState)
        // Wake word не должен держать микрофон в privacy mode. Backend тоже
        // откажет в wake_word_start (гейт живой после проводки settings_get) —
        // двойная защита, агентская сторона срабатывает первой.
        if on {
            wakeWordPoller?.pause(.privacyMode)
        } else {
            wakeWordPoller?.resume(.privacyMode)
        }
    }

    var statusUpdateTimer: Timer? {
        get { objc_getAssociatedObject(self, &statusUpdateTimerKey) as? Timer }
        set { objc_setAssociatedObject(self, &statusUpdateTimerKey, newValue, .OBJC_ASSOCIATION_RETAIN) }
    }

    /// Phase B.1: StatusIndicatorView для menu bar dot + flashGreen support.
    /// Создаётся один раз в setupHealthMonitor.
    var statusIndicatorView: StatusIndicatorView {
        get {
            if let existing = objc_getAssociatedObject(self, &statusIndicatorViewKey) as? StatusIndicatorView {
                return existing
            }
            let view = StatusIndicatorView(frame: NSRect(x: 0, y: 0, width: 12, height: 12))
            objc_setAssociatedObject(self, &statusIndicatorViewKey, view, .OBJC_ASSOCIATION_RETAIN)
            return view
        }
    }

    /// Запускает HealthMonitor + status update timer.
    /// Вызывается из completeStartupAfterBackendReady() после успешного backend ping.
    func setupHealthMonitor() {
        let monitor = HealthMonitor(pingInterval: 3.0, hangThreshold: 2)
        let socketPath = backendSupervisor.socketPath
        let supervisor = backendSupervisor
        let wedgeGate = WedgeEscalationGate()
        let delegate = self

        monitor.setPingProvider {
            let client = IPCClient(socketPath: socketPath)
            return ((try? await client.callAsync(method: "ping", timeoutSec: 2)) != nil)
        }

        // Локальный nonisolated reference для Sendable closure compliance.
        let loggerRef = AgentLogger.shared
        Task { @MainActor in
            await monitor.setOnHangDetected {
                loggerRef.warn("HealthMonitor: backend hang detected → restartIfDead")
                // Различаем ложную тревогу от настоящего события (2026-08-03):
                // тугой таймаут пинга HealthMonitor (2с) под нагрузкой иногда не
                // укладывается, хотя backend отвечает за доли секунды. Тост
                // «Backend перезапущен» на процессе, который никто не трогал,
                // вводит в заблуждение — restartIfDeadDetailed() отличает случаи.
                //
                // AGENT-3 / Sentry KRAB-EAR-AGENT-V (2026-08-05): restartIfDeadDetailed()
                // синхронно блокирует до 60+20с (isBackendAlive + ensureBackendRunning).
                // Этот callback — plain @Sendable closure, HealthMonitor.tick() зовёт
                // его НЕ на MainActor; раньше вызов был обёрнут в MainActor.run вместе
                // с UI-обновлениями и утаскивал main thread за собой. Считаем outcome
                // ЗДЕСЬ (до хопа на MainActor), на MainActor уходит только UI-хвост.
                let outcome = supervisor.restartIfDeadDetailed()
                await MainActor.run {
                    switch outcome {
                    case .alreadyAlive:
                        loggerRef.info("HealthMonitor: backend был жив — ложная тревога (таймаут пинга)")
                    case .recovered:
                        loggerRef.info("HealthMonitor: backend successfully restarted")
                        BackendToast.shared.show("Backend перезапущен", duration: 3.0)
                    case .failed:
                        loggerRef.warn("HealthMonitor: restart failed — лимит перезапусков достигнут")
                        BackendToast.shared.show("⚠ Backend не запускается — открой логи", duration: 10.0)
                    }
                }
            }
            // Заклинивший backend (инцидент 2026-08-07): восемь часов процесс был
            // жив, жёг CPU и отвергал IPC, а self-heal сделал ровно одну
            // безрезультатную попытку и замолчал навсегда. Ниже — вторая,
            // намеренно консервативная ступень.
            await monitor.setWedgeProbe {
                // 🔴 Молодой процесс — это ЗАГРУЗКА, а не заклинивание.
                // socketConnectFailed возвращается и когда сокета ещё нет
                // (backend минутами импортирует torch/mlx), и когда остался
                // ПРОТУХШИЙ сокет от нечистой смерти: ipc_server снимает его
                // только в момент bind(), то есть на всём окне загрузки
                // состояние ФС неотличимо от заклинивания. А перезапуски стали
                // чаще ровно из-за соседнего фикса (сегфолт теперь честно
                // убивает процесс) — без этого гарда агент зациклил бы загрузку.
                // Живой замер того дня: старт занял ~4 минуты, поэтому порог
                // с запасом. Настоящее заклинивание длится часами, десять минут
                // ожидания ему ничего не стоят.
                guard let ageSec = backendProcessAgeSeconds(), ageSec >= 600 else { return false }

                // Классификация — в isBackendWedgeEvidence (там же разбор,
                // почему она УЖЕ, чем isConnectionError).
                let client = IPCClient(socketPath: socketPath)
                do {
                    _ = try await client.callAsync(method: "ping", timeoutSec: 5)
                    return false
                } catch {
                    return isBackendWedgeEvidence(error)
                }
            }

            // Кап подряд-эскалаций перевзводится живым backend'ом.
            await monitor.setOnHealthyPing {
                await wedgeGate.noteHealthy()
            }

            await monitor.setOnWedgeDetected {
                // Последняя линия: локальное состояние агента, БЕЗ IPC (он и
                // сломан). Идёт запись/встреча — kickstart уничтожит её
                // безвозвратно (инцидент 2026-07-22), лучше остаться
                // заклиненным до следующей попытки через 30 минут.
                let busy = await MainActor.run {
                    delegate.isRecording || delegate.activeGenerationOwner != nil
                }
                guard !busy else {
                    loggerRef.warn(
                        "HealthMonitor: backend заклинил, но идёт запись — "
                        + "принудительный рестарт отложен (диктовка дороже)"
                    )
                    return
                }
                // Рейт-лимит и кап — в WedgedEscalationTracker (30 минут между
                // попытками, максимум 3 подряд без здорового сигнала): тот же
                // страж, что у wake-word-эскалации, специально переиспользован,
                // чтобы не заводить второй с другими порогами.
                guard await wedgeGate.shouldEscalate() else {
                    loggerRef.warn(
                        "HealthMonitor: backend заклинил, но эскалация подавлена "
                        + "(рейт-лимит или исчерпан кап принудительных рестартов)"
                    )
                    return
                }
                loggerRef.warn(
                    "HealthMonitor: backend ЗАКЛИНИЛ (соединение отвергается) — "
                    + "принудительный рестарт через launchctl kickstart"
                )
                // forceRestartBackend, а НЕ restartIfDead: последний
                // short-circuit'ится на живом процессе и именно поэтому не
                // вылечил инцидент.
                let ok = supervisor.forceRestartBackend()
                await MainActor.run {
                    BackendToast.shared.show(
                        ok ? "Backend завис — перезапускаю" : "⚠ Backend завис, рестарт не удался",
                        duration: 10.0
                    )
                }
            }

            await monitor.start()
            // Wave 656: log first health ping milestone.
            AgentRecoveryLogger.shared.logStage("first_health_ping")
        }

        self.healthMonitor = monitor

        // Phase B.1: подписываемся на rewriter_recovered SSE events → flashGreen.
        // Событие доставляется REST-процессу через EventBridge (2026-07-07,
        // backend/event_bridge.py) — подписка больше не мёртвый путь.
        let indicator = self.statusIndicatorView
        Task {
            await monitor.subscribeToProbeEvents(
                restBaseURL: "http://127.0.0.1:5005",
                statusIndicator: indicator
            )
        }

        // Periodic status indicator refresh (1s).
        let timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            Task { @MainActor [weak self] in
                guard let self, let monitor = self.healthMonitor else { return }
                let state = await monitor.currentState()
                self.applyHealthStateToStatusItem(state)
            }
        }
        self.statusUpdateTimer = timer

        logger.info("HealthMonitor запущен: pingInterval=3s, hangThreshold=2")
    }

    /// Останавливает HealthMonitor + timer. Вызывается из applicationWillTerminate.
    func tearDownHealthMonitor() {
        statusUpdateTimer?.invalidate()
        statusUpdateTimer = nil

        if let monitor = healthMonitor {
            Task {
                await monitor.stop()
            }
        }
        healthMonitor = nil
    }

    /// Обновляет визуал menu bar status item на основе HealthState.
    func applyHealthStateToStatusItem(_ state: HealthState) {
        self.lastHealthState = state
        guard let button = statusItem?.button else { return }

        // Создаем image для menu-bar с учетом privacyModeEnabled
        let dotImage = StatusIndicatorImage.image(for: state, privacyMode: self.privacyModeEnabled, size: 14)
        button.image = dotImage
        button.imagePosition = .imageLeft  // dot слева от title

        refreshStatusItemTitle()
        button.title = button.title  // plain text — без ● prefix

        // Phase B.1: синхронизируем StatusIndicatorView
        statusIndicatorView.updateState(state)
        
        // Phase B.2: синхронизируем Privacy Mode
        statusIndicatorView.setPrivacyMode(self.privacyModeEnabled)
    }
}
