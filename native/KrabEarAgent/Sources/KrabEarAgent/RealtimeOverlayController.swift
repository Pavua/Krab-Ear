/*
 Экранный realtime-оверлей Krab Ear.

 Связи модуля:
 1) main.swift: показывает и обновляет оверлей во время активной записи.
 2) IPC get_recording_state: источник промежуточного текста и таймера.

 Redesign: Liquid Glass aesthetic (NSVisualEffectView + near-cursor + reveal animation).
 - macOS 13+ target, Swift 6.0 strict concurrency.

 Gemini 3.1 Pro redesign (2026-04-19):
 4a: Recording state red dot via CABasicAnimation
 4b: State-differentiated tint (red 0.04 recording / accent 0.04 transcribing)
 4c: stageLabel as pill/badge (StageBadgeView)
 4d: Pulse via CABasicAnimation (no Timer)
*/

import AppKit
import Foundation
import QuartzCore

// MARK: - State

private enum OverlayState {
    case hidden
    case live        // during recording, pulsing text + red dot
    case reveal      // 3-stage progression after stop_recording
}

// MARK: - DynamicTintView (4b)

/// NSView с динамическим cgColor — корректно перерисовывается при смене Light/Dark темы.
@MainActor
private final class DynamicTintView: NSView {
    var tintColor: NSColor = .clear {
        didSet { needsDisplay = true }
    }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        super.updateLayer()
        layer?.backgroundColor = tintColor.cgColor
    }
}

// MARK: - StageBadgeView (4c)

/// Pill/badge для stage label в reveal animation.
@MainActor
private final class StageBadgeView: NSView {
    private let label = NSTextField(labelWithString: "")

    var stageText: String = "" {
        didSet {
            label.stringValue = stageText
            isHidden = stageText.isEmpty
        }
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setupUI()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    private func setupUI() {
        wantsLayer = true
        layer?.cornerRadius = KrabEarTheme.Metrics.tight

        label.font = KrabEarTheme.Typography.captionMedium
        label.textColor = KrabEarTheme.Colors.textSecondary
        label.translatesAutoresizingMaskIntoConstraints = false

        addSubview(label)
        NSLayoutConstraint.activate([
            label.topAnchor.constraint(equalTo: topAnchor, constant: 2),
            label.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -2),
            label.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 6),
            label.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -6),
        ])
    }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        super.updateLayer()
        layer?.backgroundColor = KrabEarTheme.Colors.textSecondary.withAlphaComponent(0.12).cgColor
    }
}

// MARK: - RealtimeOverlayController

/// Плавающий Liquid Glass оверлей для realtime-превью диктовки.
/// Near-cursor positioning, анимированное появление/исчезновение,
/// поддержка 3-стадийного reveal после окончания записи.
@MainActor
public final class RealtimeOverlayController: NSObject {

    // MARK: Panel + Views

    private let panel: NSPanel

    /// Glass background — NSVisualEffectView с виброй
    private let effectView: NSVisualEffectView

    /// Surface background (0.5 alpha cardBackground)
    private let surfaceView: DynamicTintView

    /// 4b: Dynamic tint overlay (red during recording / accent during transcribing)
    private let tintView: DynamicTintView

    /// Hairline inner border поверх effectView
    private let borderLayer = CALayer()

    /// Status row: duration + mode
    private let statusLabel  = NSTextField(labelWithString: "00:00")
    private let modeLabel    = NSTextField(labelWithString: "—")

    /// 4a: Recording indicator dot (red, CABasicAnimation pulse)
    private let recordingDot = NSView()
    private let recordingDotHalo = CALayer()

    /// 4c: Stage badge (pill) для reveal animation ("Распознано" / "Очищено" / "LLM")
    private let stageBadge   = StageBadgeView()

    /// Основной текст (preview / stage text)
    /// internal — доступен из RealtimeOverlayController+PartialSSE.swift
    let primaryLabel = NSTextField(wrappingLabelWithString: "")

    // MARK: State

    private var overlayState: OverlayState = .hidden
    private var opacityPercent: Int = 100

    /// Флаг — текущий текст является частичной транскрипцией (SSE partial).
    /// internal — используется RealtimeOverlayController+PartialSSE.swift
    var _isShowingPartial: Bool = false

    private var targetAlpha: CGFloat {
        CGFloat(opacityPercent) / 100.0
    }

