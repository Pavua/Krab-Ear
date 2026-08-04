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

        // Фиксируем адресата вставки СЕЙЧАС как ЛОКАЛЬНОЕ значение, пока terminal
        // cleanup ещё не обнулил recordingTargetApp (main+HotkeyRecording.swift чистит
        // его сразу после этого вызова) и пока сменившийся frontmost не перебил выбор.
        // Инцидент 2026-08-03: за 38 с финализации активным стал Safari, и диктовка из
        // Claude Desktop улетела туда. Намеренно БЕЗ `?? lastExternalApp`: то поле
        // переписывается на каждую активацию внешнего приложения, поэтому «последнее
        // внешнее» к этому моменту означает «куда пользователь ушёл», а не «откуда
        // диктовал». Когда запомненной цели нет, пусть решает политика в момент вставки.
        //
        // Fable-ревью M1 (2026-08-03): ЛОКАЛЬНОЕ значение, а НЕ поле делегата — поле было
        // ЕДИНЫМ на все диктовки, и пока QuickEdit-оверлей диктовки A ждёт пользователя
        // (isProcessing уже снят к этому моменту — main+HotkeyRecording.swift, ничто не
        // мешает начать диктовку B), захват B перезаписывал общее поле раньше, чем A
        // успевала его прочитать в performAutoPaste. Параметр, прокинутый через
        // continueTranscriptionResult и QuickEdit-замыкания, делает каждую диктовку
        // независимой — общего изменяемого состояния между двумя одновременными
        // диктовками больше нет.
        let pasteTarget = recordingTargetApp

        // Финализируем streaming-paste сессию АВТОРИТЕТНЫМ текстом из ответа IPC (не через
        // SSE realtime.final_transcript — тот структурно недостижим здесь: SSE уже закрыт
        // к моменту, когда backend успевает его эмиттировать внутри stop_recording; см.
        // doc-комментарий StreamingPasteController.swift). Коммитит хвост, накопленный за
        // время записи, и выставляет didStreamThisRecording — performAutoPaste читает его
        // ниже по цепочке (continueTranscriptionResult → performAutoPaste).
        if settings.streamingPasteEnabled {
            streamingPasteController?.handleFinal(cleanText)
        }

        // Resolve history_id асинхронно — раньше тут sync IPC на main thread с 5s timeout
        // вешал AppHang (Sentry KRAB-EAR-AGENT-8). Continuation запускает полный paste flow
        // только после того, как IPC ответил (или не ответил → id == nil, fallback path).
        ensureHistoryItem(text: cleanText, existingId: historyId) { [weak self] effectiveHistoryId in
            self?.continueTranscriptionResult(
                cleanText: cleanText,
                historyId: effectiveHistoryId,
                pasteTarget: pasteTarget
            )
        }
    }

    /// Continuation для `handleTranscriptionResult` после async-resolve history_id.
    /// Вызывается на main thread (см. `ensureHistoryItem` completion dispatch).
    ///
    /// `pasteTarget` — снэпшот адресата, зафиксированный в `handleTranscriptionResult`
    /// ДО этого вызова; течёт параметром, а не полем делегата (Fable-ревью M1,
    /// 2026-08-03) — см. комментарий на месте захвата.
    func continueTranscriptionResult(
        cleanText: String,
        historyId effectiveHistoryId: String?,
        pasteTarget: NSRunningApplication?
    ) {
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

        // S34 / Fable-ревью F2: putToClipboard может тихо пропустить запись (буфер
        // защищён) — уведомление ниже должно отражать реальный исход, не лгать.
        var alwaysCopyWroteClipboard = false
        if settings.clipboardMode == "always_copy" {
            alwaysCopyWroteClipboard = pasteService.putToClipboard(cleanText)
        }

        guard settings.autoPaste else {
            markPasteStatus(historyId: effectiveHistoryId, status: "failed")
            historyPanel?.onHistoryDidUpdate()
            logger.info("Автовставка выключена, текст сохранён в истории")
            if settings.clipboardMode == "always_copy" {
                notify(title: "Krab Ear", body: alwaysCopyWroteClipboard
                    ? "Текст скопирован в буфер обмена"
                    : "Буфер обмена защищён — текст не скопирован, сохранён в истории")
            } else {
                notify(title: "Krab Ear", body: "Текст сохранён в истории")
            }
            return
        }

        // Quick Edit: показываем мини-оверлей для правки перед вставкой (если включено).
        if settings.quickEditEnabled {
            let ipc = self.ipcClient
            quickEditOverlay.show(
                text: cleanText,
                timeoutSec: settings.quickEditTimeoutSec,
                onAddToVocabulary: { word in
                    // AGENT-3: IPC строго off main thread.
                    DispatchQueue.global(qos: .utility).async {
                        _ = try? ipc.call(
                            method: "add_stt_hotword",
                            params: ["word": word],
                            timeoutSec: IPCClient.quickTimeoutSec
                        )
                    }
                },
                completion: { [weak self] result in
                    guard let self else { return }
                    switch result {
                    case .paste(let editedText):
                        self.logger.info("QuickEdit: пользователь отредактировал текст, paste")
                        self.performAutoPaste(text: editedText, historyId: effectiveHistoryId, target: pasteTarget)
                    case .cancel:
                        self.logger.info("QuickEdit: пользователь отменил вставку")
                        self.markPasteStatus(historyId: effectiveHistoryId, status: "failed")
                        self.historyPanel?.onHistoryDidUpdate()
                        self.notify(title: "Krab Ear", body: "Вставка отменена")
                    case .timeout(let originalText):
                        self.logger.info("QuickEdit: таймаут — вставляем исходный текст")
                        self.performAutoPaste(text: originalText, historyId: effectiveHistoryId, target: pasteTarget)
                    }
                }
            )
            return
        }

        performAutoPaste(text: cleanText, historyId: effectiveHistoryId, target: pasteTarget)
    }

    // MARK: - Core paste helper

    /// `target` — снэпшот адресата диктовки, зафиксированный в `handleTranscriptionResult`
    /// и прокинутый через `continueTranscriptionResult`/QuickEdit-замыкания. Параметр, а не
    /// поле делегата (Fable-ревью M1, 2026-08-03) — исключает переналожение двух
    /// одновременных диктовок на общем изменяемом состоянии.
    func performAutoPaste(text: String, historyId: String?, target: NSRunningApplication?) {
        // Если streaming paste уже вставил текст по мере диктовки — пропускаем финальную
        // полную вставку, чтобы не задваивать текст. Сбрасываем флаг после принятия решения.
        if settings.streamingPasteEnabled,
           let spc = streamingPasteController,
           spc.didStreamThisRecording
        {
            spc.resetAfterFinalPaste()
            markPasteStatus(historyId: historyId, status: "ok")
            historyPanel?.onHistoryDidUpdate()
            logger.info("[StreamingPaste] Финальная вставка пропущена — текст уже вставлен потоково")
            return
        }

        guard let targetApp = resolveDictationPasteTargetApp(captured: target) else {
            markPasteStatus(historyId: historyId, status: "failed")
            historyPanel?.onHistoryDidUpdate()
            logger.warn("Не найден target app для вставки")
            handlePasteFailure(reason: "no_external_target")
            return
        }
        let targetPID = activateTargetForPaste(targetApp)

        // Применяем per-app профиль вставки если настроен
        let (textToInsert, appliedProfile) = applyPasteProfileIfNeeded(text: text, targetApp: targetApp)
        if let prof = appliedProfile {
            logger.info("PasteAppMemory: применён профиль '\(prof)' для \(targetApp.bundleIdentifier ?? "unknown")")
        }

        let pasteResult = pasteService.pasteToFrontmostApp(textToInsert, targetPID: targetPID)
        logger.info(
            "Попытка вставки: bundle=\(targetApp.bundleIdentifier ?? "unknown"), pid=\(targetPID), ok=\(pasteResult.ok), reason=\(pasteResult.reason)"
        )
        markPasteStatus(historyId: historyId, status: pasteResult.ok ? "ok" : "failed")
        historyPanel?.onHistoryDidUpdate()

        if !pasteResult.ok {
            handlePasteFailure(reason: pasteResult.reason, text: text)
        }

        // Звук завершения транскрибации (off main thread: AudioQueueXPC.Start синхронный).
        DispatchQueue.global(qos: .userInitiated).async {
            NSSound(named: "Purr")?.play()
        }
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
        // Защищённое поле: тихое уведомление, НЕ ошибка — это ожидаемое поведение.
        if reason == "secure_field_skipped" {
            logger.info("[SmartPaste] Вставка в защищённое поле пропущена — показываем тихое уведомление")
            notify(title: "Krab Ear", body: "Вставка в защищённое поле пропущена")
            return
        }

        // S34 / Fable-ревью F1+F2: буфер обмена защищён (пароль/секрет) — pasteToFrontmostApp
        // уже отказался от синтетического Cmd+V. Тихое честное уведомление, БЕЗ повторного
        // putToClipboard(text) ниже — тот снова упрётся в тот же guard и снова соврёт про
        // «буфер обмена» в финальном notify.
        if reason == "concealed_clipboard_skipped" {
            logger.info("[Clipboard] Автовставка/копирование пропущены — буфер обмена защищён")
            notify(title: "Krab Ear",
                   body: "Буфер обмена защищён (пароль/секрет) — вставка и копирование пропущены, текст сохранён в истории")
            return
        }

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

        // S34 / Fable-ревью F2: putToClipboard(text) может тихо пропустить запись
        // (буфер защищён) — финальное уведомление не должно лгать про «буфер
        // обмена», если запись реально не прошла. Различаем «не пытались» (nil
        // text / never_copy — исходное, не тронутое этим диффом поведение) от
        // «пытались и пропустили» (новый concealed-guard).
        var attemptedClipboardWrite = false
        var clipboardCopied = false
        if let text, settings.clipboardMode != "never_copy" {
            attemptedClipboardWrite = true
            clipboardCopied = pasteService.putToClipboard(text)
        }

        // Не выдёргиваем панель поверх всех окон при чисто permission-проблеме.
        if reason != "accessibility_not_granted" {
            if settings.mode != "menubar" {
                applyMode("menubar", persist: true)
            }
            openHistoryPanel(forceMenubar: false)
        }
        logger.warn("Вставка не удалась: \(details)")
        let clipboardNote = (attemptedClipboardWrite && !clipboardCopied)
            ? "Текст сохранён в истории (буфер обмена защищён — не скопирован)."
            : "Текст сохранён в истории и буфере обмена."
        notify(
            title: "Krab Ear",
            body: "Вставка не удалась. \(details) \(clipboardNote)"
        )
    }

    func markPasteStatus(historyId: String?, status: String) {
        guard let historyId else { return }
        logger.info("Обновление paste_status: history_id=\(historyId), status=\(status)")
        // Fire-and-forget: вызывается из handleTranscriptionResult на main thread.
        // Без async wrap'а IPC socket read блокирует main → AppHang (>2s).
        // Closes Sentry KRAB-EAR-AGENT-8.
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .utility).async {
            _ = try? ipcClient.call(
                method: "set_paste_status",
                params: [
                    "id": historyId,
                    "paste_status": status,
                ],
                timeoutSec: IPCClient.quickTimeoutSec
            )
        }
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

    /// Адресат автовставки ДИКТОВКИ: приоритет у приложения, в котором запись
    /// началась (решение владельца 2026-08-03 — «куда начал, туда и придёт»).
    ///
    /// Отдельный резолвер, а не изменение общего: `pasteSnapshotText` обслуживает
    /// принципиально другой запрос — пользователь прямо сейчас указал, куда
    /// вставить, и там верным адресатом остаётся текущее активное приложение.
    /// Урок S34: одна общая точка вредит, когда у вызывающих сторон разные роли.
    func resolveDictationPasteTargetApp(captured: NSRunningApplication?) -> NSRunningApplication? {
        let selfPID = ProcessInfo.processInfo.processIdentifier
        let captured = captured.flatMap { $0.isTerminated ? nil : $0 }
        let frontmost = NSWorkspace.shared.frontmostApplication.flatMap {
            ($0.processIdentifier != selfPID && $0.activationPolicy == .regular) ? $0 : nil
        }
        let lastExternal = lastExternalApp.flatMap { $0.isTerminated ? nil : $0 }

        let choice = DictationPasteTargetPolicy.choose(
            capturedIsAlive: captured != nil,
            frontmostIsExternal: frontmost != nil,
            lastExternalIsAlive: lastExternal != nil
        )

        // Расхождение стоит видеть в логе: именно оно было невидимой причиной
        // «текст ушёл не туда» до 2026-08-03.
        if choice == .captured,
           let captured,
           let frontmost,
           frontmost.processIdentifier != captured.processIdentifier {
            logger.info(
                "Автовставка целится в приложение старта записи: \(captured.bundleIdentifier ?? "unknown") "
                    + "(активно сейчас другое: \(frontmost.bundleIdentifier ?? "unknown"))"
            )
        }

        switch choice {
        case .captured: return captured
        case .frontmost: return frontmost
        case .lastExternal: return lastExternal
        case .none: return nil
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

    /// Async resolve history_id: fast-path возвращает existingId сразу,
    /// иначе IPC `add_history_item` выполняется на background queue, completion
    /// дёргается на main thread. Раньше эта функция была sync с 5s timeout
    /// и вешала main thread → AppHang ≥ 2s (Sentry KRAB-EAR-AGENT-8).
    /// Closes Sentry KRAB-EAR-AGENT-8.
    func ensureHistoryItem(
        text: String,
        existingId: String?,
        completion: @escaping @MainActor @Sendable (String?) -> Void
    ) {
        if let existingId, !existingId.isEmpty {
            completion(existingId)
            return
        }
        let ipc = self.ipcClient
        let textCopy = text
        // Capture logger separately as a Sendable value to avoid crossing the
        // @MainActor isolation boundary inside Task.detached (Swift 6 Sendable
        // error: "capture of 'self' with non-sendable type in @Sendable closure").
        let log = self.logger
        Task.detached {
            let response = try? await ipc.callAsync(
                method: "add_history_item",
                params: [
                    "text": textCopy,
                    "paste_status": "failed",
                ],
                timeoutSec: IPCClient.quickTimeoutSec
            )
            let id = (response?["result"] as? [String: Any])?["id"] as? String
            await MainActor.run {
                if let id {
                    log.info("Создана fallback запись истории: id=\(id)")
                } else {
                    log.warn("Не удалось создать fallback запись в истории")
                }
                completion(id)
            }
        }
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
                DispatchQueue.global(qos: .userInitiated).async {
                    NSSound(named: "Purr")?.play()
                }
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
