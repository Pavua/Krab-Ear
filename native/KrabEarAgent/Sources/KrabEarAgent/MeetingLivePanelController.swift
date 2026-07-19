/*
 MeetingLivePanelController — C2c (спека §2.7 + §2.7a): плавающая панель живой встречи.

 Владение: AgentAppDelegate (main+MeetingPanel.swift, Task 3). Панель показывает
 live-состояние meeting-сессии backend'а; закрытие панели саму сессию НЕ трогает
 (meeting_start/meeting_stop управляются отдельно).

 Task 1 — каркас панели + чистый рендер `render(state:)` (dict → UI, БЕЗ IPC/сети).

 Task 2 — данные: SSE (общий SSESessionDelegate, паттерн LiveSubtitlesOverlay) +
 silence-watchdog + poll-фоллбэк + meeting_stop (IPC строго off-main, AGENT-3) +
 финализация через onFinished колбэк владельца.

 Panel-boilerplate — портирован 1-в-1 из ConversationStatusOverlay (styleMask/level/
 collectionBehavior/drag/savePosition/restorePosition/isOnScreen≥80%) с новым
 positionKey и добавленной resizable-способностью (контент длиннее статус-HUD).

 Глиф-гейт (AGENT-J/M, CLAUDE.md): черновой префикс-глиф задачи из плана (пустой
 круг, HOLLOW BULLET) НЕ встречается в native/Sources (0 хитов) — заменён на «→»
 (297 хитов, established). «✓» уже established (ActionItems/SecuritySettings/
 Diagnostics). «?» — ASCII, гейт не касается.
*/

import AppKit
import Foundation

@MainActor
final class MeetingLivePanelController: NSObject, NSWindowDelegate {

    enum UIState: Equatable {
        case idle        // сессии нет
        case live        // сессия активна, рендерим state
        case finalizing  // meeting_stop отправлен, ждём отчёт (sticky)
        case privacy     // privacy_mode_active
    }

    private let panel: NSPanel
    private let positionKey = "KrabEar_MeetingLivePanelPosition"

    // --- секции UI (все на KrabEarTheme-токенах) ---
    private let headerTimerLabel = NSTextField(labelWithString: "00:00")
    private let recordIndicator = RecordingIndicator()
    private let degradedBadgeContainer = NSStackView()
    private let degradedBadge = NSTextField(labelWithString: "деградация")
    private let speakersRow = NSStackView()
    private let itemsStack = NSStackView()
    private let transcriptTailLabel = NSTextField(wrappingLabelWithString: "")
    private let transcriptContainer = NSView()
    private let stopButton = ThemeSecondaryButton(title: "Завершить встречу", target: nil, action: nil)
    
    private let statusContainer = NSStackView()
    private let statusIcon = NSImageView()
    private let statusSpinner = NSProgressIndicator()
    private let statusLabel = NSTextField(labelWithString: "")

    private(set) var uiState: UIState = .idle
    private var startedAt: TimeInterval?
    private var timerTick: Timer?

    // MARK: - Данные (Task 2)

    /// Инжектируется владельцем (main+MeetingPanel.swift, Task 3). Опционален — панель
    /// не крашится, если её показали до готовности AgentAppDelegate.
    var ipcClient: IPCClient?
    /// item_id финализированной встречи (может быть nil при отчёте без id) — владелец
    /// панели решает, как открыть/показать отчёт (главный или standalone путь).
    var onFinished: ((String?) -> Void)?
    /// One-shot гард финализации: SSE meeting.finished и IPC-ответ meeting_stop
    /// оба несут item_id — без гарда владелец открыл бы ДВА окна отчёта.
    /// Взводится заново при входе в новую live-сессию (setUIState).
    private var finishedDelivered = false

    private func deliverFinished(_ itemID: String?) {
        guard !finishedDelivered else { return }
        finishedDelivered = true
        onFinished?(itemID)
    }

    private let restBaseURL = "http://127.0.0.1:5005"
    private var sseTask: URLSessionDataTask?
    private var pendingSSEEventType: String?
    private var lastSSEActivity: TimeInterval = 0
    private var pollTimer: Timer?
    private var silenceTimer: Timer?
    private(set) var pollFallbackActive = false
    private var transientErrorActive = false
    private var transientErrorTimer: Timer?

    /// SSE-фильтр — ровно ПЯТЬ meeting.*-событий backend'а (C2a/C2b контракт).
    private static let meetingEventTypes: Set<String> = [
        "meeting.transcript_appended", "meeting.items_updated",
        "meeting.speakers_updated", "meeting.finalizing", "meeting.finished",
    ]