    /// Таск для управления стадиями reveal анимации
    private var revealTask: Task<Void, Never>?

    /// Флаг — нужно ли уважать reduce-motion
    private var reduceMotion: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    // MARK: M2: Position Memory

    /// UserDefaults key for saved overlay origin (NSPoint archived as NSValue/NSString).
    private let savedOriginKey = "RealtimeOverlay_LastOrigin"
    /// Global NSEvent monitor for drag gesture while overlay is visible.
    private var dragMonitor: Any?
    /// Position where drag started inside the panel frame.
    private var dragStartWindowLocation: NSPoint = .zero

    // MARK: M4: Multi-line Ring Buffer

    /// Maximum number of transcript lines to show simultaneously.
    private let maxVisibleLines = 4
    /// Ring buffer of live transcript lines (oldest at front, newest at back).
    private var lineRingBuffer: [String] = []

    // MARK: Layout constants

    private let minWidth:   CGFloat = 420
    private let maxWidth:   CGFloat = 640
    private let minHeight:  CGFloat = 80
    private let maxHeight:  CGFloat = 180
    private let cornerRadius: CGFloat = KrabEarTheme.Metrics.cardCornerRadius

    // CABasicAnimation keys
    private let dotPulseKey       = "krabEarDotPulse"
    private let labelPulseKey     = "krabEarLabelPulse"
    private let breathingKey      = "krabEarBreathing"

    // MARK: Init

    public override init() {
        let initialRect = NSRect(x: 0, y: 0, width: 520, height: 80)
        self.panel = NSPanel(
            contentRect: initialRect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        self.effectView = NSVisualEffectView(frame: initialRect)
        self.surfaceView = DynamicTintView(frame: initialRect)
        self.tintView   = DynamicTintView(frame: initialRect)

        super.init()
        setupPanel()
        setupEffectView()
        setupUI()
        // Note: hover dimming (F7-5) removed — panel.ignoresMouseEvents = true (M6)
        // means NSTrackingArea events never fire. Drag repositioning (M2) uses a
        // global NSEvent monitor instead.
    }

    // MARK: - Public API

    public func show() {
        revealTask?.cancel()
        guard overlayState == .hidden else { return }
        overlayState = .live
        lineRingBuffer = []  // M4: reset ring buffer on each new recording
        stageBadge.isHidden = true
        tintView.tintColor = KrabEarTheme.Colors.error.withAlphaComponent(0.04)
        // M2: restore last user-dragged position; fall back to near-cursor if off-screen or not saved.
        if !restoreSavedPosition() {
            positionNearCursor()
        }
        panel.alphaValue = 0
        panel.orderFront(nil)
        animateShow()
        startDotPulse()     // 4a / M3: 0.4↔1.0, 1.5 s period
        startLabelPulse()   // 4d
        startBreathing()    // F7-1: breathing tint alpha
        recordingDot.isHidden = false
        startDragMonitor()  // M2: track user reposition via drag
    }

    public func hide() {
        revealTask?.cancel()
        stopAllPulse()
        stopBreathing()     // F7-1: remove breathing on hide
        stopDragMonitor()   // M2: stop drag tracking
        if overlayState == .hidden { return }
        overlayState = .hidden
        recordingDot.isHidden = true
        recordingDotHalo.transform = CATransform3DIdentity
        tintView.tintColor = .clear
        animateHide { [weak self] in
            Task { @MainActor [weak self] in
                self?.panel.orderOut(nil)
            }
        }
    }

    public func update(previewText: String, translatedText: String?, durationText: String, modeHint: String) {
        guard overlayState == .live else { return }

        statusLabel.stringValue = durationText
        modeLabel.stringValue   = modeHint.isEmpty ? "—" : modeHint

        let clean = previewText.trimmingCharacters(in: .whitespacesAndNewlines)
        if clean.isEmpty {
            setPrimaryText("Слушаю…")
        } else {
            // M4: Multi-line ring buffer — push new text as a new line, keep max 4 lines.
            // Each IPC update provides the current partial sentence; we treat each non-empty
            // update as a new line only when it differs from the last ring-buffer line.
            let cleanTrans = (translatedText ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let newLine = cleanTrans.isEmpty ? clean : "\(clean)  ↔  \(cleanTrans)"

            if lineRingBuffer.last != newLine {
                lineRingBuffer.append(newLine)
                // Clip oldest lines so at most maxVisibleLines are shown.
                if lineRingBuffer.count > maxVisibleLines {
                    lineRingBuffer.removeFirst(lineRingBuffer.count - maxVisibleLines)
                }
            }
            setPrimaryText(lineRingBuffer.joined(separator: "\n"))
        }
        if panel.isVisible {
            adjustHeight()
            // 2026-05-09: слежение за курсором на каждом тике убрали — overlay
            // уезжал к краю экрана при движении мыши во время диктовки
            // (жалоба владельца). 02.09.2026 возвращено КАК ОПЦИЯ, выключенная
            // по умолчанию: поведение без настройки не меняется.
            //
            // 🔴 Два условия, без которых опция воспроизвела бы старый баг:
            //   1) ручное перетаскивание побеждает — если позиция сохранена,
            //      за курсором не идём (иначе drag становится бессмысленным);
            //   2) positionNearCursor() прижимает окно ко всем четырём краям
            //      visibleFrame, поэтому «съехать за нижнюю сторону экрана»,
            //      как в исходной жалобе, оно уже не может.
            if followCursorEnabled && !hasSavedPosition() {
                positionNearCursor()
            }
        }
    }

    public func setOpacityPercent(_ value: Int) {
        let safe = max(15, min(100, value))
        opacityPercent = safe
        if panel.isVisible && overlayState != .hidden {
            panel.alphaValue = targetAlpha
        }
    }

    public func setAudioLevel(_ rms: Float) {
        guard overlayState == .live else { return }
        let clamped = max(0.0, min(1.0, rms))
        let scale = 1.0 + CGFloat(clamped) * 2.5
        let haloOpacity = Float(0.4 + clamped * 0.4)
        
        CATransaction.begin()
        CATransaction.setAnimationDuration(0.1)
        CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .easeOut))
        recordingDotHalo.transform = CATransform3DMakeScale(scale, scale, 1.0)
        recordingDotHalo.opacity = haloOpacity
        CATransaction.commit()
    }

