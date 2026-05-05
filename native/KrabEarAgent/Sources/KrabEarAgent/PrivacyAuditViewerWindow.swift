/*
 PrivacyAuditViewerWindow — модальное окно просмотра Privacy Audit Log.

 Открывается из секции «Безопасность и приватность» в Settings tab.

 IPC-методы:
   get_privacy_audit_log(limit: Int) → {entries: [...], total_count: Int}
     Каждая запись: {ts: String, category: String, action: String, details: {}}
   clear_privacy_audit_log() → {ok: Bool}

 Columns: «Время» | «Категория / Действие» | «Подробности»
 Bottom row: Refresh + Очистить + Закрыть
*/

import AppKit
import Foundation

// MARK: - NSWindowController

/// Модальное окно просмотра и очистки privacy audit log.
@MainActor
final class PrivacyAuditViewerWindowController: NSWindowController,
    NSTableViewDataSource, NSTableViewDelegate
{

    // MARK: - State

    var ipcClient: IPCClient?

    private struct AuditEntry {
        let ts: String
        let categoryAction: String
        let details: String
    }

    private var entries: [AuditEntry] = []
    private var totalCount: Int = 0

    // MARK: - UI

    private let tableView   = NSTableView()
    private let scrollView  = NSScrollView()
    private let statusLabel = NSTextField(labelWithString: "Загрузка…")
    private let refreshButton = NSButton(title: "Обновить", target: nil, action: nil)
    private let clearButton   = NSButton(title: "Очистить", target: nil, action: nil)
    private let closeButton   = NSButton(title: "Закрыть", target: nil, action: nil)

    // MARK: - Init

    convenience init(ipcClient: IPCClient) {
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 480),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        win.title = "Privacy Audit Log"
        win.minSize = NSSize(width: 520, height: 300)
        KrabEarTheme.applyTheme(to: win)
        self.init(window: win)
        self.ipcClient = ipcClient
        buildUI()
    }

    // MARK: - Build UI

    private func buildUI() {
        guard let contentView = window?.contentView else { return }
        contentView.wantsLayer = true

        // --- Columns ---
        let tsCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("ts"))
        tsCol.title = "Время"
        tsCol.width = 160
        tsCol.minWidth = 120

        let catCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("cat"))
        catCol.title = "Категория / Действие"
        catCol.width = 200
        catCol.minWidth = 140

        let detCol = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("det"))
        detCol.title = "Подробности"
        detCol.width = 300
        detCol.minWidth = 120

        for col in [tsCol, catCol, detCol] {
            tableView.addTableColumn(col)
        }

        tableView.dataSource = self
        tableView.delegate   = self
        tableView.usesAlternatingRowBackgroundColors = true
        tableView.rowHeight  = 22
        tableView.gridStyleMask = [.solidHorizontalGridLineMask]
        tableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle

        scrollView.documentView = tableView
        scrollView.hasVerticalScroller    = true
        scrollView.hasHorizontalScroller  = false
        scrollView.autohidesScrollers     = true
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.applyThemeInnerScroll()

        // --- Status label ---
        statusLabel.font      = KrabEarTheme.Typography.caption
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        // --- Bottom buttons ---
        refreshButton.bezelStyle = .rounded
        refreshButton.font       = KrabEarTheme.Typography.body
        refreshButton.target     = self
        refreshButton.action     = #selector(onRefresh)

        clearButton.bezelStyle  = .rounded
        clearButton.font        = KrabEarTheme.Typography.body
        clearButton.target      = self
        clearButton.action      = #selector(onClear)

        closeButton.bezelStyle  = .rounded
        closeButton.font        = KrabEarTheme.Typography.body
        closeButton.target      = self
        closeButton.action      = #selector(onClose)
        closeButton.keyEquivalent = "\u{1b}" // Escape

        let bottomStack = NSStackView(views: [statusLabel, NSView(), refreshButton, clearButton, closeButton])
        bottomStack.orientation  = .horizontal
        bottomStack.alignment    = .centerY
        bottomStack.distribution = .fill
        bottomStack.spacing      = KrabEarTheme.Metrics.standard
        bottomStack.translatesAutoresizingMaskIntoConstraints = false
        // Spacer
        if let spacer = bottomStack.arrangedSubviews.first(where: { !($0 is NSTextField) && !($0 is NSButton) }) {
            spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        }

        contentView.addSubview(scrollView)
        contentView.addSubview(bottomStack)

        NSLayoutConstraint.activate([
            scrollView.topAnchor.constraint(equalTo: contentView.topAnchor, constant: 12),
            scrollView.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 12),
            scrollView.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -12),
            scrollView.bottomAnchor.constraint(equalTo: bottomStack.topAnchor, constant: -8),

            bottomStack.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 12),
            bottomStack.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -12),
            bottomStack.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -12),
            bottomStack.heightAnchor.constraint(equalToConstant: 32),
        ])
    }

    // MARK: - Show

    func showAndLoad() {
        window?.center()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        fetchEntries()
    }

    // MARK: - IPC

    private func fetchEntries() {
        guard let ipc = ipcClient else { return }
        statusLabel.stringValue = "Загрузка…"
        refreshButton.isEnabled = false

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let resp = try ipc.call(method: "get_privacy_audit_log", params: ["limit": 200])
                let result = resp["result"] as? [String: Any]
                let rawEntries = result?["entries"] as? [[String: Any]] ?? []
                let total     = result?["total_count"] as? Int ?? rawEntries.count

                let parsed: [AuditEntry] = rawEntries.map { dict in
                    let ts     = dict["ts"] as? String ?? ""
                    let cat    = dict["category"] as? String ?? ""
                    let action = dict["action"]   as? String ?? ""
                    let det    = dict["details"]
                    let detStr: String
                    if let d = det as? [String: Any], !d.isEmpty,
                       let json = try? JSONSerialization.data(withJSONObject: d),
                       let s    = String(data: json, encoding: .utf8) {
                        detStr = s
                    } else {
                        detStr = ""
                    }
                    return AuditEntry(ts: ts, categoryAction: "\(cat) / \(action)", details: detStr)
                }

                DispatchQueue.main.async {
                    self.entries    = parsed
                    self.totalCount = total
                    self.tableView.reloadData()
                    let shownCount = parsed.count
                    if total > shownCount {
                        self.statusLabel.stringValue = "Показано \(shownCount) из \(total) записей"
                    } else {
                        self.statusLabel.stringValue = "Записей: \(total)"
                    }
                    self.refreshButton.isEnabled = true
                }
            } catch {
                DispatchQueue.main.async {
                    self.statusLabel.stringValue = "Ошибка: \(error.localizedDescription)"
                    self.refreshButton.isEnabled = true
                }
            }
        }
    }

    private func clearLog() {
        guard let ipc = ipcClient else { return }
        clearButton.isEnabled   = false
        statusLabel.stringValue = "Очистка…"

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try ipc.call(method: "clear_privacy_audit_log", params: [:])
                DispatchQueue.main.async {
                    self.entries    = []
                    self.totalCount = 0
                    self.tableView.reloadData()
                    self.statusLabel.stringValue = "Лог очищен"
                    self.clearButton.isEnabled   = true
                }
            } catch {
                DispatchQueue.main.async {
                    self.statusLabel.stringValue = "Ошибка очистки: \(error.localizedDescription)"
                    self.clearButton.isEnabled   = true
                }
            }
        }
    }

    // MARK: - Actions

    @objc private func onRefresh() {
        fetchEntries()
    }

    @objc private func onClear() {
        let alert = NSAlert()
        alert.messageText    = "Очистить лог приватности?"
        alert.informativeText = "Все записи privacy audit log будут удалены без возможности восстановления."
        alert.addButton(withTitle: "Очистить")
        alert.addButton(withTitle: "Отмена")
        alert.alertStyle = .warning
        guard let window else { return }
        alert.beginSheetModal(for: window) { [weak self] response in
            if response == .alertFirstButtonReturn {
                self?.clearLog()
            }
        }
    }

    @objc private func onClose() {
        window?.close()
    }

    // MARK: - NSTableViewDataSource

    nonisolated func numberOfRows(in tableView: NSTableView) -> Int {
        MainActor.assumeIsolated { entries.count }
    }

    // MARK: - NSTableViewDelegate

    nonisolated func tableView(
        _ tableView: NSTableView,
        viewFor tableColumn: NSTableColumn?,
        row: Int
    ) -> NSView? {
        MainActor.assumeIsolated {
            guard row < entries.count else { return nil }
            let entry = entries[row]
            let id    = tableColumn?.identifier.rawValue ?? ""
            let text: String
            switch id {
            case "ts":  text = formatTimestamp(entry.ts)
            case "cat": text = entry.categoryAction
            default:    text = entry.details
            }
            let identifier = NSUserInterfaceItemIdentifier("AuditCell_\(id)")
            if let cell = tableView.makeView(withIdentifier: identifier, owner: nil) as? NSTableCellView {
                cell.textField?.stringValue = text
                return cell
            }
            let cell = NSTableCellView()
            let tf   = NSTextField(labelWithString: text)
            tf.font      = KrabEarTheme.Typography.caption
            tf.textColor = KrabEarTheme.Colors.textPrimary
            tf.lineBreakMode = .byTruncatingTail
            tf.translatesAutoresizingMaskIntoConstraints = false
            cell.addSubview(tf)
            cell.textField = tf
            cell.identifier = identifier
            NSLayoutConstraint.activate([
                tf.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
                tf.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
                tf.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            ])
            return cell
        }
    }

    // MARK: - Helpers

    private func formatTimestamp(_ iso: String) -> String {
        // ISO-8601 → local short date/time
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: iso) {
            let local = DateFormatter()
            local.dateFormat = "yyyy-MM-dd HH:mm:ss"
            return local.string(from: date)
        }
        // Fallback: убираем Z/+00:00 суффикс для удобочитаемости
        return iso.replacingOccurrences(of: "+00:00", with: "").replacingOccurrences(of: "Z", with: "")
    }
}
