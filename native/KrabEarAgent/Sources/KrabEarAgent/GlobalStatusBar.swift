/*
 GlobalStatusBar — Liquid Glass thin pill в верхней части HistoryPanel.

 Видим со ВСЕХ вкладок (mounted в windowContentView, выше tabSelector).
 Показывает текущую long-running операцию backend:
 - Транскрибация: "Транскрибация · 3/12 · diarization"
 - Obsidian sync:  "Obsidian sync · 4/24"
 - Idle:          скрыт

 Работает через SSE `/v1/events` (long-poll) + парсинг `app.status` event'а.
 Соединение жизненный цикл = жизненный цикл панели (start при viewDidAppear,
 stop при viewWillDisappear).

 См. `KrabEar/backend/service.py::_handle_transcribe_paths_async` и
 `KrabEar/backend/obsidian_sync.py::sync()` — точки emit'а.

 Auto-hide policy:
 - op="idle" → скрыть немедленно
 - нет событий >12s → скрыть (heartbeat lost / op finished без emit'а idle)

 Wave 523 — AGENT-J sister fix:
 Replaced NSTextField + legacy Unicode arrow/bullet glyphs with NSImageView + SF Symbols.
 Root cause identical to AGENT-J (bullet glyph in StatusIndicatorView, Wave 67 PR #412):
 NSTextField with non-BMP / uncommon Unicode triggers CoreText glyph-metrics build
 on main thread during ColorSync callback → AppHang. SF Symbols bypass CoreText,
 rendered via Metal/CoreGraphics path instead.
*/

import AppKit
import Foundation

final class GlobalStatusBar: NSView {

    // MARK: - Subviews

    private let backgroundView: NSVisualEffectView = {
        let v = NSVisualEffectView()
        v.material = .popover
        v.blendingMode = .behindWindow
        v.state = .active
        v.wantsLayer = true
        v.layer?.cornerRadius = 14 // Половина высоты (28) для full-pill
        v.layer?.cornerCurve = .continuous
        v.layer?.masksToBounds = true
        v.layer?.borderWidth = 1.0
        v.translatesAutoresizingMaskIntoConstraints = false
        return v
    }()

    private let iconImageView: NSImageView = {
        let v = NSImageView()
        v.imageScaling = .scaleProportionallyDown
        v.translatesAutoresizingMaskIntoConstraints = false
        return v
    }()
    
    // Статус-акцент: маленький цветной слой-индикатор
    private let dotIndicatorLayer: CALayer = {
        let l = CALayer()
        l.cornerRadius = 3
        l.bounds = CGRect(x: 0, y: 0, width: 6, height: 6)
        return l
    }()

    private let textLabel: NSTextField = {
        let l = NSTextField(labelWithString: "")
        l.font = KrabEarTheme.Typography.captionMedium.tabular()
        l.textColor = KrabEarTheme.Colors.textPrimary
        l.lineBreakMode = .byTruncatingTail
        l.maximumNumberOfLines = 1
        l.translatesAutoresizingMaskIntoConstraints = false
        return l
    }()

    private let progressBar: NSProgressIndicator = {
        let p = NSProgressIndicator()
        p.style = .bar
        p.isIndeterminate = false
        p.minValue = 0
        p.maxValue = 1.0
        p.controlSize = .small
        p.translatesAutoresizingMaskIntoConstraints = false
        return p
    }()

    // MARK: - State

    /// Heartbeat: timestamp последнего пришедшего события. Если >12s — скрываемся.
    private var lastEventAt: Date = .distantPast

    /// Heartbeat-таймер: каждую секунду проверяет, не пора ли скрыться.
    private var heartbeatTimer: Timer?

    // MARK: - SSE

    private var sseTask: URLSessionDataTask?
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
    private let restBaseURL = "http://127.0.0.1:5005"

    // MARK: - Init

    override init(frame: NSRect) {
        super.init(frame: frame)
        setupViews()
        isHidden = true  // По умолчанию скрыт
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        setupViews()
        isHidden = true
    }

