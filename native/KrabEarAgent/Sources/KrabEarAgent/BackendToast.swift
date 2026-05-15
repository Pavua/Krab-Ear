/*
 Non-modal toast для уведомления о перезапуске backend.

 Показывается в правом-нижнем углу основного экрана на 3 секунды,
 затем fade-out. НЕ блокирует активное окно.

 AGENT-K fix: NSVisualEffectView при ПЕРВОМ render на macOS 26 запускает
 heavy ColorSync transform синхронно на main thread → блокирует ≥2s → AppHang.
 Решение: prewarmPanel() создаёт NSWindow + NSVisualEffectView заранее
 в applicationDidFinishLaunching (когда startup latency приемлема), чтобы
 ColorSync transform выполнился один раз до первого show().

 Связи модуля:
 1) HealthMonitor: вызывает show() из onHangDetected.
 2) main.swift: вызывает prewarmPanel() в applicationDidFinishLaunching,
    show() при showFatalAndTerminate и backend restart events.
*/

import AppKit

@MainActor
final class BackendToast {
    static let shared = BackendToast()

    private var panel: NSPanel?
    private var dismissTimer: Timer?

    private init() {}

    /// Pre-warm NSWindow + NSVisualEffectView при app init.
    ///
    /// ColorSync transform на macOS 26 выполняется синхронно при первом
    /// attach к window (viewDidMoveToWindow). Вызов при startup позволяет
    /// "оплатить" эту цену один раз в нечувствительный момент, после чего
    /// последующие show() не блокируют main thread.
    ///
    /// Безопасно вызывать несколько раз — повторный вызов игнорируется.
    func prewarmPanel() {
        guard panel == nil else { return }
        createPanel()
        // orderFrontRegardless + немедленный orderOut заставляет AppKit
        // выполнить NSVisualEffectView layout и ColorSync transform,
        // после чего скрываем панель.
        panel?.orderFrontRegardless()
        panel?.orderOut(nil)
    }

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
            MainActor.assumeIsolated {
                panel.orderOut(nil)
            }
        })
    }
}
