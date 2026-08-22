/*
 TranslationStreamView — dual-pane live translation view (Phase 2 PR 2.3).

 UI:
   ┌────────────────────────────────────────────────────────────┐
   │ Pair: RU→EN ▾   [Старт]   ● Захват: вкл           00:42    │
   ├──────────────────────────────────┬─────────────────────────┤
   │ Оригинал (RU)                    │ Перевод (EN)            │
   │ ──────                           │ ──────                  │
   │ [scrollable history]             │ [scrollable history]    │
   │ Последняя: текущая фраза…        │ Последняя: текущий…     │
   └──────────────────────────────────┴─────────────────────────┘

 Архитектура:
 - Подписывается на SSE `/v1/events?filter=live_subs.result` (тот же
   endpoint что использует LiveSubtitlesOverlay), парсит {original, translation}
   из event.data, добавляет в two parallel scroll views.
 - Reuse existing pipeline: AgentAppDelegate.systemAudioCapture +
   live_subs_push_chunk IPC (Phase 2B). Старт/стоп captures проксируется
   через delegate callbacks.
 - В отличие от LiveSubtitlesOverlay (transient HUD с 3 строками + fade),
   этот view хранит **полную историю сессии** в обеих панелях, scroll up
   показывает прошлые фразы. После end of capture история остаётся видимой.

 Reuse:
 - SSESessionDelegate из LiveSubtitlesOverlay file-private — здесь
   создаём свой инстанс (один URLSession делegate per consumer).
 - Layout pattern: ThemeCardView для each pane, KrabEarTheme.Metrics для spacing.
*/

import AppKit
import Foundation

// MARK: - TranslationEntry

private struct TranslationEntry: Identifiable {
    let id = UUID()
    let original: String
    let translation: String
    let timestamp: Date
}

// MARK: - TranslationStreamView

@MainActor
final class TranslationStreamView: NSView {

    // MARK: - Public configuration

    /// Callback when user clicks Старт/Стоп. AppDelegate реагирует —
    /// поднимает SystemAudioCapture или останавливает.
    var onToggleCapture: (() -> Void)?

    /// External signal: capture is active (true) или нет.
    /// Меняет состояние кнопки + dot indicator.
    var isCapturing: Bool = false {
        didSet { updateCaptureUI() }
    }

    /// REST base URL для SSE subscription.
    var restBaseURL: String = "http://127.0.0.1:5005"

    // MARK: - State

    private var entries: [TranslationEntry] = []
    private let maxEntriesShown = 200  // history limit (ring buffer)

    // MARK: - UI

    private let topBar = NSStackView()
    private let langPairLabel = NSTextField(labelWithString: "Пара: авто → ru")
    private let captureButton = ThemePrimaryButton(title: "Старт", target: nil, action: nil)
    private let captureDot = NSImageView()
    private let captureStatusLabel = NSTextField(labelWithString: "Не активно")
    private let durationLabel = NSTextField(labelWithString: "00:00")

    private let panesRow = NSStackView()
    private let originalCard = ThemeCardView()
    private let translationCard = ThemeCardView()
    private let originalScroll = NSScrollView()
    private let translationScroll = NSScrollView()
    private let originalText = GlossaryAwareTextView()
    private let translationText = GlossaryAwareTextView()

    // MARK: - Glossary state

    /// Cached glossary [source.lowercased() → target]. Refreshed on init и
    /// каждые 60 секунд через timer (cheap IPC call с 5s TTL cache в backend).
    private var glossaryRefreshTimer: Timer?

    // MARK: - SSE

    private var sseStreamTask: URLSessionDataTask?
    private lazy var sseDelegate: SSESessionDelegate = SSESessionDelegate { [weak self] line in
        Task { @MainActor [weak self] in
            self?.handleSSELine(line)
        }
    }
    private lazy var sseSession = URLSession(
        configuration: .default,
        delegate: sseDelegate,
        delegateQueue: nil
    )

    // Capture timer (для duration label).
    private var captureStartedAt: Date?
    private var captureTimer: Timer?

