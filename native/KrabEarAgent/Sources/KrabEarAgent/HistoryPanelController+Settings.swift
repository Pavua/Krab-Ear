/*
 Расширение HistoryPanelController: обработчики изменения настроек.

 Содержит все @objc-методы onXxxChanged, applySettingsPatch и syncSettingsControls.
*/

import AppKit
import Foundation

// MARK: - Privacy Audit associated key

private enum PrivacyAuditAssocKeys {
    nonisolated(unsafe) static var auditViewer: UInt8 = 0
}

extension HistoryPanelController {

    // MARK: - Settings change handlers

    @objc func onQualityChanged() {
        guard !isSyncingSettings else { return }
        let qualityProfile = qualitySelector.indexOfSelectedItem == 1 ? "max" : "balanced"
        applySettingsPatch(["quality_profile": qualityProfile])
    }

    @objc func onModeChanged() {
        guard !isSyncingSettings else { return }
        let mode = modeSelector.indexOfSelectedItem == 1 ? "menubar" : "headless"
        applySettingsPatch(["mode": mode])
    }

    @objc func onCleanupProfileChanged() {
        guard !isSyncingSettings else { return }
        let profile = cleanupSelector.indexOfSelectedItem == 1 ? "strict" : "soft"
        applySettingsPatch(["cleanup_profile": profile])
    }

    @objc func onTranslationModeChanged() {
        guard !isSyncingSettings else { return }
        let mode: String
        switch translationSelector.indexOfSelectedItem {
        case 1:
            mode = "ru_to_es"
        case 2:
            mode = "es_to_ru"
        case 3:
            mode = "en_to_ru"
        case 4:
            mode = "auto"
        case 5:
            mode = "bilingual_ru_es"
        case 6:
            mode = "auto_to_ru"
        default:
            mode = "off"
        }
        applySettingsPatch(["translation_mode": mode])
    }

    @objc func onHistoryPageSizeChanged() {
        guard !isSyncingSettings else { return }
        let raw = historyPageSizeSelector.titleOfSelectedItem ?? "50"
        let value = Int(raw) ?? 50
        applySettingsPatch(["history_page_size": value])
        loadInitial()
    }

    @objc func onHistoryDensityChanged() {
        guard !isSyncingSettings else { return }
        let density = historyDensitySelector.indexOfSelectedItem == 1 ? "compact" : "normal"
        applySettingsPatch(["history_text_density": density])
        tableView.reloadData()
    }

    @objc func onNetworkModeChanged() {
        guard !isSyncingSettings else { return }
        let mode: String
        switch networkSelector.indexOfSelectedItem {
        case 1:
            mode = "offline_strict"
        case 2:
            mode = "online_opt_in"
        default:
            mode = "offline_default"
        }
        applySettingsPatch(["network_mode": mode])
    }

    @objc func onTranslationStyleChanged() {
        guard !isSyncingSettings else { return }
        let style: String
        switch translationStyleSelector.indexOfSelectedItem {
        case 1:
            style = "chat"
        case 2:
            style = "formal"
        default:
            style = "neutral"
        }
        applySettingsPatch(["translation_style": style])
    }

    @objc func onAutoPasteChanged() {
        guard !isSyncingSettings else { return }
        let autoPaste = autoPasteButton.state == .on
        applySettingsPatch(["auto_paste": autoPaste])
    }

    @objc func onPasteUndoChanged() {
        guard !isSyncingSettings else { return }
        let enabled = pasteUndoButton.state == .on
        applySettingsPatch(["paste_undo_enabled": enabled])
        syncPasteUndoToggle(enabled: enabled)
    }

    @MainActor
    func syncPasteUndoToggle(enabled: Bool) {
        pasteUndoButton.state = enabled ? .on : .off
        (NSApp.delegate as? AgentAppDelegate)?.pasteUndoService?.pasteUndoEnabled = enabled
    }

    // MARK: - Smart field-aware paste toggle

    @objc func onSmartFieldFormatChanged() {
        guard !isSyncingSettings else { return }
        let enabled = smartFieldFormatButton.state == .on
        applySettingsPatch(["smart_field_format_enabled": enabled])
        syncSmartFieldFormatToggle(enabled: enabled)
    }

    @MainActor
    func syncSmartFieldFormatToggle(enabled: Bool) {
        smartFieldFormatButton.state = enabled ? .on : .off
        (NSApp.delegate as? AgentAppDelegate)?.pasteService.smartFieldFormatEnabled = enabled
    }

    // MARK: - Streaming live paste toggle

    @objc func onStreamingPasteChanged() {
        guard !isSyncingSettings else { return }
        let enabled = streamingPasteButton.state == .on
        applySettingsPatch(["streaming_paste_enabled": enabled])
        syncStreamingPasteToggle(enabled: enabled)
    }

    @MainActor
    func syncStreamingPasteToggle(enabled: Bool) {
        streamingPasteButton.state = enabled ? .on : .off
        (NSApp.delegate as? AgentAppDelegate)?.streamingPasteController?.isEnabled = enabled
    }

    @objc func onQuickEditChanged() {
        guard !isSyncingSettings else { return }
        let enabled = quickEditButton.state == .on
        applySettingsPatch(["quick_edit_enabled": enabled])
        // Dim timeout row when quick edit is disabled.
        let alpha = enabled ? 1.0 : KrabEarTheme.Interaction.disabledOpacity
        quickEditTimeoutStepper.isEnabled = enabled
        quickEditTimeoutStepper.alphaValue = alpha
        quickEditTimeoutValueLabel.alphaValue = alpha
    }

    @objc func onQuickEditTimeoutChanged(_ sender: NSStepper) {
        guard !isSyncingSettings else { return }
        let seconds = Double(sender.integerValue)
        quickEditTimeoutValueLabel.stringValue = "\(sender.integerValue) сек"
        applySettingsPatch(["quick_edit_timeout_sec": seconds])
    }

    @objc func onPrivacyModeChanged() {
        guard !isSyncingSettings else { return }
        let enabled = privacyModeButton.state == .on
        applySettingsPatch(["privacy_mode_enabled": enabled])
        (NSApp.delegate as? AgentAppDelegate)?.setPrivacyMode(enabled)
    }

    @objc func onStartSoundChanged() {
        guard !isSyncingSettings else { return }
        let playStartSound = startSoundButton.state == .on
        applySettingsPatch(["play_start_sound": playStartSound])
    }

    @objc func onRealtimePreviewChanged() {
        guard !isSyncingSettings else { return }
        let enabled = realtimePreviewButton.state == .on
        applySettingsPatch(["realtime_preview_enabled": enabled])
        if !enabled {
            realtimeStatusLabel.stringValue = "Realtime: выключен"
            realtimeTextView.string = "Realtime preview отключен в настройках."
        } else {
            refreshRealtimePreview()
        }
    }

    @objc func onTranslateAndPasteChanged() {
        guard !isSyncingSettings else { return }
        let enabled = translateAndPasteButton.state == .on
        applySettingsPatch(["translate_and_paste": enabled])
    }

    @objc func onClipboardModeChanged() {
        guard !isSyncingSettings else { return }
        let mode: String
        switch clipboardModeSelector.indexOfSelectedItem {
        case 1:
            mode = "copy_on_fail"
        case 2:
            mode = "never_copy"
        default:
            mode = "always_copy"
        }
        applySettingsPatch(["clipboard_mode": mode])
    }

    @objc func onAudioDuckingChanged() {
        guard !isSyncingSettings else { return }
        let enabled = audioDuckingButton.state == .on
        applySettingsPatch(["audio_ducking_enabled": enabled])
    }

    @objc func onAudioDuckingPercentChanged() {
        guard !isSyncingSettings else { return }
        let percent = Int(audioDuckingSlider.doubleValue.rounded())
        audioDuckingValueLabel.stringValue = "\(percent)%"
        applySettingsPatch(["audio_ducking_percent": percent])
    }

    @objc func onDiarizationChanged() {
        guard !isSyncingSettings else { return }
        let enabled = diarizationButton.state == .on
        applySettingsPatch(["diarization_enabled": enabled])
    }

    @objc func onLlmRewriteChanged() {
        guard !isSyncingSettings else { return }
        let enabled = llmRewriteButton.state == .on
        llmModelSelector.isEnabled = enabled
        applySettingsPatch(["llm_rewrite_enabled": enabled])
    }

    @objc func onLlmModelChanged() {
        guard !isSyncingSettings else { return }
        guard let selectedModel = llmModelSelector.titleOfSelectedItem else { return }
        // Skip separator items (they have no meaningful title content)
        if selectedModel.trimmingCharacters(in: .whitespaces).isEmpty { return }
        applySettingsPatch(["llm_model": selectedModel])
    }

    // MARK: - LLM Model Dropdown Population

