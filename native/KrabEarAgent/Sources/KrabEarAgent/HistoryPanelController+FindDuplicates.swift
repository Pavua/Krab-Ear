/*
 HistoryPanelController+FindDuplicates.swift

 Поиск дублирующихся транскрипций через IPC find_duplicates.
 Кнопка «Найти дубликаты» в разделе «Статистика» вкладки «История».

 Паттерны:
 - ipcClient.call — строго off-main (DispatchQueue.global → DispatchQueue.main).
 - NSAlert — через showInfoAlert (AlertHelpers.presentAlertSheet, НИКОГДА runModal).
 - NSPanel результатов — через presentAlertSheet (неблокирующий sheet, НИКОГДА runModal).
 - Glyph-free: не используем ● ○ ◉ • ▶ ◀ ▲ ▼ ★ ✕ ✓ в NSTextField/labelWithString.
*/

import AppKit
import Foundation

extension HistoryPanelController {

    // MARK: - Setup

    /// Строит кнопку «Найти дубликаты» для раздела «Статистика».
    /// Вызывается из setupManagementSections() в +Management.swift.
    func makeFindDuplicatesButton() -> NSButton {
        let button = ThemePrimaryButton(
            title: "Найти дубликаты",
            target: self,
            action: #selector(onFindDuplicates)
        )
        button.applyThemeSecondary()
        return button
    }

    // MARK: - Action

    @objc func onFindDuplicates() {
        let ipcClient = self.ipcClient
        // AGENT-3: ipcClient.call строго off-main.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try ipcClient.call(
                    method: "find_duplicates",
                    params: [
                        "similarity_threshold": 0.9,
                        "limit": 500
                    ]
                )
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.handleFindDuplicatesResult(result)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showInfoAlert(
                        title: "Поиск дубликатов",
                        body: "Ошибка IPC: \(error.localizedDescription)"
                    )
                }
            }
        }
    }

    // MARK: - Result handler (must be called on main thread)

    private func handleFindDuplicatesResult(_ result: [String: Any]) {
        // Privacy-mode branch: backend returns {ok: false, reason: "privacy_mode_active"}.
        if let ok = result["ok"] as? Bool, !ok {
            let reason = result["reason"] as? String ?? "неизвестная ошибка"
            let body: String
            if reason.contains("privacy") {
                body = "Поиск дубликатов недоступен в режиме приватности. Отключите режим приватности и повторите."
            } else if reason.contains("too many") {
                body = "Слишком много записей для анализа (более 200). Попробуйте уменьшить лимит или очистить историю."
            } else {
                body = "Не удалось выполнить поиск дубликатов: \(reason)"
            }
            showInfoAlert(title: "Поиск дубликатов", body: body)
            return
        }

        guard let groups = result["groups"] as? [[String: Any]] else {
            showInfoAlert(
                title: "Поиск дубликатов",
                body: "Дубликаты не найдены."
            )
            return
        }

        // Filter to non-empty groups.
        let validGroups = groups.filter { group in
            guard let items = group["items"] as? [[String: Any]] else { return false }
            return items.count >= 2
        }

        if validGroups.isEmpty {
            showInfoAlert(
                title: "Поиск дубликатов",
                body: "Дубликаты не найдены."
            )
            return
        }

        let totalDuplicates = result["total_duplicates"] as? Int ?? 0
        showDuplicatesPanel(groups: validGroups, totalDuplicates: totalDuplicates)
    }

    // MARK: - Results panel (non-blocking sheet)

    private func showDuplicatesPanel(groups: [[String: Any]], totalDuplicates: Int) {
        // Build a scrollable NSView with one row per group.
        let containerStack = NSStackView()
        containerStack.orientation = .vertical
        containerStack.spacing = 12
        containerStack.alignment = .leading
        containerStack.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        containerStack.translatesAutoresizingMaskIntoConstraints = false

        // Summary header.
        let headerLabel = NSTextField(labelWithString: "Найдено \(totalDuplicates) дублирующихся записей в \(groups.count) группах:")
        headerLabel.font = KrabEarTheme.Typography.sectionTitle
        headerLabel.lineBreakMode = .byWordWrapping
        headerLabel.preferredMaxLayoutWidth = 460
        containerStack.addArrangedSubview(headerLabel)

        for (index, group) in groups.enumerated() {
            let similarity = group["similarity"] as? Double ?? 0.0
            let items = group["items"] as? [[String: Any]] ?? []
            let similarityPct = Int((similarity * 100).rounded())

            let groupLabel = NSTextField(
                labelWithString: "Группа \(index + 1) — сходство \(similarityPct)% — \(items.count) записи"
            )
            groupLabel.font = KrabEarTheme.Typography.sectionTitle
            groupLabel.lineBreakMode = .byWordWrapping
            groupLabel.preferredMaxLayoutWidth = 460
            containerStack.addArrangedSubview(groupLabel)

            for item in items {
                let label = itemLabel(for: item)
                let itemTextField = NSTextField(labelWithString: "  " + label)
                itemTextField.font = KrabEarTheme.Typography.body
                itemTextField.textColor = KrabEarTheme.Colors.textSecondary
                itemTextField.lineBreakMode = .byTruncatingTail
                itemTextField.maximumNumberOfLines = 1
                itemTextField.preferredMaxLayoutWidth = 440
                containerStack.addArrangedSubview(itemTextField)
            }
        }

        // Wrap in a scroll view so many groups don't overflow.
        let scrollView = NSScrollView(frame: NSRect(x: 0, y: 0, width: 480, height: 320))
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.autohidesScrollers = true
        scrollView.borderType = .noBorder
        scrollView.documentView = containerStack
        scrollView.translatesAutoresizingMaskIntoConstraints = false

        // Constrain containerStack width to scroll view.
        NSLayoutConstraint.activate([
            containerStack.widthAnchor.constraint(equalToConstant: 460)
        ])

        // Present as a non-blocking alert sheet.
        let alert = NSAlert()
        alert.messageText = "Поиск дубликатов"
        alert.informativeText = ""
        alert.accessoryView = scrollView
        alert.addButton(withTitle: "Закрыть")

        presentAlertSheet(alert, for: self.window) { _ in
            // Dismiss — no action needed.
        }
    }

    // MARK: - Helpers

    /// Формирует короткую метку для элемента истории: title или первые 40 символов текста,
    /// плюс временная метка если доступна.
    private func itemLabel(for item: [String: Any]) -> String {
        var text = ""

        if let title = item["title"] as? String, !title.isEmpty {
            text = title
        } else if let rawText = item["text"] as? String, !rawText.isEmpty {
            let trimmed = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
            text = trimmed.count > 40
                ? String(trimmed.prefix(40)) + "..."
                : trimmed
        } else {
            text = "(без текста)"
        }

        if let timestamp = item["ts"] as? String {
            // Show first 16 chars of ISO timestamp (YYYY-MM-DDTHH:MM).
            let ts = String(timestamp.prefix(16)).replacingOccurrences(of: "T", with: " ")
            return "\(text)  [\(ts)]"
        }
        return text
    }
}
