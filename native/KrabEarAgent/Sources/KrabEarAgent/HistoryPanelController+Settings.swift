/*
 Расширение HistoryPanelController: обработчики изменения настроек.

 Содержит все @objc-методы onXxxChanged, applySettingsPatch и syncSettingsControls.
*/

import AppKit
import Foundation

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

    @objc func onQuickEditChanged() {
        guard !isSyncingSettings else { return }
        let enabled = quickEditButton.state == .on
        applySettingsPatch(["quick_edit_enabled": enabled])
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
        applySettingsPatch(["llm_model": selectedModel])
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

        llmRewriteButton.state = settings.llmRewriteEnabled ? .on : .off
        if let idx = llmModelSelector.itemTitles.firstIndex(of: settings.llmModel) {
            llmModelSelector.selectItem(at: idx)
        }
        llmModelSelector.isEnabled = settings.llmRewriteEnabled
        glossaryStatusLabel.stringValue = "Глоссарий: \(settings.translationGlossary.count)"

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
            hStack.widthAnchor.constraint(equalTo: vStack.widthAnchor).isActive = true
            return vStack
        }
        return hStack
    }

    /// Тонкий горизонтальный разделитель для разбивки карточек на зоны.
    /// Использует NSBox.separator (AppKit-managed rendering) вместо bare NSView,
    /// чтобы цвет разделителя применялся корректно до того как view добавляется в окно.
    @MainActor
    private func makeSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }

    /// Лейбл-бейдж: малый текст, цвет из KrabEarTheme, опциональный тултип.
    @MainActor
    private func makeBadge(text: String, color: NSColor, tooltip: String? = nil) -> NSTextField {
        let badge = NSTextField(labelWithString: text)
        badge.font = KrabEarTheme.Typography.captionMedium
        badge.textColor = color
        if let tooltip {
            badge.toolTip = tooltip
        }
        return badge
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
            isExpanded: true
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
            text: "⚠ бета",
            color: KrabEarTheme.Colors.warning, // предупреждение, не ошибка — orange через токен
            tooltip: "Может вызывать сбой GPU на Apple Silicon. Отключите при нестабильной работе."
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

        // Assemble card (ThemeCardView content stack уже vertical + leading).
        card.contentStackView.addArrangedSubview(diarRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(qualRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(gigaamRow)

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
            isExpanded: true
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
            isExpanded: false
        )
        let card = ThemeCardView()

        // 1. LLM rewrite toggle
        llmRewriteButton.title = ""
        llmRewriteButton.setButtonType(.switch)
        llmRewriteButton.setAccessibilityLabel("Включить LLM постобработку текста транскрипции через LM Studio")
        let llmBetaBadge = makeBadge(
            text: "бета",
            color: KrabEarTheme.Colors.warning,
            tooltip: "LM Studio должен быть запущен локально с совместимой моделью."
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
            isExpanded: false
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
            isExpanded: false
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

        card.contentStackView.addArrangedSubview(duckingToggleRow)
        card.contentStackView.addArrangedSubview(duckRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(overlayRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(autoStartRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(dockRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
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
            isExpanded: false
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

        // 2. Wake word toggle
        vaWakeWordToggle.title = ""
        vaWakeWordToggle.setButtonType(.switch)
        vaWakeWordToggle.setAccessibilityLabel("Включить детектор пробуждения «Краб» через Porcupine")
        let wakePrivacyBadge = makeBadge(
            text: "приватность",
            color: KrabEarTheme.Colors.textSecondary,
            tooltip: "Требует Porcupine AccessKey + .ppn файл «Краб» (Picovoice Console, free tier). По умолчанию выключен."
        )
        let wakeWordRow = makeSwitchRow(
            label: "Детектор пробуждения «Краб»",
            description: "Требует Porcupine AccessKey + .ppn файл «Краб» (Picovoice Console, free tier). По умолчанию выключен — приватность.",
            button: vaWakeWordToggle,
            statusBadge: wakePrivacyBadge
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
            isExpanded: true
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

}