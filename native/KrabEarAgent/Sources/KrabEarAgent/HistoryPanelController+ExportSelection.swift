/*
 HistoryPanelController+ExportSelection.swift

 Экспорт выбранных записей истории через IPC export_selected_items.
 Триггер: пункт контекстного меню таблицы истории «Экспортировать выбранное».

 Паттерны:
 - ipcClient.call — строго off-main (DispatchQueue.global → DispatchQueue.main).
 - NSSavePanel — через AlertHelpers.presentPanelSheet (НИКОГДА .runModal()).
 - NSAlert     — через AlertHelpers.presentAlertSheet или showInfoAlert.
 - Glyph-free: не используем ● ○ ◉ • ▶ ◀ ▲ ▼ ★ ✕ ✓ ⏱ в labelWithString.
*/

import AppKit
import Foundation
import UniformTypeIdentifiers

extension HistoryPanelController {

    // MARK: - Setup

    /// Устанавливает контекстное меню таблицы истории с пунктом экспорта выбранных.
    /// Вызывается из setupHistoryTab() в ApplyTheme+HistoryTab.swift.
    func setupExportSelectionContextMenu() {
        tableView.allowsMultipleSelection = true

        let menu = NSMenu()
        let exportItem = NSMenuItem(
            title: "Экспортировать выбранное",
            action: #selector(onExportSelected),
            keyEquivalent: ""
        )
        exportItem.target = self
        menu.addItem(exportItem)

        let copyItem = NSMenuItem(
            title: "Копировать текст",
            action: #selector(onCopy),
            keyEquivalent: ""
        )
        copyItem.target = self
        menu.addItem(copyItem)

        tableView.menu = menu
    }

    // MARK: - Export selected action

    @objc func onExportSelected() {
        let selectedIndexes = tableView.selectedRowIndexes
        guard !selectedIndexes.isEmpty else {
            let alert = NSAlert()
            alert.messageText = "Экспорт выбранных"
            alert.informativeText = "Не выбрано ни одной записи. Выберите записи в таблице и повторите."
            alert.addButton(withTitle: "OK")
            presentAlertSheet(alert, for: self.window) { _ in }
            return
        }

        // Map selected row indexes → item IDs via the items data-source array.
        let ids: [String] = selectedIndexes.compactMap { row in
            guard row < items.count else { return nil }
            return items[row].id
        }
        guard !ids.isEmpty else {
            showInfoAlert(title: "Экспорт выбранных", body: "Не удалось определить идентификаторы записей.")
            return
        }

        let ipcClient = self.ipcClient
        // AGENT-3: ipcClient.call строго off-main.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try ipcClient.call(
                    method: "export_selected_items",
                    params: ["item_ids": ids, "format": "markdown"]
                )
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.handleExportSelectedResult(result, itemCount: ids.count)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showInfoAlert(
                        title: "Экспорт выбранных",
                        body: "Ошибка IPC: \(error.localizedDescription)"
                    )
                }
            }
        }
    }

    // MARK: - Result handler (must be called on main thread)

    private func handleExportSelectedResult(_ result: [String: Any], itemCount: Int) {
        let ok = result["ok"] as? Bool ?? false
        guard ok else {
            let reason = result["reason"] as? String ?? "неизвестная ошибка"
            let body: String
            if reason.contains("privacy") {
                body = "Экспорт недоступен в режиме приватности. Отключите режим приватности и повторите."
            } else if reason.contains("item_ids") {
                body = "Не выбрано ни одной записи для экспорта."
            } else {
                body = "Не удалось экспортировать: \(reason)"
            }
            showInfoAlert(title: "Экспорт выбранных", body: body)
            return
        }

        guard let content = result["content"] as? String, !content.isEmpty else {
            showInfoAlert(title: "Экспорт выбранных", body: "Бэкенд вернул пустой контент.")
            return
        }

        let entries = result["entries"] as? Int ?? itemCount
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let suggestedName = "krab_ear_selected_\(formatter.string(from: Date()))_\(entries)items.md"

        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = suggestedName
        panel.allowedContentTypes = [.plainText]
        panel.title = "Сохранить экспорт выбранных записей"
        panel.prompt = "Сохранить"

        presentPanelSheet(panel, for: self.window) { [weak self] response in
            guard response == .OK, let url = panel.url else { return }
            do {
                try content.write(to: url, atomically: true, encoding: .utf8)
                self?.showInfoAlert(
                    title: "Экспорт выбранных",
                    body: "Сохранено записей: \(entries)\n\(url.path)"
                )
            } catch {
                self?.showInfoAlert(
                    title: "Экспорт выбранных",
                    body: "Не удалось записать файл: \(error.localizedDescription)"
                )
            }
        }
    }
}