    /// Проверенные / рекомендованные модели — всегда наверху dropdown.
    /// Обновлять по результатам bench-сессий (см. комментарии в HistoryPanelController.swift).
    private static let recommendedRewriterModels: [String] = [
        "gemma-4-e4b-it-mlx",
        "Qwen3-8B-MLX-4bit",
        "huihui-qwen3-14b-abl-v2",
        "qwen3.5-9b@6bit",
        "huihui-qwen3-30b-a3b-instruct-2507-abliterated-dwq4-mlx",
        "aya-expanse-8b",
        "Qwen3.5-27B-4bit",
        "Aya-Expanse-32B-abliterated",
        "qwen2.5-14b-uncensored-mlx",
        "Hermes-3-Llama-8B",
        "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx",
    ]

    /// Заполняет dropdown llmModelSelector:
    /// 1. Рекомендованные модели наверху.
    /// 2. Разделитель.
    /// 3. Дополнительные модели из LM Studio (не вошедшие в рекомендованные).
    /// 4. Гарантирует наличие currentModel в списке (даже если пользовательская).
    @MainActor
    func populateLLMModelDropdown(currentModel: String, lmStudioModels: [String]) {
        isSyncingSettings = true
        defer { isSyncingSettings = false }

        llmModelSelector.removeAllItems()

        // 1. Recommended at top
        llmModelSelector.addItems(withTitles: HistoryPanelController.recommendedRewriterModels)

        // 2. Extras from LM Studio not already in recommended list
        let extras = lmStudioModels.filter {
            !HistoryPanelController.recommendedRewriterModels.contains($0)
        }
        if !extras.isEmpty {
            let separator = NSMenuItem.separator()
            llmModelSelector.menu?.addItem(separator)
            llmModelSelector.addItems(withTitles: extras)
        }

        // 3. Ensure currentModel is always selectable (e.g. user-typed custom value)
        let allTitles = llmModelSelector.itemTitles
        if !currentModel.isEmpty && !allTitles.contains(currentModel) {
            if extras.isEmpty {
                // No extras section yet — add separator before custom model
                let separator = NSMenuItem.separator()
                llmModelSelector.menu?.addItem(separator)
            }
            llmModelSelector.addItem(withTitle: currentModel)
        }

        llmModelSelector.selectItem(withTitle: currentModel)
    }