    private lazy var sseDelegate = SSESessionDelegate { [weak self] line in
        Task { @MainActor [weak self] in self?.handleSSELine(line) }
    }
    private lazy var sseSession = URLSession(configuration: .default,
                                             delegate: sseDelegate, delegateQueue: nil)

    // MARK: - Test hooks (паттерн ConversationStatusOverlay)

    var _testPanelLevel: NSWindow.Level { panel.level }
    var _testPanelIsDraggable: Bool { panel.isMovableByWindowBackground }
    var _testUIState: UIState { uiState }
    var _testSpeakerChipCount: Int { speakersRow.arrangedSubviews.count }
    var _testSpeakerChipTitles: [String] {
        speakersRow.arrangedSubviews.compactMap { ($0 as? SpeakerChipView)?.testTitle }
    }
    var _testItemRowCount: Int { itemsStack.arrangedSubviews.count }
    var _testTranscriptTailText: String { transcriptTailLabel.stringValue }
    var _testDegradedBadgeVisible: Bool { !degradedBadge.isHidden }
    var _testPanelStyleMask: NSWindow.StyleMask { panel.styleMask }
    var _testTitlebarAppearsTransparent: Bool { panel.titlebarAppearsTransparent }
    var _testTitleVisibility: NSWindow.TitleVisibility { panel.titleVisibility }
    var _testIsReleasedWhenClosed: Bool { panel.isReleasedWhenClosed }
    var _testPanelDelegateIsController: Bool { panel.delegate === self }
    var _testHeaderTimerActive: Bool { timerTick != nil }
    func _testSimulateWindowWillClose() {
        windowWillClose(Notification(name: NSWindow.willCloseNotification, object: panel))
    }
    var _testPollFallbackActive: Bool { pollFallbackActive }

    /// Прямой вызов SSE-парсера строки — без реального сетевого стрима.
    func _testHandleSSELine(_ line: String) { handleSSELine(line) }

    /// Сдвигает `lastSSEActivity` в прошлое и дёргает watchdog-тик напрямую —
    /// БЕЗ реального ожидания (тест не крутит RunLoop 15+ секунд).
    func _testSimulateSSESilence(seconds: TimeInterval) {
        lastSSEActivity = Date().timeIntervalSince1970 - seconds
        checkSilenceWatchdog()
    }

    // MARK: - Init

