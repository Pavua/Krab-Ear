/*
 ErrorToastView — Liquid Glass severity-aware error toast presenter.

 Архитектура:
 - ErrorToastPresenter: concrete ToastPresenting implementation (Main actor).
 - At most one NSPanel toast on screen at a time; extras are queued.
 - Auto-dismiss delay по severity: info=2s, warn=5s, error=10s, critical=manual.
 - Actionable button invokes ErrorActionHandler.handleActionTap via async Task.
 - ToastPanelFactory protocol enables panel injection for unit testing.

 Связи:
 - ErrorActionHandler.swift: protocol ToastPresenting (Task 10).
 - main+Errors.swift: setupErrorBus(toastPresenter:) конструирует presenter БЕЗ
   actionHandler (циклическая зависимость — ErrorActionHandler требует
   ToastPresenting при своём init), затем создаёт ErrorActionHandler и
   присваивает его presenter.actionHandler постфактум. См. init(actionHandler:)
   default nil + settable actionHandler property.
 - ErrorBusPoller.swift: IPC-поллинг доставляет KrabErrorPayload в
   ErrorActionHandler.handleErrorEvent → presenter.present(error:).
*/

import AppKit
import Foundation
import os

// MARK: - ToastPanelFactory

/// Protocol для создания NSPanel в тестах (без реального экрана).
@MainActor
protocol ToastPanelFactory: AnyObject {
    func makePanel(frame: NSRect) -> NSPanel
}

// MARK: - DefaultToastPanelFactory

/// Production factory — создаёт настоящие borderless floating NSPanel.
@MainActor
final class DefaultToastPanelFactory: ToastPanelFactory {
    func makePanel(frame: NSRect) -> NSPanel {
        let panel = NSPanel(
            contentRect: frame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        return panel
    }
}

// MARK: - ErrorToastPresenter

/// Concrete ToastPresenting implementation.
/// Показывает Liquid Glass NSPanel toast в правом верхнем углу экрана.
/// Очередь гарантирует одновременно не более одного toast на экране.
@MainActor
class ErrorToastPresenter: NSObject, ToastPresenting {

    // MARK: - Constants

    private enum ToastMetrics {
        static let width: CGFloat = 360
        static let minHeight: CGFloat = 80
        static let margin: CGFloat = 16
        static let padding: CGFloat = 12
        static let iconSize: CGFloat = 12
        static let cornerRadius: CGFloat = 12
        static let borderWidth: CGFloat = 0.5
    }

    private enum AutoDismissDelay {
        static let info: TimeInterval = 2.0
        static let warn: TimeInterval = 5.0
        static let error: TimeInterval = 10.0
        // critical = nil (no timer)
    }

    // MARK: - State

    private let logger = Logger(subsystem: "com.antigravity.krab-ear", category: "ErrorToastPresenter")
    /// Weak + settable-after-init to break the construction cycle with
    /// ErrorActionHandler (which itself requires a ToastPresenting at init) —
    /// see setupErrorBus in main+Errors.swift, which constructs this presenter
    /// first, then the handler, then wires this property. Optional chaining at
    /// the one call site (handleActionTap dispatch) already tolerates nil, so a
    /// presenter with no handler yet attached still displays toasts correctly;
    /// only the actionable-button tap-through would no-op.
    weak var actionHandler: ErrorActionHandler?
    private let panelFactory: any ToastPanelFactory

    /// Currently visible toast panel (nil when no toast is shown).
    private(set) var activePanel: NSPanel?

    /// Pending errors waiting to be displayed.
    private(set) var queue: [KrabErrorPayload] = []

    /// Auto-dismiss timer for the current toast.
    private var dismissTimer: Timer?

    // MARK: - Init

    init(
        actionHandler: ErrorActionHandler? = nil,
        panelFactory: (any ToastPanelFactory)? = nil
    ) {
        self.actionHandler = actionHandler
        self.panelFactory = panelFactory ?? DefaultToastPanelFactory()
        super.init()
    }

    // MARK: - ToastPresenting

