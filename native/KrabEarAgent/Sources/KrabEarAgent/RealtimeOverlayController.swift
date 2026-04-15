/*
 Экранный realtime-оверлей Krab Ear.

 Связи модуля:
 1) main.swift: показывает и обновляет оверлей во время активной записи.
 2) IPC get_recording_state: источник промежуточного текста и таймера.

 Redesign: Liquid Glass aesthetic (NSVisualEffectView + near-cursor + reveal animation).
 - macOS 13+ target, Swift 6.0 strict concurrency.
*/

import AppKit
import Foundation
import QuartzCore

// MARK: - State

private enum OverlayState {
    case hidden
    case live        // during recording, pulsing text
    case reveal      // 3-stage progression after stop_recording
}

// MARK: - RealtimeOverlayController

/// Плавающий Liquid Glass оверлей для realtime-превью диктовки.
/// Near-cursor positioning, анимированное появление/исчезновение,
/// поддержка 3-стадийного reveal после окончания записи.
@MainActor
public final class RealtimeOverlayController {

    // MARK: Panel + Views

    private let panel: NSPanel

    /// Glass background — NSVisualEffectView с виброй
    private let effectView: NSVisualEffectView

    /// Hairline inner border поверх effectView
    private let borderLayer = CALayer()

    /// Status row: duration + mode
    private let statusLabel  = NSTextField(labelWithString: "00:00")
    private let modeLabel    = NSTextField(labelWithString: "—")

    /// Stage label для reveal animation ("Распознано" / "Очищено" / "LLM")
    private let stageLabel   = NSTextField(labelWithString: "")

    /// Основной текст (preview / stage text)
    private let primaryLabel = NSTextField(wrappingLabelWithString: "")

    // MARK: State

    private var overlayState: OverlayState = .hidden
    private var opacityPercent: Int = 100
    
    private var targetAlpha: CGFloat {
        CGFloat(opacityPercent) / 100.0
    }

    /// Таймер пульсации во время live mode
    private var pulseTimer: Timer?
    private var pulseFadeOut: Bool = true
    
    /// Таск для управления стадиями reveal анимации
    private var revealTask: Task<Void, Never>?

    /// Флаг — нужно ли уважать reduce-motion
    private var reduceMotion: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    // MARK: Layout constants

    private let minWidth:   CGFloat = 420
    private let maxWidth:   CGFloat = 640
    private let minHeight:  CGFloat = 80
    private let maxHeight:  CGFloat = 180
    private let cornerRadius: CGFloat = 16

    // MARK: Init