    // MARK: - Panel / View Setup

    private func setupPanel() {
        panel.level               = .statusBar
        panel.isFloatingPanel     = true
        panel.collectionBehavior  = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hidesOnDeactivate   = false
        panel.isOpaque            = false
        panel.backgroundColor     = .clear
        panel.hasShadow           = false
        // M6: click-through — overlay passes all mouse events to apps below.
        // nonactivatingPanel already prevents focus steal; ignoresMouseEvents makes it fully transparent to clicks.
        panel.ignoresMouseEvents  = true
    }

    private func setupEffectView() {
        effectView.material      = .popover
        effectView.blendingMode  = .behindWindow
        effectView.state         = .active
        effectView.isEmphasized  = true
        effectView.wantsLayer    = true

        effectView.layer?.cornerRadius  = cornerRadius
        effectView.layer?.masksToBounds = true

        panel.contentView?.wantsLayer = true
        if let rootLayer = panel.contentView?.layer {
            rootLayer.masksToBounds   = false
            KrabEarTheme.Elevation.applyOverlay(to: rootLayer)
        }

        borderLayer.borderColor = KrabEarTheme.Colors.border.cgColor
        borderLayer.borderWidth = 1.0
        borderLayer.cornerRadius = cornerRadius
        borderLayer.frame = effectView.bounds

        effectView.layer?.addSublayer(borderLayer)

        panel.contentView?.addSubview(effectView)
        effectView.translatesAutoresizingMaskIntoConstraints = false
        if let cv = panel.contentView {
            NSLayoutConstraint.activate([
                effectView.topAnchor.constraint(equalTo: cv.topAnchor),
                effectView.leadingAnchor.constraint(equalTo: cv.leadingAnchor),
                effectView.trailingAnchor.constraint(equalTo: cv.trailingAnchor),
                effectView.bottomAnchor.constraint(equalTo: cv.bottomAnchor),
            ])
        }
    }