    private func setupViews() {
        translatesAutoresizingMaskIntoConstraints = false
        
        wantsLayer = true
        // Тень пилюли: Elevation.applyCard (тонко)
        KrabEarTheme.Elevation.applyCard(to: layer!)
        
        addSubview(backgroundView)
        backgroundView.addSubview(iconImageView)
        backgroundView.layer?.addSublayer(dotIndicatorLayer)
        backgroundView.addSubview(textLabel)
        backgroundView.addSubview(progressBar)

        let pad = KrabEarTheme.Metrics.standard
        let tight = KrabEarTheme.Metrics.tight

        NSLayoutConstraint.activate([
            backgroundView.topAnchor.constraint(equalTo: topAnchor),
            backgroundView.leadingAnchor.constraint(equalTo: leadingAnchor),
            backgroundView.trailingAnchor.constraint(equalTo: trailingAnchor),
            backgroundView.bottomAnchor.constraint(equalTo: bottomAnchor),

            iconImageView.leadingAnchor.constraint(equalTo: backgroundView.leadingAnchor, constant: pad),
            iconImageView.centerYAnchor.constraint(equalTo: backgroundView.centerYAnchor),
            iconImageView.widthAnchor.constraint(equalToConstant: 14),
            iconImageView.heightAnchor.constraint(equalToConstant: 14),

            textLabel.leadingAnchor.constraint(equalTo: iconImageView.trailingAnchor, constant: tight),
            textLabel.centerYAnchor.constraint(equalTo: backgroundView.centerYAnchor),
            textLabel.trailingAnchor.constraint(lessThanOrEqualTo: progressBar.leadingAnchor, constant: -pad),

            progressBar.trailingAnchor.constraint(equalTo: backgroundView.trailingAnchor, constant: -pad),
            progressBar.centerYAnchor.constraint(equalTo: backgroundView.centerYAnchor),
            progressBar.widthAnchor.constraint(equalToConstant: 80),

            heightAnchor.constraint(equalToConstant: 28),
        ])
        
        updateColors()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateColors()
    }
    
    private func updateColors() {
        backgroundView.layer?.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
        backgroundView.layer?.borderColor = KrabEarTheme.Colors.border.cgColor
    }
    
    override func layout() {
        super.layout()
        // Располагаем индикатор-точку поверх иконки или рядом
        // Разместим ее в левом верхнем углу iconImageView
        dotIndicatorLayer.position = CGPoint(x: iconImageView.frame.minX + 2, y: iconImageView.frame.minY + 2)
    }

    // MARK: - Public API

    /// Подписаться на SSE и запустить heartbeat. Вызывать при viewDidAppear / показе панели.
    func start() {
        startSSE()
        startHeartbeat()
    }

    /// Остановить SSE и heartbeat. Вызывать при viewWillDisappear / скрытии панели.
    func stop() {
        stopSSE()
        stopHeartbeat()
        stopPulseAnimation()
        DispatchQueue.main.async { [weak self] in self?.isHidden = true }
    }

    // MARK: - SSE Lifecycle

    private func startSSE() {
        stopSSE()
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=app.status") else { return }
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        sseTask = sseSession.dataTask(with: request)
        sseTask?.resume()
    }

    private func stopSSE() {
        sseTask?.cancel()
        sseTask = nil
    }

    // MARK: - Heartbeat

