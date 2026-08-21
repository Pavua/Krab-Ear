import Foundation

struct TranscriptEntry: Equatable {
    enum Kind: Equatable {
        case remote(text: String, translation: String?)
        case agent(text: String, textRu: String?, utteranceTs: String?,
                   interrupted: Bool, spokenText: String?, spokenFraction: Double?)
        case system(String)
    }
    var kind: Kind
}

protocol CallObserverHUDPresenting: AnyObject {
    /// Показывает HUD для указанной сессии. Контракт (I-4): ЛЮБОЙ вызов —
    /// включая повторный, для уже видимого HUD — обязан очистить ранее
    /// показанный linger-текст (`showLinger`). Координатор использует
    /// повторный showHUD как единственный способ вернуть HUD из состояния
    /// "звонок завершён" обратно в живой рендер при новом/резюмированном звонке.
    func showHUD(session: VGSessionInfo)
    /// Обновляет живой контент HUD. Контракт: как и `showHUD` — координатор
    /// зовёт это ТОЛЬКО для живых звонков (никогда во время активного linger),
    /// так что реализация вправе полагаться на то, что вызов сам по себе
    /// означает "рендерить живое", а не сохранять стейл linger-состояние.
    func updateHUD(session: VGSessionInfo, status: String, lastEntries: [TranscriptEntry],
                   listenState: CallAudioPlayer.ListenState)
    func showLinger(message: String)
    func hideHUD()
    var isHUDVisible: Bool { get }
}

protocol CallObserverPanelPresenting: AnyObject {
    func showPanel(session: VGSessionInfo)
    func updateTranscript(_ entries: [TranscriptEntry])
    func updateStatus(status: String, muted: Bool?, held: Bool?, badge: String?)
    func updateCost(_ text: String)
    func setTerminal(message: String)
    func setLive()
    func closeHangupSheetIfOpen()
    func presentHangupConfirm()
    var isPanelVisible: Bool { get }
}

protocol VGCommandPosting {
    func hangup(baseURL: URL, sessionId: String, completion: @escaping (Result<Int, Error>) -> Void)
    func fetchCostUsd(baseURL: URL, sessionId: String, completion: @escaping (Double?) -> Void)
}

protocol CallObserverSettingsProviding {
    // @Sendable (I-5 fix-round follow-up): тестовая реализация (FixedSettings)
    // теперь честно диспетчерит completion через DispatchQueue.main.async —
    // KrabEarAgentTests таргет компилируется в СТРОГОМ Swift 6 concurrency
    // режиме (в отличие от основного таргета, который сидит на
    // .swiftLanguageMode(.v5), см. Package.swift), поэтому non-Sendable
    // closure, захватываемый в async-блок, был бы ошибкой компиляции.
    func refresh(completion: @escaping @Sendable (_ hudEnabled: Bool, _ autoplay: Bool,
                                        _ privacyMode: Bool, _ baseURL: URL) -> Void)
}

/// Дирижёр Call Observer (spec §3 комп. 6 + §4.1). Весь код — на main queue
/// (все входы уже доставляются на main: watcher/stream/player так спроектированы).
/// §4.1: ПЯТЬ терминальных сигналов → one-shot per observation-generation;
/// generations ПО-СЕССИОННЫЕ (два звонка = два поколения).
final class CallObserverCoordinator: NSObject, VGSessionWatcherDelegate {
    private struct ObservedCall {
        var session: VGSessionInfo
        var generation: UInt64
        var terminalDelivered = false
        var transcript: [TranscriptEntry] = []
    }

    private let hud: CallObserverHUDPresenting
    private let panel: CallObserverPanelPresenting
    private let poster: VGCommandPosting
    private let settings: CallObserverSettingsProviding
    private let stream: VGCallStreamClient
    private let player: CallAudioPlayer
    private let tokenProvider: () -> String
    private let lingerSeconds: TimeInterval
    private let costPollInterval: TimeInterval
    private let uiCoalesceInterval: TimeInterval

