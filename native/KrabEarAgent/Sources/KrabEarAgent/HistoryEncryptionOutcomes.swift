/*
 HistoryEncryptionOutcomes.swift

 Чистая (без UI/IPC) логика разбора ответов backend для секции «Безопасность»:
   - итог миграции истории (plaintext-счётчик + терминальный статус из события
     history_encryption.migrate.progress);
   - итог set_history_encryption (успех / Keychain / прочий отказ).
 Вынесено из HistoryPanelController+SecuritySettings.swift ради тестов.
*/

import Foundation

/// Итог миграции «зашифровать существующие записи».
enum HistoryMigrationOutcome: Equatable {
    /// Все записи зашифрованы, plaintext-резервная копия удалена.
    case done
    /// В живом файле остались незашифрованные записи.
    case partial(plaintext: Int)
    /// Живой файл зашифрован, но plaintext .bak не удалось безопасно стереть.
    case backupCleanupFailed
    /// Миграция завершилась отказом (verification_failed, error, ...): «Готово» показывать нельзя.
    case failed(status: String)

    /// Итог берётся из IPC (`last_migration_status`): он переживает пропущенный SSE.
    /// SSE-наблюдатель остаётся запасным источником (например, UI подхватил уже идущую миграцию).
    static func effectiveTerminalStatus(ipc: String?, sse: String?) -> String? {
        if let ipc, !ipc.isEmpty { return ipc }
        if let sse, !sse.isEmpty { return sse }
        return nil
    }

    static func resolve(plaintext: Int, terminalStatus: String?) -> HistoryMigrationOutcome {
        // Живой файл при backup_cleanup_failed уже зашифрован (plaintext == 0), но plaintext .bak остался.
        if terminalStatus == "backup_cleanup_failed" { return .backupCleanupFailed }
        if let status = terminalStatus, !status.isEmpty, status != "done", status != "encrypting" {
            return .failed(status: status)
        }
        if plaintext == 0 { return .done }
        return .partial(plaintext: plaintext)
    }
}

/// Запоминает терминальный статус миграции из SSE-события
/// `history_encryption.migrate.progress` (backend отдаёт итог фонового потока
/// только событием: IPC-статус содержит одни счётчики).
final class HistoryMigrationTerminalWatcher: @unchecked Sendable {
    private let lock = NSLock()
    private var status: String?

    var terminalStatus: String? {
        lock.lock(); defer { lock.unlock() }
        return status
    }

    func reset() {
        lock.lock(); status = nil; lock.unlock()
    }

    func ingest(line: String) {
        guard let value = Self.terminalStatus(fromSSELine: line) else { return }
        lock.lock(); status = value; lock.unlock()
    }

    /// Событие несёт `status`: "encrypting" — промежуточный, всё прочее ("done" или
    /// причина отказа) — терминальный. Строки не-`data:` и битый JSON игнорируются.
    static func terminalStatus(fromSSELine line: String) -> String? {
        guard line.hasPrefix("data: "),
              let data = String(line.dropFirst(6)).data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let status = obj["status"] as? String,
              !status.isEmpty, status != "encrypting" else { return nil }
        return status
    }
}

/// Итог set_history_encryption.
enum EncryptionChangeOutcome: Equatable {
    case applied
    case keychainUnavailable(enabled: Bool?)
    case failed(enabled: Bool?)

    /// `response` — полный конверт IPC (nil, если вызов бросил исключение).
    static func resolve(response: [String: Any]?) -> EncryptionChangeOutcome {
        guard let result = response?["result"] as? [String: Any] else { return .failed(enabled: nil) }
        let enabled = result["enabled"] as? Bool
        if (result["ok"] as? Bool) == true { return .applied }
        if (result["error"] as? String) == "keychain_unavailable" { return .keychainUnavailable(enabled: enabled) }
        return .failed(enabled: enabled)
    }

    /// Достоверно известное состояние после отказа или nil, если подтвердить его нечем
    /// (ответ потерян и повторное чтение тоже провалилось): нельзя утверждать «не применено».
    func knownEnabled(desired: Bool, reread: Bool?) -> Bool? {
        switch self {
        case .applied:
            return desired
        case .keychainUnavailable(let enabled), .failed(let enabled):
            return reread ?? enabled
        }
    }

    /// Реальное состояние переключателя после отказа: свежее чтение > то, что сообщил
    /// backend > отмена желаемого значения.
    func actualEnabled(desired: Bool, reread: Bool?) -> Bool {
        switch self {
        case .applied:
            return desired
        case .keychainUnavailable(let enabled), .failed(let enabled):
            return reread ?? enabled ?? !desired
        }
    }
}
