/*
 main+HotkeyRecording.swift
 Расширение AgentAppDelegate: горячая клавиша, запуск/остановка записи
 и безопасная синхронизация владельца общего backend-рекордера.
*/

import AppKit
import Foundation

/// Чистая политика сопоставления общего backend-рекордера с hotkey-диктовкой.
/// Отсутствующий owner сохраняет legacy, а явный owner:null означает unmanaged.
enum HotkeyRecordingOwnershipPolicy {
    static func isForeignRecording(
        isRecording: Bool,
        owner: String?,
        ownerFieldPresent: Bool
    ) -> Bool {
        guard isRecording else { return false }
        if let owner {
            return owner != "dictation"
        }
        return ownerFieldPresent
    }

    static func representsLocalDictation(
        isRecording: Bool,
        owner: String?,
        ownerFieldPresent: Bool
    ) -> Bool {
        guard isRecording else { return false }
        if let owner {
            return owner == "dictation"
        }
        return !ownerFieldPresent
    }
}

/// Сериализует физический start, включая IPC и владение audio-ducking snapshot.
/// NSLock нужен потому, что toggle стартует из detached task, а hold — с main.
final class RecordingStartGate: @unchecked Sendable {
    private let lock = NSLock()
    private var inFlight = false

    func tryAcquire() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !inFlight else { return false }
        inFlight = true
        return true
    }

    func release() {
        lock.lock()
        inFlight = false
        lock.unlock()
    }
}

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

        // IPC уходит с main thread: синхронный callWithRecovery раньше блокировал
        // runloop более чем на 2 секунды (Sentry KRAB-EAR-AGENT-3). Снимок UI
        // читается до перехода, а обновления выполняются через MainActor ниже.
        let wasRecordingLocally = isRecording
        Task.detached { [weak self] in
            await self?.performRecordToggle(wasRecordingLocally: wasRecordingLocally)
        }
    }

    /// Выполнить логику toggle записи вне главного потока.
    /// Все обращения к @MainActor-изолированным свойствам сделаны через явные `await MainActor.run`.
    func performRecordToggle(wasRecordingLocally: Bool) async {
        let (backendRecording, backendOwner, ownerFieldPresent, stateVerified) =
            syncRecordingStateWithBackend()
        // Ошибку IPC нельзя выдавать за старый backend без owner: в таком
        // «legacy»-виде promoted meeting снова можно было бы остановить тапом.
        // Пока снимок не подтверждён, fail-safe запрещает и start, и stop.
        if !stateVerified {
            logger.warn("Не удалось подтвердить владельца записи — toggle отклонён")
            await MainActor.run {
                self.notify(
                    title: "Krab Ear",
                    body: "Не удалось проверить режим записи — запись не тронута."
                )
            }
            return
        }
        if backendRecording != wasRecordingLocally {
            logger.warn(
                "Десинхрон состояния записи: local=\(wasRecordingLocally), " +
                "backend=\(backendRecording), owner=\(backendOwner ?? "nil"), " +
                "ownerFieldPresent=\(ownerFieldPresent)"
            )
        }

        // Owner-гейт обязан стоять ДО любой ветки stop: это закрывает не только
        // потерянный локальный флаг, но и dictation→meeting promote, при котором
        // снимок wasRecordingLocally законно остаётся true.
        if HotkeyRecordingOwnershipPolicy.isForeignRecording(
            isRecording: backendRecording,
            owner: backendOwner,
            ownerFieldPresent: ownerFieldPresent
        ) {
            let human: String
            switch backendOwner {
            case "meeting":
                human = "встреча"
            case "quick_capture":
                human = "быстрая заметка"
            default:
                human = "запись другого режима"
            }
            await MainActor.run {
                self.notify(
                    title: "Krab Ear",
                    body: "Идёт \(human) — запись не тронута."
                )
            }
            return
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

        // После owner-гейта выше здесь остаётся только своя/legacy-диктовка.
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

    /// Возвращает флаг записи, владельца и наличие owner в IPC-контракте.
    /// Старый backend не отдаёт ключ, новый всегда отдаёт строку либо null.
    func syncRecordingStateWithBackend() -> (
        recording: Bool,
        owner: String?,
        ownerFieldPresent: Bool,
        stateVerified: Bool
    ) {
        guard
            let stateResponse = try? callWithRecovery(method: "get_recording_state", params: [:]),
            let state = stateResponse["result"] as? [String: Any]
        else {
            return (isRecording, nil, false, false)
        }

        let backendRecording = (state["is_recording"] as? Bool) ?? false
        let backendOwner = state["owner"] as? String
        let ownerFieldPresent = state.keys.contains("owner")
        // Общий backend-флаг нельзя слепо зеркалить в hotkey-состояние:
        // при явном meeting/quick_capture второй тап иначе остановит чужую запись.
        // Только ОТСУТСТВУЮЩИЙ ключ сохраняет auto-heal старого backend;
        // owner:null нового backend — достижимая unmanaged/pending запись.
        let backendRepresentsLocalDictation =
            HotkeyRecordingOwnershipPolicy.representsLocalDictation(
                isRecording: backendRecording,
                owner: backendOwner,
                ownerFieldPresent: ownerFieldPresent
            )
        if backendRepresentsLocalDictation != isRecording {
            isRecording = backendRepresentsLocalDictation
            refreshStatusItemTitle()
            rebuildStatusMenu()
        }
        return (backendRecording, backendOwner, ownerFieldPresent, true)
    }

    func startRecording() {
        guard recordingStartGate.tryAcquire() else {
            logger.warn("start_recording уже выполняется — повторный старт подавлен")
            return
        }
        defer { recordingStartGate.release() }

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
            let response = try callWithRecovery(
                method: "start_recording",
                params: ["source": "dictation"]
            )
            let result = response["result"] as? [String: Any]
            let status = (result?["status"] as? String) ?? "recording"
            if status == "already_recording" {
                // already_recording — не наш успешный старт: принятие его за успех
                // позволяло следующему тапу остановить чужую встречу или заметку.
                logger.warn("start_recording: запись уже идёт — не перехватываем")
                audioDuckingService.restoreAfterRecording()
                notify(
                    title: "Krab Ear",
                    body: "Запись уже идёт — новая не начата."
                )
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
            // Ducking включается до IPC; любой отказ старта обязан восстановить
            // системный звук, а не только отдельный already_recording.
            audioDuckingService.restoreAfterRecording()
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
                    "source": "dictation",
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
            case "recorder_timeout":
                // F2 (Fable-ревью 2026-07-22): audio-worker завис (PortAudio hang class),
                // финального аудио нет. Пробуем спасти диктовку из превью; пользователь
                // в любом случае ОБЯЗАН узнать, что запись не сохранилась — раньше этот
                // исход маскировался под тихий already_stopped.
                logger.error("stop_recording: recorder_timeout — аудио-поток завис, финальное аудио потеряно")
                recoverFromPreviewFallback(reason: "Аудио-подсистема зависла (recorder_timeout)") { recovered in
                    if !recovered {
                        self.notify(
                            title: "Krab Ear",
                            body: "Запись не сохранилась: аудио-подсистема зависла. Попробуйте ещё раз; если повторится — перезапустите backend."
                        )
                    }
                }
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
