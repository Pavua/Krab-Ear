/*
 main+LastErrorLine.swift — disabled-строка «Last error: …» в status-меню.

 Паттерн: main+BrainLease.swift — menuWillOpen → IPC off-main (AGENT-3) →
 UI на main. Фонового поллинга нет: строка свежая при открытии меню.

 Почему не NSStatusBarButton.toolTip: native help-окно на macOS 26 следует
 за курсором, а StatusBarDropView поверх кнопки может съесть mouseExited.
 `system.unclean_restart` живёт в ErrorBus ring до конца процесса бэкенда —
 тултип с ним лип к указателю весь сеанс. Сигнал остаётся здесь, в меню.
*/

import AppKit

/// Newest ErrorBus code from `list_recent_errors` (oldest-first ring).
func lastErrorCode(from errors: [[String: Any]]?) -> String? {
    guard let errors, let row = errors.last else { return nil }
    guard let code = row["code"] as? String else { return nil }
    let trimmed = code.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed.isEmpty ? nil : trimmed
}

/// Title for the last-error status-menu line. nil → hide the item.
func lastErrorMenuTitle(code: String?) -> String? {
    guard let code, !code.isEmpty else { return nil }
    return "Last error: \(code)"
}

extension AgentAppDelegate {
    /// Вызывается из menuWillOpen и один раз при rebuildStatusMenu.
    /// IPC строго off-main (AGENT-3); мутация NSMenuItem — на main.
    func refreshLastErrorMenuItem() {
        guard lastErrorMenuItem != nil else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var errors: [[String: Any]]?
            if let resp = try? self.ipcClient.call(
                method: "list_recent_errors",
                params: ["limit": 1]
            ), let result = resp["result"] as? [String: Any] {
                errors = result["errors"] as? [[String: Any]]
            }
            let title = lastErrorMenuTitle(code: lastErrorCode(from: errors))
            DispatchQueue.main.async {
                guard let item = self.lastErrorMenuItem else { return }
                if let title {
                    item.isHidden = false
                    item.title = title
                } else {
                    item.isHidden = true
                }
            }
        }
    }
}
