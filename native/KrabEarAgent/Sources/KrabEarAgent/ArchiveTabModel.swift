import Foundation

/*
 Модель вкладки «Архив»: разбор ответов backend и сверка предупреждений. Без UI и IPC — чисто и тестируемо.

 Карта ревью dots, 26.11: итог восстановления привязан к ID записей (а не к позиции строки или счётчику);
 старый или отсутствующий ответ не рисуется как успех; предупреждение закрывается только после сверки
 со свежим списком архива. Контракт backend — IPC_API_REFERENCE (`list_archived`, `unarchive_items`).
*/

struct ArchivedEntry: Equatable {
    let id: String
    let text: String
    let date: Date
    let sizeBytes: Int
}

enum ArchiveListOutcome: Equatable {
    case items([ArchivedEntry])
    /// Backend отказал по приватному режиму: список не показываем и не считаем «пустым».
    case privacyBlocked
    /// Ответа нет или формат чужой: прежний список не трогаем, «Архив пуст» не рисуем.
    case unavailable
}

enum ArchiveListDecoder {
    static func decode(_ response: [String: Any]?) -> ArchiveListOutcome {
        guard let result = response?["result"] as? [String: Any] else { return .unavailable }
        if (result["reason"] as? String) == "privacy_mode_active" { return .privacyBlocked }
        if (result["ok"] as? Bool) == false { return .unavailable }
        guard let raw = result["items"] as? [[String: Any]] else { return .unavailable }
        var seen = Set<String>()
        var entries: [ArchivedEntry] = []
        for item in raw {
            guard let id = item["id"] as? String, !id.isEmpty, seen.insert(id).inserted else { continue }
            let text = (item["text"] as? String) ?? ""
            let stamp = (item["archived_at"] as? String) ?? (item["ts"] as? String) ?? ""
            entries.append(ArchivedEntry(id: id, text: text, date: parseDate(stamp) ?? .distantPast,
                                         sizeBytes: text.utf8.count))
        }
        return .items(entries)
    }

    /// ISO-8601 с часовым поясом или без него (backend пишет и так и так).
    static func parseDate(_ string: String) -> Date? {
        guard !string.isEmpty else { return nil }
        let withZone = ISO8601DateFormatter()
        if let date = withZone.date(from: string) { return date }
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFraction.date(from: string) { return date }
        let local = DateFormatter()
        local.locale = Locale(identifier: "en_US_POSIX")
        local.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return local.date(from: string)
    }
}

/// Итог `unarchive_items` по ID записей архива.
enum ArchiveRestoreOutcome: Equatable {
    /// Все запрошенные записи восстановлены.
    case complete(restored: [String])
    /// Часть записей не восстановлена: `failed` — сбой (остались в архиве), `notFound` — не найдены в архиве,
    /// `unaccounted` — ответ их не упомянул (считаем НЕ восстановленными).
    case partial(restored: [String], failed: [String], notFound: [String], unaccounted: [String])
    /// Backend отказал целиком (`ok: false`: приватный режим, шифрование, слишком много id…).
    case rejected(reason: String)
    /// Ответа нет или он старого/чужого формата (нет `restored_ids`/`failed_ids`): успехом не считаем.
    case unknown
}

enum ArchiveRestoreDecoder {
    static func decode(_ response: [String: Any]?, requested: [String]) -> ArchiveRestoreOutcome {
        guard let result = response?["result"] as? [String: Any] else { return .unknown }
        if (result["ok"] as? Bool) == false {
            return .rejected(reason: (result["reason"] as? String) ?? "unknown")
        }
        guard let restored = result["restored_ids"] as? [String],
              let failed = result["failed_ids"] as? [String] else { return .unknown }
        let notFound = (result["not_found"] as? [String]) ?? []
        let requestedSet = Set(requested)
        let accounted = Set(restored).union(failed).union(notFound)
        let unaccounted = requested.filter { !accounted.contains($0) }
        // Восстановленным считаем только то, что реально запрашивали: чужие id из ответа игнорируем.
        let restoredOurs = restored.filter { requestedSet.contains($0) }
        if failed.isEmpty, notFound.isEmpty, unaccounted.isEmpty, Set(restoredOurs) == requestedSet {
            return .complete(restored: restoredOurs)
        }
        return .partial(restored: restoredOurs, failed: failed, notFound: notFound, unaccounted: unaccounted)
    }
}

/// Предупреждения по ID: закрываются только сверкой со свежим списком архива.
struct ArchiveWarningLedger: Equatable {
    /// Записи, которые пользователь пытался восстановить, но они по ответу остались в архиве.
    private(set) var unresolved: Set<String> = []
    /// Записи, о восстановлении которых сказал backend: проверяем, что их и правда нет в архиве.
    private var claimedRestored: Set<String> = []

    var isClear: Bool { unresolved.isEmpty }

    mutating func record(_ outcome: ArchiveRestoreOutcome) {
        switch outcome {
        case .complete(let restored):
            claimedRestored.formUnion(restored)
        case .partial(let restored, let failed, let notFound, let unaccounted):
            claimedRestored.formUnion(restored)
            unresolved.formUnion(failed)
            unresolved.formUnion(notFound)
            unresolved.formUnion(unaccounted)
        case .rejected, .unknown:
            break  // сообщение показывает UI; id не помечаем «восстановленными»
        }
    }

    /// Сверка со свежим списком архива: предупреждение остаётся для записей, которые всё ещё в архиве;
    /// «восстановленная» запись, оставшаяся в архиве, — расхождение, и тоже предупреждает.
    mutating func reconcile(archiveIDs: Set<String>) {
        let discrepancies = claimedRestored.intersection(archiveIDs)
        claimedRestored.removeAll()
        unresolved.formIntersection(archiveIDs)
        unresolved.formUnion(discrepancies)
    }

    mutating func reset() {
        unresolved.removeAll()
        claimedRestored.removeAll()
    }
}
