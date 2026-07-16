/*
 MeetingLivePanelController — C2c (спека §2.7 + §2.7a): плавающая панель живой встречи.

 Владение: AgentAppDelegate (main+MeetingPanel.swift, Task 3). Панель показывает
 live-состояние meeting-сессии backend'а; закрытие панели саму сессию НЕ трогает
 (meeting_start/meeting_stop управляются отдельно).

 Task 1 — только каркас панели + чистый рендер `render(state:)` (dict → UI, БЕЗ
 IPC/сети). Данные (SSE + poll-фоллбэк + финализация в отчёт) — Task 2.

 Panel-boilerplate — портирован 1-в-1 из ConversationStatusOverlay (styleMask/level/
 collectionBehavior/drag/savePosition/restorePosition/isOnScreen≥80%) с новым
 positionKey и добавленной resizable-способностью (контент длиннее статус-HUD).

 Глиф-гейт (AGENT-J/M, CLAUDE.md): черновой префикс-глиф задачи из плана (пустой
 круг, HOLLOW BULLET) НЕ встречается в native/Sources (0 хитов) — заменён на «→»
 (297 хитов, established). «✓» уже established (ActionItems/SecuritySettings/
 Diagnostics). «?» — ASCII, гейт не касается.
*/

import AppKit

@MainActor
final class MeetingLivePanelController: NSObject {

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
    private let degradedBadge = NSTextField(labelWithString: "деградация")
    private let speakersRow = NSStackView()
    private let itemsStack = NSStackView()
    private let transcriptTailLabel = NSTextField(wrappingLabelWithString: "")
    private let stopButton = ThemeSecondaryButton(title: "Завершить встречу", target: nil, action: nil)
    private let statusLabel = NSTextField(labelWithString: "")

    private(set) var uiState: UIState = .idle
    private var startedAt: TimeInterval?
    private var timerTick: Timer?

    // MARK: - Test hooks (паттерн ConversationStatusOverlay)

    var _testPanelLevel: NSWindow.Level { panel.level }
    var _testPanelIsDraggable: Bool { panel.isMovableByWindowBackground }
    var _testUIState: UIState { uiState }
    var _testSpeakerChipCount: Int { speakersRow.arrangedSubviews.count }
    var _testSpeakerChipTitles: [String] {
        speakersRow.arrangedSubviews.compactMap { ($0 as? NSTextField)?.stringValue }
    }
    var _testItemRowCount: Int { itemsStack.arrangedSubviews.count }
    var _testTranscriptTailText: String { transcriptTailLabel.stringValue }
    var _testDegradedBadgeVisible: Bool { !degradedBadge.isHidden }

    // MARK: - Init

    override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 360, height: 520),
            styleMask: [.nonactivatingPanel, .hudWindow, .utilityWindow],
            backing: .buffered, defer: false)
        super.init()
        setupPanel()
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
        panel.orderOut(nil)
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
        degradedBadge.isHidden = !((degraded["llm"] as? Bool ?? false)
                                   || (degraded["diarization"] as? Bool ?? false))
    }

    func enterFinalizing() { setUIState(.finalizing) }
    func resetToIdle() { setUIState(.idle) }   // после показа отчёта/ошибки

    /// Task 1 — пустая заглушка. Реальный meeting_stop IPC (off-main, AGENT-3)
    /// придёт в Task 2.
    func requestStop() { }

    // MARK: - Render helpers (чистые — без IPC/сети)

    private func renderSpeakers(_ speakers: [[String: Any]]) {
        speakersRow.arrangedSubviews.forEach { speakersRow.removeArrangedSubview($0); $0.removeFromSuperview() }
        let now = Date().timeIntervalSince1970
        for speaker in speakers {
            let label = speaker["label"] as? String ?? "?"
            let talkSec = Self.doubleValue(speaker["talk_sec"]) ?? 0
            let lastActive = Self.doubleValue(speaker["last_active_ts"]) ?? now
            let staleSec = max(0, now - lastActive)
            let chip = NSTextField(labelWithString: Self.speakerChipTitle(label: label, talkSec: talkSec, staleSec: staleSec))
            chip.font = KrabEarTheme.Typography.caption
            chip.textColor = KrabEarTheme.Colors.textSecondary
            chip.isBordered = false
            chip.drawsBackground = false
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
        let row = NSTextField(wrappingLabelWithString: "\(prefix) \(text)")
        row.font = KrabEarTheme.Typography.body
        row.textColor = KrabEarTheme.Colors.textPrimary
        row.isBordered = false
        row.drawsBackground = false
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
        uiState = newState
        let contentVisible = (newState == .live) || (newState == .finalizing)
        speakersRow.isHidden = !contentVisible
        itemsStack.isHidden = !contentVisible
        transcriptTailLabel.isHidden = !contentVisible
        headerTimerLabel.isHidden = !contentVisible

        switch newState {
        case .idle:
            statusLabel.stringValue = "Встреча не идёт"
            statusLabel.isHidden = false
            stopButton.isEnabled = false
        case .privacy:
            statusLabel.stringValue = "Privacy-режим"
            statusLabel.isHidden = false
            stopButton.isEnabled = false
        case .finalizing:
            statusLabel.stringValue = "Финализирую…"
            statusLabel.isHidden = false
            stopButton.isEnabled = false
        case .live:
            statusLabel.isHidden = true
            stopButton.isEnabled = true
        }

        if newState == .live {
            startTimer()
        } else {
            stopTimer()
        }
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
        headerTimerLabel.font = KrabEarTheme.Typography.display
        headerTimerLabel.textColor = KrabEarTheme.Colors.textPrimary
        headerTimerLabel.isBordered = false
        headerTimerLabel.drawsBackground = false

        degradedBadge.font = KrabEarTheme.Typography.captionMedium
        degradedBadge.textColor = KrabEarTheme.Colors.warning
        degradedBadge.isBordered = false
        degradedBadge.drawsBackground = false
        degradedBadge.isHidden = true

        let headerRow = NSStackView(views: [headerTimerLabel, degradedBadge, NSView()])
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

        statusLabel.font = KrabEarTheme.Typography.caption
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        statusLabel.isBordered = false
        statusLabel.drawsBackground = false

        stopButton.target = self
        stopButton.action = #selector(onStopTapped)

        let contentStack = NSStackView(views: [
            headerRow, speakersRow, itemsScroll, transcriptTailLabel, statusLabel, stopButton,
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
        NSLayoutConstraint.activate([
            contentStack.topAnchor.constraint(equalTo: backdrop.topAnchor),
            contentStack.leadingAnchor.constraint(equalTo: backdrop.leadingAnchor),
            contentStack.trailingAnchor.constraint(equalTo: backdrop.trailingAnchor),
            contentStack.bottomAnchor.constraint(lessThanOrEqualTo: backdrop.bottomAnchor),
            itemsScroll.widthAnchor.constraint(equalTo: contentStack.widthAnchor),
            itemsStack.widthAnchor.constraint(equalTo: itemsScroll.widthAnchor),
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
