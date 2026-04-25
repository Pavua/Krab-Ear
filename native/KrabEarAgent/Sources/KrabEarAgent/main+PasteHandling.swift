/*
 main+PasteHandling.swift
 AgentAppDelegate extension: transcription result handling, paste to target app, clipboard, history item creation.
*/

import AppKit
import Foundation

extension AgentAppDelegate {

    // MARK: - Transcription result & paste

    func handleTranscriptionResult(text: String, historyId: String?) {
        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanText.isEmpty else {
            logger.warn("handleTranscriptionResult получил пустой текст")
            notify(title: "Krab Ear", body: "Пустой текст после транскрибации")
            return
        }
        let effectiveHistoryId = ensureHistoryItem(text: cleanText, existingId: historyId)
        if lastResult == nil || lastResult?.finalText != cleanText {
            lastResult = LastTranscriptionSnapshot(
                finalText: cleanText,
                originalText: cleanText,
                translatedText: "",
                historyId: effectiveHistoryId,
                translationMode: "off",
                translationStatus: "not_requested"
            )
        }
        logger.info("Транскрибация готова: len=\(cleanText.count), history_id=\(effectiveHistoryId ?? "nil")")

        // Защита от случайной повторной автовставки одного и того же результата.
        if isDuplicateAutopasteCandidate(historyId: effectiveHistoryId, text: cleanText) {
            logger.warn("Пропущена дублирующая автовставка: history_id=\(effectiveHistoryId ?? "nil")")
            return
        }

        if settings.clipboardMode == "always_copy" {
            pasteService.putToClipboard(cleanText)
        }

        guard settings.autoPaste else {
            markPasteStatus(historyId: effectiveHistoryId, status: "failed")
            historyPanel?.onHistoryDidUpdate()
            logger.info("Автовставка выключена, текст сохранён в истории")
            if settings.clipboardMode == "always_copy" {
                notify(title: "Krab Ear", body: "Текст скопирован в буфер обмена")
            } else {
                notify(title: "Krab Ear", body: "Текст сохранён в истории")
            }
            return
        }

        guard let targetApp = resolvePreferredPasteTargetApp() else {
            markPasteStatus(historyId: effectiveHistoryId, status: "failed")
            historyPanel?.onHistoryDidUpdate()
            logger.warn("Не найден target app для вставки")
            handlePasteFailure(reason: "no_external_target")
            return
        }
        let targetPID = activateTargetForPaste(targetApp)

        // Применяем per-app профиль вставки если настроен
        let (textToInsert, appliedProfile) = applyPasteProfileIfNeeded(text: cleanText, targetApp: targetApp)
        if let prof = appliedProfile {
            logger.info("PasteAppMemory: применён профиль '\(prof)' для \(targetApp.bundleIdentifier ?? "unknown")")
        }

        let pasteResult = pasteService.pasteToFrontmostApp(textToInsert, targetPID: targetPID)
        logger.info(
            "Попытка вставки: bundle=\(targetApp.bundleIdentifier ?? "unknown"), pid=\(targetPID), ok=\(pasteResult.ok), reason=\(pasteResult.reason)"
        )
        markPasteStatus(historyId: effectiveHistoryId, status: pasteResult.ok ? "ok" : "failed")
        historyPanel?.onHistoryDidUpdate()

        if !pasteResult.ok {
            handlePasteFailure(reason: pasteResult.reason, text: cleanText)
        }

        // Звук завершения транскрибации.
        NSSound(named: "Purr")?.play()
    }

    func isDuplicateAutopasteCandidate(historyId: String?, text: String) -> Bool {
        let now = Date().timeIntervalSince1970
        let normalizedText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let key = historyId?.isEmpty == false ? "id:\(historyId!)" : "text:\(normalizedText)"
        if let previous = recentAutoPasteFingerprints[key], (now - previous) < 4.0 {
            return true
        }
        recentAutoPasteFingerprints[key] = now

        // Периодически чистим старые отпечатки, чтобы структура не росла бесконечно.
        if recentAutoPasteFingerprints.count > 120 {
            let cutoff = now - 120.0
            recentAutoPasteFingerprints = recentAutoPasteFingerprints.filter { $0.value >= cutoff }
        }
        return false
    }

    func pasteSnapshotText(text: String, historyId: String?, sourceTag: String) {
        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanText.isEmpty else {
            notify(title: "Krab Ear", body: "Нечего вставлять: текст пустой")
            return
        }

        pasteService.putToClipboard(cleanText)
        guard let targetApp = resolvePreferredPasteTargetApp() else {
            markPasteStatus(historyId: historyId, status: "failed")
            historyPanel?.onHistoryDidUpdate()
            logger.warn("Быстрая вставка \(sourceTag): target app не найден")
            handlePasteFailure(reason: "no_external_target")
            return
        }

        let targetPID = activateTargetForPaste(targetApp)

        // Применяем per-app профиль вставки если настроен
        let (textToInsert, _) = applyPasteProfileIfNeeded(text: cleanText, targetApp: targetApp)

        let pasteResult = pasteService.pasteToFrontmostApp(textToInsert, targetPID: targetPID)
        logger.info(
            "Быстрая вставка \(sourceTag): bundle=\(targetApp.bundleIdentifier ?? "unknown"), pid=\(targetPID), ok=\(pasteResult.ok), reason=\(pasteResult.reason)"
        )
        markPasteStatus(historyId: historyId, status: pasteResult.ok ? "ok" : "failed")
        historyPanel?.onHistoryDidUpdate()
        if !pasteResult.ok {
            handlePasteFailure(reason: pasteResult.reason)
        }
    }

