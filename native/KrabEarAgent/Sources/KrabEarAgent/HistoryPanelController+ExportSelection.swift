/*
 HistoryPanelController+ExportSelection.swift

 Экспорт выбранных записей истории через IPC export_selected_items.
 Триггер: пункт контекстного меню таблицы истории «Экспортировать выбранное».

 Паттерны:
 - ipcClient.call — строго off-main (DispatchQueue.global → DispatchQueue.main).
 - NSSavePanel — через AlertHelpers.presentPanelSheet (НИКОГДА через runModal).
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

        menu.addItem(NSMenuItem.separator())

        let deleteItem = NSMenuItem(
            title: "Удалить выбранные",
            action: #selector(onBulkDelete),
            keyEquivalent: ""
        )
        deleteItem.target = self
        menu.addItem(deleteItem)

        let addToCollectionItem = NSMenuItem(
            title: "Добавить в коллекцию...",
            action: #selector(onBulkAddToCollection),
            keyEquivalent: ""
        )
        addToCollectionItem.target = self
        menu.addItem(addToCollectionItem)

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

    // MARK: - Bulk Delete

    @objc func onBulkDelete() {
        let rows = tableView.selectedRowIndexes
        guard !rows.isEmpty else {
            let alert = NSAlert()
            alert.messageText = "Удалить выбранные"
            alert.informativeText = "Не выбрано ни одной записи."
            alert.addButton(withTitle: "OK")
            presentAlertSheet(alert, for: self.window) { _ in }
            return
        }

        let count = rows.count
        let confirmAlert = NSAlert()
        confirmAlert.messageText = "Удалить выбранные записи?"
        confirmAlert.informativeText = "Будет удалено записей: \(count). Действие необратимо."
        confirmAlert.addButton(withTitle: "Удалить")   // .alertFirstButtonReturn
        confirmAlert.addButton(withTitle: "Отмена")

        presentAlertSheet(confirmAlert, for: self.window) { [weak self] response in
            guard response == .alertFirstButtonReturn, let self else { return }

            // Collect IDs before mutating items.
            let ids: [String] = rows.compactMap { idx in
                guard idx < self.items.count else { return nil }
                return self.items[idx].id
            }

            // Optimistic UI: remove rows in descending order so indexes stay valid.
            for idx in rows.sorted(by: >) {
                if idx < self.items.count {
                    self.items.remove(at: idx)
                }
            }
            self.tableView.reloadData()

            // AGENT-3: IPC off-main.
            let ipcClient = self.ipcClient
            DispatchQueue.global(qos: .userInitiated).async {
                for id in ids {
                    _ = try? ipcClient.call(method: "delete_history_item", params: ["id": id])
                }
            }
        }
    }

    // MARK: - Bulk Add to Collection

    @objc func onBulkAddToCollection() {
        let rows = tableView.selectedRowIndexes
        guard !rows.isEmpty else {
            let alert = NSAlert()
            alert.messageText = "Добавить в коллекцию"
            alert.informativeText = "Не выбрано ни одной записи."
            alert.addButton(withTitle: "OK")
            presentAlertSheet(alert, for: self.window) { _ in }
            return
        }

        let ids: [String] = rows.compactMap { idx in
            guard idx < items.count else { return nil }
            return items[idx].id
        }
        guard !ids.isEmpty else { return }

        // Step 1: fetch collection list off-main.
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            nonisolated(unsafe) let response = try? ipcClient.call(method: "list_collections", params: [:])
            nonisolated(unsafe) let result = response?["result"] as? [String: Any] ?? [:]
            DispatchQueue.main.async {
                self?.presentAddToCollectionSheet(result: result, ids: ids, ipcClient: ipcClient)
            }
        }
    }

    // MARK: - Add-to-collection sheet (must be called on main thread)

    private func presentAddToCollectionSheet(
        result: [String: Any],
        ids: [String],
        ipcClient: IPCClient
    ) {
        // Privacy-mode guard.
        if let reason = result["reason"] as? String, reason.contains("privacy") {
            showInfoAlert(title: "Добавить в коллекцию", body: "Недоступно в режиме приватности.")
            return
        }

        let existingNames: [String] = (result["collections"] as? [[String: Any]])?.compactMap {
            $0["name"] as? String
        } ?? []

        // Build alert with NSComboBox accessory view.
        let alert = NSAlert()
        alert.messageText = "Добавить в коллекцию"
        alert.informativeText = "Выберите существующую коллекцию или введите имя новой."
        alert.addButton(withTitle: "Добавить")   // .alertFirstButtonReturn
        alert.addButton(withTitle: "Отмена")

        let comboBox = NSComboBox(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
        comboBox.isEditable = true
        for name in existingNames {
            comboBox.addItem(withObjectValue: name)
        }
        alert.accessoryView = comboBox
        alert.window.initialFirstResponder = comboBox

        presentAlertSheet(alert, for: self.window) { [weak self] response in
            guard response == .alertFirstButtonReturn else { return }
            let name = comboBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !name.isEmpty else {
                self?.showInfoAlert(title: "Добавить в коллекцию", body: "Имя коллекции не может быть пустым.")
                return
            }

            let isNew = !existingNames.contains(name)
            let capturedIds = ids

            // AGENT-3: IPC off-main. Считаем фактические успехи/сбои —
            // не показываем ложный success-алерт, если бэкенд отклонил запрос.
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                if isNew {
                    _ = try? ipcClient.call(
                        method: "create_collection",
                        params: ["name": name, "description": ""]
                    )
                }
                var added = 0
                var failed = 0
                for id in capturedIds {
                    do {
                        _ = try ipcClient.call(
                            method: "add_to_collection",
                            params: ["collection_name": name, "item_id": id]
                        )
                        added += 1
                    } catch {
                        failed += 1
                    }
                }
                DispatchQueue.main.async {
                    let body: String
                    if added > 0 {
                        let suffix = failed > 0 ? " Не удалось: \(failed)." : ""
                        body = "Добавлено записей: \(added) в коллекцию \"\(name)\".\(suffix)"
                    } else {
                        body = "Не удалось добавить записи в коллекцию \"\(name)\"."
                    }
                    self?.showInfoAlert(title: "Добавить в коллекцию", body: body)
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
