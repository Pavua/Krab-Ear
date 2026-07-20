/*
 Non-modal toast для уведомления о перезапуске backend.

 Показывается в правом-нижнем углу основного экрана на 3 секунды,
 затем fade-out. НЕ блокирует активное окно.

 AGENT-K fix: NSVisualEffectView при ПЕРВОМ render на macOS 26 запускает
 heavy ColorSync transform синхронно на main thread → блокирует ≥2s → AppHang.
 Решение: prewarmPanel() создаёт NSWindow + NSVisualEffectView заранее
 в applicationDidFinishLaunching (когда startup latency приемлема), чтобы
 ColorSync transform выполнился один раз до первого show().

 AGENT-M fix (Wave 266): show() вызывал label.sizeToFit() + panel.orderFront()
 синхронно на main thread. sizeToFit() на первом Cyrillic/emoji message
 запускает CoreText glyph-metrics build → блокирует >16ms → _doOrderWindow
 AppHang. Решение: prewarmPanel() теперь также прогревает шрифтовой кэш
 через sizeToFit() с representative Cyrillic string. show() выставляет позицию
 ДО orderFront (пока панель скрыта), избегая layout при видимом окне.
 Guard на nil window предотвращает crash при stale bundle (AGENT-K сестра).

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
    private let panelOrdering: PanelOrdering

    /// Singleton всегда использует прямые AppKit-команды.
    private init() {
        panelOrdering = AppKitPanelOrdering()
    }

    /// Отдельный экземпляр нужен unit-тестам для подмены только экранного эффекта.
    init(panelOrdering: PanelOrdering) {
        self.panelOrdering = panelOrdering
    }

    /// Pre-warm NSWindow + NSVisualEffectView + CoreText font cache при app init.
    ///
    /// 1. ColorSync transform на macOS 26 выполняется синхронно при первом
    ///    attach к window (viewDidMoveToWindow). Вызов при startup позволяет
    ///    "оплатить" эту цену один раз в нечувствительный момент.
    /// 2. AGENT-M: label.sizeToFit() с Cyrillic строкой прогревает CoreText
    ///    glyph-metrics build при startup, чтобы show() не блокировал main thread.
    /// 3. positionPanel() вызывается заранее, чтобы _doOrderWindow не делал
    ///    layout при первом orderFront.
    ///
    /// Безопасно вызывать несколько раз — повторный вызов игнорируется.
    func prewarmPanel() {
        guard panel == nil else { return }
        createPanel()
        guard let panel = panel,
              let label = panel.contentView?.subviews.first as? NSTextField
        else { return }

        // Прогрев CoreText glyph cache: representative Cyrillic + Latin + emoji string.
        // Это тот же шрифт (systemFont 13pt .medium), который используется в show().
        // Глиф ✓ — НАМЕРЕННАЯ prewarm-строка (прогревает CoreText glyph cache до первого
        // show()), а не runtime-рендер в ColorSync-callback.
        label.stringValue = "Backend перезапущен ✓"  // SF-SYMBOL-SAFE
        label.sizeToFit()
        label.stringValue = ""

        // Позиционируем ДО первого orderFront, чтобы при show() окно уже знало свои координаты.
        positionPanel(panel)

        // orderFrontRegardless + немедленный orderOut заставляет AppKit
        // выполнить NSVisualEffectView layout и ColorSync transform,
        // после чего скрываем панель.
        panelOrdering.orderFrontRegardless(panel)
        panelOrdering.orderOut(panel)
    }

    /// Показывает toast с заданным текстом на `duration` секунд.
    /// Повторный вызов до dismiss заменяет текст на новый.
    ///
    /// AGENT-M: позиционирование выполняется ДО orderFront пока окно скрыто,
    /// избегая синхронного layout pass в _doOrderWindow на видимом окне.
    /// Guard на nil window предотвращает crash при stale bundle (сестра AGENT-K).
    func show(_ message: String, duration: TimeInterval = 3.0) {
        dismissTimer?.invalidate()

        if panel == nil {
            NSLog("[BackendToast] WARNING: show() called before prewarmPanel()! This will cause an AppHang due to synchronous ColorSync and CoreText setup.")
            createPanel()
        }
        guard let panel = panel, panel.contentView?.window != nil else { return }

        guard let label = panel.contentView?.subviews.first as? NSTextField
        else { return }

        // Если панель уже видима (repeat show) — только обновляем текст.
        if panelOrdering.isVisible(panel) {
            updateLabelAndScheduleDismiss(panel: panel, message: message, duration: duration)
            return
        }

        label.stringValue = message
        // sizeToFit после прогрева в prewarmPanel() — быстро (glyph cache hit).
        label.sizeToFit()

        // Позиционируем ПОКА панель скрыта — layout без CALayer commit на экране.
        // AGENT-M: это ключевой момент — positionPanel до orderFront предотвращает
        // синхронный layout в _doOrderWindow, который был источником AppHang.
        positionPanel(panel)
        panel.alphaValue = 1.0
        // orderFront после позиционирования: _doOrderWindow только композитирует
        // уже готовый CALayer, не делает layout заново.
        panelOrdering.orderFront(panel)

        scheduleDismiss(duration: duration)
    }

    // MARK: - Private helpers

    private func updateLabelAndScheduleDismiss(panel: NSPanel, message: String, duration: TimeInterval) {
        guard let label = panel.contentView?.subviews.first as? NSTextField else { return }
        label.stringValue = message
        label.sizeToFit()
        panel.alphaValue = 1.0
        scheduleDismiss(duration: duration)
    }

    private func scheduleDismiss(duration: TimeInterval) {
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
        guard let panel = panel, panel.contentView?.window != nil else { return }
        let panelOrdering = panelOrdering
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.25
            panel.animator().alphaValue = 0.0
        }, completionHandler: {
            MainActor.assumeIsolated {
                panelOrdering.orderOut(panel)
            }
        })
    }
}