    func handlePasteFailure(reason: String, text: String? = nil) {
        let details: String
        switch reason {
        case "accessibility_not_granted":
            details = "Не выдан доступ Accessibility. Откройте: Системные настройки -> Конфиденциальность и безопасность -> Accessibility."
            if hasShownAccessibilityHint {
                logger.warn("Повторная ошибка accessibility_not_granted подавлена")
                return
            }
            hasShownAccessibilityHint = true
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
                NSWorkspace.shared.open(url)
            }
        case "no_editable_focus":
            details = "Активное окно найдено, но текстовое поле не в фокусе."
        case "no_external_target", "no_external_target_app":
            details = "Не найдено внешнее активное приложение для вставки."
        case "modifiers_stuck":
            details = "Клавиши-модификаторы не были отпущены вовремя."
        default:
            details = "Причина: \(reason)."
        }

        if let text, settings.clipboardMode != "never_copy" {
            pasteService.putToClipboard(text)
        }

        // Не выдёргиваем панель поверх всех окон при чисто permission-проблеме.
        if reason != "accessibility_not_granted" {
            if settings.mode != "menubar" {
                applyMode("menubar", persist: true)
            }
            openHistoryPanel(forceMenubar: false)
        }
        logger.warn("Вставка не удалась: \(details)")
        notify(
            title: "Krab Ear",
            body: "Вставка не удалась. \(details) Текст сохранён в истории и буфере обмена."
        )
    }

    func markPasteStatus(historyId: String?, status: String) {
        guard let historyId else { return }
        logger.info("Обновление paste_status: history_id=\(historyId), status=\(status)")
        _ = try? ipcClient.call(
            method: "set_paste_status",
            params: [
                "id": historyId,
                "paste_status": status,
            ]
        )
    }

    func captureRecordingTargetApp() {
        if let frontmost = NSWorkspace.shared.frontmostApplication,
           frontmost.processIdentifier != ProcessInfo.processInfo.processIdentifier,
           frontmost.activationPolicy == .regular {
            recordingTargetApp = frontmost
            lastExternalApp = frontmost
            logger.info("Запомнен target app на старте записи: \(frontmost.bundleIdentifier ?? "unknown")")
            return
        }
        recordingTargetApp = lastExternalApp
        if let lastExternalApp {
            logger.info("Использован fallback target app: \(lastExternalApp.bundleIdentifier ?? "unknown")")
        } else {
            logger.warn("Не удалось определить target app на старте записи")
        }
    }

    func resolvePreferredPasteTargetApp() -> NSRunningApplication? {
        let selfPID = ProcessInfo.processInfo.processIdentifier
        if let current = NSWorkspace.shared.frontmostApplication,
           current.processIdentifier != selfPID,
           current.activationPolicy == .regular {
            return current
        }
        if let recordingTargetApp, !recordingTargetApp.isTerminated {
            return recordingTargetApp
        }
        if let lastExternalApp, !lastExternalApp.isTerminated {
            return lastExternalApp
        }
        return nil
    }

    func activateTargetForPaste(_ target: NSRunningApplication) -> pid_t {
        let currentPID = NSWorkspace.shared.frontmostApplication?.processIdentifier
        if currentPID != target.processIdentifier {
            logger.info("Активация target app перед вставкой: \(target.bundleIdentifier ?? "unknown")")
            _ = target.activate(options: [.activateIgnoringOtherApps])

            var attempts = 0
            while attempts < 10 {
                let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier
                if pid == target.processIdentifier {
                    break
                }
                usleep(40_000)
                attempts += 1
            }
        }
        return target.processIdentifier
    }

    func ensureHistoryItem(text: String, existingId: String?) -> String? {
        if let existingId, !existingId.isEmpty {
            return existingId
        }
        guard
            let response = try? ipcClient.call(
                method: "add_history_item",
                params: [
                    "text": text,
                    "paste_status": "failed",
                ]
            ),
            let result = response["result"] as? [String: Any]
        else {
            logger.warn("Не удалось создать fallback запись в истории")
            return nil
        }
        logger.info("Создана fallback запись истории: id=\(result["id"] as? String ?? "nil")")
        return result["id"] as? String
    }


    // MARK: - Quick replay (Cmd+Option+V)

    func handleQuickReplayPaste() {
        let result = pasteService.repastLast()
        switch result.reason {
        case "no_last_paste":
            notify(title: "Krab Ear", body: "Нет сохранённой вставки для повтора")
            logger.info("Quick replay: нет сохранённого текста")
        case "repaste_too_soon":
            notify(title: "Krab Ear", body: "Слишком быстро — подождите секунду")
            logger.info("Quick replay: повтор слишком быстрый (cooldown)")
        default:
            if result.ok {
                logger.info("Quick replay: успешно вставлен текст (reason=\(result.reason))")
                NSSound(named: "Purr")?.play()
            } else {
                handlePasteFailure(reason: result.reason)
            }
        }
    }

    func normalizePlainText(_ text: String) -> String {
        // Plain режим: убираем табы/лишние переносы и схлопываем повторяющиеся пробелы.
        let replaced = text
            .replacingOccurrences(of: "\t", with: " ")
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        let lines = replaced
            .split(separator: "\n")
            .map { raw in
                raw
                    .split(whereSeparator: { $0 == " " || $0 == "\t" })
                    .joined(separator: " ")
            }
            .filter { !$0.isEmpty }
        return lines.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
