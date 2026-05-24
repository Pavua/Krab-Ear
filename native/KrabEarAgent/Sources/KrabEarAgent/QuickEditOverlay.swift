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
        let textHeight: CGFloat = min(200, max(56, estimatedTextHeight(initialText, width: panelWidth - 32)))
        let buttonBarH: CGFloat = 40
        let countdownH: CGFloat = 20
        let vPad: CGFloat = 12
        let totalH: CGFloat = vPad + textHeight + 8 + countdownH + 8 + buttonBarH + vPad

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

        // Visual effect background (Liquid Glass)
        let blur = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: panelWidth, height: totalH))
        blur.material = .popover
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.wantsLayer = true
        blur.layer?.cornerRadius = 12
        blur.layer?.masksToBounds = true

        // Thin border
        let borderLayer = CALayer()
        borderLayer.frame = blur.bounds
        borderLayer.cornerRadius = 12
        borderLayer.borderColor = NSColor(white: 1.0, alpha: 0.18).cgColor
        borderLayer.borderWidth = 1
        borderLayer.zPosition = 1
        blur.layer?.addSublayer(borderLayer)

        // NSTextView scroll wrapper
        let scrollView = NSScrollView(frame: NSRect(
            x: 16, y: vPad + buttonBarH + 8 + countdownH + 8,
            width: panelWidth - 32, height: textHeight
        ))
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.autohidesScrollers = true
        scrollView.drawsBackground = false
        scrollView.wantsLayer = true

        let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: panelWidth - 32, height: textHeight))
        tv.isEditable = true
        tv.isSelectable = true
        tv.isRichText = false
        tv.backgroundColor = .clear
        tv.drawsBackground = false
        tv.string = initialText
        tv.font = NSFont.systemFont(ofSize: 14)
        tv.textColor = NSColor.labelColor
        tv.isAutomaticSpellingCorrectionEnabled = false
        tv.isAutomaticTextReplacementEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.textContainerInset = NSSize(width: 4, height: 4)
        scrollView.documentView = tv
        self.textView = tv

        // Countdown label
        let countdown = NSTextField(
            labelWithString: formatCountdown(remainingSeconds)
        )
        countdown.frame = NSRect(
            x: 16, y: vPad + buttonBarH + 8,
            width: 120, height: countdownH
        )
        countdown.font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .regular)
        countdown.textColor = NSColor.secondaryLabelColor
        countdown.alignment = .left
        self.countdownLabel = countdown

        // Кнопка «Вставить» (Enter)
        let pasteBtn = ThemePrimaryButton(title: "Вставить", target: self, action: #selector(onPaste))
        pasteBtn.frame = NSRect(x: panelWidth - 16 - 100 - 8 - 90, y: vPad, width: 90, height: 28)
        pasteBtn.keyEquivalent = "\r"
        pasteBtn.font = NSFont.systemFont(ofSize: 13)

        // Кнопка «Отменить» (Esc)
        let cancelBtn = ThemeSecondaryButton(title: "Отменить", target: self, action: #selector(onCancel))
        cancelBtn.frame = NSRect(x: panelWidth - 16 - 100, y: vPad, width: 100, height: 28)
        cancelBtn.keyEquivalent = "\u{1B}"
        cancelBtn.font = NSFont.systemFont(ofSize: 13)

        // Hint label
        let hint = NSTextField(labelWithString: "Enter — вставить  |  Esc — отменить")
        hint.frame = NSRect(x: 16, y: vPad + 5, width: 280, height: 18)
        hint.font = NSFont.systemFont(ofSize: 11)
        hint.textColor = NSColor.tertiaryLabelColor

        blur.addSubview(scrollView)
        blur.addSubview(countdown)
        blur.addSubview(pasteBtn)
        blur.addSubview(cancelBtn)
        blur.addSubview(hint)
        panel.contentView = blur

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
                ctx.duration = 0.15
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
        let font = NSFont.systemFont(ofSize: 14)
        let attributes: [NSAttributedString.Key: Any] = [.font: font]
        let bounds = (text as NSString).boundingRect(
            with: CGSize(width: width - 8, height: 10000),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: attributes
        )
        return max(56, min(200, bounds.height + 24))
    }
}