    /// Асинхронно запрашивает список моделей из LM Studio через IPC list_llm_models,
    /// затем заполняет dropdown на main thread. Graceful fallback если LM Studio недоступен.
    func fetchAndPopulateLLMModels(currentModel: String) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let lmModels: [String]
            do {
                let resp = try ipc.call(method: "list_llm_models", params: [:])
                let result = resp["result"] as? [String: Any]
                lmModels = result?["models"] as? [String] ?? []
            } catch {
                lmModels = []
            }
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.populateLLMModelDropdown(currentModel: currentModel, lmStudioModels: lmModels)
            }
        }
    }

    @objc func onOverlayOpacityChanged() {
        guard !isSyncingSettings else { return }
        let percent = Int(overlayOpacitySlider.doubleValue.rounded())
        overlayOpacityValueLabel.stringValue = "\(percent)%"
        applySettingsPatch(["overlay_opacity_percent": percent])
    }

    @objc func onAutostartChanged() {
        guard !isSyncingSettings else { return }
        let autoStartEnabled = autoStartButton.state == .on
        applySettingsPatch(["auto_start_enabled": autoStartEnabled])
    }

    @objc func onDockChanged() {
        guard !isSyncingSettings else { return }
        let showDockIcon = dockIconButton.state == .on
        applySettingsPatch(["show_dock_icon": showDockIcon])
    }

    @objc func onHotkeyChanged() {
        guard !isSyncingSettings else { return }
        let idx = hotkeySelector.indexOfSelectedItem
        let val: String
        switch idx {
        case 1: val = "left_option"
        case 2: val = "any_option"
        default: val = "right_option"
        }
        applySettingsPatch(["hotkey": val])
    }

    @objc func onHotkeyProfileChanged() {
        guard !isSyncingSettings else { return }
        let idx = hotkeyProfileSelector.indexOfSelectedItem
        let val: String
        switch idx {
        case 1: val = "meeting"
        case 2: val = "translation"
        default: val = "default"
        }
        applySettingsPatch(["hotkey_profile": val])
    }

    @objc func onHotkeyModeChanged() {
        guard !isSyncingSettings else { return }
        let mode = hotkeyModeHoldRadio.state == .on ? "hold" : "toggle"
        applySettingsPatch(["hotkey_mode": mode])
    }

    @objc func onCaptureSourceModeChanged() {
        guard !isSyncingSettings else { return }
        applySettingsPatch(["capture_source_mode": selectedCaptureSourceMode()])
    }

    @objc func onCallNotifyChanged() {
        guard !isSyncingSettings else { return }
        let enabled = callNotifyButton.state == .on
        applySettingsPatch(["call_notify_default": enabled])
    }

    @objc func onCallAutoSummaryChanged() {
        guard !isSyncingSettings else { return }
        let enabled = callAutoSummaryButton.state == .on
        applySettingsPatch(["call_auto_summary": enabled])
    }

    @objc func onVoiceGatewayURLChanged() {
        guard !isSyncingSettings else { return }
        let raw = voiceGatewayURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        applySettingsPatch(["voice_gateway_url": raw])
    }

    @objc func onVoiceGatewayAPIKeyChanged() {
        guard !isSyncingSettings else { return }
        let raw = voiceGatewayAPIKeyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        applySettingsPatch(["voice_gateway_api_key": raw])
    }

    @objc func onCheckVoiceGateway() {
        var url = voiceGatewayURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if url.isEmpty {
            url = "http://127.0.0.1:8090"
        }
        if url.hasSuffix("/") {
            url.removeLast()
        }
        guard let healthURL = URL(string: "\(url)/health") else {
            showInfoAlert(title: "Voice Gateway", body: "Некорректный URL: \(url)")
            return
        }

        voiceGatewayCheckButton.isEnabled = false
        voiceGatewayCheckButton.title = "Проверяю..."

        var request = URLRequest(url: healthURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3.0)
        request.httpMethod = "GET"

        let task = URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.voiceGatewayCheckButton.isEnabled = true
                self.voiceGatewayCheckButton.title = "Проверить Gateway"

                if let error {
                    self.showInfoAlert(title: "Voice Gateway", body: "Связь не установлена: \(error.localizedDescription)")
                    return
                }

                guard let http = response as? HTTPURLResponse else {
                    self.showInfoAlert(title: "Voice Gateway", body: "Gateway вернул некорректный ответ.")
                    return
                }

                let payloadPreview: String
                if let data, let text = String(data: data, encoding: .utf8), !text.isEmpty {
                    payloadPreview = text
                } else {
                    payloadPreview = "(пустой ответ)"
                }

                if 200 <= http.statusCode && http.statusCode < 300 {
                    self.showInfoAlert(
                        title: "Voice Gateway",
                        body: "Gateway доступен.\nURL: \(healthURL.absoluteString)\nHTTP: \(http.statusCode)\nОтвет: \(payloadPreview)"
                    )
                } else {
                    self.showInfoAlert(
                        title: "Voice Gateway",
                        body: "Gateway ответил с ошибкой.\nURL: \(healthURL.absoluteString)\nHTTP: \(http.statusCode)\nОтвет: \(payloadPreview)"
                    )
                }
            }
        }
        task.resume()
    }

    // MARK: - applySettingsPatch

    func applySettingsPatch(_ patch: [String: Any]) {
        var payload = settingsProvider().toPayload()
        for (key, value) in patch {
            payload[key] = value
        }
        let updated = settingsUpdater(payload)
        syncSettingsControls(using: updated)
    }

    // MARK: - syncSettingsControls

    func syncSettingsControls(using value: AgentSettings? = nil) {
        let settings = value ?? settingsProvider()

        isSyncingSettings = true
        qualitySelector.selectItem(at: settings.qualityProfile == "max" ? 1 : 0)
        cleanupSelector.selectItem(at: settings.cleanupProfile == "strict" ? 1 : 0)
        switch settings.translationMode {
        case "ru_to_es":
            translationSelector.selectItem(at: 1)
            swapRuEsButton.title = "Swap: RU -> ES"
        case "es_to_ru":
            translationSelector.selectItem(at: 2)
            swapRuEsButton.title = "Swap: ES -> RU"
        case "en_to_ru":
            translationSelector.selectItem(at: 3)
            swapRuEsButton.title = "Swap RU<->ES"
        case "auto":
            translationSelector.selectItem(at: 4)
            swapRuEsButton.title = "Swap RU<->ES"
        case "bilingual_ru_es":
            translationSelector.selectItem(at: 5)
            swapRuEsButton.title = "Swap RU<->ES"
        case "auto_to_ru":
            translationSelector.selectItem(at: 6)
            swapRuEsButton.title = "Swap RU<->ES"
        default:
            translationSelector.selectItem(at: 0)
            swapRuEsButton.title = "Swap RU<->ES"
        }
        swapRuEsButton.isEnabled = settings.translationMode == "ru_to_es"
            || settings.translationMode == "es_to_ru"
            || settings.translationMode == "auto"
            || settings.translationMode == "bilingual_ru_es"
        let normalizedPageSize = normalizePageSize(settings.historyPageSize)
        let idx = historyPageSizeSelector.indexOfItem(withTitle: "\(normalizedPageSize)")
        if idx >= 0 {
            historyPageSizeSelector.selectItem(at: idx)
        } else {
            historyPageSizeSelector.selectItem(withTitle: "50")
        }
        historyDensitySelector.selectItem(at: settings.historyTextDensity == "compact" ? 1 : 0)
        switch settings.networkMode {
        case "offline_strict":
            networkSelector.selectItem(at: 1)
        case "online_opt_in":
            networkSelector.selectItem(at: 2)
        default:
            networkSelector.selectItem(at: 0)
        }
        switch settings.translationStyle {
        case "chat":
            translationStyleSelector.selectItem(at: 1)
        case "formal":
            translationStyleSelector.selectItem(at: 2)
        default:
            translationStyleSelector.selectItem(at: 0)
        }
        modeSelector.selectItem(at: settings.mode == "menubar" ? 1 : 0)
        autoPasteButton.state = settings.autoPaste ? .on : .off
        quickEditButton.state = settings.quickEditEnabled ? .on : .off
        let safeTimeout = max(1, min(Int(settings.quickEditTimeoutSec), 30))
        quickEditTimeoutStepper.integerValue = safeTimeout
        quickEditTimeoutValueLabel.stringValue = "\(safeTimeout) сек"
        let timeoutAlpha = settings.quickEditEnabled ? 1.0 : KrabEarTheme.Interaction.disabledOpacity
        quickEditTimeoutStepper.isEnabled = settings.quickEditEnabled
        quickEditTimeoutStepper.alphaValue = timeoutAlpha
        quickEditTimeoutValueLabel.alphaValue = timeoutAlpha
        privacyModeButton.state = settings.privacyModeEnabled ? .on : .off
        syncVoiceCommandsToggles(enabled: settings.voiceCommandsEnabled, strictMode: settings.voiceCommandsStrictMode)
        syncRetentionSettings(enabled: settings.autoPurgeEnabled, retentionDays: settings.autoPurgeRetentionDays)
        if let t = objc_getAssociatedObject(self, &STTVocabAssocKeys.autoLearnToggle) as? NSButton {
            t.state = settings.autoLearnCorrectionsEnabled ? .on : .off
        }
        if let t = objc_getAssociatedObject(self, &STTVocabAssocKeys.cdAutoLearnToggle) as? NSButton {
            t.state = settings.autoLearnCorrectionsEnabled ? .on : .off
        }
        syncTextSnippetsToggles(enabled: settings.textSnippetsEnabled)
        syncPhoneticVocabToggles(enabled: settings.phoneticVocabEnabled)
        syncPasteUndoToggle(enabled: settings.pasteUndoEnabled)
        syncSmartFieldFormatToggle(enabled: settings.smartFieldFormatEnabled)
        syncStreamingPasteToggle(enabled: settings.streamingPasteEnabled)
        // Статус шифрования получаем из backend напрямую (не из AgentSettings),
        // потому что available зависит от состояния Keychain, а не только от флага.
        loadEncryptionStatus()
        (NSApp.delegate as? AgentAppDelegate)?.setPrivacyMode(settings.privacyModeEnabled)
        startSoundButton.state = settings.playStartSound ? .on : .off
        realtimePreviewButton.state = settings.realtimePreviewEnabled ? .on : .off
        translateAndPasteButton.state = settings.translateAndPaste ? .on : .off
        autoStartButton.state = settings.autoStartEnabled ? .on : .off
        dockIconButton.state = settings.showDockIcon ? .on : .off
        callNotifyButton.state = settings.callNotifyDefault ? .on : .off
        callAutoSummaryButton.state = settings.callAutoSummary ? .on : .off
        voiceGatewayURLField.stringValue = settings.voiceGatewayURL
        voiceGatewayAPIKeyField.stringValue = settings.voiceGatewayAPIKey
        selectCaptureSourceMode(settings.captureSourceMode)
        switch settings.clipboardMode {
        case "copy_on_fail":
            clipboardModeSelector.selectItem(at: 1)
        case "never_copy":
            clipboardModeSelector.selectItem(at: 2)
        default:
            clipboardModeSelector.selectItem(at: 0)
        }
        audioDuckingButton.state = settings.audioDuckingEnabled ? .on : .off
        audioDuckingSlider.isEnabled = settings.audioDuckingEnabled
        let duckingAlpha = settings.audioDuckingEnabled ? 1.0 : KrabEarTheme.Interaction.disabledOpacity
        audioDuckingSlider.alphaValue = duckingAlpha
        audioDuckingValueLabel.alphaValue = duckingAlpha
        let safeDuckPercent = max(0, min(settings.audioDuckingPercent, 100))
        audioDuckingSlider.doubleValue = Double(safeDuckPercent)
        audioDuckingValueLabel.stringValue = "\(safeDuckPercent)%"
        let safeOverlayPercent = max(15, min(settings.overlayOpacityPercent, 90))
        overlayOpacitySlider.doubleValue = Double(safeOverlayPercent)
        overlayOpacityValueLabel.stringValue = "\(safeOverlayPercent)%"
        // D.10a: AI Settings Sync
        diarizationButton.state = settings.diarizationEnabled ? .on : .off

        // PR #20: sub-caption под диарезацией (tag 202401) — dim к 0.4 при off.
        // UI ONLY: backend читает diarization_enabled только на старте, runtime re-init TBD в follow-up.
        if let subCaption = self.window?.contentView?.viewWithTag(202401) as? NSTextField {
            KrabEarTheme.Motion.animate(
                duration: KrabEarTheme.Motion.Duration.short,
                easing: KrabEarTheme.Motion.Easing.easeOut
            ) {
                subCaption.alphaValue = settings.diarizationEnabled ? 1.0 : 0.4
            }
        }

        syncCloudRewriterControls(settings: settings)
        llmRewriteButton.state = settings.llmRewriteEnabled ? .on : .off
        // Ensure current model is always visible; fetch LM Studio models async to expand dropdown.
        let currentModel = settings.llmModel
        if llmModelSelector.itemTitles.contains(currentModel) {
            llmModelSelector.selectItem(withTitle: currentModel)
        } else if !currentModel.isEmpty {
            // Model not in dropdown yet — add it immediately so the user sees it selected,
            // async fetch will rebuild the full list shortly.
            if !llmModelSelector.itemTitles.contains(currentModel) {
                llmModelSelector.addItem(withTitle: currentModel)
            }
            llmModelSelector.selectItem(withTitle: currentModel)
        }
        fetchAndPopulateLLMModels(currentModel: currentModel)
        llmModelSelector.isEnabled = settings.llmRewriteEnabled
        llmModelSelector.alphaValue = settings.llmRewriteEnabled ? 1.0 : KrabEarTheme.Interaction.disabledOpacity
        glossaryStatusLabel.stringValue = "Глоссарий: \(settings.translationGlossary.count)"
        // Reload glossary search list with current filter query.
        reloadGlossaryList(
            glossary: settings.translationGlossary,
            query: glossarySearchField.stringValue
        )

        switch settings.hotkey {
        case "left_option":
            hotkeySelector.selectItem(at: 1)
        case "any_option":
            hotkeySelector.selectItem(at: 2)
        default:
            hotkeySelector.selectItem(at: 0)
        }

        switch settings.hotkeyProfile {
        case "meeting":
            hotkeyProfileSelector.selectItem(at: 1)
        case "translation":
            hotkeyProfileSelector.selectItem(at: 2)
        default:
            hotkeyProfileSelector.selectItem(at: 0)
        }

        // Sync hotkey mode radio buttons
        if settings.hotkeyMode == "hold" {
            hotkeyModeHoldRadio.state = .on
            hotkeyModeToggleRadio.state = .off
        } else {
            hotkeyModeToggleRadio.state = .on
            hotkeyModeHoldRadio.state = .off
        }

        isSyncingTabs = true

        let tab = PanelTab.from(settingsValue: settings.uiLastTab)
        switch tab {
        case .dictation:
            mainTabView.selectTabViewItem(at: 0)
        case .liveTranslation:
            mainTabView.selectTabViewItem(at: 1)
        case .history:
            mainTabView.selectTabViewItem(at: 2)
        case .conversation:
            mainTabView.selectTabViewItem(at: 3)
        case .callAutomation:
            mainTabView.selectTabViewItem(at: 4)
        case .diagnostics:
            mainTabView.selectTabViewItem(at: 5)
        case .archive:
            mainTabView.selectTabViewItem(at: 6)
        }
        isSyncingTabs = false
        applyHistoryFocusMode(settings.historyFocusMode)
        applyHistoryTextDensity(settings.historyTextDensity)
        syncVoiceAssistantControls()
        syncSelectionTranslatorControls()
        isSyncingSettings = false
        updateLoadMoreButtonCaption()
        refreshCaptureSourceHint()
        refreshCallAssistState()
    }

    // MARK: - Gemini 3.1 Pro Design Helpers (Settings Redesign)

    /// Унифицированная строка настройки: лейбл слева, опциональный badge рядом с лейблом,
    /// контрол прижат вправо, описание снизу мелким шрифтом.
    @MainActor
    func makeSettingRow(
        label: String,
        description: String? = nil,
        control: NSView,
        badge: NSView? = nil
    ) -> NSView {
        let labelField = NSTextField(labelWithString: label)
        labelField.font = KrabEarTheme.Typography.body
        labelField.textColor = KrabEarTheme.Colors.textPrimary

        let labelStack = NSStackView()
        labelStack.orientation = .horizontal
        labelStack.spacing = KrabEarTheme.Metrics.tight
        labelStack.alignment = .centerY
        labelStack.addArrangedSubview(labelField)
        if let badge {
            labelStack.addArrangedSubview(badge)
        }

        // Spacer: suppressed from accessibility and focus traversal — it is a purely
        // visual layout element; allowing Tab to land here wastes a key press.
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        spacer.setAccessibilityElement(false)

        let hStack = NSStackView()
        hStack.orientation = .horizontal
        hStack.distribution = .fill
        hStack.alignment = .centerY
        hStack.spacing = KrabEarTheme.Metrics.standard
        hStack.addArrangedSubview(labelStack)
        hStack.addArrangedSubview(spacer)
        hStack.addArrangedSubview(control)

        if let desc = description {
            let descLabel = NSTextField(labelWithString: desc)
            descLabel.font = KrabEarTheme.Typography.caption
            descLabel.textColor = KrabEarTheme.Colors.textDisabled
            descLabel.lineBreakMode = .byWordWrapping
            descLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

            let vStack = NSStackView()
            vStack.orientation = .vertical
            vStack.alignment = .leading
            vStack.spacing = KrabEarTheme.Metrics.tight
            vStack.addArrangedSubview(hStack)
            vStack.addArrangedSubview(descLabel)
            // No explicit widthAnchor constraint needed: NSStackView with .leading
            // alignment already stretches arranged subviews to match the stack's
            // width. An explicit hStack↔vStack constraint activated before vStack
            // is parented causes "no common ancestor" NSGenericException (KRAB-EAR-AGENT-2).
            return vStack
        }
        return hStack
    }

    /// Тонкий горизонтальный разделитель для разбивки карточек на зоны.
    /// Использует NSBox.separator (AppKit-managed rendering) вместо bare NSView,
    /// чтобы цвет разделителя применялся корректно до того как view добавляется в окно.
    @MainActor
    func makeSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }

    /// Лейбл-бейдж: малый текст, цвет из KrabEarTheme, опциональный тултип.
    @MainActor
    func makeBadge(text: String, color: NSColor, tooltip: String? = nil, symbol: String? = nil) -> NSView {
        let container = NSView()
        container.wantsLayer = true
        container.layer?.cornerRadius = 8
        container.layer?.backgroundColor = color.withAlphaComponent(0.15).cgColor
        
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = 4
        stack.alignment = .centerY
        
        if let symbol = symbol, let image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) {
            let imageView = NSImageView(image: image)
            imageView.contentTintColor = color
            imageView.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 10, weight: .medium)
            stack.addArrangedSubview(imageView)
        }
        
        let label = NSTextField(labelWithString: text)
        label.font = KrabEarTheme.Typography.captionMedium.tabular()
        label.textColor = color
        label.isEditable = false
        label.isBordered = false
        label.drawsBackground = false
        stack.addArrangedSubview(label)
        
        container.addSubview(stack)
        stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: container.topAnchor, constant: 2),
            stack.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -2),
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 6),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -6)
        ])
        
        if let tooltip = tooltip {
            container.toolTip = tooltip
        }
        
        return container
    }

    /// Тонкий подзаголовок для группировки внутри карточки.
    @MainActor
    func makeSubhead(_ title: String) -> NSTextField {
        let label = NSTextField(labelWithString: title)
        label.font = KrabEarTheme.Typography.captionMedium
        label.textColor = KrabEarTheme.Colors.textSecondary
        label.isEditable = false
        label.isBordered = false
        label.drawsBackground = false
        return label
    }

    /// Строка с NSButton.switch в стиле Liquid Glass + лейбл + опциональный badge.
    @MainActor
    func makeSwitchRow(
        label: String,
        description: String? = nil,
        button: NSButton,
        statusBadge: NSView? = nil
    ) -> NSView {
        KrabEarTheme.styleCheckbox(button)
        return makeSettingRow(label: label, description: description, control: button, badge: statusBadge)
    }

    // MARK: - Audio Pipeline Section (PR #20 — Gemini 3.1 Pro)
    //
    // UI ONLY — IPC runtime apply (settings_service + engine.py diarization re-init
    // on-the-fly) является follow-up Claude PR. Эта PR wires toggle/picker через
    // applySettingsPatch → значение персистит в settings.json, НО backend читает
    // diarization_enabled / quality_profile только на старте.
    //
    // AudioEngine.__init__ кэширует pyannote pipeline, hot-swap out of scope для MVP.
    // User-facing restart hint показывается явно.
    func buildAudioPipelineSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_audio_pipeline",
            title: "Аудио-пайплайн",
            isExpanded: true,
            iconSymbol: "waveform"
        )

        let card = ThemeCardView()

        // 1. Diarization Toggle Row — Gemini design: makeSwitchRow + GPU-crash badge.
        //    Reparents global `diarizationButton` (был в aiSettingsRow1 / AI секции)
        //    в новую секцию. AppKit автоматически удаляет view из старого superview
        //    при addArrangedSubview в новый stack.
        diarizationButton.title = ""
        diarizationButton.setButtonType(.switch)
        diarizationButton.setAccessibilityLabel(
            "Включить разделение говорящих (диарезация pyannote через Metal). Требует перезапуск backend."
        )

        let gpuCrashBadge = makeBadge(
            text: "бета",
            color: KrabEarTheme.Colors.warning, // предупреждение, не ошибка — orange через токен
            tooltip: "Может вызывать сбой GPU на Apple Silicon. Отключите при нестабильной работе.",
            symbol: "exclamationmark.triangle.fill"
        )

        let diarSubCaption = NSTextField(labelWithString:
            "pyannote.audio через Metal. На M4 + macOS 26 возможен краш инициализации — выключайте, если backend падает на старте."
        )
        diarSubCaption.font = KrabEarTheme.Typography.caption
        diarSubCaption.textColor = KrabEarTheme.Colors.textDisabled
        diarSubCaption.lineBreakMode = .byWordWrapping
        diarSubCaption.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        diarSubCaption.tag = 202401 // Linked к syncSettingsControls alpha animation
        diarSubCaption.setAccessibilityLabel(
            "Подсказка: pyannote.audio через Metal, возможен краш на M4+macOS 26"
        )

        // Оборачиваем в вертикальный стек: строка-toggle + sub-caption
        let diarRowInner = makeSwitchRow(
            label: "Разделение говорящих",
            button: diarizationButton,
            statusBadge: gpuCrashBadge
        )
        let diarRow = NSStackView()
        diarRow.orientation = .vertical
        diarRow.alignment = .leading
        diarRow.spacing = KrabEarTheme.Metrics.tight
        diarRow.addArrangedSubview(diarRowInner)
        diarRow.addArrangedSubview(diarSubCaption)

        // 2. Quality Profile Row — Gemini design: makeSettingRow.
        //    Reparents global `qualitySelector` (был в settingsRow1 / Recording секции).
        qualitySelector.setAccessibilityLabel(
            "Выбор профиля качества STT: Balanced для скорости, Max для точности"
        )

        let qualRow = makeSettingRow(
            label: "Качество распознавания",
            description: "Balanced — whisper-large-v3-turbo, быстро. Max — candidate chain (v3 → turbo), точнее на сложных записях, ~2× медленнее.",
            control: qualitySelector
        )

        // 3. GigaAM-RNNT v2 toggle — RU специализированная модель (опционально).
        //    Pre-flight check на venv (~/.venv_krab_ear_gigaam) делается в handler.
        gigaamEnabledButton.title = ""
        gigaamEnabledButton.setButtonType(.switch)
        gigaamEnabledButton.setAccessibilityLabel(
            "Включить GigaAM-RNNT v2 для русского STT — ~2.5× выше точность на разговорной речи. Требует установки venv через scripts/install_gigaam_venv.command."
        )

        let gigaamBetaBadge = makeBadge(
            text: "RU only",
            color: KrabEarTheme.Colors.accent,
            tooltip: "Работает только для русского. Для en/es остаётся whisper."
        )

        let gigaamSubCaption = NSTextField(labelWithString:
            "WER ~3.8% vs whisper ~9.8% на русском. Hard-limit ~25 сек на одну диктовку (long-form пока не поддерживается). Требует install_gigaam_venv.command (одноразово)."
        )
        gigaamSubCaption.font = KrabEarTheme.Typography.caption
        gigaamSubCaption.textColor = KrabEarTheme.Colors.textDisabled
        gigaamSubCaption.lineBreakMode = .byWordWrapping
        gigaamSubCaption.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let gigaamRowInner = makeSwitchRow(
            label: "GigaAM-RNNT v2 для русского",
            button: gigaamEnabledButton,
            statusBadge: gigaamBetaBadge
        )
        let gigaamRow = NSStackView()
        gigaamRow.orientation = .vertical
        gigaamRow.alignment = .leading
        gigaamRow.spacing = KrabEarTheme.Metrics.tight
        gigaamRow.addArrangedSubview(gigaamRowInner)
        gigaamRow.addArrangedSubview(gigaamSubCaption)

        // 4. STT engine indicator — read-only label showing last active engine.
        sttEngineLabel.font = KrabEarTheme.Typography.body
        sttEngineLabel.textColor = KrabEarTheme.Colors.accent
        sttEngineLabel.setContentHuggingPriority(.required, for: .horizontal)
        sttEngineLabel.setAccessibilityLabel("Последний использованный STT движок")

        let engineRow = makeSettingRow(
            label: "Активный STT движок",
            description: "Движок, использованный при последней транскрибации. Обновляется автоматически.",
            control: sttEngineLabel
        )

        // Assemble card (ThemeCardView content stack уже vertical + leading).
        card.contentStackView.addArrangedSubview(diarRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(qualRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(gigaamRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(engineRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Translation Section

    /// Секция «Перевод» в Live Translation tab.
    /// Строки: режим перевода, сеть, стиль, swap, глоссарий.
    /// Переписана через makeSettingRow / makeSwitchRow / makeSeparator (Path A).
    func buildTranslationSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "live_translation_settings",
            title: "Настройки перевода",
            isExpanded: true,
            iconSymbol: "globe"
        )
        let card = ThemeCardView()

        // 1. Translation mode
        translationSelector.setAccessibilityLabel("Режим перевода: направление или выключен")
        let modeRow = makeSettingRow(
            label: "Перевод",
            description: "Off — без перевода. Auto — определяет язык автоматически. Bilingual — добавляет оба языка в вывод.",
            control: translationSelector
        )

        // 2. Network mode
        networkSelector.setAccessibilityLabel("Режим сети: offline или online")
        let networkRow = makeSettingRow(
            label: "Сеть",
            description: "Offline default — STT только локально. Offline strict — никаких внешних запросов. Online opt-in — разрешить облако явно.",
            control: networkSelector
        )

        // 3. Translation style
        translationStyleSelector.setAccessibilityLabel("Стиль перевода: нейтральный, разговорный или официальный")
        let styleRow = makeSettingRow(
            label: "Стиль перевода",
            description: "Neutral — стандартный. Chat — разговорный. Formal — официальный.",
            control: translationStyleSelector
        )

        card.contentStackView.addArrangedSubview(modeRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(networkRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(styleRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - LLM Section

    /// Секция «LLM постобработка» в Dictation tab.
    /// Строки: toggle LLM rewrite + модель.
    /// Переписана через makeSwitchRow / makeSettingRow / makeSeparator (Path A).
    func buildLLMSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_llm",
            title: "LLM постобработка",
            isExpanded: false,
            iconSymbol: "brain"
        )
        let card = ThemeCardView()

        // 1. LLM rewrite toggle
        llmRewriteButton.title = ""
        llmRewriteButton.setButtonType(.switch)
        llmRewriteButton.setAccessibilityLabel("Включить LLM постобработку текста транскрипции через LM Studio")
        let llmBetaBadge = makeBadge(
            text: "бета",
            color: KrabEarTheme.Colors.warning,
            tooltip: "LM Studio должен быть запущен локально с совместимой моделью.",
            symbol: "exclamationmark.triangle.fill"
        )
        let llmToggleRow = makeSwitchRow(
            label: "LLM постобработка",
            description: "Пропускает транскрипт через локальную LLM (LM Studio) для улучшения читаемости. Требует запущенный LM Studio.",
            button: llmRewriteButton,
            statusBadge: llmBetaBadge
        )

        // 2. Model selector
        llmModelSelector.setAccessibilityLabel("Выбор LLM-модели для постобработки")
        let modelRow = makeSettingRow(
            label: "Модель LLM",
            description: "Должна совпадать с именем загруженной модели в LM Studio.",
            control: llmModelSelector
        )

        card.contentStackView.addArrangedSubview(llmToggleRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(modelRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Hotkey Section

    /// Секция «Горячие клавиши» в Dictation tab.
    /// Строки: hotkey selector + hotkey mode (toggle/hold) + hotkey profile.
    /// Переписана через makeSettingRow / makeSeparator (Path A).
    func buildHotkeySection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_hotkeys",
            title: "Горячие клавиши",
            isExpanded: false,
            iconSymbol: "keyboard"
        )
        let card = ThemeCardView()

        // 1. Hotkey key selector
        hotkeySelector.setAccessibilityLabel("Выбор клавиши-триггера: Right Option, Left Option или любой Option")
        let hotkeyRow = makeSettingRow(
            label: "Клавиша записи",
            description: "Выберите клавишу для управления записью.",
            control: hotkeySelector
        )

        // 2. Hotkey mode: toggle vs hold
        hotkeyModeToggleRadio.target = self
        hotkeyModeToggleRadio.action = #selector(onHotkeyModeChanged)
        hotkeyModeToggleRadio.setAccessibilityLabel("Режим Toggle: нажать — старт, нажать снова — стоп")
        hotkeyModeHoldRadio.target = self
        hotkeyModeHoldRadio.action = #selector(onHotkeyModeChanged)
        hotkeyModeHoldRadio.setAccessibilityLabel("Режим Hold: зажать — пишет, отпустить — стоп")
        // Группируем в вертикальный стек
        let radioStack = NSStackView(views: [hotkeyModeToggleRadio, hotkeyModeHoldRadio])
        radioStack.orientation = .vertical
        radioStack.alignment = .leading
        radioStack.spacing = 4
        let modeRow = makeSettingRow(
            label: "Режим клавиши",
            description: "Toggle — классический режим. Hold — зажал клавишу → пишет, отпустил → транскрибирует.",
            control: radioStack
        )

        // 3. Hotkey profile
        hotkeyProfileSelector.setAccessibilityLabel("Профиль горячей клавиши: Default, Meeting или Translation")
        let profileRow = makeSettingRow(
            label: "Профиль",
            description: "Default — стандартные параметры. Meeting — оптимизация для совещаний. Translation — автоматический перевод.",
            control: hotkeyProfileSelector
        )

        card.contentStackView.addArrangedSubview(hotkeyRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(modeRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(profileRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - System Section

    /// Секция «Система» в Dictation tab.
    /// Строки: audioDucking toggle + slider, overlay opacity slider, autoStart toggle, dockIcon toggle.
    /// Переписана через makeSwitchRow / makeSettingRow / makeSeparator (Path A).
    func buildSystemSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_system_settings",
            title: "Система",
            isExpanded: false,
            iconSymbol: "gearshape"
        )
        let card = ThemeCardView()

        // 1. Audio ducking toggle
        audioDuckingButton.title = ""
        audioDuckingButton.setButtonType(.switch)
        audioDuckingButton.setAccessibilityLabel("Приглушать системный звук во время записи")
        let duckingToggleRow = makeSwitchRow(
            label: "Приглушение звука при записи",
            description: "Снижает громкость системного аудио во время диктовки — уменьшает обратную связь и эхо.",
            button: audioDuckingButton
        )

        // 2. Audio ducking percent slider (complex layout — not helper-ified)
        //    Slider + value label together don't fit makeSettingRow's single-control model;
        //    assembled manually consistent with the existing pattern used in buildAudioPipelineSection.
        let duckLabelField = NSTextField(labelWithString: "Громкость при записи")
        duckLabelField.font = KrabEarTheme.Typography.body
        duckLabelField.textColor = KrabEarTheme.Colors.textPrimary
        let duckSpacer = NSView()
        duckSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        duckSpacer.setAccessibilityElement(false)
        let duckSliderStack = NSStackView()
        duckSliderStack.orientation = .horizontal
        duckSliderStack.spacing = KrabEarTheme.Metrics.tight
        duckSliderStack.alignment = .centerY
        audioDuckingValueLabel.font = KrabEarTheme.Typography.monospace.tabular()
        duckSliderStack.addArrangedSubview(audioDuckingSlider)
        duckSliderStack.addArrangedSubview(audioDuckingValueLabel)
        let duckRow = NSStackView()
        duckRow.orientation = .horizontal
        duckRow.distribution = .fill
        duckRow.alignment = .centerY
        duckRow.spacing = KrabEarTheme.Metrics.standard
        duckRow.addArrangedSubview(duckLabelField)
        duckRow.addArrangedSubview(duckSpacer)
        duckRow.addArrangedSubview(duckSliderStack)

        // 3. Overlay opacity slider (same complex layout)
        let overlayLabelField = NSTextField(labelWithString: "Прозрачность Live Preview")
        overlayLabelField.font = KrabEarTheme.Typography.body
        overlayLabelField.textColor = KrabEarTheme.Colors.textPrimary
        let overlaySpacer = NSView()
        overlaySpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        overlaySpacer.setAccessibilityElement(false)
        let overlaySliderStack = NSStackView()
        overlaySliderStack.orientation = .horizontal
        overlaySliderStack.spacing = KrabEarTheme.Metrics.tight
        overlaySliderStack.alignment = .centerY
        overlayOpacityValueLabel.font = KrabEarTheme.Typography.monospace.tabular()
        overlaySliderStack.addArrangedSubview(overlayOpacitySlider)
        overlaySliderStack.addArrangedSubview(overlayOpacityValueLabel)
        let overlayRow = NSStackView()
        overlayRow.orientation = .horizontal
        overlayRow.distribution = .fill
        overlayRow.alignment = .centerY
        overlayRow.spacing = KrabEarTheme.Metrics.standard
        overlayRow.addArrangedSubview(overlayLabelField)
        overlayRow.addArrangedSubview(overlaySpacer)
        overlayRow.addArrangedSubview(overlaySliderStack)

        // 4. Autostart toggle
        autoStartButton.title = ""
        autoStartButton.setButtonType(.switch)
        autoStartButton.setAccessibilityLabel("Запускать Krab Ear автоматически при входе в систему")
        let autoStartRow = makeSwitchRow(
            label: "Автозапуск при старте macOS",
            description: "Krab Ear запускается автоматически через LaunchAgent при входе в систему.",
            button: autoStartButton
        )

        // 5. Dock icon toggle
        dockIconButton.title = ""
        dockIconButton.setButtonType(.switch)
        dockIconButton.setAccessibilityLabel("Показывать иконку в Dock")
        let dockRow = makeSwitchRow(
            label: "Иконка в Dock",
            description: "Показывать иконку приложения в Dock. По умолчанию Krab Ear работает только в строке меню.",
            button: dockIconButton
        )

        // 6. A/B variant toggle — Claude Design compact layout
        let abToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onUseClaudeDesignChanged))
        abToggle.setButtonType(.switch)
        abToggle.state = UserDefaults.standard.useClaudeDesignVariant ? .on : .off
        abToggle.tag = 202501
        let abRow = makeSwitchRow(
            label: "Использовать компактную вёрстку (Claude Design A/B)",
            description: "Переключает панель настроек между Gemini-дизайном (по умолчанию) и компактным Claude Design. Изменение вступает в силу при следующем открытии панели.",
            button: abToggle
        )

        card.contentStackView.addArrangedSubview(makeSubhead("ЗВУК"))
        card.contentStackView.addArrangedSubview(duckingToggleRow)
        card.contentStackView.addArrangedSubview(duckRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(makeSubhead("ИНТЕРФЕЙС И ЗАПУСК"))
        card.contentStackView.addArrangedSubview(overlayRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(autoStartRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(dockRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(makeSubhead("ДИЗАЙН"))
        card.contentStackView.addArrangedSubview(abRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Voice Assistant Section (PR 1.5, Path A refactor)

    /// Секция «Разговор с AI» в Settings tab.
    /// Содержит:
    ///   1. Toggle «Включить горячую клавишу» (Right Option double-tap)
    ///   2. Toggle «Детектор пробуждения Краб» (Porcupine, default OFF)
    ///   3. Dropdown «Предпочтительный движок» (auto / moshi / seamless)
    ///   4. Dropdown «Мозг LLM» (auto / qwen3-30b / qwen3-4b)
    /// Переписана через makeSwitchRow / makeSettingRow / makeSeparator (Path A).
    func buildVoiceAssistantSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "settings_voice_assistant",
            title: "Разговор с AI",
            isExpanded: false,
            iconSymbol: "sparkles"
        )

        let card = ThemeCardView()

        // 1. Hotkey double-tap toggle
        vaHotkeyToggle.title = ""
        vaHotkeyToggle.setButtonType(.switch)
        vaHotkeyToggle.setAccessibilityLabel("Включить запуск разговора с AI двойным тапом Right Option")
        let hotkeyToggleRow = makeSwitchRow(
            label: "Горячая клавиша (двойной тап Right Option)",
            description: "Двойной тап Right Option за 300 мс запускает или останавливает разговор с AI. Одиночный hold сохраняет диктовку.",
            button: vaHotkeyToggle
        )

        // 2. Wake word toggle (openWakeWord в backend, IPC-поллинг — spec 2026-07-05)
        vaWakeWordToggle.title = ""
        vaWakeWordToggle.setButtonType(.switch)
        vaWakeWordToggle.setAccessibilityLabel("Включить детектор слова-пробуждения (openWakeWord)")
        let wakePrivacyBadge = makeBadge(
            text: "приватность",
            color: KrabEarTheme.Colors.textSecondary,
            tooltip: "Слушает только слово-пробуждение локально (openWakeWord, Apache-2.0, без ключей). Ставится на паузу при записи, разговоре и в privacy mode. По умолчанию выключен.",
            symbol: "lock.fill"
        )
        let wakeWordRow = makeSwitchRow(
            label: "Детектор слова-пробуждения",
            description: "openWakeWord в backend — без ключей и регистраций. Скажи слово-пробуждение — откроется «Разговор с AI». По умолчанию выключен — приватность.",
            button: vaWakeWordToggle,
            statusBadge: wakePrivacyBadge
        )

        // 2a. Статус движка
        vaWakeWordStatusLabel.font = NSFont.systemFont(ofSize: 11)
        vaWakeWordStatusLabel.textColor = KrabEarTheme.Colors.textSecondary
        vaWakeWordStatusLabel.lineBreakMode = .byTruncatingTail
        let wakeStatusRow = makeSettingRow(
            label: "Статус",
            description: "Установлен ли openWakeWord в Python-окружении backend.",
            control: vaWakeWordStatusLabel
        )

        // 2b. Модель (слово-пробуждение)
        vaWakeWordModelSelector.removeAllItems()
        vaWakeWordModelSelector.addItems(withTitles: ["hey_jarvis", "alexa", "hey_mycroft"])
        vaWakeWordModelSelector.setAccessibilityLabel("Модель слова-пробуждения")
        let savedWakeModel = UserDefaults.standard.string(forKey: "KrabEar_WakeWordModel") ?? "hey_jarvis"
        vaWakeWordModelSelector.selectItem(withTitle: savedWakeModel)
        let wakeModelRow = makeSettingRow(
            label: "Слово-пробуждение",
            description: "Встроенные модели openWakeWord (англ.). Кастомная «Краб» (.onnx в wake_word_models/) появится в списке автоматически.",
            control: vaWakeWordModelSelector
        )

        // 2c. Порог уверенности
        vaWakeWordThresholdSlider.isContinuous = false
        vaWakeWordThresholdSlider.setAccessibilityLabel("Порог уверенности детектора слова-пробуждения")
        let savedWakeThreshold = UserDefaults.standard.double(forKey: "KrabEar_WakeWordThreshold")
        vaWakeWordThresholdSlider.doubleValue = savedWakeThreshold > 0 ? savedWakeThreshold : 0.5
        let wakeThresholdRow = makeSettingRow(
            label: "Порог уверенности",
            description: "Ниже — чувствительнее (больше ложных срабатываний), выше — строже. По умолчанию 0.5.",
            control: vaWakeWordThresholdSlider
        )

        // 3. Engine selector
        vaEngineSelector.removeAllItems()
        vaEngineSelector.addItems(withTitles: ["Авто", "Moshi (EN, 160 мс)", "SeamlessM4T (RU/ES, 1–2 с)"])
        vaEngineSelector.setAccessibilityLabel("Предпочтительный движок для разговора с AI")
        let engineRow = makeSettingRow(
            label: "Движок",
            description: "Moshi — для английского, быстрее. SeamlessM4T — для русского и других языков.",
            control: vaEngineSelector
        )

        // 4. Brain (LLM) selector
        vaBrainSelector.removeAllItems()
        vaBrainSelector.addItems(withTitles: ["Авто", "qwen3-30b (точнее, 17 GB)", "qwen3-4b (быстрее, 4 GB)"])
        vaBrainSelector.setAccessibilityLabel("Выбор LLM-мозга для разговора с AI")
        let brainRow = makeSettingRow(
            label: "Мозг LLM",
            description: "qwen3-30b — лучшее качество русского. qwen3-4b — быстро, меньше памяти.",
            control: vaBrainSelector
        )

        card.contentStackView.addArrangedSubview(hotkeyToggleRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(wakeWordRow)
        card.contentStackView.addArrangedSubview(wakeStatusRow)
        card.contentStackView.addArrangedSubview(wakeModelRow)
        card.contentStackView.addArrangedSubview(wakeThresholdRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(engineRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(brainRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - VA Settings handlers

    @objc func onVAHotkeyToggleChanged() {
        let enabled = vaHotkeyToggle.state == .on
        UserDefaults.standard.set(enabled, forKey: "KrabEar_ConversationHotkeyEnabled")
        // Применить немедленно через applyConversationHotkeyEnabled
        if let appDelegate = NSApp.delegate as? AgentAppDelegate {
            appDelegate.applyConversationHotkeyEnabled(enabled)
        }
    }

    @objc func onVAWakeWordToggleChanged() {
        let enabled = vaWakeWordToggle.state == .on
        UserDefaults.standard.set(enabled, forKey: "KrabEar_WakeWordEnabled")
        if let appDelegate = NSApp.delegate as? AgentAppDelegate {
            appDelegate.applyWakeWordEnabled(enabled)
        }
        refreshWakeWordStatusRow()
    }

    @objc func onVAWakeWordModelChanged() {
        let model = vaWakeWordModelSelector.titleOfSelectedItem ?? "hey_jarvis"
        UserDefaults.standard.set(model, forKey: "KrabEar_WakeWordModel")
        restartWakeWordIfEnabled()
    }

    @objc func onVAWakeWordThresholdChanged() {
        UserDefaults.standard.set(vaWakeWordThresholdSlider.doubleValue, forKey: "KrabEar_WakeWordThreshold")
        restartWakeWordIfEnabled()
    }

    /// Смена модели/порога на лету: пере-старт wake word сессии, если тумблер включён.
    private func restartWakeWordIfEnabled() {
        guard UserDefaults.standard.bool(forKey: "KrabEar_WakeWordEnabled"),
              let appDelegate = NSApp.delegate as? AgentAppDelegate else { return }
        appDelegate.wakeWordPoller?.deactivate()
        appDelegate.setupWakeWordListenerIfEnabled()
    }

    /// Off-main запрос wake_word_status + wake_word_list_models для статус-строки
    /// и актуального списка моделей (кастомные .onnx из wake_word_models/).
    func refreshWakeWordStatusRow() {
        let ipc = self.ipcClient
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let status = try? ipc.call(method: "wake_word_status", params: [:])
            let models = try? ipc.call(method: "wake_word_list_models", params: [:])
            DispatchQueue.main.async {
                guard let self else { return }
                let result = status?["result"] as? [String: Any]
                let available = result?["engine_available"] as? Bool ?? false
                self.vaWakeWordStatusLabel.stringValue = available
                    ? "openWakeWord: установлен"
                    : "openWakeWord: не установлен. Установка: pip install -r KrabEar/requirements-wakeword.txt"
                if let list = (models?["result"] as? [String: Any])?["models"] as? [[String: Any]] {
                    let names = list.compactMap { $0["name"] as? String }
                    if !names.isEmpty {
                        let selected = self.vaWakeWordModelSelector.titleOfSelectedItem
                        self.vaWakeWordModelSelector.removeAllItems()
                        self.vaWakeWordModelSelector.addItems(withTitles: names)
                        if let selected, names.contains(selected) {
                            self.vaWakeWordModelSelector.selectItem(withTitle: selected)
                        }
                    }
                }
            }
        }
    }

    @objc func onVAEngineSelectorChanged() {
        let idx = vaEngineSelector.indexOfSelectedItem
        let value: String
        switch idx {
        case 1: value = "moshi"
        case 2: value = "seamless"
        default: value = "auto"
        }
        UserDefaults.standard.set(value, forKey: "KrabEar_ConversationEngine")
    }

    @objc func onVABrainSelectorChanged() {
        let idx = vaBrainSelector.indexOfSelectedItem
        let value: String
        switch idx {
        case 1: value = "qwen3-30b"
        case 2: value = "qwen3-4b"
        default: value = "auto"
        }
        UserDefaults.standard.set(value, forKey: "KrabEar_ConversationBrain")
    }

    /// Синхронизировать состояние VA-контролей с UserDefaults.
    func syncVoiceAssistantControls() {
        let hotkeyEnabled = UserDefaults.standard.bool(forKey: "KrabEar_ConversationHotkeyEnabled")
        // Если ключ не установлен → дефолт ON (удобно для первого запуска)
        let hotkeyEnabledDefault = UserDefaults.standard.object(forKey: "KrabEar_ConversationHotkeyEnabled") != nil
            ? hotkeyEnabled : true
        vaHotkeyToggle.state = hotkeyEnabledDefault ? .on : .off

        let wakeWordEnabled = UserDefaults.standard.bool(forKey: "KrabEar_WakeWordEnabled")
        vaWakeWordToggle.state = wakeWordEnabled ? .on : .off
        refreshWakeWordStatusRow()

        let engine = UserDefaults.standard.string(forKey: "KrabEar_ConversationEngine") ?? "auto"
        switch engine {
        case "moshi":    vaEngineSelector.selectItem(at: 1)
        case "seamless": vaEngineSelector.selectItem(at: 2)
        default:         vaEngineSelector.selectItem(at: 0)
        }

        let brain = UserDefaults.standard.string(forKey: "KrabEar_ConversationBrain") ?? "auto"
        switch brain {
        case "qwen3-30b": vaBrainSelector.selectItem(at: 1)
        case "qwen3-4b":  vaBrainSelector.selectItem(at: 2)
        default:          vaBrainSelector.selectItem(at: 0)
        }
    }

    // MARK: - Quick Preset Section

    func buildQuickPresetSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "settings_quick_presets",
            title: "Пресеты записи",
            isExpanded: true,
            iconSymbol: "slider.horizontal.3"
        )
        let card = ThemeCardView()
        let buttonStack = NSStackView()
        buttonStack.orientation = .horizontal
        buttonStack.distribution = .fillEqually
        buttonStack.spacing = KrabEarTheme.Metrics.standard
        buttonStack.translatesAutoresizingMaskIntoConstraints = false
        let presetIds = ["default", "meeting", "translation", "call_recording"]
        let presetLabels = ["Default (D)", "Meeting (M)", "Translation (T)", "Call (C)"]
        let activePreset = UserDefaults.standard.string(forKey: "KrabEar_ActivePreset") ?? "default"
        for tag in 0..<presetIds.count {
            let btn = NSButton()
            btn.title = presetLabels[tag]
            btn.bezelStyle = .rounded
            btn.tag = tag
            btn.target = self
            btn.action = #selector(onQuickPresetButtonClicked(_:))
            btn.state = presetIds[tag] == activePreset ? .on : .off
            buttonStack.addArrangedSubview(btn)
        }
        let descLabel = NSTextField(labelWithString: "Cmd+Shift+P — следующий пресет")
        descLabel.font = KrabEarTheme.Typography.caption
        descLabel.textColor = KrabEarTheme.Colors.textSecondary
        descLabel.translatesAutoresizingMaskIntoConstraints = false
        card.contentStackView.addArrangedSubview(buttonStack)
        card.contentStackView.addArrangedSubview(descLabel)
        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Quick Capture Section (C3a Task 3)

    /// Настройки «Быстрые заметки» (спека 2026-07-16-c3-quick-capture-design.md §3.3):
    /// opt-in дублирование сохранённой заметки в Apple Notes / Obsidian + выбор
    /// комбинации хоткея старт/стоп. Ни один из трёх ключей НЕ хранится в
    /// кэшируемой AgentSettings — main+QuickCapture.swift читает их живьём через
    /// get_settings (чекбокс должен действовать сразу, без ожидания следующего
    /// цикла обновления кэша), поэтому эта секция сама гидратирует свои контролы
    /// отдельным off-main запросом (refreshQuickCaptureSectionState, образец
    /// refreshWakeWordStatusRow).
    func buildQuickCaptureSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "settings_quick_capture",
            title: "Быстрые заметки",
            isExpanded: true,
            iconSymbol: "note.text.badge.plus"
        )
        let card = ThemeCardView()

        quickCaptureNotesButton.title = ""
        quickCaptureNotesButton.setButtonType(.switch)
        quickCaptureNotesButton.target = self
        quickCaptureNotesButton.action = #selector(onQuickCaptureNotesChanged)
        quickCaptureNotesButton.setAccessibilityLabel("Дублировать сохранённую быструю заметку в Apple Notes")
        let notesRow = makeSwitchRow(
            label: "Дублировать в Apple Notes",
            description: "После сохранения заметки создаёт копию в Notes.app (папка «Krab Ear»). "
                + "Режим приватности блокирует отправку.",
            button: quickCaptureNotesButton
        )

        quickCaptureObsidianButton.title = ""
        quickCaptureObsidianButton.setButtonType(.switch)
        quickCaptureObsidianButton.target = self
        quickCaptureObsidianButton.action = #selector(onQuickCaptureObsidianChanged)
        quickCaptureObsidianButton.setAccessibilityLabel("Синхронизировать сохранённую быструю заметку с Obsidian vault")
        let obsidianRow = makeSwitchRow(
            label: "Синхронизировать в Obsidian",
            description: "Форс-синк заметки в настроенный Obsidian vault. Без настроенного vault синк молча пропускается.",
            button: quickCaptureObsidianButton
        )

        // Глиф-гейт (feedback_glyph_gate_swift_workers): символ Command и символ
        // Option уже рендерятся в проекте (DictationHeroCard "⌥ Right",
        // LiveSubsSettings "Cmd+⌥+Shift+L"), но символы Shift и Control — 0
        // вхождений во всём native/ до этого коммита → НЕ вводим их, используем
        // ASCII "Shift"/"Ctrl" по прецеденту "Cmd+Shift+P" (buildQuickPresetSection)
        // и "Cmd+Ctrl+Z" (pasteUndoButton title).
        quickCaptureHotkeySelector.addItems(withTitles: ["Cmd+Shift+N", "Cmd+⌥+N", "Ctrl+Shift+N"])
        quickCaptureHotkeySelector.target = self
        quickCaptureHotkeySelector.action = #selector(onQuickCaptureHotkeyChanged)
        quickCaptureHotkeySelector.setAccessibilityLabel("Комбинация хоткея старт/стоп быстрой заметки")
        let hotkeyRow = makeSettingRow(
            label: "Хоткей заметки",
            description: "Cmd+Shift+N по умолчанию затеняет «Новую папку» в Finder — при конфликте выберите другую комбинацию.",
            control: quickCaptureHotkeySelector
        )

        card.contentStackView.addArrangedSubview(notesRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(obsidianRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(hotkeyRow)

        section.contentStackView.addArrangedSubview(card)
        refreshQuickCaptureSectionState()
        return section
    }

    /// Фиксированный порядок ID хоткеев — индекс совпадает с порядком пунктов
    /// дропдауна (Cmd+Shift+N / Cmd+⌥+N / Ctrl+Shift+N) и с allowlist в
    /// settings_validator.py _ENUM_FIELDS["quick_capture_hotkey"].
    private static let quickCaptureHotkeyIds = ["cmd_shift_n", "cmd_opt_n", "ctrl_shift_n"]

    @objc func onQuickCaptureNotesChanged() {
        let isOn = quickCaptureNotesButton.state == .on
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async {
            _ = try? ipcClient.call(method: "set_settings", params: ["quick_capture_send_to_notes": isOn])
        }
    }

    @objc func onQuickCaptureObsidianChanged() {
        let isOn = quickCaptureObsidianButton.state == .on
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async {
            _ = try? ipcClient.call(method: "set_settings", params: ["quick_capture_obsidian_sync": isOn])
        }
    }

    /// Смена комбинации → set_settings off-main → пере-арм монитора на новую
    /// комбинацию (stop затем start; start сам живьём перечитает
    /// quick_capture_hotkey через get_settings — см. main+QuickCapture.swift).
    @objc func onQuickCaptureHotkeyChanged() {
        let ids = HistoryPanelController.quickCaptureHotkeyIds
        let idx = quickCaptureHotkeySelector.indexOfSelectedItem
        guard idx >= 0, idx < ids.count else { return }
        let hotkeyId = ids[idx]
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async {
            _ = try? ipcClient.call(method: "set_settings", params: ["quick_capture_hotkey": hotkeyId])
            DispatchQueue.main.async {
                guard let appDelegate = NSApp.delegate as? AgentAppDelegate else { return }
                appDelegate.stopQuickCaptureHotkeyMonitor()
                appDelegate.startQuickCaptureHotkeyMonitor()
            }
        }
    }

    /// Гидратирует чекбоксы/дропдаун реальным состоянием backend'а (get_settings
    /// живьём, off-main) — без этого шага контролы показывали бы стейл дефолт
    /// (off / Cmd+Shift+N) после каждого пересоздания панели, даже если
    /// пользователь ранее включил чекбокс в предыдущей сессии.
    func refreshQuickCaptureSectionState() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let resp = try? ipcClient.call(method: "get_settings", params: [:]),
                  let result = resp["result"] as? [String: Any] else { return }
            DispatchQueue.main.async {
                guard let self else { return }
                self.quickCaptureNotesButton.state = ((result["quick_capture_send_to_notes"] as? Bool) == true) ? .on : .off
                self.quickCaptureObsidianButton.state = ((result["quick_capture_obsidian_sync"] as? Bool) == true) ? .on : .off
                let hotkeyId = (result["quick_capture_hotkey"] as? String) ?? "cmd_shift_n"
                let idx = HistoryPanelController.quickCaptureHotkeyIds.firstIndex(of: hotkeyId) ?? 0
                self.quickCaptureHotkeySelector.selectItem(at: idx)
            }
        }
    }

    // MARK: - Privacy & Security Section (Phase D.5)

    /// Секция «Безопасность и приватность» в Dictation tab.
    /// Содержит единственный toggle Privacy Mode:
    ///   - Sentry telemetry полностью отключается.
    ///   - Перевод принудительно переводится в offline_only режим.
    ///   - LM Studio (127.0.0.1) остаётся разрешённым — он локальный.
    func buildPrivacySection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "settings_privacy_security",
            title: "Безопасность и приватность",
            isExpanded: true,
            iconSymbol: "shield"
        )
        let card = ThemeCardView()

        privacyModeButton.title = ""
        privacyModeButton.setButtonType(.switch)
        privacyModeButton.target = self
        privacyModeButton.action = #selector(onPrivacyModeChanged)
        privacyModeButton.setAccessibilityLabel(
            "Режим приватности: отключает Sentry telemetry и принудительно переводит перевод в offline-режим."
        )

        let shieldBadge = makeBadge(
            text: "приватность",
            color: KrabEarTheme.Colors.accent,
            tooltip: "Данные не покидают устройство. LM Studio (127.0.0.1) по-прежнему доступен.",
            symbol: "lock.fill"
        )

        let privacyRow = makeSwitchRow(
            label: "Режим приватности",
            description: "Отключает Sentry crash-telemetry и принудительно переводит перевод в offline-режим. Никакие данные не покидают устройство. LM Studio (127.0.0.1) остаётся разрешённым. По умолчанию выключен — opt-in.",
            button: privacyModeButton,
            statusBadge: shieldBadge
        )

        card.contentStackView.addArrangedSubview(privacyRow)
        card.contentStackView.addArrangedSubview(makeSeparator())

        // Кнопка «Просмотр audit log» — открывает PrivacyAuditViewerWindowController
        let auditButton = ThemeSecondaryButton(
            title: "Просмотр audit log",
            target: self,
            action: #selector(onShowPrivacyAuditLog)
        )
        auditButton.setAccessibilityLabel(
            "Открыть журнал событий режима конфиденциальности: заблокированные Sentry-отчёты, принудительный offline-перевод."
        )

        let auditRow = makeSettingRow(
            label: "Журнал событий",
            description: "События режима приватности: Sentry blocked, translate forced offline.",
            control: auditButton
        )
        card.contentStackView.addArrangedSubview(auditRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Privacy Audit Viewer

    @objc func onShowPrivacyAuditLog() {
        let viewer = PrivacyAuditViewerWindowController(ipcClient: ipcClient)
        // Держим сильную ссылку пока окно открыто
        objc_setAssociatedObject(
            self,
            &PrivacyAuditAssocKeys.auditViewer,
            viewer,
            .OBJC_ASSOCIATION_RETAIN
        )
        viewer.showAndLoad()
    }

    @objc func onQuickPresetButtonClicked(_ sender: NSButton) {
        let presetIds = ["default", "meeting", "translation", "call_recording"]
        guard sender.tag >= 0, sender.tag < presetIds.count else { return }
        let presetId = presetIds[sender.tag]
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let response = try self.ipcClient.call(method: "apply_profile_preset", params: ["profile": presetId])
                guard let result = response["result"] as? [String: Any] else { return }
                DispatchQueue.main.async {
                    let updated = self.settingsUpdater(result)
                    self.syncSettingsControls(using: updated)
                    UserDefaults.standard.set(presetId, forKey: "KrabEar_ActivePreset")
                    if let appDelegate = NSApp.delegate as? AgentAppDelegate {
                        appDelegate.refreshStatusItemTitle()
                        appDelegate.rebuildStatusMenu()
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self.showInfoAlert(title: "Ошибка пресета", body: error.localizedDescription)
                }
            }
        }
    }

    // MARK: - STT Engine Indicator

    /// Преобразует сырое значение engine (из get_diagnostics stt.last_engine) в читаемое имя.
    nonisolated func humanReadableSTTEngine(_ raw: String?) -> String {
        guard let raw, !raw.isEmpty else { return "—" }
        if raw.hasPrefix("gigaam-") {
            let mode = raw.replacingOccurrences(of: "gigaam-", with: "").uppercased()
            return "GigaAM (\(mode))"
        }
        if raw.contains("whisper-large-v3-mlx") { return "Whisper Large v3 (MLX)" }
        if raw.contains("russian") || raw.contains("antony66") { return "Whisper RU fine-tune" }
        if raw.contains("turbo") { return "Whisper Turbo (MLX)" }
        if raw.contains("whisper") { return "Whisper (MLX)" }
        if raw == "remote" { return "Whisper Remote" }
        if raw == "vad_skip" { return "VAD пропуск (тишина)" }
        return raw
    }

    /// Асинхронно запрашивает get_diagnostics и обновляет sttEngineLabel на main thread.
    /// Вызывается при открытии вкладки Settings и после каждой записи.
    func fetchAndUpdateSTTEngineLabel() {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let engineRaw: String?
            do {
                let resp = try ipc.call(method: "get_diagnostics", params: [:])
                let result = resp["result"] as? [String: Any]
                let stt = result?["stt"] as? [String: Any]
                engineRaw = stt?["last_engine"] as? String
            } catch {
                engineRaw = nil
            }
            let label = self.humanReadableSTTEngine(engineRaw)
            DispatchQueue.main.async { [weak self] in
                self?.sttEngineLabel.stringValue = label
            }
        }
    }

}