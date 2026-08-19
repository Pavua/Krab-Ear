/*
 DiagnosticsTabView — вкладка «Диагностика» в панели Krab Ear.

 Layout: вертикальный NSStackView из 3 секций:
   1. Фильтры: severity chips (multi-select pushOnPushOff) + компонент dropdown
   2. ThemeCardView с NSScrollView+NSTableView (растягивается, 5 колонок)
   3. Footer: «Очистить» (destructive) слева + «Скопировать», «Открыть лог» справа

 Цвета и размеры по Gemini design spec (Phase B.2 F6):
   - minWidth=650, minHeight=450
   - headerHeight=44, footerHeight=44, rowHeight=32, chipSize=24
   - outerPadding=20, chipGap=8, sectionGap=16
   - Fonts: header weight=medium size=13, row size=13, monospace для timestamp

 Поддерживаемые компоненты: paste, rewriter, stt, diarization, translation,
   mlx, history, vocabulary, hotkey.
*/

import AppKit
import os

// MARK: - DiagnosticsTabViewController

@MainActor
final class DiagnosticsTabViewController: NSViewController {

    // MARK: - Dimensions (Gemini spec)

    private enum Layout {
        static let minWidth: CGFloat     = 650
        static let minHeight: CGFloat    = 450
        static let headerHeight: CGFloat = 44
        static let footerHeight: CGFloat = 44
        static let rowHeight: CGFloat    = 32
        static let chipSize: CGFloat     = 24
        static let outerPadding: CGFloat = 20
        static let chipGap: CGFloat      = 8
        static let sectionGap: CGFloat   = 16
    }

    // MARK: - Severity chip colors (Gemini spec)

    private enum SeverityColor {
        static func color(for severity: String) -> NSColor {
            switch severity {
            case "info":     return .secondaryLabelColor
            case "warn":     return .systemYellow
            case "error":    return .systemOrange
            case "critical": return .systemRed
            default:         return .secondaryLabelColor
            }
        }
    }

    // MARK: - Component mapping (Russian labels)

    private let componentMap: [(id: String, label: String)] = [
        ("paste",       "Вставка"),
        ("rewriter",    "Rewriter"),
        ("stt",         "STT"),
        ("diarization", "Diarization"),
        ("translation", "Перевод"),
        ("mlx",         "MLX"),
        ("history",     "История"),
        ("vocabulary",  "Словарь"),
        ("hotkey",      "Hotkey"),
    ]

    // MARK: - State

    private let ipcClient: IPCClient
    private let logger = Logger(subsystem: "com.antigravity.krab-ear", category: "DiagnosticsTab")

    var allErrors: [KrabErrorPayload] = []
    var filteredErrors: [KrabErrorPayload] = []
    var activeSeverities: Set<String> = ["info", "warn", "error", "critical"]
    var activeComponent: String? = nil  // nil = "Все"

    // Memory stats refresh timer
    private var memoryRefreshTimer: Timer?

    // MARK: - UI Subviews

    private let tableView = NSTableView()
    let emptyStateLabel = NSTextField(labelWithString: "Нет ошибок — всё работает")

    private var severityChips: [String: NSButton] = [:]
    private var componentPopup: NSPopUpButton!

    // Memory stats bar: "Backend: 730 MB | Agent: 150 MB | Workers: 1570 MB"
    private let memoryStatsLabel = NSTextField(labelWithString: "Память: загрузка…")

    // MARK: - Init

