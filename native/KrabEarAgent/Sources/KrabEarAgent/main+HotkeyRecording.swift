/*
 main+HotkeyRecording.swift
 AgentAppDelegate extension: hotkey toggle handler, start/stop recording, state sync with backend.
*/

import AppKit
import Foundation

extension AgentAppDelegate {

    // MARK: - Hotkey recording

    func handleRecordToggleRequest() {
        // C3a: во время быстрой заметки диктовка отвергается (взаимное
        // исключение — спека 2026-07-16-c3-quick-capture-design.md §2a).
        if quickCaptureActive {
            BackendToast.shared.show("Идёт быстрая заметка — сначала завершите её")
            return
        }
        if isProcessing {
            return
        }

        let now = Date().timeIntervalSince1970
        if now - lastToggleRequestAt < toggleDebounceSec {
            logger.warn("Игнорирую повторный toggle (debounce)")
            return
        }
        lastToggleRequestAt = now

        // Dispatch IPC work off the main thread to prevent the 2000ms+ main-thread
        // hang (Sentry KRAB-EAR-AGENT-3) caused by synchronous `callWithRecovery`
        // blocking the main runloop. UI state reads happen before the hop;
        // stopRecording/startRecording update UI state via @MainActor methods inside.
        let wasRecordingLocally = isRecording
        Task.detached { [weak self] in
            await self?.performRecordToggle(wasRecordingLocally: wasRecordingLocally)
        }
    }

    /// Выполнить логику toggle записи вне главного потока.
    /// Все обращения к @MainActor-изолированным свойствам сделаны через явные `await MainActor.run`.
    func performRecordToggle(wasRecordingLocally: Bool) async {
        let backendRecording = syncRecordingStateWithBackend()
        if backendRecording != wasRecordingLocally {
            logger.warn("Десинхрон состояния записи: local=\(wasRecordingLocally), backend=\(backendRecording)")
        }

        // Если локально считалось, что пишем, но backend уже idle — не стартуем новую
        // запись этим же нажатием. Сначала фиксируем состояние, следующий toggle начнёт запись явно.
        if wasRecordingLocally && !backendRecording {
            await MainActor.run {
                self.notify(
                    title: "Krab Ear",
                    body: "Запись уже остановлена в backend. Состояние синхронизировано."
                )
            }
            return
        }

        // Если backend пишет, а локально флаг был сбит, сначала корректно завершаем зависшую запись.
        if !wasRecordingLocally && backendRecording {
            await MainActor.run {
                self.notify(
                    title: "Krab Ear",
                    body: "Найден рассинхрон записи. Сначала завершаю зависшую сессию."
                )
            }
            stopRecording()
            return
        }

        if wasRecordingLocally {
            stopRecording()
        } else {
            startRecording()
        }
    }

    func syncRecordingStateWithBackend() -> Bool {
        guard
            let stateResponse = try? callWithRecovery(method: "get_recording_state", params: [:]),
            let state = stateResponse["result"] as? [String: Any]
        else {
            return isRecording
        }

        let backendRecording = (state["is_recording"] as? Bool) ?? false
        if backendRecording != isRecording {
            isRecording = backendRecording
            refreshStatusItemTitle()
            rebuildStatusMenu()
        }
        return backendRecording
    }

    func startRecording() {
        captureRecordingTargetApp()
        let targetBundle = recordingTargetApp?.bundleIdentifier ?? "nil"
        logger.info("Старт записи. targetApp=\(targetBundle)")
        do {
            // Сначала приглушаем системный звук, чтобы в запись не попадали внешние звуки.
            // В режиме mic принудительно используем mute (100), иначе даже 25%/50%
            // может физически пробиваться в микрофон и давать ложную транскрипцию.
            let effectiveDuckingPercent: Int
            if settings.captureSourceMode == "mic" {
                effectiveDuckingPercent = 100
            } else {
                effectiveDuckingPercent = settings.audioDuckingPercent
            }
            audioDuckingService.duckForRecording(
                enabled: settings.audioDuckingEnabled,
                duckPercent: effectiveDuckingPercent
            )
            let response = try callWithRecovery(method: "start_recording", params: [:])
            let result = response["result"] as? [String: Any]
            let status = (result?["status"] as? String) ?? "recording"
            if status == "already_recording" {
                logger.warn("Backend вернул already_recording на start_recording")
                isRecording = true
                startRealtimeOverlayPolling()
                refreshStatusItemTitle()
                rebuildStatusMenu()
                // Это штатная идемпотентная синхронизация, не показываем шумный алерт.
                return
            }
            if status != "recording" {
                throw NSError(
                    domain: "KrabEarAgent",
                    code: -1,
                    userInfo: [NSLocalizedDescriptionKey: "Неожиданный статус backend: \(status)"]
                )
            }
            isRecording = true
            lastPreviewTranslationSource = ""
            lastPreviewTranslationText = ""
            lastPreviewTranslationAt = 0
            startRealtimeOverlayPolling()
            playStartSoundIfEnabled()
            refreshStatusItemTitle()
            rebuildStatusMenu()
        } catch {
            logger.error("Ошибка start_recording: \(error.localizedDescription)")
            notify(
                title: "Krab Ear",
                body: "Не удалось начать запись: \(error.localizedDescription)"
            )
        }
    }

