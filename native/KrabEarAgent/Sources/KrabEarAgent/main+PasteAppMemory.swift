/*
 main+PasteAppMemory.swift
 AgentAppDelegate extension: per-app paste profile memory.

 При вставке определяет bundle_id активного приложения и запрашивает
 у backend профиль форматирования (markdown/plain/html/…).
 Если профиль найден — применяет его к тексту перед вставкой.
 Если пользователь вручную выбирает профиль — записывает ассоциацию bundle→profile.
*/

import AppKit
import Foundation

extension AgentAppDelegate {

    // MARK: - Per-app paste profile

    // Shared reader-writer cache: bundleId → profile string.
    // Extracted into PasteProfileCache for unit-testability (Wave 265).
    // Populated by background prefetch; stale entries survive for the process lifetime
    // (profiles rarely change mid-session).
    static let pasteProfileCache = PasteProfileCache()

    private func cachedPasteProfile(for bundleId: String) -> String? {
        Self.pasteProfileCache.get(bundleId)
    }

    private func storeCachedPasteProfile(_ profile: String, for bundleId: String) {
        Self.pasteProfileCache.set(profile, for: bundleId)
    }

    /// Запрашивает профиль вставки для указанного приложения.
    /// При cache-hit — возвращает немедленно (zero latency).
    /// При cache-miss — возвращает nil сразу и запускает background prefetch,
    /// который заполнит кеш для следующей вставки. Graceful degrade: нет AppHang.
    func fetchPasteProfileForApp(bundleId: String) -> String? {
        guard !bundleId.isEmpty else { return nil }

        // Fast path: cache hit — no IPC, no latency.
        if let cached = cachedPasteProfile(for: bundleId) {
            return cached
        }

        // Slow path: cache miss — schedule background prefetch, return nil now.
        let endpoint = ipcClient.endpoint
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let client = IPCClient(socketPath: endpoint)
            guard
                let response = try? client.call(
                    method: "get_paste_profile_for_app",
                    params: ["bundle_id": bundleId]),
                let result = response["result"] as? [String: Any],
                let profile = result["profile"] as? String
            else { return }
            DispatchQueue.main.async { [weak self] in
                self?.storeCachedPasteProfile(profile, for: bundleId)
            }
        }
        return nil
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