    override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 360, height: 520),
            styleMask: [.nonactivatingPanel, .hudWindow, .utilityWindow],
            backing: .buffered, defer: false)
        super.init()
        setupPanel()
        // Ревью №1: панель закрываема крестиком и переиспользуема —
        // isReleasedWhenClosed=false обязателен, иначе повторный show()
        // после закрытия обращается к деаллоцированному окну (крэш).
        panel.styleMask.insert(.closable)
        // C3b Fable-ревью (2026-07-19, унаследованный баг): `.closable` БЕЗ
        // `.titled` не рендерит НИКАКОЙ видимый крестик закрытия (AX на живой
        // QuickCapturePanelController подтвердил 0 кнопок AXCloseButton, тот
        // же panel-boilerplate портирован сюда 1-в-1). `.titled` даёт реальную
        // нативную кнопку; titlebarAppearsTransparent + titleVisibility=.hidden
        // убирают саму полосу тайтлбара визуально (безрамочный HUD).
        panel.styleMask.insert(.titled)
        panel.titlebarAppearsTransparent = true
        panel.titleVisibility = .hidden
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        buildLayout()
        setUIState(.idle)
    }

    // MARK: - Public API

    /// Показывает панель на сохранённой (или дефолтной топ-правой) позиции.
    /// nonactivatingPanel — НЕ активирует приложение, фокус остаётся у текущего.
    func show() {
        restorePosition()
        panel.orderFront(nil)
    }

    func hide() {
        stopUpdates()
        panel.orderOut(nil)
    }

    /// Ревью №1: закрытие панели крестиком глушит обновления и таймеры;
    /// сессия backend'а ПРОДОЛЖАЕТСЯ (спека §3) — панель переоткрывается
    /// из меню с полным состоянием.
    func windowWillClose(_ notification: Notification) {
        stopUpdates()
    }

    /// Разовый off-main poll (get_meeting_live_state → render), затем SSE+watchdog.
    /// Владелец зовёт после show(); повторный show() → снова startUpdates().
    func startUpdates() {
        lastSSEActivity = Date().timeIntervalSince1970
        pollOnce()
        startSSE()
        armSilenceWatchdog()
    }

    /// Снимает SSE-стрим и все таймеры. Сессию backend'а НЕ трогает — вызывается
    /// из hide(), панель просто перестаёт тянуть данные, пока скрыта.
    func stopUpdates() {
        stopSSE()
        pollTimer?.invalidate(); pollTimer = nil
        silenceTimer?.invalidate(); silenceTimer = nil
        deactivatePollFallback()
        stopTimer()  // ревью №4: header-таймер не должен тикать за закрытой панелью
    }

    /// Единственная точка входа данных: полный снапшот get_meeting_live_state
    /// ИЛИ склеенный из SSE-событий (Task 2). Чистая функция состояния → UI.
    func render(state: [String: Any]) {
        if uiState == .finalizing { return }  // sticky до отчёта/reset
        if (state["privacy_mode_active"] as? Bool) == true { setUIState(.privacy); return }
        guard (state["active"] as? Bool) == true else { setUIState(.idle); return }
        setUIState(.live)
        startedAt = state["started_at"] as? TimeInterval ?? Self.doubleValue(state["started_at"])
        renderSpeakers(state["speakers"] as? [[String: Any]] ?? [])
        renderItems(items: state["items"] as? [[String: Any]] ?? [],
                    decisions: state["decisions"] as? [String] ?? [],
                    questions: state["questions"] as? [String] ?? [])
        transcriptTailLabel.stringValue = state["transcript_tail"] as? String ?? ""
        let degraded = state["degraded"] as? [String: Any] ?? [:]
        let isDegraded = ((degraded["llm"] as? Bool ?? false) || (degraded["diarization"] as? Bool ?? false))
        degradedBadge.isHidden = !isDegraded
        degradedBadgeContainer.isHidden = !isDegraded
    }

    func enterFinalizing() { setUIState(.finalizing) }
    func resetToIdle() { setUIState(.idle) }   // после показа отчёта/ошибки

    /// meeting_stop IPC (off-main, AGENT-3). item_id в ответе → onFinished сразу
    /// (не ждём SSE); без item_id — остаёмся в .finalizing до meeting.finished по SSE.
    func requestStop() {
        enterFinalizing()
        guard let client = ipcClient else {
            showTransientError("Нет соединения с backend'ом")
            resetToIdle()
            return
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try client.call(method: "meeting_stop", params: [:])
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                let itemID = result["item_id"] as? String
                DispatchQueue.main.async {
                    if let itemID, !itemID.isEmpty {
                        self?.deliverFinished(itemID)
                    }
                    // без item_id — ждём meeting.finished по SSE, состояние остаётся .finalizing
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showTransientError("Не удалось завершить встречу: \(error.localizedDescription)")
                    self?.resetToIdle()
                }
            }
        }
    }

    /// Временное сообщение об ошибке в statusLabel (~5с). Не переключает uiState —
    /// переживает setUIState()/render()/resetToIdle(), пока не истечёт свой таймер
    /// (иначе resetToIdle(), вызванный сразу следом на ошибочных путях, немедленно
    /// стёр бы текст ошибки штатным «Встреча не идёт»).
    func showTransientError(_ text: String) {
        transientErrorTimer?.invalidate()
        transientErrorActive = true
        statusLabel.stringValue = text
        statusIcon.image = NSImage(systemSymbolName: "exclamationmark.triangle", accessibilityDescription: nil)
        statusIcon.contentTintColor = KrabEarTheme.Colors.error
        statusIcon.isHidden = false
        statusSpinner.stopAnimation(nil)
        statusSpinner.isHidden = true
        statusContainer.isHidden = false
        transientErrorTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: false) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.transientErrorActive = false
                self.applyStatusText(for: self.uiState)
            }
        }
    }

    // MARK: - Render helpers (чистые — без IPC/сети)

    private func renderSpeakers(_ speakers: [[String: Any]]) {
        speakersRow.arrangedSubviews.forEach { speakersRow.removeArrangedSubview($0); $0.removeFromSuperview() }
        let now = Date().timeIntervalSince1970
        for (index, speaker) in speakers.enumerated() {
            let label = speaker["label"] as? String ?? "?"
            let talkSec = Self.doubleValue(speaker["talk_sec"]) ?? 0
            let lastActive = Self.doubleValue(speaker["last_active_ts"]) ?? now
            let staleSec = max(0, now - lastActive)
            
            let fullTitle = Self.speakerChipTitle(label: label, talkSec: talkSec, staleSec: staleSec)
            
            let totalTalk = max(0, Int(talkSec))
            let mins = totalTalk / 60
            let secs = totalTalk % 60
            let talkStr = mins > 0 ? "\(mins)м \(secs)с" : "\(secs)с"
            
            let totalStale = max(0, Int(staleSec))
            let staleStr = totalStale < 60 ? "\(totalStale)с назад" : "\(totalStale / 60)мин назад"
            
            let isActive = staleSec < 5.0
            let chip = SpeakerChipView(testTitle: fullTitle, labelStr: label, talkStr: talkStr, staleStr: staleStr, isActive: isActive, colorIndex: index)
            speakersRow.addArrangedSubview(chip)
        }
    }

    private func renderItems(items: [[String: Any]], decisions: [String], questions: [String]) {
        itemsStack.arrangedSubviews.forEach { itemsStack.removeArrangedSubview($0); $0.removeFromSuperview() }
        for item in items {
            addItemRow(prefix: "→", text: item["text"] as? String ?? "")
        }
        for decision in decisions {
            addItemRow(prefix: "✓", text: decision)
        }
        for question in questions {
            addItemRow(prefix: "?", text: question)
        }
    }

    private func addItemRow(prefix: String, text: String) {
        let row = NSStackView()
        row.orientation = .horizontal
        row.spacing = KrabEarTheme.Metrics.tight
        row.alignment = .top
        
        let iconView = NSImageView()
        iconView.translatesAutoresizingMaskIntoConstraints = false
        
        let symbolName: String
        let tint: NSColor
        if prefix == "→" {
            symbolName = "arrow.right"
            tint = KrabEarTheme.Colors.accent
        } else if prefix == "✓" {
            symbolName = "checkmark"
            tint = KrabEarTheme.Colors.success
        } else {
            symbolName = "questionmark"
            tint = KrabEarTheme.Colors.warning
        }
        
        let image = NSImage(systemSymbolName: symbolName, accessibilityDescription: nil)
        iconView.image = image
        iconView.contentTintColor = tint
        iconView.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 11, weight: .semibold)
        
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = KrabEarTheme.Typography.body
        label.textColor = KrabEarTheme.Colors.textPrimary
        label.isBordered = false
        label.drawsBackground = false
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        
        row.addArrangedSubview(iconView)
        row.addArrangedSubview(label)
        
        itemsStack.addArrangedSubview(row)
    }

    private static func doubleValue(_ any: Any?) -> Double? {
        if let d = any as? Double { return d }
        if let i = any as? Int { return Double(i) }
        return nil
    }

    /// Заголовок чипа спикера: «label» · Xм Yс наговорено · возраст последней активности.
    /// nonisolated static — чистая функция, тестируема без instance.
    nonisolated static func speakerChipTitle(label: String, talkSec: Double, staleSec: Double) -> String {
        let totalTalk = max(0, Int(talkSec))
        let mins = totalTalk / 60
        let secs = totalTalk % 60
        let talkStr = mins > 0 ? "\(mins)м \(secs)с" : "\(secs)с"

        let totalStale = max(0, Int(staleSec))
        let staleStr = totalStale < 60 ? "\(totalStale)с назад" : "\(totalStale / 60)мин назад"

        return "«\(label)» · \(talkStr) · \(staleStr)"
    }

    // MARK: - UI state machine

    private func setUIState(_ newState: UIState) {
        if newState == .live && uiState != .live && uiState != .finalizing {
            finishedDelivered = false  // новая сессия — гард финализации заново
        }
        uiState = newState
        let contentVisible = (newState == .live) || (newState == .finalizing)
        speakersRow.isHidden = !contentVisible
        itemsStack.isHidden = !contentVisible
        transcriptContainer.isHidden = !contentVisible
        headerTimerLabel.isHidden = !contentVisible
        recordIndicator.isHidden = !contentVisible
        if newState == .live {
            recordIndicator.startPulsing()
        } else {
            recordIndicator.stopPulsing()
        }

        // Активная transient-ошибка (showTransientError) переживает переключение
        // состояния — её собственный таймер решает, когда вернуть штатный текст.
        if !transientErrorActive {
            applyStatusText(for: newState)
        }
        stopButton.isEnabled = (newState == .live)

        if newState == .live {
            startTimer()
        } else {
            stopTimer()
        }
    }

    private func applyStatusText(for state: UIState) {
        statusSpinner.stopAnimation(nil)
        statusIcon.isHidden = false
        statusSpinner.isHidden = true
        statusIcon.contentTintColor = KrabEarTheme.Colors.textSecondary
        
        switch state {
        case .idle:
            statusLabel.stringValue = "Встреча не идёт"
            statusIcon.image = NSImage(systemSymbolName: "mic.slash", accessibilityDescription: nil)
            statusContainer.isHidden = false
        case .privacy:
            statusLabel.stringValue = "Privacy-режим"
            statusIcon.image = NSImage(systemSymbolName: "hand.raised.fill", accessibilityDescription: nil)
            statusContainer.isHidden = false
        case .finalizing:
            statusLabel.stringValue = "Финализирую…"
            statusIcon.isHidden = true
            statusSpinner.isHidden = false
            statusSpinner.startAnimation(nil)
            statusContainer.isHidden = false
        case .live:
            statusContainer.isHidden = true
        }
    }

    // MARK: - Poll (get_meeting_live_state)

    private func pollOnce() {
        guard let client = ipcClient else { return }
        // AGENT-3: ipcClient.call строго off-main.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try client.call(
                    method: "get_meeting_live_state", params: [:])
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? response
                DispatchQueue.main.async { self?.handlePollState(result) }
            } catch {
                // Poll — best-effort фоллбэк; SSE остаётся основным каналом,
                // одиночная неудача poll'а не показывается пользователю.
            }
        }
    }

    /// Ревью №2: единая обработка poll-снапшота. В .finalizing render — no-op
    /// (sticky), но inactive-снапшот означает «meeting.finished потерян» —
    /// доставляем финализацию с nil (отчёт без item_id не построить; запись
    /// доступна в истории), выводя панель из вечного «Финализирую…».
    private func handlePollState(_ state: [String: Any]) {
        if uiState == .finalizing && (state["active"] as? Bool) != true {
            deliverFinished(state["item_id"] as? String)  // почти всегда nil
            return
        }
        render(state: state)
    }

    func _testHandlePollState(_ state: [String: Any]) { handlePollState(state) }

    /// Немедленный внеплановый poll (владелец зовёт после успешного meeting_start —
    /// иначе панель, открытая до старта сессии, ждала бы 15с watchdog'а в idle).
    func pollNow() { pollOnce() }

    // MARK: - SSE (общий SSESessionDelegate, паттерн LiveSubtitlesOverlay)

    private func startSSE() {
        stopSSE()
        let filter = Self.meetingEventTypes.sorted().joined(separator: ",")
        // sorted() — детерминированный URL для тестов/логов; backend принимает
        // comma-list в любом порядке (rest_server.py:1649).
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=\(filter)") else { return }
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let task = sseSession.dataTask(with: request)
        sseTask = task
        task.resume()
    }

    private func stopSSE() {
        sseTask?.cancel()
        sseTask = nil
        pendingSSEEventType = nil
    }

    /// Паттерн LiveSubtitlesOverlay.handleSSELine: `event: ` трекает тип, `data: `
    /// диспатчит по нему. lastSSEActivity обновляется на КАЖДОЙ строке (в т.ч.
    /// строках-разделителях) — это единственный сигнал watchdog'а о живом стриме.
    private func handleSSELine(_ line: String) {
        lastSSEActivity = Date().timeIntervalSince1970
        if pollFallbackActive { deactivatePollFallback() }

        if line.hasPrefix("event: ") {
            pendingSSEEventType = String(line.dropFirst(7)).trimmingCharacters(in: .whitespaces)
        } else if line.hasPrefix("data: ") {
            let type = pendingSSEEventType
            pendingSSEEventType = nil
            guard let type, Self.meetingEventTypes.contains(type) else { return }
            dispatchSSEEvent(type: type, json: String(line.dropFirst(6)))
        } else if line.isEmpty {
            pendingSSEEventType = nil
        }
    }

    private func dispatchSSEEvent(type: String, json: String) {
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        // Конверт {type, data: {...}} ИЛИ плоский {...} — как parseSSEData в LiveSubs.
        let eventData = obj["data"] as? [String: Any] ?? obj
        switch type {
        case "meeting.transcript_appended":
            appendTranscriptChunk(eventData["chunk_text"] as? String ?? "")
        case "meeting.items_updated":
            renderItems(items: eventData["items"] as? [[String: Any]] ?? [],
                        decisions: eventData["decisions"] as? [String] ?? [],
                        questions: eventData["questions"] as? [String] ?? [])
        case "meeting.speakers_updated":
            renderSpeakers(eventData["speakers"] as? [[String: Any]] ?? [])
        case "meeting.finalizing":
            enterFinalizing()
        case "meeting.finished":
            deliverFinished(eventData["item_id"] as? String)
        default:
            break  // недостижимо — фильтр handleSSELine уже отсёк чужие типы
        }
    }

    /// Хвост транскрипта копится инкрементально по SSE-чанкам; обрезаем спереди,
    /// чтобы label не рос неограниченно на длинной встрече.
    private func appendTranscriptChunk(_ chunk: String) {
        guard !chunk.isEmpty else { return }
        transcriptTailLabel.stringValue += chunk + " "
        let maxLen = 600
        if transcriptTailLabel.stringValue.count > maxLen {
            transcriptTailLabel.stringValue = String(transcriptTailLabel.stringValue.suffix(maxLen))
        }
    }

    // MARK: - Silence watchdog + poll-фоллбэк

    private func armSilenceWatchdog() {
        silenceTimer?.invalidate()
        silenceTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.checkSilenceWatchdog() }
        }
    }

    /// Тикает каждые 5с (или напрямую из _testSimulateSSESilence). SSE молчит >15с
    /// при активной сессии → включаем poll-фоллбэк и пересоздаём SSE-стрим.
    private func checkSilenceWatchdog() {
        // Ревью №2 + живой смок 16-07: watchdog работает в ЛЮБОМ состоянии,
        // пока обновления запущены. .finalizing — потерянный meeting.finished
        // вешал «Финализирую…» навечно; .idle — панель, открытая до завершения
        // meeting_start, залипала в «Встреча не идёт» при живой сессии.
        let now = Date().timeIntervalSince1970
        if now - lastSSEActivity > 15 {
            activatePollFallback()
        }
    }

    private func activatePollFallback() {
        guard !pollFallbackActive else { return }
        pollFallbackActive = true
        stopSSE()
        startSSE()
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.pollOnce() }
        }
    }

    /// Любая живая SSE-строка (см. handleSSELine) снимает фоллбэк.
    private func deactivatePollFallback() {
        guard pollFallbackActive else { return }
        pollFallbackActive = false
        pollTimer?.invalidate()
        pollTimer = nil
    }

    // MARK: - Header-таймер (mm:ss от started_at, только в .live)

    private func startTimer() {
        stopTimer()
        updateTimer()
        timerTick = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.updateTimer() }
        }
    }

    private func stopTimer() {
        timerTick?.invalidate()
        timerTick = nil
    }

    private func updateTimer() {
        guard uiState == .live, let startedAt else { return }
        let elapsed = max(0, Int(Date().timeIntervalSince1970 - startedAt))
        headerTimerLabel.stringValue = String(format: "%02d:%02d", elapsed / 60, elapsed % 60)
    }

    // MARK: - Panel setup (паттерн ConversationStatusOverlay)

    private func setupPanel() {
        panel.styleMask.insert(.resizable)
        panel.minSize = NSSize(width: 300, height: 360)
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isOpaque = false
        panel.alphaValue = 0.97
    }

    private func buildLayout() {
        headerTimerLabel.font = KrabEarTheme.Typography.display.tabular()
        headerTimerLabel.textColor = KrabEarTheme.Colors.textPrimary
        headerTimerLabel.isBordered = false
        headerTimerLabel.drawsBackground = false

        degradedBadge.font = KrabEarTheme.Typography.captionMedium
        degradedBadge.textColor = KrabEarTheme.Colors.warning
        degradedBadge.isBordered = false
        degradedBadge.drawsBackground = false

        degradedBadgeContainer.orientation = .horizontal
        degradedBadgeContainer.wantsLayer = true
        degradedBadgeContainer.layer?.cornerRadius = 4
        degradedBadgeContainer.layer?.backgroundColor = KrabEarTheme.Colors.warning.withAlphaComponent(0.15).cgColor
        degradedBadgeContainer.edgeInsets = NSEdgeInsets(top: 2, left: 6, bottom: 2, right: 6)
        degradedBadgeContainer.addArrangedSubview(degradedBadge)
        degradedBadgeContainer.isHidden = true

        let headerRow = NSStackView(views: [recordIndicator, headerTimerLabel, degradedBadgeContainer, NSView()])
        headerRow.orientation = .horizontal
        headerRow.spacing = KrabEarTheme.Metrics.standard
        headerRow.alignment = .centerY

        speakersRow.orientation = .horizontal
        speakersRow.spacing = KrabEarTheme.Metrics.standard
        speakersRow.alignment = .centerY

        itemsStack.orientation = .vertical
        itemsStack.spacing = KrabEarTheme.Metrics.itemSpacing
        itemsStack.alignment = .leading
        itemsStack.translatesAutoresizingMaskIntoConstraints = false

        let itemsScroll = NSScrollView()
        itemsScroll.hasVerticalScroller = true
        itemsScroll.drawsBackground = false
        itemsScroll.documentView = itemsStack
        itemsScroll.translatesAutoresizingMaskIntoConstraints = false
        itemsScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 180).isActive = true

        transcriptTailLabel.font = KrabEarTheme.Typography.caption
        transcriptTailLabel.textColor = KrabEarTheme.Colors.textSecondary
        transcriptTailLabel.isBordered = false
        transcriptTailLabel.drawsBackground = false
        transcriptTailLabel.maximumNumberOfLines = 3

        transcriptContainer.wantsLayer = true
        transcriptContainer.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        transcriptContainer.layer?.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
        transcriptContainer.layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        transcriptContainer.layer?.borderWidth = 1.0
        
        transcriptContainer.addSubview(transcriptTailLabel)
        transcriptTailLabel.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            transcriptTailLabel.topAnchor.constraint(equalTo: transcriptContainer.topAnchor, constant: KrabEarTheme.Metrics.standard),
            transcriptTailLabel.bottomAnchor.constraint(equalTo: transcriptContainer.bottomAnchor, constant: -KrabEarTheme.Metrics.standard),
            transcriptTailLabel.leadingAnchor.constraint(equalTo: transcriptContainer.leadingAnchor, constant: KrabEarTheme.Metrics.standard),
            transcriptTailLabel.trailingAnchor.constraint(equalTo: transcriptContainer.trailingAnchor, constant: -KrabEarTheme.Metrics.standard),
        ])

        statusContainer.orientation = .vertical
        statusContainer.alignment = .centerX
        statusContainer.spacing = KrabEarTheme.Metrics.standard
        statusContainer.wantsLayer = true
        statusContainer.layer?.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
        statusContainer.layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        statusContainer.edgeInsets = NSEdgeInsets(top: 24, left: 32, bottom: 24, right: 32)
        
        statusSpinner.style = .spinning
        statusSpinner.controlSize = .small
        statusSpinner.isDisplayedWhenStopped = false
        
        statusIcon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 24, weight: .regular)
        statusIcon.contentTintColor = KrabEarTheme.Colors.textSecondary
        
        statusLabel.font = KrabEarTheme.Typography.body
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        statusLabel.isBordered = false
        statusLabel.drawsBackground = false
        
        statusContainer.addArrangedSubview(statusIcon)
        statusContainer.addArrangedSubview(statusSpinner)
        statusContainer.addArrangedSubview(statusLabel)

        stopButton.target = self
        stopButton.action = #selector(onStopTapped)

        let contentStack = NSStackView(views: [
            headerRow, speakersRow, itemsScroll, transcriptContainer, stopButton,
        ])
        contentStack.orientation = .vertical
        contentStack.spacing = KrabEarTheme.Metrics.comfortable
        contentStack.alignment = .leading
        contentStack.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.spacious,
            left: KrabEarTheme.Metrics.spacious,
            bottom: KrabEarTheme.Metrics.spacious,
            right: KrabEarTheme.Metrics.spacious
        )
        contentStack.translatesAutoresizingMaskIntoConstraints = false

        let backdrop = NSVisualEffectView()
        backdrop.material = .popover
        backdrop.blendingMode = .behindWindow
        backdrop.state = .active
        backdrop.wantsLayer = true
        backdrop.layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        backdrop.layer?.cornerCurve = .continuous
        backdrop.layer?.masksToBounds = true

        backdrop.addSubview(contentStack)
        backdrop.addSubview(statusContainer)
        
        statusContainer.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            contentStack.topAnchor.constraint(equalTo: backdrop.topAnchor),
            contentStack.leadingAnchor.constraint(equalTo: backdrop.leadingAnchor),
            contentStack.trailingAnchor.constraint(equalTo: backdrop.trailingAnchor),
            contentStack.bottomAnchor.constraint(lessThanOrEqualTo: backdrop.bottomAnchor),
            itemsScroll.widthAnchor.constraint(equalTo: contentStack.widthAnchor, constant: -(KrabEarTheme.Metrics.spacious * 2)),
            itemsStack.widthAnchor.constraint(equalTo: itemsScroll.widthAnchor),
            transcriptContainer.widthAnchor.constraint(equalTo: contentStack.widthAnchor, constant: -(KrabEarTheme.Metrics.spacious * 2)),
            statusContainer.centerXAnchor.constraint(equalTo: backdrop.centerXAnchor),
            statusContainer.centerYAnchor.constraint(equalTo: backdrop.centerYAnchor)
        ])

        backdrop.frame = panel.contentView!.bounds
        backdrop.autoresizingMask = [.width, .height]
        panel.contentView = backdrop

        let drag = NSPanGestureRecognizer(target: self, action: #selector(handleDrag(_:)))
        backdrop.addGestureRecognizer(drag)
    }

    @objc private func onStopTapped() {
        requestStop()
    }

    // MARK: - Position persistence (паттерн ConversationStatusOverlay/LiveSubtitlesOverlay)

    private func placeTopRight() {
        guard let screen = NSScreen.main else { return }
        let vf = screen.visibleFrame
        let size = panel.frame.size
        let x = vf.maxX - size.width - 24
        let y = vf.maxY - size.height - 24
        panel.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: true)
    }

    /// Валидна ли candidate-позиция — хотя бы 80% площади пересекается с visibleFrame
    /// какого-нибудь ТЕКУЩЕГО экрана. Портировано из ConversationStatusOverlay
    /// (изначально — RealtimeOverlayController.restoreSavedPosition(), M2): без этой
    /// проверки отключение второго монитора навсегда прячет панель за экраном.
    private func isOnScreen(_ candidate: NSRect) -> Bool {
        let totalArea = candidate.width * candidate.height
        guard totalArea > 0 else { return false }
        return NSScreen.screens.contains { screen in
            let intersection = candidate.intersection(screen.visibleFrame)
            let coveredArea = intersection.width * intersection.height
            return coveredArea / totalArea >= 0.80
        }
    }

    private func restorePosition() {
        if let saved = UserDefaults.standard.string(forKey: positionKey),
           let data = saved.data(using: .utf8),
           let dict = try? JSONSerialization.jsonObject(with: data) as? [String: CGFloat],
           let x = dict["x"], let y = dict["y"] {
            let size = panel.frame.size
            let candidate = NSRect(x: x, y: y, width: size.width, height: size.height)
            if isOnScreen(candidate) {
                panel.setFrame(candidate, display: false)
                return
            }
        }
        // Нет сохранённой позиции ИЛИ она вне видимых экранов (guard выше) — дефолт.
        placeTopRight()
    }

    private func savePosition() {
        let origin = panel.frame.origin
        let dict: [String: CGFloat] = ["x": origin.x, "y": origin.y]
        if let data = try? JSONSerialization.data(withJSONObject: dict),
           let str = String(data: data, encoding: .utf8) {
            UserDefaults.standard.set(str, forKey: positionKey)
        }
    }

    @objc private func handleDrag(_ gr: NSPanGestureRecognizer) {
        if gr.state == .ended || gr.state == .changed {
            savePosition()
        }
    }
}