    public init() {
        let initialRect = NSRect(x: 0, y: 0, width: 520, height: 80)
        self.panel = NSPanel(
            contentRect: initialRect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        self.effectView = NSVisualEffectView(frame: initialRect)

        setupPanel()
        setupEffectView()
        setupUI()
    }

    // MARK: - Public API

    public func show() {
        revealTask?.cancel()
        guard overlayState == .hidden else { return }
        overlayState = .live
        stageLabel.isHidden = true
        positionNearCursor()
        panel.alphaValue = 0
        panel.orderFront(nil)
        animateShow()
        startPulse()
    }

    public func hide() {
        revealTask?.cancel()
        stopPulse()
        if overlayState == .hidden { return }
        overlayState = .hidden
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
            primaryLabel.stringValue = "Слушаю…"
        } else {
            let cleanTrans = (translatedText ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if cleanTrans.isEmpty {
                primaryLabel.stringValue = clean
            } else {
                primaryLabel.stringValue = "\(clean)\n\n↔ Перевод\n\(cleanTrans)"
            }
        }
        if panel.isVisible {
            adjustHeight()
            positionNearCursor()
        }
    }

    public func setOpacityPercent(_ value: Int) {
        let safe = max(15, min(100, value))
        opacityPercent = safe
        if panel.isVisible && overlayState != .hidden {
            panel.alphaValue = targetAlpha
        }
    }

    // MARK: - Reveal Animation API

    /// Показывает 3-стадийный reveal после stop_recording.
    /// Stage 1 (raw Whisper) → Stage 2 (D.7 cleaned) → Stage 3 (LLM/итог).
    /// По истечении `duration` автоматически вызывает hide().
    public func showRevealAnimation(
        rawText: String,
        cleanedText: String,
        finalText: String,
        llmApplied: Bool,
        duration: TimeInterval = 2.5
    ) {
        revealTask?.cancel()
        stopPulse()
        
        if overlayState == .hidden {
            primaryLabel.stringValue = rawText.isEmpty ? "…" : rawText
            positionNearCursor()
            panel.alphaValue = 0
            panel.orderFront(nil)
            animateShow()
        }

        overlayState = .reveal
        let stageInterval = duration / 3.0

        revealTask = Task { @MainActor in
            // Stage 1 — Распознано
            showStage(text: rawText.isEmpty ? "…" : rawText, label: "Распознано")
            
            try? await Task.sleep(nanoseconds: UInt64(stageInterval * 1_000_000_000))
            guard !Task.isCancelled else { return }

            // Stage 2 — Очищено
            crossfadeStage(
                text: cleanedText.isEmpty ? rawText : cleanedText,
                label: "Очищено"
            )
            
            try? await Task.sleep(nanoseconds: UInt64(stageInterval * 1_000_000_000))
            guard !Task.isCancelled else { return }

            // Stage 3 — LLM или Итог
            let finalLabel = llmApplied ? "LLM rewrite" : "Итог"
            let finalContent = finalText.isEmpty ? (cleanedText.isEmpty ? rawText : cleanedText) : finalText
            crossfadeStage(text: finalContent, label: finalLabel)
            
            try? await Task.sleep(nanoseconds: UInt64((stageInterval + 0.4) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            
            hide()
        }
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
        panel.ignoresMouseEvents  = true
    }

    private func setupEffectView() {
        effectView.material      = .hudWindow
        effectView.blendingMode  = .behindWindow
        effectView.state         = .active
        effectView.isEmphasized  = true
        effectView.wantsLayer    = true

        effectView.layer?.cornerRadius  = cornerRadius
        effectView.layer?.masksToBounds = true

        panel.contentView?.wantsLayer = true
        if let rootLayer = panel.contentView?.layer {
            rootLayer.masksToBounds   = false
            rootLayer.shadowColor     = KrabEarTheme.Colors.overlayShadow.cgColor
            rootLayer.shadowOpacity   = 0.25
            rootLayer.shadowRadius    = 28
            rootLayer.shadowOffset    = CGSize(width: 0, height: -4)
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
        statusLabel.font      = KrabEarTheme.Typography.captionMedium
        statusLabel.textColor = .tertiaryLabelColor
        statusLabel.alignment = .right
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        modeLabel.font      = KrabEarTheme.Typography.captionMedium
        modeLabel.textColor = .tertiaryLabelColor
        modeLabel.alignment = .left
        modeLabel.translatesAutoresizingMaskIntoConstraints = false

        stageLabel.font      = KrabEarTheme.Typography.captionMedium
        stageLabel.textColor = .secondaryLabelColor
        stageLabel.alignment = .left
        stageLabel.isHidden  = true
        stageLabel.translatesAutoresizingMaskIntoConstraints = false

        primaryLabel.font            = KrabEarTheme.Typography.display
        primaryLabel.textColor       = .labelColor
        primaryLabel.alignment       = .left
        primaryLabel.maximumNumberOfLines = 0
        primaryLabel.lineBreakMode   = .byWordWrapping
        primaryLabel.stringValue     = "Слушаю…"
        primaryLabel.translatesAutoresizingMaskIntoConstraints = false

        effectView.addSubview(statusLabel)
        effectView.addSubview(modeLabel)
        effectView.addSubview(stageLabel)
        effectView.addSubview(primaryLabel)

        NSLayoutConstraint.activate([
            modeLabel.topAnchor.constraint(equalTo: effectView.topAnchor, constant: 10),
            modeLabel.leadingAnchor.constraint(equalTo: effectView.leadingAnchor, constant: 14),

            statusLabel.centerYAnchor.constraint(equalTo: modeLabel.centerYAnchor),
            statusLabel.trailingAnchor.constraint(equalTo: effectView.trailingAnchor, constant: -14),

            stageLabel.topAnchor.constraint(equalTo: modeLabel.bottomAnchor, constant: 8),
            stageLabel.leadingAnchor.constraint(equalTo: effectView.leadingAnchor, constant: 14),

            primaryLabel.topAnchor.constraint(equalTo: stageLabel.bottomAnchor, constant: 2),
            primaryLabel.leadingAnchor.constraint(equalTo: effectView.leadingAnchor, constant: 14),
            primaryLabel.trailingAnchor.constraint(equalTo: effectView.trailingAnchor, constant: -14),
            primaryLabel.bottomAnchor.constraint(lessThanOrEqualTo: effectView.bottomAnchor, constant: -12),
        ])
    }

    // MARK: - Animations

    private func animateShow() {
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
            ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
            ctx.allowsImplicitAnimation = true
            panel.animator().alphaValue = targetAlpha
            panel.contentView?.layer?.transform = CATransform3DIdentity
        })
    }

    private func animateHide(completion: @escaping @Sendable () -> Void) {
        if reduceMotion {
            panel.alphaValue = 0
            completion()
            return
        }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.20
            ctx.timingFunction = CAMediaTimingFunction(name: .easeIn)
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

    private func startPulse() {
        guard !reduceMotion else { return }
        stopPulse()
        pulseFadeOut = true
        pulseTimer = Timer.scheduledTimer(withTimeInterval: 0.7, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.overlayState == .live else { return }
                NSAnimationContext.runAnimationGroup { ctx in
                    ctx.duration = 0.7
                    ctx.allowsImplicitAnimation = true
                    self.primaryLabel.animator().alphaValue = self.pulseFadeOut ? 0.65 : 1.0
                }
                self.pulseFadeOut.toggle()
            }
        }
        if let t = pulseTimer { RunLoop.main.add(t, forMode: .common) }
    }

    private func stopPulse() {
        pulseTimer?.invalidate()
        pulseTimer = nil
        primaryLabel.alphaValue = 1.0
    }

    // MARK: - Reveal animation helpers

    private func showStage(text: String, label: String) {
        stageLabel.isHidden  = false
        stageLabel.stringValue = label
        primaryLabel.stringValue = text
        adjustHeight()
    }

    private func crossfadeStage(text: String, label: String) {
        if reduceMotion {
            stageLabel.stringValue   = label
            primaryLabel.stringValue = text
            adjustHeight()
            return
        }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.3
            ctx.allowsImplicitAnimation = true
            primaryLabel.animator().alphaValue = 0
            stageLabel.animator().alphaValue   = 0
        }, completionHandler: { [weak self] in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.stageLabel.stringValue   = label
                self.primaryLabel.stringValue = text
                self.adjustHeight()
                NSAnimationContext.runAnimationGroup { ctx in
                    ctx.duration = 0.3
                    ctx.allowsImplicitAnimation = true
                    self.primaryLabel.animator().alphaValue = 1.0
                    self.stageLabel.animator().alphaValue   = 1.0
                }
            }
        })
    }

    // MARK: - Positioning

    private func positionNearCursor() {
        let cursor = NSEvent.mouseLocation

        let screen = NSScreen.screens.first { $0.frame.contains(cursor) }
                  ?? NSScreen.main
                  ?? NSScreen.screens.first

        guard let screen else { return }
        let visible = screen.visibleFrame

        let width = clamp(value: 520, min: minWidth, max: maxWidth)
        let height = currentPanelHeight()

        var x = cursor.x + 50
        var y = cursor.y - height - 30

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

    private func adjustHeight() {
        let width = clamp(value: 520, min: minWidth, max: maxWidth)
        let insets: CGFloat = 14 * 2
        let topRowH: CGFloat = 26
        let stageLabelH: CGFloat = stageLabel.isHidden ? 0 : 18
        let padding: CGFloat = 10 + 8 + 2 + 12
        let textWidth = width - insets

        let textH = heightForString(primaryLabel.stringValue, font: primaryLabel.font ?? KrabEarTheme.Typography.display, width: textWidth)
        let total = topRowH + stageLabelH + padding + textH
        let height = clamp(value: total, min: minHeight, max: maxHeight)

        var frame = panel.frame
        frame.size.height = height
        panel.setFrame(frame, display: true)

        borderLayer.frame = effectView.bounds
    }

    private func currentPanelHeight() -> CGFloat {
        let width: CGFloat = 520
        let insets: CGFloat = 14 * 2
        let topRowH: CGFloat = 26
        let stageLabelH: CGFloat = stageLabel.isHidden ? 0 : 18
        let padding: CGFloat = 10 + 8 + 2 + 12
        let textWidth = width - insets
        let textH = heightForString(primaryLabel.stringValue, font: primaryLabel.font ?? KrabEarTheme.Typography.display, width: textWidth)
        let total = topRowH + stageLabelH + padding + textH
        return clamp(value: total, min: minHeight, max: maxHeight)
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