    private var observed: [String: ObservedCall] = [:]
    private var selectedId: String?
    private var hudManuallyClosed = false
    private var hudAutoShown = false
    /// I-4: HUD в данный момент показывает linger-текст ("Звонок завершён"/
    /// "Связь с VG потеряна"), а не живой рендер — сбрасывается любым showHUD
    /// (см. maybeAutoShowHUD/clearLingerIfShowingAndLive) или hideHUD.
    private var hudShowingLinger = false
    private var listenStartedManually = false
    private var hangupInFlight = false
    private var lingerWork: DispatchWorkItem?
    private var pushWork: DispatchWorkItem?
    private var costTimer: DispatchSourceTimer?
    private var listenState: CallAudioPlayer.ListenState = .idle

    private var hudEnabled = true
    private var autoplay = false
    private var privacyMode = false
    private var baseURL = URL(string: "http://127.0.0.1:8090")!

    private static let transcriptCap = 500

    init(hud: CallObserverHUDPresenting, panel: CallObserverPanelPresenting,
         poster: VGCommandPosting, settings: CallObserverSettingsProviding,
         stream: VGCallStreamClient, player: CallAudioPlayer,
         tokenProvider: @escaping () -> String,
         lingerSeconds: TimeInterval = 3.0,
         costPollInterval: TimeInterval = 3.0,
         uiCoalesceInterval: TimeInterval = 0.1) {
        self.hud = hud
        self.panel = panel
        self.poster = poster
        self.settings = settings
        self.stream = stream
        self.player = player
        self.tokenProvider = tokenProvider
        self.lingerSeconds = lingerSeconds
        self.costPollInterval = costPollInterval
        self.uiCoalesceInterval = uiCoalesceInterval
        super.init()
        stream.onEvent = { [weak self] event, gen in self?.handleStreamEvent(event, gen) }
        player.onStateChange = { [weak self] state, _ in
            self?.listenState = state
            self?.refreshHUD()
        }
        stream.onConnectionState = { [weak self] connected, _ in
            guard let self, let id = self.selectedId,
                  let call = self.observed[id], !call.terminalDelivered else { return }
            self.panel.updateStatus(status: call.session.status, muted: nil, held: nil,
                                    badge: connected ? nil : "переподключение…")
        }
        refreshSettings()
    }

    var hasLiveCall: Bool { observed.values.contains { !$0.terminalDelivered } }

    /// Явный teardown на завершение приложения (main+CallObserver.swift
    /// tearDownCallObserver()) — закрывает events-stream и прослушку явно,
    /// чтобы VG WS-соединения не висели до process exit.
    func tearDown() {
        stream.disconnect()
        player.stopListening()
    }

    // MARK: - Watcher delegate (main queue)

    func watcherCallAppeared(_ s: VGSessionInfo, generation: UInt64, resurrected: Bool) {
        refreshSettings()
        lingerWork?.cancel()  // B в linger-окне A: чужой таймер не смеет спрятать живой HUD
        lingerWork = nil
        var call = ObservedCall(session: s, generation: generation)
        if resurrected, let old = observed[s.id] {
            call.transcript = old.transcript  // VG не реплеит историю — не стирать
        }
        observed[s.id] = call
        // C-1 (ревью): selectedId (панель+стримы) переключается ТОЛЬКО когда
        // ТЕКУЩИЙ выбранный звонок уже не живой (terminalDelivered или пропал) —
        // живой выбранный никогда не уступает место новому, ни при каких
        // условиях панели (spec §4.1 "no automatic selection switch" для
        // конкурентных звонков). Старая проверка смотрела лишь на nil/пропажу
        // записи, а `observed` никогда не чистит ЖИВЫЕ записи → после первого
        // завершившегося звонка второй звонок никогда бы не наблюдался
        // (наблюдатель становился одноразовым — C-1). HUD, наоборот, следит за
        // НОВЕЙШИМ живым независимо от selectedId — через hudTrackedId ниже.
        let selectedIsLive = selectedId.flatMap { observed[$0] }.map { !$0.terminalDelivered } ?? false
        if !selectedIsLive {
            selectedId = s.id
        }
        if s.id == selectedId {
            connectStreams(for: call)
            if resurrected, panel.isPanelVisible {
                panel.setLive()
                startCostTimer()  // I-6: cost-поллинг был остановлен terminal'ом до resurrection
            }
            pushTranscript()
        }
        // C-2 (ревью): сброс hudManuallyClosed принадлежит НОВОМУ звонку (другой
        // id — первое появление), а НЕ resurrection ТОГО ЖЕ звонка: владелец
        // явно закрыл HUD именно ДЛЯ ЭТОГО звонка, resurrection не должно тихо
        // стирать это решение. Старая версия делала ровно наоборот (сбрасывала
        // на resurrected, сохраняла на новых id) — после первого ручного
        // закрытия HUD никогда бы не показался автоматически снова, даже для
        // совершенно НЕСВЯЗАННОГО будущего звонка.
        if !resurrected { hudManuallyClosed = false }
        maybeAutoShowHUD(for: s)
        if autoplay && !privacyMode && !resurrected && s.id == selectedId {
            player.startListening(baseURL: baseURL, sessionId: s.id,
                                  generation: generation, tokenProvider: tokenProvider)
        }
        clearLingerIfShowingAndLive()  // I-4: живой звонок гасит стейл linger-надпись
        pruneStaleTerminalObserved(keeping: s.id)  // память + будущий пикер
        refreshHUD()
    }

