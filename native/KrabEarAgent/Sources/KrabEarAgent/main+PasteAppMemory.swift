/*
 main+PasteAppMemory.swift
 AgentAppDelegate extension: per-app paste profile memory.

 При вставке определяет bundle_id активного приложения и запрашивает
 у backend профиль форматирования (markdown/plain/html/…).
 Если профиль найден — применяет его к тексту перед вставкой.
 Если пользователь вручную выбирает профиль — записывает ассоциацию bundle→profile.

 Wave 59 (AGENT-J): fetch использует quickTimeoutSec + in-memory cache
 чтобы не блокировать main thread на paste path при slow/dead backend.
 record вынесен в background queue.
*/

import AppKit
import Foundation
import ObjectiveC.runtime

// Cache живёт только в памяти agent'а — backend остаётся источником истины.
// Cleared при restart. Synchronization через main thread (paste flow весь на main).
private nonisolated(unsafe) var pasteProfileCacheKey: UInt8 = 0

extension AgentAppDelegate {

    private var pasteProfileCache: NSMutableDictionary {
        if let existing = objc_getAssociatedObject(self, &pasteProfileCacheKey) as? NSMutableDictionary {
            return existing
        }
        let dict = NSMutableDictionary()
        objc_setAssociatedObject(self, &pasteProfileCacheKey, dict, .OBJC_ASSOCIATION_RETAIN)
        return dict
    }

    // MARK: - Per-app paste profile

    /// Запрашивает профиль вставки для указанного приложения.
    /// Возвращает nil если backend не вернул профиль, кэш пуст и feature выключена.
    ///
    /// Wave 59 (AGENT-J): использует in-memory cache + quickTimeoutSec (5s) чтобы
    /// не блокировать main thread на paste path. При cache miss и slow backend —
    /// graceful degradation: возвращаем nil немедленно, профиль применится в
    /// следующий paste после async prefetch.
    func fetchPasteProfileForApp(bundleId: String) -> String? {
        guard !bundleId.isEmpty else { return nil }

        // Cache hit — мгновенный возврат, ноль IPC.
        // NSNull = sentinel для "backend ответил nil/no profile" (отрицательный кэш).
        if let cached = pasteProfileCache[bundleId] {
            if cached is NSNull { return nil }
            return cached as? String
        }

        // Cache miss — синхронный fetch с quickTimeout, чтобы при backend hang
        // не зависнуть на defaultTimeoutSec (30s). 5s достаточно для здорового backend.
        guard let response = try? ipcClient.call(
            method: "get_paste_profile_for_app",
            params: ["bundle_id": bundleId],
            timeoutSec: IPCClient.quickTimeoutSec
        ),
        let result = response["result"] as? [String: Any] else {
            // Backend не ответил за 5s — НЕ кэшируем (попробуем в следующий paste).
            return nil
        }

        let profile = result["profile"] as? String
        // Кэшируем оба случая: nil (NSNull) и присутствующий профиль.
        pasteProfileCache[bundleId] = profile ?? NSNull()
        return profile
    }

    /// Записывает ассоциацию bundle_id → profile в backend.
    /// Вызывается когда пользователь вручную выбирает профиль вставки.
    ///
    /// Wave 59: вызов IPC ушёл в utility queue — paste path остаётся отзывчивым
    /// даже при slow backend. Cache обновляется на main thread сразу.
    func recordPasteProfileForApp(bundleId: String, profile: String) {
        guard !bundleId.isEmpty, !profile.isEmpty else { return }

        // Сразу обновляем in-memory cache — следующий fetch вернёт новое значение.
        pasteProfileCache[bundleId] = profile

        let client = ipcClient
        let loggerRef = logger
        DispatchQueue.global(qos: .utility).async {
            _ = try? client.call(
                method: "record_paste_app_profile",
                params: ["bundle_id": bundleId, "profile": profile]
            )
            loggerRef.info("PasteAppMemory: записан профиль bundle=\(bundleId) → profile=\(profile)")
        }
    }

    /// Применяет профиль вставки к тексту если профиль определён для активного приложения.
    /// Возвращает (обработанный текст, профиль) или (оригинальный текст, nil).
    func applyPasteProfileIfNeeded(text: String, targetApp: NSRunningApplication) -> (String, String?) {
        guard let bundleId = targetApp.bundleIdentifier, !bundleId.isEmpty else {
            return (text, nil)
        }

        guard let profile = fetchPasteProfileForApp(bundleId: bundleId) else {
            return (text, nil)
        }

        let formatted = formatTextForProfile(text: text, profile: profile)
        logger.info("PasteAppMemory: применён профиль \(profile) для \(bundleId)")
        return (formatted, profile)
    }

    /// Форматирует текст согласно профилю вставки.
    private func formatTextForProfile(text: String, profile: String) -> String {
        switch profile {
        case "plain":
            return normalizePlainText(text)
        case "markdown":
            // Markdown — текст передаётся как есть, форматирование на стороне backend
            return text
        case "html":
            return text
        case "telegram":
            // Telegram: убираем лишние переносы, оставляем одиночные
            return text
                .replacingOccurrences(of: "\r\n", with: "\n")
                .replacingOccurrences(of: "\r", with: "\n")
        case "email":
            return text
        case "notes":
            return text
        default:
            return text
        }
    }
}
