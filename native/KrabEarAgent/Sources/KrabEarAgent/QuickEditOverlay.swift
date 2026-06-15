/*
 QuickEditOverlay.swift
 Мини-оверлей для быстрого редактирования текста перед автовставкой.

 Интеграция:
 1) Вызывается из handleTranscriptionResult в main+PasteHandling.swift.
 2) AgentSettings: флаг quickEditEnabled и quickEditTimeoutSec.

 Поведение:
 - Enter / кнопка «Вставить» → paste(editedText)
 - Esc  / кнопка «Отменить» → cancel (вставки нет)
 - Таймаут без действия     → timeout(originalText) → paste original
 - NSPanel.nonactivatingPanel: НЕ уводит фокус из активного приложения пользователя
*/

import AppKit
import Foundation

// MARK: - QuickEditResult

enum QuickEditResult {
    /// Пользователь нажал «Вставить» (или Enter) — paste отредактированного текста.
    case paste(String)
    /// Пользователь нажал «Отменить» (или Esc) — вставки не будет.
    case cancel
    /// Таймаут истёк без действия пользователя — paste исходного текста.
    case timeout(String)
}

// MARK: - QuickEditOverlay

@MainActor
final class QuickEditOverlay: NSObject {

    // MARK: Private state

    private var panel: NSPanel?
    private var textView: NSTextView?
    private var countdownLabel: NSTextField?
    private var timer: Timer?
    private var remainingSeconds: Double = 0
    private var originalText: String = ""
    private var completion: ((QuickEditResult) -> Void)?

    // MARK: Public API

    /// Показывает оверлей рядом с курсором мыши.
    ///
    /// - Parameters:
    ///   - text: Исходный транскрибированный текст (предзаполняет textarea).
    ///   - timeoutSec: Таймаут автовставки (сек). По умолчанию 5.
    ///   - completion: Вызывается ровно один раз с результатом.
    func show(text: String, timeoutSec: Double = 5.0, completion: @escaping (QuickEditResult) -> Void) {
        // Если оверлей уже открыт — сначала закрываем (edge case: две быстрые записи).
        dismiss(animated: false)

        self.originalText = text
        self.completion = completion
        self.remainingSeconds = timeoutSec

        let panel = buildPanel(initialText: text)
        self.panel = panel

        positionPanel(panel)
        panel.makeKeyAndOrderFront(nil)

        // Фокус на textarea, чтобы пользователь мог сразу набирать текст.
        if let tv = textView {
            panel.makeFirstResponder(tv)
            tv.selectAll(nil)
        }

        startTimer(interval: timeoutSec)
    }

    // MARK: - Panel construction

    private func buildPanel(initialText: String) -> NSPanel {
        let panelWidth: CGFloat = 600
        let hPad = KrabEarTheme.Metrics.comfortable
        let vPad = KrabEarTheme.Metrics.comfortable
        let spacing = KrabEarTheme.Metrics.standard
        let buttonBarH = KrabEarTheme.Metrics.controlHeight

        let scrollViewWidth = panelWidth - hPad * 2
        let textHeight = min(200, max(56, estimatedTextHeight(initialText, width: scrollViewWidth)))
        
        let totalH = vPad + textHeight + spacing + buttonBarH + vPad

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: panelWidth, height: totalH),
            styleMask: [.utilityWindow, .nonactivatingPanel, .fullSizeContentView, .borderless],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.isOpaque = false

        // Container view for shadow
        let container = NSView(frame: NSRect(x: 0, y: 0, width: panelWidth, height: totalH))
        container.wantsLayer = true
        KrabEarTheme.Elevation.applyOverlay(to: container.layer!)

        // Visual effect background (Liquid Glass)
        let blur = NSVisualEffectView(frame: container.bounds)
        blur.material = .popover
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.wantsLayer = true
        blur.layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        blur.layer?.masksToBounds = true
        container.addSubview(blur)

