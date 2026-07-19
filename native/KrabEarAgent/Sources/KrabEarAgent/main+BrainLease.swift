/*
 main+BrainLease.swift — B3: инфо-строка «кто держит LM Studio» в status-меню.

 Спека: docs/superpowers/specs/2026-07-19-b3-brain-lease-visibility-design.md
 Паттерн: refreshQuickNotesSubmenu (main+QuickCapture.swift) — menuWillOpen →
 IPC off-main (AGENT-3) → обновление UI на main. Фонового поллинга НЕТ:
 строка свежая на момент открытия меню, ноль постоянного IPC-трафика.
*/

import AppKit

/// Чистый форматтер заголовка пункта меню brain-lease.
///
/// - Parameter result: `result`-словарь ответа `get_brain_lease_status`
///   или nil при провале IPC.
/// - Returns: nil → пункт скрыть (лиз выключен настройкой llm_brain_lease_enabled);
///   строка → показать. Провал IPC даёт плейсхолдер «—», НЕ скрытие: скрытие
///   по ошибке маскировало бы умерший backend (для этого рядом есть status-dot).
func brainLeaseMenuTitle(from result: [String: Any]?) -> String? {
    guard let result else { return "LM Studio: —" }
    let enabled = result["enabled"] as? Bool ?? true
    guard enabled else { return nil }
    guard result["held"] as? Bool ?? false else { return "LM Studio: свободен" }

    let rawOwner = result["owner"] as? String ?? "?"
    let owner: String
    switch rawOwner {
    case "krab_ear": owner = "Krab Ear"
    case "krab": owner = "Краб"
    default: owner = rawOwner   // forward-compat: новые владельцы — как есть
    }
    if let secondsLeft = result["seconds_left"] as? Double, secondsLeft > 0 {
        return "LM Studio: \(owner) · ещё \(Int(secondsLeft))с"
    }
    return "LM Studio: \(owner)"
}

extension AgentAppDelegate {
    /// Вызывается из menuWillOpen (main+MenuBarRecap.swift) при каждом
    /// открытии status-меню и один раз при построении меню (rebuildStatusMenu).
    /// IPC строго off-main (AGENT-3); мутация NSMenuItem — на main.
    func refreshBrainLeaseMenuItem() {
        guard brainLeaseMenuItem != nil else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var result: [String: Any]?
            // call() возвращает ПОЛНЫЙ конверт {ok, result, id} — поля лежат
            // в result (тот же контракт, что в refreshQuickNotesSubmenu).
            if let resp = try? self.ipcClient.call(
                method: "get_brain_lease_status", params: [:]),
               let res = resp["result"] as? [String: Any] {
                result = res
            }
            let title = brainLeaseMenuTitle(from: result)
            DispatchQueue.main.async {
                guard let item = self.brainLeaseMenuItem else { return }
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
