/*
 Non-modal toast для уведомления о перезапуске backend.

 Показывается в правом-нижнем углу основного экрана на 3 секунды,
 затем fade-out. НЕ блокирует активное окно.

 Связи модуля:
 1) HealthMonitor: вызывает show() из onHangDetected.
 2) main.swift: создаёт singleton при старте приложения.
*/

import AppKit

@MainActor
final class BackendToast {
    static let shared = BackendToast()

    private var panel: NSPanel?
    private var dismissTimer: Timer?

    private init() {}

    /// Показывает toast с заданным текстом на `duration` секунд.
    /// Повторный вызов до dismiss заменяет текст на новый.
    func show(_ message: String, duration: TimeInterval = 3.0) {
        dismissTimer?.invalidate()

        if panel == nil {
            createPanel()
        }
        guard let panel = panel,
              let label = panel.contentView?.subviews.first as? NSTextField
        else { return }

        label.stringValue = message
        label.sizeToFit()

        positionPanel(panel)
        panel.alphaValue = 1.0
        panel.orderFront(nil)

        dismissTimer = Timer.scheduledTimer(withTimeInterval: duration, repeats: false) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.fadeOutAndHide()
            }
        }
    }

    private func createPanel() {
        let rect = NSRect(x: 0, y: 0, width: 280, height: 56)
        let panel = NSPanel(
            contentRect: rect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true

        let visualEffect = NSVisualEffectView(frame: rect)
        visualEffect.material = .hudWindow
        visualEffect.state = .active
        visualEffect.blendingMode = .behindWindow
        visualEffect.wantsLayer = true
        visualEffect.layer?.cornerRadius = 12

        let label = NSTextField(labelWithString: "")
        label.font = .systemFont(ofSize: 13, weight: .medium)
        label.textColor = .labelColor
        label.alignment = .center
        label.frame = NSRect(x: 12, y: 12, width: rect.width - 24, height: rect.height - 24)
        label.lineBreakMode = .byTruncatingTail

        visualEffect.addSubview(label)
        panel.contentView = visualEffect
        self.panel = panel
    }

    private func positionPanel(_ panel: NSPanel) {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        let x = visible.maxX - panel.frame.width - 24
        let y = visible.minY + 24
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    private func fadeOutAndHide() {
        guard let panel = panel else { return }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.25
            panel.animator().alphaValue = 0.0
        }, completionHandler: {
            panel.orderOut(nil)
        })
    }
}