    func watcherCallUpdated(_ s: VGSessionInfo, generation: UInt64) {
        guard var call = observed[s.id], call.generation == generation else { return }
        call.session = s
        observed[s.id] = call
        // I-5: applyPrivacySuppressionIfNeeded ОБЯЗАН видеть уже применённые
        // значения — settings.refresh асинхронен в проде (IPC-роундтрип), а
        // вызов "сразу после" refreshSettings() читал бы privacyMode ещё СТАРОГО
        // такта (эффект появлялся бы с опозданием на один полл-тик, ровно когда
        // он нужнее всего — прямо на флип privacy mid-call).
        refreshSettings { [weak self] in
            self?.applyPrivacySuppressionIfNeeded()
            self?.refreshHUD()
        }
    }

    func watcherCallGone(sessionId: String, generation: UInt64) {
        deliverTerminal(sessionId: sessionId, generation: generation,
                        message: "Звонок завершён")
    }

    func watcherVGLost(sessionId: String, generation: UInt64) {
        deliverTerminal(sessionId: sessionId, generation: generation,
                        message: "Связь с VG потеряна")
    }

    func watcherAuthRejected() {
        NSLog("CallObserver: VG отверг токен — форс-обновление креденшела W1892")
        refreshSettings()  // провайдер перечитает get_voice_gateway_credential
        if panel.isPanelVisible {
            panel.updateStatus(status: "", muted: nil, held: nil,
                               badge: "VG отверг токен — проверьте voice_gateway_api_key")
        }
    }

    // MARK: - Stream events (main queue, generation уже отфильтрован клиентом,
    // но дублируем проверку против selected: сессий может быть >1)

    private func handleStreamEvent(_ event: VGCallEvent, _ gen: UInt64) {
        guard let id = selectedId, var call = observed[id], call.generation == gen else { return }
        switch event {
        case .sttFinal(let text, _, _):
            append(&call, .init(kind: .remote(text: text, translation: nil)))
        case .translationFinal(let text, _, _, _):
            // Перевод приклеивается к последней remote-строке без перевода.
            if let idx = call.transcript.lastIndex(where: {
                if case .remote(_, nil) = $0.kind { return true } else { return false }
            }), case .remote(let orig, _) = call.transcript[idx].kind {
                call.transcript[idx] = .init(kind: .remote(text: orig, translation: text))
            } else {
                append(&call, .init(kind: .remote(text: "", translation: text)))
            }
        case .agentResponse(let text, let textRu, let uts, _):
            append(&call, .init(kind: .agent(text: text, textRu: textRu, utteranceTs: uts,
                                             interrupted: false, spokenText: nil, spokenFraction: nil)))
        case .agentAutoSpoken(let text, let textRu, _, _):
            append(&call, .init(kind: .agent(text: text, textRu: textRu, utteranceTs: nil,
                                             interrupted: false, spokenText: nil, spokenFraction: nil)))
        case .agentInterrupted(let uts, let fraction, let spoken):
            if let uts, let idx = call.transcript.firstIndex(where: {
                if case .agent(_, _, uts, false, _, _) = $0.kind { return true } else { return false }
            }), case .agent(let t, let ru, _, _, _, _) = call.transcript[idx].kind {
                call.transcript[idx] = .init(kind: .agent(text: t, textRu: ru, utteranceTs: uts,
                                                          interrupted: true, spokenText: spoken,
                                                          spokenFraction: fraction))
            }
        case .callState(let status, let muted, let held):
            call.session = VGSessionInfo(id: call.session.id, status: status,
                                         phone: call.session.phone,
                                         callDirection: call.session.callDirection,
                                         createdAt: call.session.createdAt,
                                         updatedAt: call.session.updatedAt,
                                         srcLang: call.session.srcLang,
                                         tgtLang: call.session.tgtLang,
                                         callBrief: call.session.callBrief)
            observed[id] = call
            panel.updateStatus(status: status, muted: muted, held: held, badge: nil)
            refreshHUD()
            return
        case .callEnded, .callClosed:
            observed[id] = call
            deliverTerminal(sessionId: id, generation: gen, message: "Звонок завершён")
            return
        case .diagnosticError:
            append(&call, .init(kind: .system("Реплика не переведена")))
        case .screeningStarted:
            append(&call, .init(kind: .system("Скрининг входящего")))
        case .costAlert(_, let usd, _):
            panel.updateCost(usd.map { String(format: "⚠ $%.2f", $0) } ?? "⚠")
            return
        case .callRinging, .callAnswered, .ignored:
            return
        }
        observed[id] = call
        pushTranscript()
        refreshHUD()
    }

