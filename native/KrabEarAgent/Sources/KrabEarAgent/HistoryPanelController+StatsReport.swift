/*
 HistoryPanelController+StatsReport.swift

 Экспорт полного Markdown-отчёта статистики через IPC generate_stats_report.
 Кнопка «Сгенерировать отчёт» в разделе «Статистика» вкладки «История».

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

    /// Строит кнопку «Сгенерировать отчёт» для раздела «Статистика».
    /// Вызывается из setupManagementSections() в +Management.swift.
    func makeStatsReportButton() -> NSButton {
        let button = ThemePrimaryButton(
            title: "Сгенерировать отчёт",
            target: self,
            action: #selector(onGenerateStatsReport)
        )
        button.applyThemeSecondary()
        return button
    }

    // MARK: - Action

    @objc func onGenerateStatsReport() {
        let days = 30

        let ipcClient = self.ipcClient
        // AGENT-3: ipcClient.call строго off-main.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try ipcClient.call(
                    method: "generate_stats_report",
                    params: ["days": days]
                )
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.handleStatsReportResult(result, days: days)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showInfoAlert(
                        title: "Статистический отчёт",
                        body: "Ошибка IPC: \(error.localizedDescription)"
                    )
                }
            }
        }
    }

    // MARK: - Result handler (must be called on main thread)

    private func handleStatsReportResult(_ result: [String: Any], days: Int) {
        // Privacy-mode branch: backend returns {ok: false, reason: "privacy_mode_active"}.
        if let ok = result["ok"] as? Bool, !ok {
            let reason = result["reason"] as? String ?? "неизвестная ошибка"
            let body: String
            if reason.contains("privacy") {
                body = "Статистический отчёт недоступен в режиме приватности. Отключите режим приватности и повторите."
            } else {
                body = "Не удалось сгенерировать отчёт: \(reason)"
            }
            showInfoAlert(title: "Статистический отчёт", body: body)
            return
        }

        guard let markdown = result["markdown"] as? String, !markdown.isEmpty else {
            showInfoAlert(
                title: "Статистический отчёт",
                body: "Бэкенд вернул пустой отчёт. Возможно, история пуста за последние \(days) дней."
            )
            return
        }

        let suggestedName = "krab-ear-stats-\(days)d.md"

        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = suggestedName
        panel.allowedContentTypes = [.plainText]
        panel.title = "Сохранить статистический отчёт"
        panel.prompt = "Сохранить"

        presentPanelSheet(panel, for: self.window) { [weak self] response in
            guard response == .OK, let url = panel.url else { return }
            do {
                try markdown.write(to: url, atomically: true, encoding: .utf8)
                self?.showInfoAlert(
                    title: "Статистический отчёт",
                    body: "Отчёт за \(days) дней сохранён:\n\(url.path)"
                )
            } catch {
                self?.showInfoAlert(
                    title: "Статистический отчёт",
                    body: "Не удалось записать файл: \(error.localizedDescription)"
                )
            }
        }
    }
}