        // Tint layer for card background
        let tintLayer = CALayer()
        tintLayer.frame = blur.bounds
        tintLayer.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
        blur.layer?.addSublayer(tintLayer)

        // Thin dynamic border
        let borderLayer = CALayer()
        borderLayer.frame = blur.bounds
        borderLayer.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        borderLayer.borderColor = KrabEarTheme.Colors.border.cgColor
        borderLayer.borderWidth = 1
        borderLayer.zPosition = 1
        blur.layer?.addSublayer(borderLayer)

        // Text view wrapper
        let textY = vPad + buttonBarH + spacing
        let scrollView = NSScrollView(frame: NSRect(x: hPad, y: textY, width: scrollViewWidth, height: textHeight))
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.autohidesScrollers = true
        scrollView.wantsLayer = true
        scrollView.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        scrollView.layer?.masksToBounds = true
        scrollView.backgroundColor = KrabEarTheme.Colors.cardBackground
        scrollView.drawsBackground = true

        // Inner border for text field so it looks neat
        let scrollBorder = CALayer()
        scrollBorder.frame = scrollView.bounds
        scrollBorder.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        scrollBorder.borderColor = KrabEarTheme.Colors.border.cgColor
        scrollBorder.borderWidth = 1
        scrollBorder.zPosition = 1
        scrollView.layer?.addSublayer(scrollBorder)

