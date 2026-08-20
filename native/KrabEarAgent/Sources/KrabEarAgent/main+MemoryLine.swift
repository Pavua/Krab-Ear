/*
 main+MemoryLine.swift — T8 (волна Memory Conductor): инфо-строка «Память: …»
 в status-меню, СРАЗУ ПОСЛЕ brain-lease строки.

 Паттерн — main+BrainLease.swift (B3): disabled-пункт меню, обновление в
 menuWillOpen, IPC строго off-main (AGENT-3). Фонового поллинга НЕТ — строка
 свежая на момент открытия меню.

 Отличие от brain-lease: видимость строки управляется ОТДЕЛЬНЫМ вызовом
 (`get_settings.memory_conductor_enabled`), а не полем внутри ответа самого
 ledger-метода — IPC-контракт `get_memory_ledger` пинован волной и не несёт
 флага enabled. Оба вызова идут последовательно на одном off-main потоке.
*/

import AppKit

/// Рендер одной записи ledger. Ключ имеет форму `"<owner>/<resident>"` —
/// имя пункта = часть после «/». `state == "idle"` показывает время простоя
/// от `idle_since_ts` (минуты) ВМЕСТО размера; active/warm показывают размер
/// в гигабайтах (округление `size_mb`/1024), без дополнительного суффикса.
private func memoryEntryTitle(key: String, entry: [String: Any], nowTs: Double) -> String {
    let name = key.split(separator: "/").last.map(String.init) ?? key
    let state = entry["state"] as? String ?? ""
    if state == "idle" {
        if let idleSince = entry["idle_since_ts"] as? Double {
            let minutes = max(0, Int((nowTs - idleSince) / 60))
            return "\(name) idle \(minutes)м"
        }
        return "\(name) idle"
    }
    // Три состояния brain (условие enforce-волны): выгруженная модель не должна
    // рендериться как «0Г» — это выглядит поломкой, а не фактом. size_mb=null
    // означает «состояние неизвестно», и врать про размер тоже нельзя.
    if state == "unloaded" {
        return "\(name) выгружен"
    }
    guard let sizeMB = entry["size_mb"] as? Double else {
        return "\(name) —"
    }
    let sizeGB = Int((sizeMB / 1024.0).rounded())
    return "\(name) \(sizeGB)Г"
}

/// Чистый форматтер заголовка пункта меню «Память: …».
///
/// - Parameters:
///   - result: `result`-словарь ответа `get_memory_ledger`, или nil при
///     провале/пропуске запроса.
///   - enabled: живое значение настройки `memory_conductor_enabled`
///     (`get_settings`, отдельный вызов) — управляет ТОЛЬКО видимостью строки.
///   - now: точка отсчёта для арифметики минут/дней (инъекция для тестов).
/// - Returns: nil → пункт скрыть (настройка выключена); строка → показать.
///   При enabled=true, но провале/пустоте ledger — плейсхолдер «Память: —»,
///   НЕ скрытие (тот же класс, что brainLeaseMenuTitle: скрытие маскировало
///   бы умерший backend — для этого уже есть status-dot).
func memoryLineMenuTitle(from result: [String: Any]?, enabled: Bool, now: Date = Date()) -> String? {
    guard enabled else { return nil }
    guard let result,
          let ledger = result["ledger"] as? [String: Any],
          let entries = ledger["entries"] as? [String: Any],
          !entries.isEmpty
    else {
        return "Память: —"
    }

    let nowTs = now.timeIntervalSince1970
    // Сортировка ключей — детерминированный порядок (словарь Swift его не
    // гарантирует), важно и для стабильного вида меню, и для тестов.
    let parts = entries.keys.sorted().compactMap { key -> String? in
        guard let entry = entries[key] as? [String: Any] else { return nil }
        return memoryEntryTitle(key: key, entry: entry, nowTs: nowTs)
    }
    guard !parts.isEmpty else { return "Память: —" }

    var line = "Память: " + parts.joined(separator: " · ")

    // shadow-режим ≥7 дней — суффикс на весь конductor, не на отдельную запись.
    if let conductor = result["conductor"] as? [String: Any],
       let shadowSince = conductor["shadow_since"] as? Double {
        let days = Int((nowTs - shadowSince) / 86400)
        if days >= 7 {
            line += " · shadow \(days) дн"
        }
    }
    return line
}

extension AgentAppDelegate {
    /// Вызывается из menuWillOpen (main+MenuBarRecap.swift) при каждом
    /// открытии status-меню и один раз при построении меню (rebuildStatusMenu).
    /// IPC строго off-main (AGENT-3); мутация NSMenuItem — на main.
    func refreshMemoryLineMenuItem() {
        guard memoryLineMenuItem != nil else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            // Видимость решает ОТДЕЛЬНАЯ настройка — get_memory_ledger её не несёт.
            // Провал этого вызова fail-open в сторону видимой строки (ledger-вызов
            // ниже сам даст плейсхолдер «—» при собственном провале).
            var enabled = true
            if let resp = try? self.ipcClient.call(method: "get_settings", params: [:]),
               let settings = resp["result"] as? [String: Any] {
                enabled = settings["memory_conductor_enabled"] as? Bool ?? true
            }

            var ledgerResult: [String: Any]?
            if enabled {
                if let resp = try? self.ipcClient.call(method: "get_memory_ledger", params: [:]),
                   let res = resp["result"] as? [String: Any] {
                    ledgerResult = res
                }
            }

            let title = memoryLineMenuTitle(from: ledgerResult, enabled: enabled)
            DispatchQueue.main.async {
                guard let item = self.memoryLineMenuItem else { return }
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
