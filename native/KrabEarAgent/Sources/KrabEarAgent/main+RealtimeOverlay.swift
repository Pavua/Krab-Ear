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
        realtimeOverlay.stopPartialSSE()
        realtimeOverlay.hide()
    }

    func refreshRealtimeOverlay() {
        guard isRecording, settings.realtimePreviewEnabled else {
            return
        }
        guard
            let response = try? ipcClient.call(method: "get_recording_state", params: [:]),
            let result = response["result"] as? [String: Any]
        else {
            return
        }

        let previewText = (result["preview_text"] as? String) ?? ""
        let durationSec = (result["duration_sec"] as? Double) ?? 0.0
        let durationText = formatDuration(durationSec)
        let translatedPreview = translatePreviewTextIfNeeded(previewText)
        let modeHint = previewTranslationModeHint()
        realtimeOverlay.update(
            previewText: previewText,
            translatedText: translatedPreview,
            durationText: durationText,
            modeHint: modeHint
        )

        // VU meter: обновляем уровень RMS (~0.85 Hz из polling)
        if let audioRms = result["audio_rms"] as? Double {
            realtimeOverlay.setAudioLevel(Float(audioRms))
        }
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

        guard
            let response = try? ipcClient.call(
                method: "translate_text",
                params: [
                    "text": cleanPreview,
                    "translation_mode": settings.translationMode,
                    "translation_style": settings.translationStyle,
                    "network_mode": settings.networkMode,
                ]
            ),
            let result = response["result"] as? [String: Any]
        else {
            return lastPreviewTranslationText.isEmpty ? nil : lastPreviewTranslationText
        }

        let status = (result["status"] as? String) ?? "unknown"
        let translated = ((result["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        lastPreviewTranslationSource = cleanPreview
        lastPreviewTranslationAt = now
        lastPreviewTranslationMode = settings.translationMode
        if status == "ok", !translated.isEmpty {
            lastPreviewTranslationText = translated
            lastPreviewTranslationFailures = 0
            lastPreviewTranslationFailureAt = 0
            lastPreviewTranslationSuccessAt = now
        } else {
            lastPreviewTranslationFailures += 1
            lastPreviewTranslationFailureAt = now
            // Если перевод временно недоступен, удерживаем последний валидный перевод короткое время.
            if now - lastPreviewTranslationSuccessAt > 8.0 {
                lastPreviewTranslationText = ""
            }
        }
        return lastPreviewTranslationText.isEmpty ? nil : lastPreviewTranslationText
    }

    func formatDuration(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded()))
        let minutes = total / 60
        let secs = total % 60
        return String(format: "%02d:%02d", minutes, secs)
    }

    func recoverFromPreviewFallback(reason: String) -> Bool {
        logger.warn("Запуск fallback из realtime preview: \(reason)")
        guard
            let stateResponse = try? ipcClient.call(method: "get_recording_state", params: [:]),
            let state = stateResponse["result"] as? [String: Any]
        else {
            return false
        }

        let rawPreviewText = ((state["preview_text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let previewText = sanitizePreviewFallbackText(rawPreviewText)
        guard previewText.count >= 8 else {
            logger.warn("Fallback отменён: previewText слишком короткий")
            return false
        }

        var historyId: String?
        if let addResponse = try? ipcClient.call(
            method: "add_history_item",
            params: [
                "text": previewText,
                "paste_status": "failed",
            ]
        ), let result = addResponse["result"] as? [String: Any] {
            historyId = result["id"] as? String
        }

        handleTranscriptionResult(text: previewText, historyId: historyId)
        notify(
            title: "Krab Ear",
            body: "Использован fallback realtime-текста: \(reason)"
        )
        return true
    }

    func sanitizePreviewFallbackText(_ text: String) -> String {
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

    func normalizeForHeuristic(_ text: String) -> String {
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

    func looksLikeLoopingFallback(tokens: [String]) -> Bool {
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

    func containsRepeatedChunk(tokens: [String], minRepeats: Int) -> Bool {
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