    private func startHeartbeat() {
        stopHeartbeat()
        let timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                let elapsed = Date().timeIntervalSince(self.lastEventAt)
                if elapsed > 12.0 && !self.isHidden {
                    self.isHidden = true
                    self.stopPulseAnimation()
                }
            }
        }
        RunLoop.current.add(timer, forMode: .common)
        heartbeatTimer = timer
    }

    private func stopHeartbeat() {
        heartbeatTimer?.invalidate()
        heartbeatTimer = nil
    }

    // MARK: - SSE line parsing

    @MainActor
    private func handleSSELine(_ line: String) {
        guard line.hasPrefix("data: ") else { return }
        let json = String(line.dropFirst(6))
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }

        // EventBus envelope: data field is the inner payload (since /v1/events emits `data: {jsondata}\n\n`
        // where jsondata = event['data']). See backend/event_bus.py::sse_stream.
        let payload = obj  // already the inner data dict per sse_stream contract

        let op = (payload["op"] as? String) ?? "idle"
        if op == "idle" {
            isHidden = true
            stopPulseAnimation()
            lastEventAt = Date()
            return
        }

        let stage = (payload["stage"] as? String) ?? ""
        let fileIndex = payload["file_index"] as? Int
        let totalFiles = payload["total_files"] as? Int
        let currentFile = (payload["current_file"] as? String) ?? ""
        let progress = (payload["progress"] as? Double) ?? -1.0

        let opLabel = labelForOp(op)
        var parts: [String] = [opLabel]
        if let fi = fileIndex, let tf = totalFiles, tf > 0 {
            parts.append("\(fi)/\(tf)")
        }
        if !stage.isEmpty && stage != "idle" {
            parts.append(stageRu(stage))
        }
        if !currentFile.isEmpty && currentFile.count < 40 {
            parts.append("· \(currentFile)")
        }

        textLabel.stringValue = parts.joined(separator: " · ")
        iconImageView.image = imageForOp(op)
        
        if progress >= 0 {
            progressBar.isHidden = false
            progressBar.doubleValue = max(0, min(1.0, progress))
        } else {
            progressBar.isHidden = true
        }
        
        if isHidden {
            startPulseAnimation()
        }
        isHidden = false
        lastEventAt = Date()
    }
    
    private func startPulseAnimation() {
        guard !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else {
            dotIndicatorLayer.opacity = 1.0
            return
        }
        let anim = CABasicAnimation(keyPath: "opacity")
        anim.fromValue = 1.0
        anim.toValue = 0.3
        anim.duration = KrabEarTheme.Motion.Duration.long
        anim.autoreverses = true
        anim.repeatCount = .infinity
        anim.timingFunction = KrabEarTheme.Motion.Easing.easeInOut
        dotIndicatorLayer.add(anim, forKey: "pulse")
    }
    
    private func stopPulseAnimation() {
        dotIndicatorLayer.removeAnimation(forKey: "pulse")
        dotIndicatorLayer.opacity = 1.0
    }

    // MARK: - Labels

    private func labelForOp(_ op: String) -> String {
        switch op {
        case "transcribe_job":  return "Транскрибация"
        case "obsidian_sync":   return "Obsidian sync"
        case "mlx_inference":   return "MLX"
        default:                return op
        }
    }

    /// Returns an SF Symbol image for the given operation type.
    ///
    /// Replaces the old Unicode-glyph string approach that caused CoreText
    /// fallback → AppHang (same AGENT-J root cause as the bullet glyph in StatusIndicatorView).
    /// SF Symbols are rendered via Metal/CoreGraphics, bypassing CoreText glyph lookup.
    ///
    /// - "transcribe_job"  → `waveform`  (audio waveform — transcription in progress)
    /// - "obsidian_sync"   → `arrow.triangle.2.circlepath`  (sync arrows)
    /// - "mlx_inference"   → `cpu`  (compute/inference)
    /// - default           → `circle.fill`  (generic activity indicator)
    func imageForOp(_ op: String) -> NSImage? {
        let symbolName: String
        let tintColor: NSColor
        switch op {
        case "transcribe_job":
            symbolName = "waveform"
            tintColor = KrabEarTheme.Colors.accent
        case "obsidian_sync":
            symbolName = "arrow.triangle.2.circlepath"
            tintColor = KrabEarTheme.Colors.success
        case "mlx_inference":
            symbolName = "cpu"
            // Используем .warning
            tintColor = KrabEarTheme.Colors.warning
        default:
            symbolName = "circle.fill"
            tintColor = KrabEarTheme.Colors.textSecondary
        }
        
        dotIndicatorLayer.backgroundColor = tintColor.cgColor
        
        let config = NSImage.SymbolConfiguration(pointSize: 12, weight: .medium)
            .applying(NSImage.SymbolConfiguration(paletteColors: [tintColor]))
        return NSImage(systemSymbolName: symbolName, accessibilityDescription: op)?
            .withSymbolConfiguration(config)
    }

    /// Перевод stage в русские слова. Stage'и из backend:
    /// idle, normalizing, transcribing, diarizing, llm_rewrite, saving, started, syncing
    private func stageRu(_ stage: String) -> String {
        switch stage {
        case "started":       return "старт"
        case "idle":          return ""
        case "normalizing":   return "нормализация"
        case "transcribing":  return "Whisper"
        case "diarizing":     return "диаризация"
        case "llm_rewrite":   return "LLM"
        case "saving":        return "сохранение"
        case "syncing":       return "синк"
        default:              return stage
        }
    }
}