    private func append(_ call: inout ObservedCall, _ entry: TranscriptEntry) {
        call.transcript.append(entry)
        if call.transcript.count > Self.transcriptCap {
            call.transcript.removeFirst(call.transcript.count - Self.transcriptCap)
        }
    }

    // MARK: - §4.1 one-shot terminal

    private func deliverTerminal(sessionId: String, generation: UInt64, message: String) {
        guard var call = observed[sessionId], call.generation == generation,
              !call.terminalDelivered else { return }
        call.terminalDelivered = true
        observed[sessionId] = call

        if sessionId == selectedId {
            stream.disconnect()
            player.stopListening()
            stopCostTimer()
            panel.closeHangupSheetIfOpen()
            if panel.isPanelVisible { panel.setTerminal(message: message) }
            // Минор (ревью): hangupInFlight принадлежит ИМЕННО той сессии, на
            // которую летел hangup (всегда selectedId в момент вызова
            // userRequestedHangupConfirmed) — терминал ЧУЖОЙ сессии не должен
            // тихо освобождать single-flight guard, пока ответ по ЭТОЙ сессии
            // ещё не пришёл (иначе второй hangup для неё мог бы уйти раньше
            // первого ответа).
            hangupInFlight = false
        }

        // Linger: только если НЕТ другого живого звонка и HUD не закрыт вручную.
        let anotherLive = observed.contains { $0.key != sessionId && !$0.value.terminalDelivered }
        if !anotherLive && !hudManuallyClosed && (hud.isHUDVisible || hudAutoShown) {
            hud.showLinger(message: message)
            hudShowingLinger = true
            lingerWork?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self, !self.hasLiveCall else { return }  // появился живой — не прятать
                self.hud.hideHUD()
                self.hudAutoShown = false
                self.hudShowingLinger = false
            }
            lingerWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + lingerSeconds, execute: work)
        }
        refreshHUD()
    }

    // MARK: - User actions

    func userExpandedHUD() {
        guard let id = selectedId, let call = observed[id] else { return }
        hud.hideHUD()
        hudAutoShown = false  // I-4 minor: expand — явное действие владельца, не "авто" больше
        panel.showPanel(session: call.session)
        if call.terminalDelivered {
            panel.setTerminal(message: "Звонок завершён")
        } else {
            panel.setLive()
            startCostTimer()
        }
        pushTranscript()
    }

    func userClosedHUD() {
        hudManuallyClosed = true
        hudAutoShown = false
        hudShowingLinger = false
        hud.hideHUD()
        // Окна ≠ соединения: стримы/прослушка живут дальше (§4).
    }

    func userClosedPanel() {
        stopCostTimer()
        // Соединения не рвём (§4): транскрипт копится, аудио играет дальше.
    }

    func userToggledListen() {
        guard let id = selectedId, let call = observed[id], !call.terminalDelivered else { return }
        if listenState == .idle || listenState == .subscriberLimit || listenState == .failed {
            listenStartedManually = true
            player.startListening(baseURL: baseURL, sessionId: id,
                                  generation: call.generation, tokenProvider: tokenProvider)
        } else {
            listenStartedManually = false
            player.stopListening()
        }
    }

    func userRequestedHangupConfirmed() {
        guard let id = selectedId, let call = observed[id],
              !call.terminalDelivered, !hangupInFlight else { return }
        hangupInFlight = true
        let gen = call.generation
        poster.hangup(baseURL: baseURL, sessionId: id) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                self.hangupInFlight = false
                switch result {
                case .success(let code) where code == 200 || code == 404 || code == 409:
                    // 200 → терминал приедет по WS/поллу, но не ждём: сигнал №4.
                    // 404/409 после конца — тихо, звонок и так мёртв (§2.4).
                    self.deliverTerminal(sessionId: id, generation: gen,
                                         message: "Звонок завершён")
                case .success, .failure:
                    self.panel.updateStatus(status: self.observed[id]?.session.status ?? "",
                                            muted: nil, held: nil,
                                            badge: "Не удалось положить трубку")
                }
            }
        }
    }

    /// Трубка из HUD: панель откроется и поднимет confirm-sheet (HUD без окна для sheet).
    func userRequestedHangupFromHUD() {
        userExpandedHUD()
        panel.presentHangupConfirm()
    }

    func userSelectedSession(_ id: String) {
        guard let call = observed[id], id != selectedId else { return }
        selectedId = id
        stream.disconnect()
        player.stopListening()
        panel.showPanel(session: call.session)  // showPanel НЕ трогает state-бейдж
        if call.terminalDelivered {
            panel.setTerminal(message: "Звонок завершён")
        } else {
            panel.setLive()
            connectStreams(for: call)
            startCostTimer()
        }
        pushTranscript()
    }

    func openPanelFromMenu() {
        guard let id = selectedId, let call = observed[id] else { return }
        panel.showPanel(session: call.session)
        if call.terminalDelivered {
            panel.setTerminal(message: "Звонок завершён")
        } else {
            panel.setLive()
            startCostTimer()
        }
        pushTranscript()
    }

    /// Для пикера сессий в панели (>1 одновременных звонков — редкость).
    func observedSessions() -> [(id: String, label: String)] {
        observed
            .map { ($0.key, $0.value.session.phone.isEmpty ? $0.key : $0.value.session.phone) }
            .sorted { $0.0 < $1.0 }
    }

    // MARK: - Internals

    private func connectStreams(for call: ObservedCall) {
        stream.connect(baseURL: baseURL, sessionId: call.session.id,
                       generation: call.generation, tokenProvider: tokenProvider)
    }

    private func maybeAutoShowHUD(for s: VGSessionInfo) {
        guard hudEnabled, !privacyMode, !hudManuallyClosed, !panel.isPanelVisible else { return }
        // HUD следит за НОВЕЙШИМ живым звонком.
        hudAutoShown = true
        hudShowingLinger = false  // I-4: showHUD — контрактный способ погасить linger-текст
        hud.showHUD(session: s)
    }

    /// I-2: HUD-контент обязан отражать НОВЕЙШИЙ живой звонок (spec §4.1), а не
    /// обязательно selectedId — после C-1-фикса selectedId (панель+стримы)
    /// намеренно НЕ переключается автоматически между конкурентными звонками,
    /// но HUD по-прежнему должен "следить за новейшим". Newest = максимальный
    /// generation среди живых: generation минтится ГЛОБАЛЬНЫМ монотонным
    /// счётчиком в VGSessionWatcher (не per-session), так что сравнение валидно
    /// между разными id. Фоллбэк на selectedId, когда живых нет вовсе (например
    /// во время linger терминального звонка) — refreshHUD всё ещё может
    /// отрисовать его контент.
    private var hudTrackedId: String? {
        let liveNewest = observed.filter { !$0.value.terminalDelivered }
            .max { $0.value.generation < $1.value.generation }
        return liveNewest?.key ?? selectedId
    }

    /// I-4: явно возвращает HUD из linger-рендера в живой, когда новый/
    /// резюмированный звонок появляется, а HUD ещё показывает linger-текст от
    /// ПРЕДЫДУЩЕГО (уже отвязанного) наблюдения. maybeAutoShowHUD уже неявно
    /// делает то же самое, КОГДА её guard проходит (панель закрыта и т.д.) —
    /// это явный, не полагающийся на побочный эффект, вызов на случай, когда
    /// тот guard не сработал, а HUD всё ещё физически видим с linger-текстом.
    private func clearLingerIfShowingAndLive() {
        guard hudShowingLinger, hud.isHUDVisible,
              let id = hudTrackedId, let call = observed[id], !call.terminalDelivered else { return }
        hud.showHUD(session: call.session)
        hudShowingLinger = false
    }

    /// Минор (ревью): без чистки `observed` растёт вечно — терминальные записи
    /// ЧУЖИХ (не только что появившегося и не выбранного) звонков не нужны ни
    /// для памяти, ни для будущего пикера (observedSessions()). Выбранный
    /// звонок сохраняется — панель может ещё показывать его terminal-состояние.
    private func pruneStaleTerminalObserved(keeping newId: String) {
        for (id, entry) in observed where entry.terminalDelivered && id != newId && id != selectedId {
            observed.removeValue(forKey: id)
        }
    }

    private func refreshHUD() {
        guard hud.isHUDVisible, let id = hudTrackedId, let call = observed[id] else { return }
        hud.updateHUD(session: call.session, status: call.session.status,
                      lastEntries: Array(call.transcript.suffix(2)),
                      listenState: listenState)
    }

    private func pushTranscript() {
        guard panel.isPanelVisible, let id = selectedId, let call = observed[id] else { return }
        if uiCoalesceInterval <= 0 {
            panel.updateTranscript(call.transcript)
            return
        }
        // Коалесируем шторм событий (§8): не чаще одного рендера в uiCoalesceInterval.
        if pushWork != nil { return }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pushWork = nil
            guard self.panel.isPanelVisible, let id = self.selectedId,
                  let call = self.observed[id] else { return }
            self.panel.updateTranscript(call.transcript)
        }
        pushWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + uiCoalesceInterval, execute: work)
    }

    private func refreshSettings(then: (() -> Void)? = nil) {
        settings.refresh { [weak self] hudEnabled, autoplay, privacy, base in
            let apply = {
                guard let self else { return }
                self.hudEnabled = hudEnabled
                self.autoplay = autoplay
                self.privacyMode = privacy
                self.baseURL = base
                then?()
            }
            // Синхронный провайдер на main (тесты, кэш) обязан примениться ДО
            // maybeAutoShowHUD — иначе privacy-гейт первого звонка читает стейл.
            if Thread.isMainThread { apply() } else { DispatchQueue.main.async(execute: apply) }
        }
    }

    /// Чекбоксы настроек зовут это напрямую (generic-нотификации в агенте НЕТ).
    func settingsDidChange() {
        // I-5: applyPrivacySuppressionIfNeeded — ВНУТРИ completion, после
        // применения новых значений (см. watcherCallUpdated — тот же класс бага).
        refreshSettings { [weak self] in
            self?.applyPrivacySuppressionIfNeeded()
        }
    }

    /// privacy включили мид-колл: авто-показанный HUD прячем, autoplay-аудио глушим;
    /// вручную открытое/включённое остаётся (явные действия владельца).
    private func applyPrivacySuppressionIfNeeded() {
        guard privacyMode else { return }
        if hudAutoShown && hud.isHUDVisible { hud.hideHUD(); hudAutoShown = false }
        if !listenStartedManually && listenState != .idle { player.stopListening() }
    }

    private func startCostTimer() {
        stopCostTimer()
        guard let id = selectedId else { return }
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + costPollInterval, repeating: costPollInterval)
        t.setEventHandler { [weak self] in
            guard let self, let call = self.observed[id], !call.terminalDelivered else { return }
            self.poster.fetchCostUsd(baseURL: self.baseURL, sessionId: id) { usd in
                DispatchQueue.main.async {
                    self.panel.updateCost(usd.map { String(format: "$%.2f", $0) } ?? "—")
                }
            }
        }
        t.resume()
        costTimer = t
    }

    private func stopCostTimer() {
        costTimer?.cancel()
        costTimer = nil
    }
}
