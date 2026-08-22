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

/// Причина легитимного молчания backend'а — НЕ зависание.
///
/// Живой инцидент 2026-08-03: длинная диктовка (32 с → 2 чанка) финализируется
/// 46 секунд, backend всё это время занят STT и не отвечает на ping. Сторож с
/// порогом 6 с (pingInterval 3 × hangThreshold 2) объявлял его зависшим и
/// УБИВАЛ ровно посреди транскрибации — запись не доезжала ни до вставки, ни
/// до истории. Короткие диктовки проходили (укладывались в 6 с), длинные не
/// проходили НИКОГДА. Тот же класс, что уже чинился дважды в этой волне:
/// механизм не знал про легитимный источник молчания (у WakeWordPoller — запись,
/// у WakeWordWatchdog — встреча, здесь — финализация).
enum HealthSuspendReason: String, CaseIterable, Sendable {
    case finalizingRecording   // ждём stop_recording: backend занят STT
}

/// Actor, который раз в `pingInterval` секунд дёргает ping и трекает
/// последовательные fails. После `hangThreshold` подряд fails переключает
/// state в `.hung` и зовёт `onHangDetected` ровно один раз.
actor HealthMonitor {
    private let pingInterval: TimeInterval
    private let hangThreshold: Int
    /// Сколько подряд неудачных ping'ов, прежде чем ПРОВЕРЯТЬ гипотезу «завис».
    /// Дефолт 20 × 3с = минута: заведомо дольше любого штатного затыка.
    private let wedgeThreshold: Int
    /// Не чаще этого повторно проверяем и эскалируем, пока отказы продолжаются.
    private let wedgeReprobeInterval: TimeInterval

    private var consecutiveFailures: Int = 0
    private var state: HealthState = .stopped
    private var monitorTask: Task<Void, Never>?
    private var pingProvider: (@Sendable () async -> Bool)?
    private var onHangDetected: (@Sendable () async -> Void)?
    private var hangFiredForCurrentEpisode: Bool = false

    // MARK: Заклинивший (живой, но не отвечающий) backend — инцидент 2026-08-07
    //
    // onHangDetected одноразов на эпизод, а сбрасывается ТОЛЬКО успешным
    // ping'ом; его обработчик зовёт restartIfDeadDetailed(), который на ЖИВОМ
    // процессе возвращает .alreadyAlive и не делает ничего. Итог: на
    // заклинившем backend'е выполнялась ровно одна безрезультатная попытка, и
    // дальше self-heal молчал навсегда (класс «sticky state without an exit»).
    // Прод так пролежал 8 часов.
    //
    // 🔴 Почему нельзя просто «рестартовать после N неудач»: под нагрузкой
    // здоровый backend отвечает медленно (живой RTT доходил до 2.9 с при
    // тугом таймауте ping'а 2 с), а kickstart под активной диктовкой теряет
    // её безвозвратно (инцидент 2026-07-22). Поэтому эскалация требует ДВУХ
    // независимых подтверждений:
    //   1) wedgeProbe сказал, что соединение ОТВЕРГАЕТСЯ (а не таймаутит) —
    //      медленный-но-живой backend соединение принимает;
    //   2) backend хотя бы раз был здоров в этом эпизоде — иначе мы бы убивали
    //      МЕДЛЕННО СТАРТУЮЩИЙ процесс (импорт torch под свопом занимает
    //      минуты, сокета в это время ещё нет).
    private var wedgeProbe: (@Sendable () async -> Bool)?
    private var onWedgeDetected: (@Sendable () async -> Void)?
    private var onHealthyPing: (@Sendable () async -> Void)?
    private var sawHealthyPing: Bool = false
    private var lastWedgeCheckAt: Date?
    /// Причины, по которым backend ЛЕГИТИМНО может не отвечать на ping.
    /// Set (не счётчик) — идемпотентно по причине, как pausedReasons поллера.
    private var suspendedReasons: Set<HealthSuspendReason> = []

    // Phase B.1: хранит Task подписки на probe events чтобы можно было отменить.
    private var probeSubscriptionTask: Task<Void, Never>?
    /// Тестовая граница подписки. nil сохраняет рабочий путь через ProbeSSEBox.
    private let probeSubscriptionOperation: (@Sendable (URL) async -> Void)?

    init(
        pingInterval: TimeInterval = 3.0,
        hangThreshold: Int = 2,
        wedgeThreshold: Int = 20,
        wedgeReprobeInterval: TimeInterval = 60.0,
        probeSubscriptionOperation: (@Sendable (URL) async -> Void)? = nil
    ) {
        self.pingInterval = pingInterval
        self.hangThreshold = hangThreshold
        self.wedgeThreshold = wedgeThreshold
        self.wedgeReprobeInterval = wedgeReprobeInterval
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

    /// Проба «заклинило ли»: `true` — соединение ОТВЕРГАЕТСЯ (backend жив, но
    /// accept-loop мёртв), `false` — соединение принимается, backend просто
    /// медленный. Без пробы эскалация не происходит НИКОГДА (fail-safe: лучше
    /// не лечить, чем убить живую диктовку).
    func setWedgeProbe(_ probe: @escaping @Sendable () async -> Bool) {
        self.wedgeProbe = probe
    }

    /// Зовётся, когда заклинивание ПОДТВЕРЖДЕНО пробой. Рейт-лимит и кап
    /// принудительных рестартов — на стороне обработчика
    /// (`WedgedEscalationTracker`), здесь только детекция.
    func setOnWedgeDetected(_ callback: @escaping @Sendable () async -> Void) {
        self.onWedgeDetected = callback
    }

    /// Зовётся на КАЖДЫЙ успешный ping. Нужен, чтобы кап подряд-эскалаций
    /// перевзводился: без этого три эскалации за всю жизнь агента (пусть даже
    /// разнесённые на дни и каждая успешно вылечившая backend) навсегда
    /// выключали бы вторую ступень — тот же «замолчал навсегда», ради которого
    /// всё и делалось, просто отложенный.
    func setOnHealthyPing(_ callback: @escaping @Sendable () async -> Void) {
        self.onHealthyPing = callback
    }

    func currentState() -> HealthState {
        return state
    }

    func start() {
        guard monitorTask == nil else { return }
        state = .healthy
        consecutiveFailures = 0
        hangFiredForCurrentEpisode = false
        // Пока не увидели ЖИВОЙ ответ, эскалировать нельзя: агент мог
        // стартовать раньше backend'а, и «не отвечает» означало бы «ещё
        // грузится», а не «завис».
        sawHealthyPing = false
        lastWedgeCheckAt = nil

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

    /// Приостановить детекцию зависания: backend легитимно занят.
    /// Идемпотентно по причине. Счётчик неудач сбрасывается, чтобы отказы,
    /// накопленные ДО паузы, не сложились с послепаузными в ложное зависание.
    func suspend(_ reason: HealthSuspendReason) {
        suspendedReasons.insert(reason)
        consecutiveFailures = 0
    }

    /// Снять причину. Счётчик сбрасывается: первый ping после долгой операции
    /// может не успеть (backend дописывает историю) — это не повод убивать.
    func resume(_ reason: HealthSuspendReason) {
        suspendedReasons.remove(reason)
        consecutiveFailures = 0
    }

    func isSuspended() -> Bool { !suspendedReasons.isEmpty }

    private func tick() async {
        guard let provider = pingProvider else { return }
        // Пока причина активна — не считаем неответы отказами. Ping НЕ шлём
        // вовсе: под занятым backend'ом он всё равно упрётся в таймаут и
        // только добавит нагрузки на сокет, у которого лимит коннектов.
        if !suspendedReasons.isEmpty {
            consecutiveFailures = 0
            return
        }
        let ok = await provider()
        if ok {
            consecutiveFailures = 0
            sawHealthyPing = true
            lastWedgeCheckAt = nil
            if let healthy = onHealthyPing {
                await healthy()
            }
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
            await checkForWedgeIfNeeded()
        }
    }

    /// Отказы продолжаются дольше `wedgeThreshold` тиков — проверяем гипотезу
    /// «backend жив, но заклинил», и только при подтверждении зовём эскалацию.
    ///
    /// Намеренно НЕ одноразово на эпизод: если принудительный рестарт не помог,
    /// молчать навсегда — это ровно тот дефект, который здесь и чинится. Частоту
    /// ограничивает `wedgeReprobeInterval`, число рестартов — обработчик.
    private func checkForWedgeIfNeeded() async {
        guard consecutiveFailures >= wedgeThreshold else { return }
        guard sawHealthyPing else { return }
        guard let probe = wedgeProbe, let callback = onWedgeDetected else { return }

        let now = Date()
        if let last = lastWedgeCheckAt, now.timeIntervalSince(last) < wedgeReprobeInterval {
            return
        }
        lastWedgeCheckAt = now

        guard await probe() else { return }

        // Актор реентерабелен: пока проба висела (до 5 с), состояние могло
        // измениться — например, пользователь дожал стоп диктовки и пришёл
        // suspend(.finalizingRecording), или backend успел ответить. Без
        // перепроверки мы бы дёрнули kickstart ровно во время финализации STT.
        guard suspendedReasons.isEmpty, consecutiveFailures >= wedgeThreshold else { return }
        await callback()
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

}

// MARK: - Транспорт probe SSE

/// Минимальная граница URLSessionDataTask для unit-тестов без сетевого запроса.
protocol ProbeSSETask: Sendable {
    func resume()
    func cancel()
}

extension URLSessionDataTask: ProbeSSETask {}

/// Минимальная граница URLSession для создания и завершения probe SSE запроса.
protocol ProbeSSESession: Sendable {
    func makeDataTask(with request: URLRequest) -> any ProbeSSETask
    func invalidateAndCancel()
}

extension URLSession: ProbeSSESession {
    func makeDataTask(with request: URLRequest) -> any ProbeSSETask {
        dataTask(with: request)
    }
}

typealias ProbeSSESessionFactory = @Sendable (SSESessionDelegate) -> any ProbeSSESession

/// Одноразовый шлюз ожидания, корректный при отмене до регистрации продолжения.
final class ProbeSSECancellationGate: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Never>?
    private var isCancelled = false

    func wait() async {
        await withCheckedContinuation { continuation in
            let shouldResumeImmediately: Bool
            lock.lock()
            if isCancelled {
                shouldResumeImmediately = true
            } else {
                self.continuation = continuation
                shouldResumeImmediately = false
            }
            lock.unlock()

            if shouldResumeImmediately {
                continuation.resume()
            }
        }
    }

    func cancel() {
        let pendingContinuation: CheckedContinuation<Void, Never>?
        lock.lock()
        guard !isCancelled else {
            lock.unlock()
            return
        }
        isCancelled = true
        pendingContinuation = continuation
        continuation = nil
        lock.unlock()

        pendingContinuation?.resume()
    }
}

// MARK: - ProbeSSEBox

/// Владелец SSE-подписки probe-событий, изолированный от actor для Swift 6 Sendable.
/// Внутренняя видимость нужна только `@testable` тестам рабочего пути без реальной сети.
final class ProbeSSEBox: @unchecked Sendable {
    private let statusIndicator: StatusIndicatorView
    private let sessionFactory: ProbeSSESessionFactory
    private var session: (any ProbeSSESession)?
    private var task: (any ProbeSSETask)?
    private var sseDelegate: SSESessionDelegate?

    /// Для парсинга SSE event name
    private var pendingEventType = ""

    init(
        statusIndicator: StatusIndicatorView,
        sessionFactory: @escaping ProbeSSESessionFactory = { delegate in
            URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        }
    ) {
        self.statusIndicator = statusIndicator
        self.sessionFactory = sessionFactory
    }

    deinit {
        cancelTransport()
    }

    func startStreaming(url: URL) async {
        let delegate = SSESessionDelegate { [weak self] line in
            self?.handleSSELine(line)
        }
        self.sseDelegate = delegate
        let session = sessionFactory(delegate)
        self.session = session

        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let dataTask = session.makeDataTask(with: request)
        self.task = dataTask
        dataTask.resume()

        let cancellationGate = ProbeSSECancellationGate()
        await withTaskCancellationHandler {
            // SSESessionDelegate обрабатывает поток, шлюз удерживает метод до отмены.
            await cancellationGate.wait()
        } onCancel: {
            self.cancelTransport()
            cancellationGate.cancel()
        }
    }

    private func cancelTransport() {
        let activeTask = task
        let activeSession = session
        task = nil
        session = nil
        sseDelegate = nil
        activeTask?.cancel()
        activeSession?.invalidateAndCancel()
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
