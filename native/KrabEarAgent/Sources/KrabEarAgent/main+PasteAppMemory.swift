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

    /// Запрашивает профиль вставки для указанного приложения.
    /// Возвращает nil если backend не вернул профиль или feature выключена.
    func fetchPasteProfileForApp(bundleId: String) -> String? {
        guard !bundleId.isEmpty else { return nil }
        guard let response = try? ipcClient.call(
            method: "get_paste_profile_for_app",
            params: ["bundle_id": bundleId]
        ),
        let result = response["result"] as? [String: Any],
        let profile = result["profile"] as? String
        else { return nil }
        return profile
    }

    /// Записывает ассоциацию bundle_id → profile в backend.
    /// Вызывается когда пользователь вручную выбирает профиль вставки.
    func recordPasteProfileForApp(bundleId: String, profile: String) {
        guard !bundleId.isEmpty, !profile.isEmpty else { return }
        _ = try? ipcClient.call(
            method: "record_paste_app_profile",
            params: ["bundle_id": bundleId, "profile": profile]
        )
        logger.info("PasteAppMemory: записан профиль bundle=\(bundleId) → profile=\(profile)")
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