    private func setupUI() {
        // Surface background
        surfaceView.wantsLayer = true
        surfaceView.layer?.cornerRadius = cornerRadius
        surfaceView.tintColor = KrabEarTheme.Colors.cardBackground
        surfaceView.translatesAutoresizingMaskIntoConstraints = false
        effectView.addSubview(surfaceView)

        // 4b: tint view (behind all labels, inside effectView)
        tintView.wantsLayer = true
        tintView.layer?.cornerRadius = cornerRadius
        tintView.tintColor = .clear
        tintView.translatesAutoresizingMaskIntoConstraints = false
        effectView.addSubview(tintView)

        NSLayoutConstraint.activate([
            surfaceView.topAnchor.constraint(equalTo: effectView.topAnchor),
            surfaceView.leadingAnchor.constraint(equalTo: effectView.leadingAnchor),
            surfaceView.trailingAnchor.constraint(equalTo: effectView.trailingAnchor),
            surfaceView.bottomAnchor.constraint(equalTo: effectView.bottomAnchor),

            tintView.topAnchor.constraint(equalTo: effectView.topAnchor),
            tintView.leadingAnchor.constraint(equalTo: effectView.leadingAnchor),
            tintView.trailingAnchor.constraint(equalTo: effectView.trailingAnchor),
            tintView.bottomAnchor.constraint(equalTo: effectView.bottomAnchor),
        ])

        statusLabel.font      = KrabEarTheme.Typography.captionMedium.tabular()
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        statusLabel.alignment = .right
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        modeLabel.font      = KrabEarTheme.Typography.captionMedium
        modeLabel.textColor = KrabEarTheme.Colors.textSecondary
        modeLabel.alignment = .left
        modeLabel.translatesAutoresizingMaskIntoConstraints = false

        // 4a: Recording dot
        recordingDot.wantsLayer = true
        recordingDot.layer?.cornerRadius = 4 // 8x8 dot
        recordingDot.layer?.backgroundColor = KrabEarTheme.Colors.error.cgColor
        recordingDot.isHidden = true
        recordingDot.translatesAutoresizingMaskIntoConstraints = false

        recordingDotHalo.backgroundColor = KrabEarTheme.Colors.error.withAlphaComponent(0.4).cgColor
        recordingDotHalo.cornerRadius = 4
        recordingDotHalo.frame = CGRect(x: 0, y: 0, width: 8, height: 8)
        recordingDot.layer?.addSublayer(recordingDotHalo)

        // 4c: Stage badge (pill)
        stageBadge.isHidden = true
        stageBadge.translatesAutoresizingMaskIntoConstraints = false

        primaryLabel.font            = KrabEarTheme.Typography.display
        primaryLabel.textColor       = KrabEarTheme.Colors.textPrimary
        primaryLabel.alignment       = .left
        primaryLabel.maximumNumberOfLines = 0
        primaryLabel.lineBreakMode   = .byWordWrapping
        primaryLabel.wantsLayer      = true
        primaryLabel.translatesAutoresizingMaskIntoConstraints = false
        setPrimaryText("Слушаю…")  // F7-4: use kern-attributed setter

        effectView.addSubview(recordingDot)
        effectView.addSubview(modeLabel)
        effectView.addSubview(statusLabel)
        effectView.addSubview(stageBadge)
        effectView.addSubview(primaryLabel)

        NSLayoutConstraint.activate([
            recordingDot.leadingAnchor.constraint(equalTo: effectView.leadingAnchor, constant: KrabEarTheme.Metrics.comfortable),
            recordingDot.topAnchor.constraint(equalTo: effectView.topAnchor, constant: KrabEarTheme.Metrics.comfortable),
            recordingDot.widthAnchor.constraint(equalToConstant: 8),
            recordingDot.heightAnchor.constraint(equalToConstant: 8),

            modeLabel.centerYAnchor.constraint(equalTo: recordingDot.centerYAnchor),
            modeLabel.leadingAnchor.constraint(equalTo: recordingDot.trailingAnchor, constant: KrabEarTheme.Metrics.standard),

            statusLabel.centerYAnchor.constraint(equalTo: modeLabel.centerYAnchor),
            statusLabel.trailingAnchor.constraint(equalTo: effectView.trailingAnchor, constant: -KrabEarTheme.Metrics.comfortable),

            // stage badge — below top row in reveal
            stageBadge.topAnchor.constraint(equalTo: modeLabel.bottomAnchor, constant: KrabEarTheme.Metrics.standard),
            stageBadge.leadingAnchor.constraint(equalTo: effectView.leadingAnchor, constant: KrabEarTheme.Metrics.comfortable),

            primaryLabel.topAnchor.constraint(equalTo: stageBadge.bottomAnchor, constant: KrabEarTheme.Metrics.tight),
            primaryLabel.leadingAnchor.constraint(equalTo: effectView.leadingAnchor, constant: KrabEarTheme.Metrics.comfortable),
            primaryLabel.trailingAnchor.constraint(equalTo: effectView.trailingAnchor, constant: -KrabEarTheme.Metrics.comfortable),
            primaryLabel.bottomAnchor.constraint(lessThanOrEqualTo: effectView.bottomAnchor, constant: -KrabEarTheme.Metrics.comfortable),
        ])
    }