    func stopRecording() {
        logger.info("Остановка записи запрошена")
        stopRealtimeOverlayPolling()
        isRecording = false
        isProcessing = true
        refreshStatusItemTitle()
        rebuildStatusMenu()

        defer {
            // Важно: восстанавливаем системный звук только после завершения stop-пайплайна,
            // чтобы хвост фонового аудио не попадал в запись при отпускании hotkey.
            audioDuckingService.restoreAfterRecording()
            isProcessing = false
            refreshStatusItemTitle()
            rebuildStatusMenu()
            recordingTargetApp = nil
        }

        do {
            let response = try callWithRecovery(
                method: "stop_recording",
                params: [
                    "quality_profile": settings.qualityProfile,
                    "cleanup_profile": settings.cleanupProfile,
                    "translation_mode": settings.translationMode,
                    "translation_style": settings.translationStyle,
                    "translate_and_paste": settings.translateAndPaste,
                ]
            )

            guard let result = response["result"] as? [String: Any] else {
                notify(title: "Krab Ear", body: "Backend вернул пустой ответ")
                return
            }

            let status = (result["status"] as? String) ?? "unknown"
            let historyId = result["history_id"] as? String
            let text = (result["text"] as? String) ?? ""
            let originalText = (result["original_text"] as? String) ?? text
            let translatedText = (result["translated_text"] as? String) ?? ""
            let translationMode = (result["translation_mode"] as? String) ?? "off"
            let translationStatus = (result["translation_status"] as? String) ?? "not_requested"
            let translateAndPaste = (result["translate_and_paste"] as? Bool) ?? false
            logger.info("Ответ stop_recording: status=\(status), text_len=\(text.count), history_id=\(historyId ?? "nil")")

            switch status {
            case "ok":
                if translationMode != "off" && translationStatus != "ok" && translateAndPaste {
                    notify(
                        title: "Krab Ear",
                        body: "Перевод сейчас недоступен (\(translationStatus)). Вставлен оригинальный текст."
                    )
                }
                lastResult = LastTranscriptionSnapshot(
                    finalText: text,
                    originalText: originalText,
                    translatedText: translatedText,
                    historyId: historyId,
                    translationMode: translationMode,
                    translationStatus: translationStatus
                )
                handleTranscriptionResult(text: text, historyId: historyId)
                // Обновляем индикатор STT движка после успешной транскрибации.
                historyPanel?.fetchAndUpdateSTTEngineLabel()
            case "already_stopped":
                // Идемпотентный stop: backend уже в idle, лишние уведомления пользователю не нужны.
                logger.info("stop_recording: backend уже idle (already_stopped), синхронизирую состояние")
                _ = syncRecordingStateWithBackend()
            case "empty_audio":
                logger.warn("stop_recording вернул empty_audio")
                notify(title: "Krab Ear", body: "Аудио пустое, попробуйте ещё раз")
            case "empty_text":
                logger.warn("stop_recording вернул empty_text")
                recoverFromPreviewFallback(reason: "Финальная транскрибация пустая") { recovered in
                    if !recovered {
                        self.notify(title: "Krab Ear", body: "Речь не распознана")
                    }
                }
            default:
                logger.warn("stop_recording вернул неожиданный статус: \(status)")
                recoverFromPreviewFallback(reason: "Неожиданный статус stop: \(status)") { recovered in
                    if !recovered {
                        self.notify(title: "Krab Ear", body: "Неожиданный статус: \(status)")
                    }
                }
            }
        } catch {
            logger.error("Ошибка stop_recording: \(error.localizedDescription)")
            if isBackendNotRecordingError(error) {
                logger.warn("stop_recording: backend уже в idle, fallback не нужен")
                _ = syncRecordingStateWithBackend()
                return
            }
            let errorDescription = error.localizedDescription
            recoverFromPreviewFallback(reason: "Ошибка stop_recording: \(errorDescription)") { recovered in
                if !recovered {
                    self.notify(
                        title: "Krab Ear",
                        body: "Не удалось завершить запись: \(errorDescription)"
                    )
                }
            }
        }
    }
}
