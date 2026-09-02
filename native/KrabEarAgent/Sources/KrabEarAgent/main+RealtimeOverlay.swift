/*
 main+RealtimeOverlay.swift
 AgentAppDelegate extension: realtime overlay polling, preview translation, preview text sanitization.
*/

import AppKit
import Foundation

extension AgentAppDelegate {

    // MARK: - Realtime overlay

    func startRealtimeOverlayPolling() {
        stopRealtimeOverlayPolling()

        // Streaming live paste SSE — запускаем независимо от realtimePreviewEnabled.
        // C3a: подавлен для быстрой заметки — партиалы не должны печататься в
        // активное окно (спека 2026-07-16-c3-quick-capture-design.md §2a).
        if !quickCaptureActive { streamingPasteController?.recordingDidStart() }

        // Запись владеет микрофоном; иначе wake word ловит собственную диктовку.
        wakeWordPoller?.pause(.recording)

        guard settings.realtimePreviewEnabled else { return }

        realtimeOverlay.show()
        realtimeOverlay.startPartialSSE()
        realtimeOverlayTimer = Timer.scheduledTimer(withTimeInterval: 0.85, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                self.refreshRealtimeOverlay()
            }
        }
        if let realtimeOverlayTimer {
            RunLoop.main.add(realtimeOverlayTimer, forMode: .common)
        }
        refreshRealtimeOverlay()
    }

    func stopRealtimeOverlayPolling() {
        realtimeOverlayTimer?.invalidate()
        realtimeOverlayTimer = nil
        lastPreviewTranslationSource = ""
        lastPreviewTranslationText = ""
        lastPreviewTranslationAt = 0
        lastPreviewTranslationMode = ""
        lastPreviewTranslationFailureAt = 0
        lastPreviewTranslationFailures = 0
        lastPreviewTranslationSuccessAt = 0
        previewSilenceTickCount = 0
        previewLastAudioRms = 1.0
        realtimeOverlay.stopPartialSSE()
        realtimeOverlay.hide()
        // Streaming live paste SSE — останавливаем вместе с оверлеем.
        // C3a: тот же гард, что на старте — симметрия обязательна.
        if !quickCaptureActive { streamingPasteController?.recordingDidStop() }
        wakeWordPoller?.resume(.recording)
    }

    // MARK: - A3 Adaptive backoff constants

    /// RMS below this threshold is considered silence (normalised 0–1 scale).
    /// Typical speech is 0.05–0.3; idle mic noise is < 0.01.
    static let previewSilenceRmsThreshold: Double = 0.02
    /// After this many consecutive silence ticks the poll interval widens to `previewSilenceInterval`.
    static let previewSilenceTicksToBackoff: Int = 3
    /// Active-speech poll interval (seconds).
    static let previewActiveInterval: TimeInterval = 0.85
    /// Silence poll interval (seconds) — 3.5× slower, saves CPU + IPC round-trips.
    static let previewSilenceInterval: TimeInterval = 3.0

    func refreshRealtimeOverlay() {
        guard isRecording, settings.realtimePreviewEnabled else {
            return
        }
        // Настройка читается на каждом тике, а не один раз при показе: владелец
        // может переключить её во время записи, и ждать следующей диктовки,
        // чтобы увидеть эффект, — неочевидно.
        realtimeOverlay.followCursorEnabled = settings.overlayFollowCursor
        // Sync IPC на main thread каждые 0.85s через NSTimer вызывал AppHang
        // когда backend под нагрузкой (диктовка + STT) — Sentry KRAB-EAR-AGENT-8 регрессия.
        // Перенесли на background queue с UI update обратно на main.
        let ipc = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipc.call(method: "get_recording_state", params: [:]),
                let result = response["result"] as? [String: Any]
            else {
                return
            }
            let previewText = (result["preview_text"] as? String) ?? ""
            // 🔴 elapsed_sec, а НЕ duration_sec (02.09.2026, жалоба владельца
            // «таймер висит на 1 с»). Оба ключа есть в ответе, но источники
            // разные: duration_sec — это _preview_duration_sec, курсор
            // превью-воркера, и обновляет его ТОЛЬКО он. Стоит воркеру
            // застрять (живой случай: 25с на захвате mlx_lock), как таймер
            // замирает на последнем значении. elapsed_sec приходит напрямую из
            // recorder.get_duration_sec() и не зависит от превью вовсе.
            // Фоллбэк на duration_sec оставлен на случай старого backend.
            let durationSec = (result["elapsed_sec"] as? Double)
                ?? (result["duration_sec"] as? Double)
                ?? 0.0
            let audioRms = result["audio_rms"] as? Double
            DispatchQueue.main.async {
                guard let self else { return }
                let durationText = self.formatDuration(durationSec)
                // translatePreviewTextIfNeeded вызывает second IPC sync — он async-aware ниже,
                // возвращает cached value сразу.
                let translatedPreview = self.translatePreviewTextIfNeeded(previewText)
                let modeHint = self.previewTranslationModeHint()
                self.realtimeOverlay.update(
                    previewText: previewText,
                    translatedText: translatedPreview,
                    durationText: durationText,
                    modeHint: modeHint
                )
                if let audioRms {
                    self.realtimeOverlay.setAudioLevel(Float(audioRms))
                    // A3: adaptive backoff — throttle polling when mic is silent
                    self.updatePreviewPollingInterval(rms: audioRms)
                }
            }
        }
    }

    // MARK: - A3 Adaptive polling interval

    /// Adjusts the realtimeOverlayTimer interval based on observed audio RMS.
    ///
    /// Logic:
    ///  - Each call below `previewSilenceRmsThreshold` increments `previewSilenceTickCount`.
    ///  - After `previewSilenceTicksToBackoff` consecutive silent ticks the timer is
    ///    replaced with a `previewSilenceInterval` (3.0 s) timer.
    ///  - Any tick with RMS ≥ threshold resets the counter and restores the fast
    ///    `previewActiveInterval` (0.85 s) timer if currently in backoff mode.
    ///
    /// Timer replacement only happens on mode transitions to avoid recreation churn.
    func updatePreviewPollingInterval(rms: Double) {
        let isSilent = rms < AgentAppDelegate.previewSilenceRmsThreshold
        let currentInterval = realtimeOverlayTimer?.timeInterval ?? AgentAppDelegate.previewActiveInterval

        if isSilent {
            previewSilenceTickCount += 1
            let shouldBackoff = previewSilenceTickCount >= AgentAppDelegate.previewSilenceTicksToBackoff
            if shouldBackoff, currentInterval < AgentAppDelegate.previewSilenceInterval - 0.1 {
                // Transition → slow polling
                restartPreviewTimer(interval: AgentAppDelegate.previewSilenceInterval)
            }
        } else {
            // Audio detected — reset silence counter and restore fast polling if needed
            if previewSilenceTickCount >= AgentAppDelegate.previewSilenceTicksToBackoff,
               currentInterval > AgentAppDelegate.previewActiveInterval + 0.1 {
                // Transition → fast polling
                restartPreviewTimer(interval: AgentAppDelegate.previewActiveInterval)
            }
            previewSilenceTickCount = 0
        }
        previewLastAudioRms = rms
    }

    /// Replaces the polling timer with a new one at `interval` seconds.
    /// Must be called on the main thread.
    private func restartPreviewTimer(interval: TimeInterval) {
        realtimeOverlayTimer?.invalidate()
        let t = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                self.refreshRealtimeOverlay()
            }
        }
        RunLoop.main.add(t, forMode: .common)
        realtimeOverlayTimer = t
    }

    func previewTranslationModeHint() -> String {
        switch settings.translationMode {
        case "ru_to_es":
            return "RU -> ES"
        case "es_to_ru":
            return "ES -> RU"
        case "en_to_ru":
            return "EN -> RU"
        case "auto":
            return "AUTO"
        case "auto_to_ru":
            return "AUTO -> RU"
        case "bilingual_ru_es":
            return "RU<->ES"
        default:
            return "OFF"
        }
    }

    func translatePreviewTextIfNeeded(_ previewText: String) -> String? {
        guard settings.translationMode != "off" else {
            return nil
        }
        let cleanPreview = previewText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard cleanPreview.count >= 8 else {
            return nil
        }

        let now = Date().timeIntervalSince1970
        let minInterval: TimeInterval
        if cleanPreview.count >= 240 {
            minInterval = 1.1
        } else if cleanPreview.count >= 120 {
            minInterval = 1.35
        } else {
            minInterval = 1.8
        }
        let hasSentenceBoundary = cleanPreview.hasSuffix(".") || cleanPreview.hasSuffix("!") || cleanPreview.hasSuffix("?")
        let deltaLength = abs(cleanPreview.count - lastPreviewTranslationSource.count)
        let minimumDelta = cleanPreview.count >= 120 ? 8 : 4
        let modeChanged = settings.translationMode != lastPreviewTranslationMode
        let enoughProgress = hasSentenceBoundary || deltaLength >= minimumDelta

        let failureBackoff = min(6.0, 1.2 + Double(lastPreviewTranslationFailures) * 0.9)
        if lastPreviewTranslationFailureAt > 0, (now - lastPreviewTranslationFailureAt) < failureBackoff {
            return lastPreviewTranslationText.isEmpty ? nil : lastPreviewTranslationText
        }

        let needsRefresh = (
            modeChanged ||
            (cleanPreview != lastPreviewTranslationSource &&
                enoughProgress &&
                (now - lastPreviewTranslationAt >= minInterval || lastPreviewTranslationText.isEmpty))
        )

        guard needsRefresh else {
            return lastPreviewTranslationText.isEmpty ? nil : lastPreviewTranslationText
        }

        // IPC на main thread блокирует ~100-500ms (translation pass + LLM). При
        // частоте refresh 0.85s + случайной задержке backend это ловит AppHang.
        // Запускаем в background queue, UI получит результат на следующем tick'е (≤1s lag).
        let ipc = self.ipcClient
        let translationMode = settings.translationMode
        let translationStyle = settings.translationStyle
        let networkMode = settings.networkMode
        let cleanPreviewCopy = cleanPreview
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let response = try? ipc.call(
                method: "translate_text",
                params: [
                    "text": cleanPreviewCopy,
                    "translation_mode": translationMode,
                    "translation_style": translationStyle,
                    "network_mode": networkMode,
                ]
            )
            DispatchQueue.main.async {
                guard let self else { return }
                let now2 = Date().timeIntervalSince1970
                guard
                    let result = response?["result"] as? [String: Any]
                else {
                    self.lastPreviewTranslationFailures += 1
                    self.lastPreviewTranslationFailureAt = now2
                    if now2 - self.lastPreviewTranslationSuccessAt > 8.0 {
                        self.lastPreviewTranslationText = ""
                    }
                    return
                }
                let status = (result["status"] as? String) ?? "unknown"
                let translated = ((result["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                self.lastPreviewTranslationSource = cleanPreviewCopy
                self.lastPreviewTranslationAt = now2
                self.lastPreviewTranslationMode = translationMode
                if status == "ok", !translated.isEmpty {
                    self.lastPreviewTranslationText = translated
                    self.lastPreviewTranslationFailures = 0
                    self.lastPreviewTranslationFailureAt = 0
                    self.lastPreviewTranslationSuccessAt = now2
                } else {
                    self.lastPreviewTranslationFailures += 1
                    self.lastPreviewTranslationFailureAt = now2
                    if now2 - self.lastPreviewTranslationSuccessAt > 8.0 {
                        self.lastPreviewTranslationText = ""
                    }
                }
            }
        }
        // Возвращаем cached value немедленно — UI не ждёт IPC.
        return lastPreviewTranslationText.isEmpty ? nil : lastPreviewTranslationText
    }

    func formatDuration(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded()))
        let minutes = total / 60
        let secs = total % 60
        return String(format: "%02d:%02d", minutes, secs)
    }

    func recoverFromPreviewFallback(reason: String, completion: @escaping (Bool) -> Void) {
        logger.warn("Запуск fallback из realtime preview: \(reason)")
        // Fable-ревью L2 (2026-08-04): recoverFromPreviewFallback вызывается СИНХРОННО
        // из stopRecording() ДО terminal cleanup, но ТЕЛО асинхронно — 2 раунда IPC на
        // фоновой очереди. Terminal cleanup выполняется сразу после этого вызова, не
        // дожидаясь его завершения, и успевает обнулить recordingTargetApp раньше, чем
        // фоновая работа дойдёт до handleTranscriptionResult. Захват здесь, СЕЙЧАС, пока
        // поле ещё достоверно — тот же класс фикса, что M1/основной путь.
        let capturedTarget = recordingTargetApp
        let client = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else {
                DispatchQueue.main.async { completion(false) }
                return
            }
            guard
                let stateResponse = try? client.call(method: "get_recording_state", params: [:]),
                let state = stateResponse["result"] as? [String: Any]
            else {
                DispatchQueue.main.async { completion(false) }
                return
            }

            let rawPreviewText = ((state["preview_text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let previewText = self.sanitizePreviewFallbackText(rawPreviewText)
            guard previewText.count >= 8 else {
                self.logger.warn("Fallback отменён: previewText слишком короткий")
                DispatchQueue.main.async { completion(false) }
                return
            }

            var historyId: String?
            if let addResponse = try? client.call(
                method: "add_history_item",
                params: [
                    "text": previewText,
                    "paste_status": "failed",
                ]
            ), let result = addResponse["result"] as? [String: Any] {
                historyId = result["id"] as? String
            }

            DispatchQueue.main.async {
                self.handleTranscriptionResult(
                    text: previewText,
                    historyId: historyId,
                    pasteTargetOverride: capturedTarget
                )
                self.notify(
                    title: "Krab Ear",
                    body: "Использован fallback realtime-текста: \(reason)"
                )
                completion(true)
            }
        }
    }

    // Wave 554: marked `nonisolated` so it can be called from the
    // DispatchQueue.global(qos:.userInitiated) background queue without crossing
    // MainActor boundary (pure-function text processing — no UI state touched).
    nonisolated func sanitizePreviewFallbackText(_ text: String) -> String {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return "" }

        let lowered = clean.lowercased()
        if lowered.contains("<begin_of_box>") || lowered.contains("<end_of_box>") || lowered.contains("\"action\":") {
            return ""
        }

        let normalized = normalizeForHeuristic(clean)
        let blockedFragments = [
            "продолжение следует",
            "to be continued",
            "сохраняй смысл ставь корректную пунктуац",
            "сохраняй смысл ставь корректную пункту",
            "ставь корректную пунктуац",
            "ставь корректную пункту",
        ]
        if blockedFragments.contains(where: { normalized.contains($0) }) {
            return ""
        }

        let tokens = normalized.split(separator: " ").map(String.init)
        let hasSaveMeaningEcho = tokens.contains("сохраняй")
            && tokens.contains("смысл")
            && tokens.contains(where: { $0.hasPrefix("корр") })
            && tokens.contains(where: { $0.hasPrefix("пункт") })
        if hasSaveMeaningEcho {
            return ""
        }
        if looksLikeLoopingFallback(tokens: tokens) {
            return ""
        }
        return clean
    }

    // Wave 578: nonisolated for DispatchQueue.global call from sanitizePreviewFallbackText
    nonisolated func normalizeForHeuristic(_ text: String) -> String {
        let lowered = text.lowercased()
        let allowed = lowered.map { char -> Character in
            if char.isLetter || char.isNumber || char == " " || char == "-" {
                return char
            }
            return " "
        }
        let compact = String(allowed).replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
        return compact.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // Wave 578: nonisolated for DispatchQueue.global call from sanitizePreviewFallbackText
    nonisolated func looksLikeLoopingFallback(tokens: [String]) -> Bool {
        guard tokens.count >= 6 else { return false }

        var frequency: [String: Int] = [:]
        for token in tokens {
            frequency[token, default: 0] += 1
        }

        let maxFreq = frequency.values.max() ?? 0
        let uniqueRatio = Double(frequency.count) / Double(max(tokens.count, 1))
        if uniqueRatio <= 0.42 && maxFreq >= max(3, Int(Double(tokens.count) * 0.34)) {
            return true
        }

        if frequency.count <= 2 && tokens.count >= 5 && maxFreq >= 4 {
            return true
        }

        var bigrams: [String: Int] = [:]
        if tokens.count >= 2 {
            for idx in 0..<(tokens.count - 1) {
                let key = "\(tokens[idx]) \(tokens[idx + 1])"
                bigrams[key, default: 0] += 1
            }
        }
        let topBigram = bigrams.values.max() ?? 0
        if topBigram >= max(3, tokens.count / 5) {
            return true
        }

        return containsRepeatedChunk(tokens: tokens, minRepeats: 3)
    }

    nonisolated func containsRepeatedChunk(tokens: [String], minRepeats: Int) -> Bool {
        let total = tokens.count
        guard total >= 6 else { return false }

        let maxChunk = min(7, total / max(minRepeats, 1))
        guard maxChunk >= 2 else { return false }

        for chunkSize in 2...maxChunk {
            var start = 0
            while start + (chunkSize * minRepeats) <= total {
                let chunk = Array(tokens[start..<(start + chunkSize)])
                var repeats = 1
                while start + (chunkSize * (repeats + 1)) <= total {
                    let nextChunk = Array(tokens[(start + chunkSize * repeats)..<(start + chunkSize * (repeats + 1))])
                    if nextChunk != chunk {
                        break
                    }
                    repeats += 1
                }
                if repeats >= minRepeats {
                    return true
                }
                start += 1
            }
        }
        return false
    }
}