    init(ipcClient: IPCClient) {
        self.ipcClient = ipcClient
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) not implemented")
    }

    // MARK: - loadView

    override func loadView() {
        let root = NSView()
        root.translatesAutoresizingMaskIntoConstraints = false
        self.view = root

        // Root outer stack
        let outerStack = NSStackView()
        outerStack.orientation = .vertical
        outerStack.spacing = Layout.sectionGap
        outerStack.translatesAutoresizingMaskIntoConstraints = false
        outerStack.edgeInsets = NSEdgeInsets(
            top: Layout.outerPadding,
            left: Layout.outerPadding,
            bottom: Layout.outerPadding,
            right: Layout.outerPadding
        )
        root.addSubview(outerStack)

        // --- Section 1: Filters ---
        let filtersBar = buildFiltersBar()
        outerStack.addArrangedSubview(filtersBar)

        // --- Section 2: Table in ThemeCardView ---
        let tableContainer = buildTableSection()
        outerStack.addArrangedSubview(tableContainer)
        outerStack.setCustomSpacing(Layout.sectionGap, after: filtersBar)

        // Allow table container to grow
        tableContainer.setContentHuggingPriority(.defaultLow, for: .vertical)
        tableContainer.setContentCompressionResistancePriority(.defaultLow, for: .vertical)

        // --- Section 3: Memory stats bar ---
        let memBar = buildMemoryBar()
        outerStack.addArrangedSubview(memBar)

        // --- Section 4: Footer ---
        let footer = buildFooter()
        outerStack.addArrangedSubview(footer)

        NSLayoutConstraint.activate([
            outerStack.topAnchor.constraint(equalTo: root.topAnchor),
            outerStack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            outerStack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            outerStack.bottomAnchor.constraint(equalTo: root.bottomAnchor),

            root.widthAnchor.constraint(greaterThanOrEqualToConstant: Layout.minWidth),
            root.heightAnchor.constraint(greaterThanOrEqualToConstant: Layout.minHeight),

            filtersBar.heightAnchor.constraint(equalToConstant: Layout.headerHeight),
            memBar.heightAnchor.constraint(equalToConstant: 28),
            footer.heightAnchor.constraint(equalToConstant: Layout.footerHeight),
        ])
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        Task { await refresh() }
        Task { await refreshMemoryStats() }
        // Refresh memory stats every 5 seconds while tab is visible
        memoryRefreshTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                await self.refreshMemoryStats()
            }
        }
    }

    override func viewWillDisappear() {
        super.viewWillDisappear()
        memoryRefreshTimer?.invalidate()
        memoryRefreshTimer = nil
    }

    // MARK: - Filters Bar (Section 1)

    private func buildFiltersBar() -> NSView {
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = Layout.chipGap
        stack.alignment = .centerY
        stack.translatesAutoresizingMaskIntoConstraints = false

        // Severity chips
        let severities: [(id: String, label: String)] = [
            ("info",     "Info"),
            ("warn",     "Warn"),
            ("error",    "Error"),
            ("critical", "Critical"),
        ]

        for (id, label) in severities {
            let chip = makeSeverityChip(id: id, label: label)
            severityChips[id] = chip
            stack.addArrangedSubview(chip)
        }

        // Spacer
        let spacer = NSView()
        spacer.translatesAutoresizingMaskIntoConstraints = false
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        stack.addArrangedSubview(spacer)

        // Component dropdown
        componentPopup = NSPopUpButton()
        componentPopup.translatesAutoresizingMaskIntoConstraints = false
        componentPopup.addItem(withTitle: "Все")
        for comp in componentMap {
            componentPopup.addItem(withTitle: comp.label)
        }
        componentPopup.target = self
        componentPopup.action = #selector(onComponentChanged)
        stack.addArrangedSubview(componentPopup)

        return stack
    }

    private func makeSeverityChip(id: String, label: String) -> NSButton {
        let btn = NSButton()
        btn.setButtonType(.pushOnPushOff)
        btn.state = .on  // all active by default
        btn.translatesAutoresizingMaskIntoConstraints = false

        // Wave 67 (AGENT-J): use SF Symbol instead of `●` U+25CF to avoid TFPFont hang.
        let dotColor = SeverityColor.color(for: id)
        let symConfig = NSImage.SymbolConfiguration(pointSize: 9, weight: .bold)
            .applying(NSImage.SymbolConfiguration(paletteColors: [dotColor]))
        if let dotImg = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)?
                .withSymbolConfiguration(symConfig) {
            btn.image = dotImg
            btn.imagePosition = .imageLeft
        }
        btn.title = label
        btn.font = NSFont.systemFont(ofSize: 13, weight: .medium)

        btn.bezelStyle = .rounded
        btn.identifier = NSUserInterfaceItemIdentifier("chip_\(id)")
        btn.target = self
        btn.action = #selector(onSeverityChipToggled(_:))

        NSLayoutConstraint.activate([
            btn.heightAnchor.constraint(equalToConstant: Layout.chipSize),
        ])
        return btn
    }

    // MARK: - Table Section (Section 2)

    private func buildTableSection() -> NSView {
        // ThemeCardView wraps scroll+table
        let card = ThemeCardView()
        card.title = "Журнал ошибок"
        card.translatesAutoresizingMaskIntoConstraints = false

        // Configure table columns
        let colTime = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("time"))
        colTime.title = "Время"
        colTime.width = 100
        colTime.minWidth = 80

        let colSeverity = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("severity"))
        colSeverity.title = "Серьёзность"
        colSeverity.width = 90
        colSeverity.minWidth = 70

        let colComponent = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("component"))
        colComponent.title = "Компонент"
        colComponent.width = 110
        colComponent.minWidth = 80

        let colMessage = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("message"))
        colMessage.title = "Сообщение"
        colMessage.resizingMask = .autoresizingMask
        colMessage.minWidth = 150

        let colAction = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("action"))
        colAction.title = "Действие"
        colAction.width = 90
        colAction.minWidth = 70

        tableView.addTableColumn(colTime)
        tableView.addTableColumn(colSeverity)
        tableView.addTableColumn(colComponent)
        tableView.addTableColumn(colMessage)
        tableView.addTableColumn(colAction)

        tableView.delegate = self
        tableView.dataSource = self
        tableView.rowHeight = Layout.rowHeight
        tableView.allowsMultipleSelection = false
        tableView.allowsColumnReordering = false
        tableView.usesAlternatingRowBackgroundColors = true
        tableView.backgroundColor = .clear
        tableView.headerView?.wantsLayer = true
        tableView.translatesAutoresizingMaskIntoConstraints = false
        tableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle

        let scrollView = NSScrollView()
        scrollView.documentView = tableView
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.autohidesScrollers = true
        scrollView.drawsBackground = false
        scrollView.backgroundColor = .clear
        scrollView.translatesAutoresizingMaskIntoConstraints = false

        // Empty state label (centered, shown when no rows)
        emptyStateLabel.font = KrabEarTheme.Typography.body
        emptyStateLabel.textColor = KrabEarTheme.Colors.textSecondary
        emptyStateLabel.alignment = .center
        emptyStateLabel.isHidden = true
        emptyStateLabel.translatesAutoresizingMaskIntoConstraints = false

        // Container for scroll + empty state overlay
        let container = NSView()
        container.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(scrollView)
        container.addSubview(emptyStateLabel)

        NSLayoutConstraint.activate([
            scrollView.topAnchor.constraint(equalTo: container.topAnchor),
            scrollView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: container.bottomAnchor),

            emptyStateLabel.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            emptyStateLabel.centerYAnchor.constraint(equalTo: container.centerYAnchor),
        ])

        // Add container to the card's contentStackView
        container.setContentHuggingPriority(.defaultLow, for: .vertical)
        container.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        card.contentStackView.addArrangedSubview(container)
        card.contentStackView.alignment = .leading
        // container fills card horizontally
        NSLayoutConstraint.activate([
            container.leadingAnchor.constraint(equalTo: card.contentStackView.leadingAnchor),
            container.trailingAnchor.constraint(equalTo: card.contentStackView.trailingAnchor),
            container.heightAnchor.constraint(greaterThanOrEqualToConstant: 200),
        ])

        return card
    }

    // MARK: - Memory Bar (Section 3)

    private func buildMemoryBar() -> NSView {
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = KrabEarTheme.Metrics.standard
        stack.alignment = .centerY
        stack.translatesAutoresizingMaskIntoConstraints = false

        memoryStatsLabel.font = KrabEarTheme.Typography.monospace
        memoryStatsLabel.textColor = KrabEarTheme.Colors.textSecondary
        memoryStatsLabel.translatesAutoresizingMaskIntoConstraints = false
        memoryStatsLabel.setContentHuggingPriority(.defaultLow, for: .horizontal)
        stack.addArrangedSubview(memoryStatsLabel)

        return stack
    }

    // MARK: - Footer (Section 4)

    private func buildFooter() -> NSView {
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = KrabEarTheme.Metrics.standard
        stack.alignment = .centerY
        stack.translatesAutoresizingMaskIntoConstraints = false

        // Destructive clear button (left)
        let clearBtn = NSButton(title: "Очистить", target: self, action: #selector(onClearErrors))
        clearBtn.bezelStyle = .rounded
        clearBtn.contentTintColor = .systemRed
        clearBtn.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(clearBtn)

        // Spacer
        let spacer = NSView()
        spacer.translatesAutoresizingMaskIntoConstraints = false
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        stack.addArrangedSubview(spacer)

        // Send to Sentry button
        let sentryBtn = NSButton(title: "Отправить в Sentry", target: self, action: #selector(onSendToSentry))
        sentryBtn.bezelStyle = .rounded
        sentryBtn.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(sentryBtn)

        // Copy button (right)
        let copyBtn = NSButton(title: "Скопировать", target: self, action: #selector(onCopyErrors))
        copyBtn.bezelStyle = .rounded
        copyBtn.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(copyBtn)

        // Open Log button (right)
        let openLogBtn = NSButton(title: "Открыть лог", target: self, action: #selector(onOpenLog))
        openLogBtn.bezelStyle = .rounded
        openLogBtn.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(openLogBtn)

        return stack
    }

    // MARK: - Data

    func refresh() async {
        do {
            let response = try await ipcClient.callAsync(
                method: "list_recent_errors",
                params: [:],
                timeoutSec: IPCClient.quickTimeoutSec
            )
            guard let result = response["result"] as? [String: Any],
                  let items = result["errors"] as? [[String: Any]] else {
                logger.warning("list_recent_errors: unexpected response format")
                applyFilter()
                return
            }

            allErrors = items.compactMap { dict -> KrabErrorPayload? in
                guard let data = try? JSONSerialization.data(withJSONObject: dict),
                      let payload = try? JSONDecoder().decode(KrabErrorPayload.self, from: data)
                else { return nil }
                return payload
            }
            applyFilter()
        } catch {
            logger.error("list_recent_errors failed: \(error.localizedDescription, privacy: .public)")
            applyFilter()
        }
    }

    func applyFilter() {
        filteredErrors = allErrors.filter { payload in
            activeSeverities.contains(payload.severity) &&
            (activeComponent == nil || payload.component == activeComponent)
        }
        tableView.reloadData()
        emptyStateLabel.isHidden = !filteredErrors.isEmpty
    }

    // MARK: - Actions

    @objc private func onSeverityChipToggled(_ sender: NSButton) {
        guard let idStr = sender.identifier?.rawValue,
              idStr.hasPrefix("chip_") else { return }
        let severityId = String(idStr.dropFirst(5))  // "chip_" prefix = 5 chars
        if sender.state == .on {
            activeSeverities.insert(severityId)
        } else {
            activeSeverities.remove(severityId)
        }
        applyFilter()
    }

    @objc private func onComponentChanged() {
        let index = componentPopup.indexOfSelectedItem
        if index == 0 {
            activeComponent = nil
        } else {
            let compIndex = index - 1  // offset by "Все" item
            if compIndex >= 0 && compIndex < componentMap.count {
                activeComponent = componentMap[compIndex].id
            }
        }
        applyFilter()
    }

    @objc private func onClearErrors() {
        Task { @MainActor in
            do {
                _ = try await ipcClient.callAsync(
                    method: "clear_recent_errors",
                    params: [:],
                    timeoutSec: IPCClient.quickTimeoutSec
                )
                allErrors = []
                applyFilter()
            } catch {
                logger.error("clear_recent_errors failed: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    @objc private func onCopyErrors() {
        let lines = filteredErrors.map { payload in
            "[\(payload.timestamp)] [\(payload.severity.uppercased())] [\(payload.component)] \(payload.message_user)"
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lines.joined(separator: "\n"), forType: .string)
    }

    @objc private func onOpenLog() {
        let logDir = ("~/Library/Logs/KrabEar" as NSString).expandingTildeInPath
        NSWorkspace.shared.open(URL(fileURLWithPath: logDir))
    }

    @objc private func onSendToSentry() {
        Task { @MainActor in
            do {
                let response = try await ipcClient.callAsync(
                    method: "send_diagnostics_to_sentry",
                    params: [:],
                    timeoutSec: IPCClient.quickTimeoutSec
                )
                guard let result = response["result"] as? [String: Any] else { return }
                let ok = result["ok"] as? Bool ?? false
                if ok {
                    let count = result["sent_count"] as? Int ?? 0
                    showAlert(title: "Sentry", message: "Отправлено \(count) ошибок в Sentry.")
                } else {
                    let reason = result["reason"] as? String ?? "unknown"
                    showAlert(title: "Sentry — ошибка", message: "Не удалось отправить: \(reason)")
                }
            } catch {
                logger.error("send_diagnostics_to_sentry failed: \(error.localizedDescription, privacy: .public)")
                showAlert(title: "Sentry — ошибка", message: error.localizedDescription)
            }
        }
    }

    private func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        if let window = view.window {
            alert.beginSheetModal(for: window, completionHandler: nil)
        } else {
            alert.runModal()
        }
    }

    func refreshMemoryStats() async {
        do {
            let response = try await ipcClient.callAsync(
                method: "get_memory_stats",
                params: [:],
                timeoutSec: IPCClient.quickTimeoutSec
            )
            guard let result = response["result"] as? [String: Any],
                  let ok = result["ok"] as? Bool, ok,
                  let processes = result["processes"] as? [[String: Any]] else {
                memoryStatsLabel.stringValue = "Память: нет данных"
                return
            }

            if processes.isEmpty {
                memoryStatsLabel.stringValue = "Память: процессы не найдены"
                return
            }

            // Group by kind, sum RSS_MB
            var kindTotals: [String: Double] = [:]
            for proc in processes {
                let kind = proc["kind"] as? String ?? "other"
                let rss = proc["rss_mb"] as? Double ?? 0
                kindTotals[kind, default: 0] += rss
            }

            var parts: [String] = []
            let order = ["backend", "agent", "worker"]
            let labels: [String: String] = ["backend": "Backend", "agent": "Agent", "worker": "Workers"]
            for key in order {
                if let total = kindTotals[key] {
                    parts.append("\(labels[key] ?? key): \(Int(total)) MB")
                }
            }
            memoryStatsLabel.stringValue = "Память: " + parts.joined(separator: " | ")
        } catch {
            logger.warning("get_memory_stats failed: \(error.localizedDescription, privacy: .public)")
            memoryStatsLabel.stringValue = "Память: ошибка получения"
        }
    }

    // MARK: - Action button tap

    func handleActionButtonTap(at row: Int) {
        guard row >= 0 && row < filteredErrors.count else { return }
        let payload = filteredErrors[row]
        guard payload.actionable, let actionId = payload.action_id else { return }

        Task { @MainActor in
            do {
                _ = try await ipcClient.callAsync(
                    method: "handle_error_action",
                    params: ["action_id": actionId],
                    timeoutSec: IPCClient.quickTimeoutSec
                )
                logger.info("handle_error_action sent: \(actionId, privacy: .public)")
            } catch {
                logger.error("handle_error_action failed: \(error.localizedDescription, privacy: .public)")
            }
        }
    }
}

// MARK: - NSTableViewDataSource

extension DiagnosticsTabViewController: NSTableViewDataSource {
    func numberOfRows(in tableView: NSTableView) -> Int {
        filteredErrors.count
    }
}

// MARK: - NSTableViewDelegate

extension DiagnosticsTabViewController: NSTableViewDelegate {

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row >= 0 && row < filteredErrors.count else { return nil }
        let payload = filteredErrors[row]
        let colId = tableColumn?.identifier.rawValue ?? ""

        switch colId {
        case "time":
            let cell = makeTextCell(id: "time_cell")
            cell.textField?.font = KrabEarTheme.Typography.monospace
            cell.textField?.stringValue = formatTimestamp(payload.timestamp)
            return cell

        case "severity":
            let cell = makeTextCell(id: "severity_cell")
            let color = SeverityColor.color(for: payload.severity)
            let attrs: [NSAttributedString.Key: Any] = [
                .foregroundColor: color,
                .font: NSFont.systemFont(ofSize: 13, weight: .medium),
            ]
            cell.textField?.attributedStringValue = NSAttributedString(
                string: payload.severity.uppercased(),
                attributes: attrs
            )
            return cell

        case "component":
            let cell = makeTextCell(id: "component_cell")
            cell.textField?.stringValue = russianComponentLabel(for: payload.component)
            cell.textField?.font = KrabEarTheme.Typography.body
            return cell

        case "message":
            let cell = makeTextCell(id: "message_cell")
            cell.textField?.stringValue = payload.message_user
            cell.textField?.font = KrabEarTheme.Typography.body
            cell.textField?.lineBreakMode = .byTruncatingTail
            return cell

        case "action":
            if payload.actionable, let actionId = payload.action_id {
                let container = NSTableCellView()
                container.identifier = NSUserInterfaceItemIdentifier("action_cell")

                let btn = NSButton(title: actionButtonLabel(for: payload), target: self, action: #selector(onActionButtonTapped(_:)))
                btn.bezelStyle = .inline
                btn.tag = row
                btn.identifier = NSUserInterfaceItemIdentifier("action_\(actionId)")
                btn.translatesAutoresizingMaskIntoConstraints = false
                container.addSubview(btn)
                NSLayoutConstraint.activate([
                    btn.centerYAnchor.constraint(equalTo: container.centerYAnchor),
                    btn.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 4),
                    btn.trailingAnchor.constraint(lessThanOrEqualTo: container.trailingAnchor, constant: -4),
                ])
                return container
            }
            return makeTextCell(id: "action_cell")

        default:
            return nil
        }
    }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        Layout.rowHeight
    }

    @objc private func onActionButtonTapped(_ sender: NSButton) {
        handleActionButtonTap(at: sender.tag)
    }

    // MARK: - Cell helpers

    /// Label from context["action_label"] if present, else default "Исправить".
    /// Symmetric to ErrorToastView.buildActionButton(payload:actionId:) — same
    /// source of truth (backend ERROR_REGISTRY via ErrorBus.push), different default.
    private func actionButtonLabel(for payload: KrabErrorPayload) -> String {
        if let codable = payload.context["action_label"],
           let str = codable.value as? String {
            return str
        }
        return "Исправить"
    }

    private func makeTextCell(id: String) -> NSTableCellView {
        let cell = NSTableCellView()
        cell.identifier = NSUserInterfaceItemIdentifier(id)
        let tf = NSTextField(labelWithString: "")
        tf.translatesAutoresizingMaskIntoConstraints = false
        tf.lineBreakMode = .byTruncatingTail
        tf.maximumNumberOfLines = 1
        cell.addSubview(tf)
        cell.textField = tf
        NSLayoutConstraint.activate([
            tf.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            tf.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
            tf.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
        ])
        return cell
    }

    private func formatTimestamp(_ ts: String) -> String {
        // ISO8601 → "HH:mm:ss" display
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = iso.date(from: ts) {
            let fmt = DateFormatter()
            fmt.dateFormat = "HH:mm:ss"
            return fmt.string(from: date)
        }
        // fallback: last 8 chars e.g. "12:34:56"
        return ts.count >= 8 ? String(ts.suffix(8)) : ts
    }

    private func russianComponentLabel(for componentId: String) -> String {
        let mapping: [String: String] = [
            "paste":       "Вставка",
            "rewriter":    "Rewriter",
            "stt":         "STT",
            "diarization": "Diarization",
            "translation": "Перевод",
            "mlx":         "MLX",
            "history":     "История",
            "vocabulary":  "Словарь",
            "hotkey":      "Hotkey",
        ]
        return mapping[componentId] ?? componentId
    }
}