    /// Enqueue an error for display. Thread-safe call — always routes to Main thread.
    func present(error: KrabErrorPayload) {
        // Already on MainActor, but called from possibly non-async code.
        queue.append(error)
        drainQueue()
    }

    // MARK: - Queue drain

    private func drainQueue() {
        guard activePanel == nil, !queue.isEmpty else { return }
        let payload = queue.removeFirst()
        showToast(for: payload)
    }

    // MARK: - Show toast

    private func showToast(for payload: KrabErrorPayload) {
        let toastView = buildToastView(for: payload)

        // Size the panel to fit the content
        toastView.layoutSubtreeIfNeeded()
        let fittingSize = toastView.fittingSize
        let height = max(ToastMetrics.minHeight, fittingSize.height + ToastMetrics.padding * 2)
        let frame = panelFrame(width: ToastMetrics.width, height: height)

        let panel = panelFactory.makePanel(frame: frame)
        panel.contentView = toastView
        activePanel = panel

        panel.orderFront(nil)
        logger.info("toast shown: severity=\(payload.severity, privacy: .public) code=\(payload.code, privacy: .public)")

        // Schedule auto-dismiss if applicable
        let delay = autoDismissDelay(for: payload.severity)
        if let delay {
            let timer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.dismissCurrentToast()
                }
            }
            dismissTimer = timer
        }
    }

    // MARK: - Dismiss

    /// Dismiss the active toast immediately and drain the queue.
    func dismissCurrentToast() {
        dismissTimer?.invalidate()
        dismissTimer = nil

        activePanel?.orderOut(nil)
        activePanel = nil

        logger.debug("toast dismissed")

        // Show next if queued
        drainQueue()
    }

    // MARK: - Helpers

    private func autoDismissDelay(for severity: String) -> TimeInterval? {
        switch severity {
        case "info":     return AutoDismissDelay.info
        case "warn":     return AutoDismissDelay.warn
        case "error":    return AutoDismissDelay.error
        case "critical": return nil  // manual dismiss only
        default:         return AutoDismissDelay.error
        }
    }

    private func panelFrame(width: CGFloat, height: CGFloat) -> NSRect {
        guard let screen = NSScreen.main else {
            return NSRect(x: 100, y: 100, width: width, height: height)
        }
        let visibleFrame = screen.visibleFrame
        let x = visibleFrame.maxX - width - ToastMetrics.margin
        let y = visibleFrame.maxY - height - ToastMetrics.margin
        return NSRect(x: x, y: y, width: width, height: height)
    }

    // MARK: - Toast view construction

    private func buildToastView(for payload: KrabErrorPayload) -> NSView {
        // Backdrop: Liquid Glass NSVisualEffectView
        let backdrop = NSVisualEffectView()
        backdrop.material = .hudWindow
        backdrop.blendingMode = .behindWindow
        backdrop.state = .active
        backdrop.wantsLayer = true
        backdrop.layer?.cornerRadius = ToastMetrics.cornerRadius
        backdrop.layer?.cornerCurve = .continuous
        backdrop.layer?.masksToBounds = true
        backdrop.layer?.borderWidth = ToastMetrics.borderWidth
        backdrop.layer?.borderColor = KrabEarTheme.Colors.border.cgColor

        // Content stack (vertical)
        let contentStack = NSStackView()
        contentStack.orientation = .vertical
        contentStack.spacing = KrabEarTheme.Metrics.standard
        contentStack.alignment = .leading
        contentStack.edgeInsets = NSEdgeInsets(
            top: ToastMetrics.padding,
            left: ToastMetrics.padding,
            bottom: ToastMetrics.padding,
            right: ToastMetrics.padding
        )
        contentStack.translatesAutoresizingMaskIntoConstraints = false

        // Header row: severity icon + message
        let headerRow = buildHeaderRow(for: payload)
        contentStack.addArrangedSubview(headerRow)

        // Action button (if applicable)
        if payload.actionable, let actionId = payload.action_id {
            let actionButton = buildActionButton(payload: payload, actionId: actionId)
            contentStack.addArrangedSubview(actionButton)
        }

        backdrop.addSubview(contentStack)
        NSLayoutConstraint.activate([
            contentStack.topAnchor.constraint(equalTo: backdrop.topAnchor),
            contentStack.leadingAnchor.constraint(equalTo: backdrop.leadingAnchor),
            contentStack.trailingAnchor.constraint(equalTo: backdrop.trailingAnchor),
            contentStack.bottomAnchor.constraint(equalTo: backdrop.bottomAnchor),
        ])

        return backdrop
    }

    private func buildHeaderRow(for payload: KrabErrorPayload) -> NSView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.spacing = KrabEarTheme.Metrics.standard
        row.alignment = .centerY

        // Severity icon: colored circle
        let iconView = NSView()
        iconView.wantsLayer = true
        iconView.layer?.cornerRadius = ToastMetrics.iconSize / 2
        iconView.layer?.backgroundColor = severityColor(for: payload.severity).cgColor
        iconView.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            iconView.widthAnchor.constraint(equalToConstant: ToastMetrics.iconSize),
            iconView.heightAnchor.constraint(equalToConstant: ToastMetrics.iconSize),
        ])

        // Message label
        let messageLabel = NSTextField(wrappingLabelWithString: payload.message_user)
        messageLabel.font = KrabEarTheme.Typography.body
        messageLabel.textColor = KrabEarTheme.Colors.textPrimary
        messageLabel.isEditable = false
        messageLabel.isBordered = false
        messageLabel.drawsBackground = false
        messageLabel.maximumNumberOfLines = 4
        messageLabel.lineBreakMode = .byWordWrapping
        messageLabel.translatesAutoresizingMaskIntoConstraints = false
        messageLabel.setContentHuggingPriority(.defaultLow, for: .horizontal)
        messageLabel.setContentCompressionResistancePriority(.required, for: .vertical)

        // Dismiss button (×)
        let dismissButton = NSButton(title: "×", target: self, action: #selector(onDismissTapped))
        dismissButton.isBordered = false
        dismissButton.bezelStyle = .push
        dismissButton.font = .systemFont(ofSize: 16, weight: .light)
        dismissButton.contentTintColor = KrabEarTheme.Colors.textSecondary
        dismissButton.setContentHuggingPriority(.required, for: .horizontal)

        row.addArrangedSubview(iconView)
        row.addArrangedSubview(messageLabel)
        row.addArrangedSubview(dismissButton)

        return row
    }

    private func buildActionButton(payload: KrabErrorPayload, actionId: String) -> NSButton {
        // Label from context["action_label"] if present, else default
        let label: String
        if let codable = payload.context["action_label"],
           let str = codable.value as? String {
            label = str
        } else {
            label = "Действие"
        }

        let button = NSButton(title: label, target: self, action: #selector(onActionTapped(_:)))
        button.applyThemeSecondary()
        // Tag not available for string; use representedObject via associated object trick.
        // Instead we store actionId via objc associated object on the button.
        objc_setAssociatedObject(button, &actionIdKey, actionId, .OBJC_ASSOCIATION_COPY_NONATOMIC)
        return button
    }

    // MARK: - Button actions

    @objc private func onDismissTapped() {
        dismissCurrentToast()
    }

    @objc private func onActionTapped(_ sender: NSButton) {
        guard let actionId = objc_getAssociatedObject(sender, &actionIdKey) as? String else { return }
        dismissCurrentToast()
        Task { @MainActor [weak self] in
            await self?.invokeActionAsync(actionId: actionId)
        }
    }

    /// Invokes the action. Override in tests to capture calls without real IPC.
    func invokeActionAsync(actionId: String) async {
        await actionHandler?.handleActionTap(actionId: actionId)
    }

    // MARK: - Severity color

    private func severityColor(for severity: String) -> NSColor {
        switch severity {
        case "info":     return .secondaryLabelColor
        case "warn":     return .systemYellow
        case "error":    return .systemOrange
        case "critical": return .systemRed
        default:         return .secondaryLabelColor
        }
    }
}

// MARK: - Associated object key for action ID

private nonisolated(unsafe) var actionIdKey: UInt8 = 0