        let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: scrollViewWidth, height: textHeight))
        tv.isEditable = true
        tv.isSelectable = true
        tv.isRichText = false
        tv.backgroundColor = .clear // Let the scroll view background show
        tv.drawsBackground = false
        tv.string = initialText
        tv.font = KrabEarTheme.Typography.body
        tv.textColor = KrabEarTheme.Colors.textPrimary
        tv.isAutomaticSpellingCorrectionEnabled = false
        tv.isAutomaticTextReplacementEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.textContainerInset = NSSize(width: KrabEarTheme.Metrics.comfortable, height: KrabEarTheme.Metrics.comfortable)
        scrollView.documentView = tv
        self.textView = tv

        // Button bar layout
        let pasteBtnW: CGFloat = 90
        let cancelBtnW: CGFloat = 90
        let btnSpacing = KrabEarTheme.Metrics.tight
        
        let pasteBtn = ThemePrimaryButton(frame: NSRect(
            x: panelWidth - hPad - pasteBtnW,
            y: vPad,
            width: pasteBtnW,
            height: buttonBarH
        ))
        pasteBtn.title = "Вставить"
        pasteBtn.target = self
        pasteBtn.action = #selector(onPaste)
        pasteBtn.keyEquivalent = "\r"

        let cancelBtn = ThemeSecondaryButton(frame: NSRect(
            x: panelWidth - hPad - pasteBtnW - btnSpacing - cancelBtnW,
            y: vPad,
            width: cancelBtnW,
            height: buttonBarH
        ))
        cancelBtn.title = "Отменить"
        cancelBtn.target = self
        cancelBtn.action = #selector(onCancel)
        cancelBtn.keyEquivalent = "\u{1B}"

        // Countdown label
        let countdown = NSTextField(labelWithString: formatCountdown(remainingSeconds))
        countdown.frame = NSRect(
            x: hPad,
            y: vPad + (buttonBarH - 16) / 2, // Center vertically with buttons
            width: 40,
            height: 16
        )
        countdown.font = KrabEarTheme.Typography.captionMedium.tabular()
        countdown.textColor = KrabEarTheme.Colors.accent
        countdown.isEditable = false
        countdown.isBordered = false
        countdown.drawsBackground = false
        self.countdownLabel = countdown

        // Hint label
        let hint = NSTextField(labelWithString: "Enter — вставить  |  Esc — отменить")
        hint.frame = NSRect(
            x: hPad + 40 + btnSpacing,
            y: vPad + (buttonBarH - 16) / 2,
            width: 280,
            height: 16
        )
        hint.font = KrabEarTheme.Typography.caption
        hint.textColor = KrabEarTheme.Colors.textDisabled
        hint.isEditable = false
        hint.isBordered = false
        hint.drawsBackground = false

        container.addSubview(scrollView)
        container.addSubview(countdown)
        container.addSubview(hint)
        container.addSubview(cancelBtn)
        container.addSubview(pasteBtn)
        panel.contentView = container

        return panel
    }

    // MARK: - Positioning

    private func positionPanel(_ panel: NSPanel) {
        guard let screen = NSScreen.main else { return }
        let mouseLocation = NSEvent.mouseLocation
        let size = panel.frame.size
        let screenFrame = screen.visibleFrame

        var x = mouseLocation.x - size.width / 2
        var y = mouseLocation.y - size.height - 16

        // Clamping: не выходить за границы экрана
        x = max(screenFrame.minX + 8, min(x, screenFrame.maxX - size.width - 8))

        // Если снизу не хватает места — показываем выше курсора
        if y < screenFrame.minY + 8 {
            y = mouseLocation.y + 24
        }
        y = min(y, screenFrame.maxY - size.height - 8)

        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    // MARK: - Timer

    private func startTimer(interval: Double) {
        remainingSeconds = interval
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.tickTimer()
            }
        }
        RunLoop.main.add(timer!, forMode: .common)
    }

    private func tickTimer() {
        remainingSeconds -= 0.1
        countdownLabel?.stringValue = formatCountdown(max(0, remainingSeconds))

        if remainingSeconds <= 0 {
            timer?.invalidate()
            timer = nil
            let original = originalText
            dismiss(animated: true) {
                self.fireCompletion(.timeout(original))
            }
        }
    }

    // MARK: - Button handlers

    @objc private func onPaste() {
        timer?.invalidate()
        timer = nil
        let edited = textView?.string ?? originalText
        dismiss(animated: true) {
            self.fireCompletion(.paste(edited))
        }
    }

    @objc private func onCancel() {
        timer?.invalidate()
        timer = nil
        dismiss(animated: true) {
            self.fireCompletion(.cancel)
        }
    }

    // MARK: - Dismiss

    private func dismiss(animated: Bool, completion: (@MainActor @Sendable () -> Void)? = nil) {
        guard let panel else {
            completion?()
            return
        }
        if animated {
            NSAnimationContext.runAnimationGroup({ ctx in
                let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
                ctx.duration = reduceMotion ? 0.0 : KrabEarTheme.Motion.Duration.micro
                ctx.timingFunction = KrabEarTheme.Motion.Easing.easeOut
                panel.animator().alphaValue = 0
            }, completionHandler: {
                MainActor.assumeIsolated {
                    panel.orderOut(nil)
                    self.panel = nil
                    self.textView = nil
                    self.countdownLabel = nil
                    completion?()
                }
            })
        } else {
            panel.orderOut(nil)
            self.panel = nil
            self.textView = nil
            self.countdownLabel = nil
            completion?()
        }
    }

    // MARK: - Helpers

    private func fireCompletion(_ result: QuickEditResult) {
        guard let cb = completion else { return }
        completion = nil
        cb(result)
    }

    private func formatCountdown(_ seconds: Double) -> String {
        let s = Int(ceil(seconds))
        return "\(s)с"
    }

    private func estimatedTextHeight(_ text: String, width: CGFloat) -> CGFloat {
        let font = KrabEarTheme.Typography.body
        let attributes: [NSAttributedString.Key: Any] = [.font: font]
        let bounds = (text as NSString).boundingRect(
            with: CGSize(width: width - KrabEarTheme.Metrics.comfortable * 2, height: 10000),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: attributes
        )
        return max(56, min(200, bounds.height + KrabEarTheme.Metrics.comfortable * 2))
    }
}