class SpeakerChipView: NSView {
    let testTitle: String
    private let label = NSTextField(labelWithString: "")
    private let dot = NSView()
    
    init(testTitle: String, labelStr: String, talkStr: String, staleStr: String, isActive: Bool, colorIndex: Int) {
        self.testTitle = testTitle
        super.init(frame: .zero)
        
        wantsLayer = true
        layer?.cornerRadius = 10
        layer?.cornerCurve = .continuous
        layer?.borderWidth = 1.0
        
        let colors = [
            NSColor.systemBlue, NSColor.systemPurple, NSColor.systemTeal, NSColor.systemOrange, NSColor.systemPink, NSColor.systemGreen
        ]
        let tint = colors[colorIndex % colors.count]
        
        layer?.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
        layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        
        dot.wantsLayer = true
        dot.layer?.cornerRadius = 3
        dot.layer?.backgroundColor = isActive ? tint.cgColor : NSColor.clear.cgColor
        dot.translatesAutoresizingMaskIntoConstraints = false
        
        let attrStr = NSMutableAttributedString()
        attrStr.append(NSAttributedString(string: labelStr, attributes: [
            .font: KrabEarTheme.Typography.captionMedium,
            .foregroundColor: KrabEarTheme.Colors.textPrimary
        ]))
        attrStr.append(NSAttributedString(string: " · \(talkStr)", attributes: [
            .font: KrabEarTheme.Typography.caption,
            .foregroundColor: KrabEarTheme.Colors.textPrimary
        ]))
        attrStr.append(NSAttributedString(string: " · \(staleStr)", attributes: [
            .font: KrabEarTheme.Typography.caption,
            .foregroundColor: KrabEarTheme.Colors.textSecondary
        ]))
        
        label.attributedStringValue = attrStr
        label.isBordered = false
        label.drawsBackground = false
        label.translatesAutoresizingMaskIntoConstraints = false
        
        let stack = NSStackView(views: [dot, label])
        stack.orientation = .horizontal
        stack.spacing = 6
        stack.alignment = .centerY
        stack.translatesAutoresizingMaskIntoConstraints = false
        
        addSubview(stack)
        NSLayoutConstraint.activate([
            dot.widthAnchor.constraint(equalToConstant: 6),
            dot.heightAnchor.constraint(equalToConstant: 6),
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 4),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -4),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 8),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8)
        ])
    }
    required init?(coder: NSCoder) { fatalError() }
}

class RecordingIndicator: NSView {
    init() {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.backgroundColor = KrabEarTheme.Colors.error.cgColor
        layer?.cornerRadius = 4
        translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            widthAnchor.constraint(equalToConstant: 8),
            heightAnchor.constraint(equalToConstant: 8)
        ])
    }
    required init?(coder: NSCoder) { fatalError() }
    
    func startPulsing() {
        layer?.removeAllAnimations()
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion { return }
        let anim = CABasicAnimation(keyPath: "opacity")
        anim.fromValue = 1.0
        anim.toValue = 0.2
        anim.duration = KrabEarTheme.Motion.Duration.long
        anim.autoreverses = true
        anim.repeatCount = .infinity
        anim.timingFunction = KrabEarTheme.Motion.Easing.easeInOut
        layer?.add(anim, forKey: "pulse")
    }
    func stopPulsing() {
        layer?.removeAllAnimations()
        layer?.opacity = 1.0
    }
}