    // MARK: - Init

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setupLayout()
        captureButton.target = self
        captureButton.action = #selector(onCaptureClick)
        startSSE()
        startGlossaryRefresh()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        setupLayout()
        captureButton.target = self
        captureButton.action = #selector(onCaptureClick)
        startSSE()
        startGlossaryRefresh()
    }

    deinit {
        sseStreamTask?.cancel()
        // Note: Timer.invalidate must run on main; deinit not guaranteed main.
        // Acceptable: Timer holds weak self, becomes no-op после dealloc.
    }

    // MARK: - Layout

    private func setupLayout() {
        translatesAutoresizingMaskIntoConstraints = false

        // Top bar: pair + button + status + duration.
        topBar.orientation = .horizontal
        topBar.alignment = .centerY
        topBar.spacing = KrabEarTheme.Metrics.comfortable
        topBar.translatesAutoresizingMaskIntoConstraints = false
        topBar.distribution = .fill

        langPairLabel.font = KrabEarTheme.Typography.body
        langPairLabel.textColor = KrabEarTheme.Colors.textPrimary

        let dotImg = NSImage(systemSymbolName: "waveform.circle.fill", accessibilityDescription: nil)
        captureDot.image = dotImg
        captureDot.contentTintColor = NSColor.tertiaryLabelColor
        captureDot.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            captureDot.widthAnchor.constraint(equalToConstant: 14),
            captureDot.heightAnchor.constraint(equalToConstant: 14),
        ])

        captureStatusLabel.font = KrabEarTheme.Typography.caption
        // De-emphasize secondary status text per Gemini design 2026-04-26 review.
        captureStatusLabel.textColor = KrabEarTheme.Colors.textSecondary.withAlphaComponent(0.7)

        // Tabular figures (monospaced digits) — стабильная ширина при changes seconds.
        durationLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .medium)
        durationLabel.textColor = KrabEarTheme.Colors.textSecondary

        topBar.addArrangedSubview(langPairLabel)
        topBar.addArrangedSubview(NSView()) // small flexible spacer
        topBar.addArrangedSubview(captureButton)
        topBar.addArrangedSubview(captureDot)
        topBar.addArrangedSubview(captureStatusLabel)
        topBar.addArrangedSubview(NSView()) // flexible spacer right
        topBar.addArrangedSubview(durationLabel)

        // Two scroll panes.
        configurePane(scroll: originalScroll, text: originalText, card: originalCard, title: "Оригинал")
        configurePane(scroll: translationScroll, text: translationText, card: translationCard, title: "Перевод")

        panesRow.orientation = .horizontal
        panesRow.alignment = .top
        panesRow.distribution = .fillEqually
        panesRow.spacing = KrabEarTheme.Metrics.standard
        panesRow.translatesAutoresizingMaskIntoConstraints = false
        panesRow.addArrangedSubview(originalCard)
        panesRow.addArrangedSubview(translationCard)

        addSubview(topBar)
        addSubview(panesRow)

        NSLayoutConstraint.activate([
            topBar.topAnchor.constraint(equalTo: topAnchor, constant: KrabEarTheme.Metrics.standard),
            topBar.leadingAnchor.constraint(equalTo: leadingAnchor, constant: KrabEarTheme.Metrics.standard),
            topBar.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -KrabEarTheme.Metrics.standard),

            panesRow.topAnchor.constraint(equalTo: topBar.bottomAnchor, constant: KrabEarTheme.Metrics.standard),
            panesRow.leadingAnchor.constraint(equalTo: leadingAnchor, constant: KrabEarTheme.Metrics.standard),
            panesRow.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -KrabEarTheme.Metrics.standard),
            panesRow.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -KrabEarTheme.Metrics.standard),
            panesRow.heightAnchor.constraint(greaterThanOrEqualToConstant: 280),
        ])

        updateCaptureUI()
    }

    private func configurePane(scroll: NSScrollView, text: NSTextView, card: ThemeCardView, title: String) {
        card.title = title
        // Liquid Glass effect: 0.5pt internal border имитирует свет на ребре стекла.
        // Gemini design review 2026-04-26: subtle inner edge делает carb visually thinner.
        card.wantsLayer = true
        card.layer?.borderWidth = 0.5
        card.layer?.borderColor = NSColor.white.withAlphaComponent(0.15).cgColor

        text.isEditable = false
        text.isSelectable = true
        text.drawsBackground = false
        text.backgroundColor = .clear
        text.font = KrabEarTheme.Typography.body
        // Wider inset (12 vs 8) даёт breathing room для длинных переводов.
        text.textContainerInset = NSSize(width: 12, height: 12)
        text.textColor = KrabEarTheme.Colors.textPrimary

        // lineHeight 1.35 + paragraphSpacing 12 — улучшают legibility и
        // визуально отделяют SSE chunks (Gemini design 2026-04-26).
        let paragraphStyle = NSMutableParagraphStyle()
        paragraphStyle.lineHeightMultiple = 1.35
        paragraphStyle.paragraphSpacing = 12
        text.defaultParagraphStyle = paragraphStyle
        text.translatesAutoresizingMaskIntoConstraints = false

        scroll.documentView = text
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.autohidesScrollers = true
        scroll.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 240),
        ])

        card.contentStackView.addArrangedSubview(scroll)
    }

    // MARK: - Capture button

    @objc private func onCaptureClick() {
        onToggleCapture?()
    }

    private func updateCaptureUI() {
        if isCapturing {
            captureButton.title = "Стоп"
            captureDot.contentTintColor = KrabEarTheme.Colors.accent
            captureStatusLabel.stringValue = "Захват вкл"
            captureStatusLabel.textColor = KrabEarTheme.Colors.accent
            startCaptureTimer()
            startCapturePulse()
        } else {
            captureButton.title = "Старт"
            captureDot.contentTintColor = NSColor.tertiaryLabelColor
            captureStatusLabel.stringValue = "Не активно"
            captureStatusLabel.textColor = KrabEarTheme.Colors.textSecondary.withAlphaComponent(0.7)
            stopCaptureTimer()
            stopCapturePulse()
        }
    }

    /// Soft pulsing opacity (0.4 ↔ 1.0, 1.2s autoreverse) на waveform icon когда
    /// захват активен — визуальный сигнал «слушаю» без агрессивного motion.
    /// Honors Reduce Motion (accessibility). Gemini design 2026-04-26.
    private func startCapturePulse() {
        captureDot.wantsLayer = true
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion { return }
        let anim = CABasicAnimation(keyPath: "opacity")
        anim.fromValue = 1.0
        anim.toValue = 0.4
        anim.duration = 1.2
        anim.autoreverses = true
        anim.repeatCount = .infinity
        anim.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        captureDot.layer?.add(anim, forKey: "capture-pulse")
    }

    private func stopCapturePulse() {
        captureDot.layer?.removeAnimation(forKey: "capture-pulse")
        captureDot.layer?.opacity = 1.0
    }

    private func startCaptureTimer() {
        captureStartedAt = Date()
        captureTimer?.invalidate()
        captureTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.tickDurationLabel()
            }
        }
        tickDurationLabel()
    }

    private func stopCaptureTimer() {
        captureTimer?.invalidate()
        captureTimer = nil
        captureStartedAt = nil
    }

    private func tickDurationLabel() {
        guard let start = captureStartedAt else {
            durationLabel.stringValue = "00:00"
            return
        }
        let elapsed = Int(Date().timeIntervalSince(start))
        let m = elapsed / 60
        let s = elapsed % 60
        durationLabel.stringValue = String(format: "%02d:%02d", m, s)
    }

    // MARK: - SSE

    private func startSSE() {
        guard sseStreamTask == nil else { return }
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=live_subs.result") else { return }
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let task = sseSession.dataTask(with: request)
        sseStreamTask = task
        task.resume()
    }

    private func handleSSELine(_ line: String) {
        guard line.hasPrefix("data: ") else { return }
        let json = String(line.dropFirst(6))
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        let payload = obj["data"] as? [String: Any] ?? obj
        // Backend LiveSubsResult emits "text" (not "original"); accept both for safety.
        let original = (payload["original"] as? String) ?? (payload["text"] as? String) ?? ""
        let translation = (payload["translation"] as? String)
            ?? (payload["translated"] as? String)
            ?? ""
        guard !original.isEmpty || !translation.isEmpty else { return }
        appendEntry(original: original, translation: translation)
    }

    /// Добавляет entry в обе панели + auto-scroll к низу.
    private func appendEntry(original: String, translation: String) {
        let entry = TranslationEntry(original: original, translation: translation, timestamp: Date())
        entries.append(entry)
        // Ring buffer — отрезаем top если слишком много.
        if entries.count > maxEntriesShown {
            let drop = entries.count - maxEntriesShown
            entries.removeFirst(drop)
            // Перерисовать с нуля — простая, быстрая стратегия для max 200 entries.
            originalText.string = entries.map { $0.original }.joined(separator: "\n")
            translationText.string = entries.map { $0.translation }.joined(separator: "\n")
        } else {
            // Append to existing text (faster).
            if !originalText.string.isEmpty { originalText.string += "\n" }
            originalText.string += original
            if !translationText.string.isEmpty { translationText.string += "\n" }
            translationText.string += translation
        }
        // Auto-scroll bottom.
        originalText.scrollToEndOfDocument(nil)
        translationText.scrollToEndOfDocument(nil)
    }

    // MARK: - Glossary refresh

    /// Запускает периодический refresh glossary (60s интервал) + immediate first fetch.
    private func startGlossaryRefresh() {
        refreshGlossary()
        glossaryRefreshTimer?.invalidate()
        glossaryRefreshTimer = Timer.scheduledTimer(withTimeInterval: 60.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.refreshGlossary()
            }
        }
    }

    /// Получает settings.translation_glossary через IPC + обновляет text views.
    /// Async (DispatchQueue.global) чтобы не блокировать main thread даже на 5s timeout.
    private func refreshGlossary() {
        guard let app = NSApp.delegate as? AgentAppDelegate else { return }
        let ipcClient = app.ipcClient
        DispatchQueue.global(qos: .utility).async {
            guard
                let response = try? ipcClient.call(
                    method: "get_settings",
                    params: [:],
                    timeoutSec: IPCClient.quickTimeoutSec
                ),
                let result = response["result"] as? [String: Any],
                let glossary = result["translation_glossary"] as? [String: String]
            else {
                return
            }
            DispatchQueue.main.async { [weak self] in
                guard let self = self else { return }
                self.originalText.setGlossary(glossary)
                self.translationText.setGlossary(glossary)
            }
        }
    }
}