    // MARK: - Animations

    private func animateShow() {
        // M1: fade-in 250 ms, easeInEaseOut. Reduce-motion: instant.
        if reduceMotion {
            panel.alphaValue = targetAlpha
            return
        }
        if let layer = panel.contentView?.layer {
            let scaleT = CATransform3DMakeScale(0.98, 0.98, 1.0)
            let translateT = CATransform3DMakeTranslation(0, -10, 0)
            layer.transform = CATransform3DConcat(scaleT, translateT)
        }
        panel.alphaValue = 0

        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.25
            ctx.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            ctx.allowsImplicitAnimation = true
            panel.animator().alphaValue = targetAlpha
            panel.contentView?.layer?.transform = CATransform3DIdentity
        })
    }

    private func animateHide(completion: @escaping @Sendable () -> Void) {
        // M1: fade-out 350 ms, easeInEaseOut. Reduce-motion: instant.
        if reduceMotion {
            panel.alphaValue = 0
            completion()
            return
        }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.35
            ctx.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            ctx.allowsImplicitAnimation = true
            panel.animator().alphaValue = 0
            if let layer = panel.contentView?.layer {
                var t = CATransform3DIdentity
                t = CATransform3DScale(t, 0.96, 0.96, 1.0)
                t = CATransform3DTranslate(t, 0, 8, 0)
                layer.transform = t
            }
        }, completionHandler: {
            Task { @MainActor in completion() }
        })
    }

    // MARK: - 4a / M3: Recording Dot Pulse (CABasicAnimation)
    // M3: pulse range 0.4↔1.0 per spec (was 1.0→0.2 which looked like blinking-off).
    // 1.5 s period (0.75 s per half), autoreverses, reduce-motion skipped.

    private func startDotPulse() {
        guard !reduceMotion else {
            recordingDot.layer?.opacity = 1.0
            return
        }
        recordingDot.layer?.removeAnimation(forKey: dotPulseKey)

        let pulse = CABasicAnimation(keyPath: "opacity")
        pulse.fromValue = 0.4   // M3: min opacity (was 1.0)
        pulse.toValue   = 1.0   // M3: max opacity (was 0.2)
        pulse.duration  = 0.75  // half-period → 1.5 s full cycle
        pulse.autoreverses   = true
        pulse.repeatCount    = .infinity
        pulse.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)

        recordingDot.layer?.add(pulse, forKey: dotPulseKey)
    }

    private func stopDotPulse() {
        recordingDot.layer?.removeAnimation(forKey: dotPulseKey)
        recordingDot.layer?.opacity = 1.0
    }

    // MARK: - 4d: Label Pulse (CABasicAnimation, replaces pulseTimer)

    private func startLabelPulse() {
        guard !reduceMotion else {
            primaryLabel.layer?.opacity = 1.0
            return
        }
        primaryLabel.layer?.removeAnimation(forKey: labelPulseKey)

        let pulse = CABasicAnimation(keyPath: "opacity")
        pulse.fromValue = 1.0
        pulse.toValue   = 0.65
        pulse.duration  = KrabEarTheme.Motion.Duration.long
        pulse.autoreverses  = true
        pulse.repeatCount   = .infinity
        pulse.timingFunction = KrabEarTheme.Motion.Easing.easeInOut

        primaryLabel.layer?.add(pulse, forKey: labelPulseKey)
    }

    private func stopLabelPulse() {
        primaryLabel.layer?.removeAnimation(forKey: labelPulseKey)
        primaryLabel.layer?.opacity = 1.0
    }

    private func stopAllPulse() {
        stopDotPulse()
        stopLabelPulse()
    }

    // MARK: - Positioning

    /// Следовать ли за курсором на каждом тике обновления.
    /// Выключено по умолчанию — включается настройкой `overlay_follow_cursor`.
    var followCursorEnabled: Bool = false

    private func positionNearCursor() {
        let cursor = NSEvent.mouseLocation

        let screen = NSScreen.screens.first { $0.frame.contains(cursor) }
                  ?? NSScreen.main
                  ?? NSScreen.screens.first

        guard let screen else { return }
        let visible = screen.visibleFrame

        let width = clamp(value: 520, min: minWidth, max: maxWidth)
        let height = currentPanelHeight()

        // F7-2: cursor offset 16pt right, 24pt below (bottom-right of cursor tip)
        var x = cursor.x + 16
        var y = cursor.y - 24 - height

        if x + width > visible.maxX {
            x = cursor.x - width - 10
        }
        if x < visible.minX {
            x = visible.minX + 8
        }
        if y < visible.minY {
            y = cursor.y + 30
        }
        if y + height > visible.maxY {
            y = visible.maxY - height - 8
        }

        panel.setFrame(NSRect(x: x, y: y, width: width, height: height), display: true)
        borderLayer.frame = effectView.bounds
    }

    /// internal — доступен из RealtimeOverlayController+PartialSSE.swift
    func adjustHeight() {
        let width = clamp(value: 520, min: minWidth, max: maxWidth)
        let insets: CGFloat = KrabEarTheme.Metrics.comfortable * 2
        let topRowH: CGFloat = KrabEarTheme.Metrics.comfortable + 16
        let stageLabelH: CGFloat = stageBadge.isHidden ? 0 : (18 + KrabEarTheme.Metrics.standard)
        let padding: CGFloat = KrabEarTheme.Metrics.tight + KrabEarTheme.Metrics.comfortable
        let textWidth = width - insets

        let textH = heightForString(primaryLabel.stringValue, font: primaryLabel.font ?? KrabEarTheme.Typography.display, width: textWidth)
        let total = topRowH + stageLabelH + padding + textH
        let height = clamp(value: total, min: minHeight, max: maxHeight)

        let oldFrame = panel.frame
        let fixedHeight = maxHeight
        if abs(oldFrame.size.height - fixedHeight) > 0.5 {
            // Once: enlarge panel to fixed height anchored at top-left.
            let topLeft = NSPoint(x: oldFrame.minX, y: oldFrame.maxY)
            panel.setContentSize(NSSize(width: oldFrame.size.width, height: fixedHeight))
            panel.setFrameTopLeftPoint(topLeft)
        }
        // height parameter ignored — panel always at maxHeight.
        _ = height

        borderLayer.frame = effectView.bounds
    }

    private func currentPanelHeight() -> CGFloat {
        let width: CGFloat = 520
        let insets: CGFloat = KrabEarTheme.Metrics.comfortable * 2
        let topRowH: CGFloat = KrabEarTheme.Metrics.comfortable + 16
        let stageLabelH: CGFloat = stageBadge.isHidden ? 0 : (18 + KrabEarTheme.Metrics.standard)
        let padding: CGFloat = KrabEarTheme.Metrics.tight + KrabEarTheme.Metrics.comfortable
        let textWidth = width - insets
        let textH = heightForString(primaryLabel.stringValue, font: primaryLabel.font ?? KrabEarTheme.Typography.display, width: textWidth)
        let total = topRowH + stageLabelH + padding + textH
        return clamp(value: total, min: minHeight, max: maxHeight)
    }

    // MARK: - F7-1: Breathing Tint Animation

    private func startBreathing() {
        guard !reduceMotion else { return }
        tintView.layer?.removeAnimation(forKey: breathingKey)
        let breathing = CABasicAnimation(keyPath: "opacity")
        breathing.fromValue  = 0.03
        breathing.toValue    = 0.08
        breathing.duration   = 1.5
        breathing.autoreverses = true
        breathing.repeatCount  = .infinity
        tintView.layer?.add(breathing, forKey: breathingKey)
    }

    private func stopBreathing() {
        tintView.layer?.removeAnimation(forKey: breathingKey)
    }

    // MARK: - F7-4: Typographic Tracking Helper

    /// Sets primaryLabel text with 0.3pt letter-spacing kern applied.
    func setPrimaryText(_ text: String) {
        let font = primaryLabel.font ?? KrabEarTheme.Typography.display
        let attrs: [NSAttributedString.Key: Any] = [
            .kern: 0.3 as NSNumber,
            .font: font,
            .foregroundColor: KrabEarTheme.Colors.textPrimary
        ]
        primaryLabel.attributedStringValue = NSAttributedString(string: text, attributes: attrs)
    }

    // MARK: - M2: Position Memory + Drag Monitor

    /// Returns true and repositions the panel if a valid saved origin exists on any current screen.
    @discardableResult
    private func restoreSavedPosition() -> Bool {
        guard let dict = UserDefaults.standard.dictionary(forKey: savedOriginKey),
              let x = dict["x"] as? CGFloat,
              let y = dict["y"] as? CGFloat
        else { return false }

        let origin = NSPoint(x: x, y: y)
        let width = clamp(value: 520, min: minWidth, max: maxWidth)
        let height = currentPanelHeight()
        let candidate = NSRect(origin: origin, size: CGSize(width: width, height: height))

        // Validate: at least 80% of the frame must be on some screen (handles monitor disconnect).
        let isOnScreen = NSScreen.screens.contains { screen in
            let intersection = candidate.intersection(screen.visibleFrame)
            let coveredArea = intersection.width * intersection.height
            let totalArea = width * height
            return coveredArea / totalArea >= 0.80
        }
        guard isOnScreen else { return false }

        panel.setFrame(candidate, display: true)
        borderLayer.frame = effectView.bounds
        return true
    }

    /// Saves the current panel origin to UserDefaults.
    private func saveCurrentPosition() {
        let origin = panel.frame.origin
        UserDefaults.standard.set(["x": origin.x, "y": origin.y], forKey: savedOriginKey)
    }

    /// Installs a global NSEvent monitor to detect when the user drags the overlay window.
    /// Since `ignoresMouseEvents = true`, we use a global monitor to observe drags anywhere.
    /// We detect a drag near the overlay by checking if mouseDown is inside the panel frame.
    private func startDragMonitor() {
        stopDragMonitor()

        // We capture leftMouseDown + leftMouseDragged at the global level.
        // When mouseDown is within the panel frame, we allow dragging by temporarily
        // disabling ignoresMouseEvents during the drag gesture.
        var isDragging = false
        var dragStartMouseLocation: NSPoint = .zero
        var dragStartFrameOrigin: NSPoint = .zero

        dragMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: [.leftMouseDown, .leftMouseDragged, .leftMouseUp]
        ) { [weak self] event in
            guard let self else { return }
            Task { @MainActor [weak self] in
                guard let self else { return }
                let mouseLocation = NSEvent.mouseLocation
                switch event.type {
                case .leftMouseDown:
                    // Check if click is within the panel frame (or within 8pt of border for easy grab)
                    let panelFrame = self.panel.frame.insetBy(dx: -8, dy: -8)
                    if panelFrame.contains(mouseLocation) {
                        isDragging = true
                        dragStartMouseLocation = mouseLocation
                        dragStartFrameOrigin = self.panel.frame.origin
                        // Enable mouse events temporarily so the panel responds to drag
                        self.panel.ignoresMouseEvents = false
                    }
                case .leftMouseDragged where isDragging:
                    let dx = mouseLocation.x - dragStartMouseLocation.x
                    let dy = mouseLocation.y - dragStartMouseLocation.y
                    let newOrigin = NSPoint(
                        x: dragStartFrameOrigin.x + dx,
                        y: dragStartFrameOrigin.y + dy
                    )
                    var newFrame = self.panel.frame
                    newFrame.origin = newOrigin
                    self.panel.setFrame(newFrame, display: true)
                    self.borderLayer.frame = self.effectView.bounds
                case .leftMouseUp where isDragging:
                    isDragging = false
                    self.panel.ignoresMouseEvents = true  // M6: restore click-through
                    self.saveCurrentPosition()            // M2: persist dragged position
                default:
                    break
                }
            }
        }
    }

    /// Removes the global drag event monitor.
    private func stopDragMonitor() {
        if let monitor = dragMonitor {
            NSEvent.removeMonitor(monitor)
            dragMonitor = nil
        }
        // Ensure click-through is restored if drag monitor is stopped mid-drag.
        panel.ignoresMouseEvents = true
    }

    // MARK: - Helpers

    private func heightForString(_ string: String, font: NSFont, width: CGFloat) -> CGFloat {
        guard !string.isEmpty, width > 0 else { return 22 }
        let attrs: [NSAttributedString.Key: Any] = [.font: font]
        let boundingRect = (string as NSString).boundingRect(
            with: CGSize(width: width, height: CGFloat.greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: attrs
        )
        return ceil(boundingRect.height)
    }

    private func clamp(value: CGFloat, min minV: CGFloat, max maxV: CGFloat) -> CGFloat {
        Swift.max(minV, Swift.min(maxV, value))
    }
}
