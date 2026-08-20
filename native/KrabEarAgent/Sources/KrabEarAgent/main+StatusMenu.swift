/*
 main+StatusMenu.swift
 AgentAppDelegate extension: NSStatusItem management, menu bar construction, mode switching.
*/

import AppKit
import Foundation

// MARK: - Shared STT engine name helper

/// Converts raw engine identifier (from get_diagnostics stt.last_engine) to a human-readable name.
/// Free function so it can be used both by AgentAppDelegate tooltip and HistoryPanelController+Settings.
func humanReadableSTTEngineShared(_ raw: String?) -> String {
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

extension AgentAppDelegate {

    // MARK: - Status item & mode

    func ensureStatusItem() {
        if statusItem == nil {
            let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            item.button?.title = "KE"
            item.button?.toolTip = "Krab Ear"
            statusItem = item
            // Drag-drop аудиофайлов на иконку menu bar (main+StatusDragDrop.swift).
            setupStatusItemDragDrop()
        }
    }

    func removeStatusItem() {
        guard let statusItem else {
            return
        }
        NSStatusBar.system.removeStatusItem(statusItem)
        self.statusItem = nil
    }

    func refreshStatusItemTitle() {
        guard let button = statusItem?.button else {
            return
        }
        let badge = activePresetBadge()
        if isRecording {
            // Wave 67 (AGENT-J): `●` U+25CF → SF Symbol to avoid TFPFont::CopyGlyphPath hang.
            let symConfig = NSImage.SymbolConfiguration(pointSize: 9, weight: .bold)
                .applying(NSImage.SymbolConfiguration(paletteColors: [NSColor.systemRed]))
            if let dotImg = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)?
                    .withSymbolConfiguration(symConfig) {
                button.image = dotImg
                button.imagePosition = .imageLeft
            }
            button.title = "KE [\(badge)]"
        } else if isProcessing {
            button.image = nil
            button.title = "KE … [\(badge)]"
        } else {
            button.image = nil
            button.title = "KE [\(badge)]"
        }
    }

    func applyMode(_ mode: String, persist: Bool) {
        let normalized = mode == "menubar" ? "menubar" : "headless"
        settings.mode = normalized
        applyActivationPolicy()

        if normalized == "menubar" {
            ensureStatusItem()
        } else {
            removeStatusItem()
        }

        if persist {
            persistSettingsPayload(settings.toPayload())
        }

        refreshStatusItemTitle()
        rebuildStatusMenu()
    }

    func applyActivationPolicy() {
        let policy: NSApplication.ActivationPolicy = settings.showDockIcon ? .regular : .accessory
        _ = NSApp.setActivationPolicy(policy)
    }

    func applySettingsSideEffects(previous: AgentSettings, current: AgentSettings) {
        if previous.autoStartEnabled != current.autoStartEnabled {
            launchAgentManager.setAutostart(enabled: current.autoStartEnabled)
        }
        if previous.realtimePreviewEnabled != current.realtimePreviewEnabled {
            if current.realtimePreviewEnabled && isRecording {
                startRealtimeOverlayPolling()
            } else {
                stopRealtimeOverlayPolling()
            }
        }
        if previous.overlayOpacityPercent != current.overlayOpacityPercent {
            realtimeOverlay.setOpacityPercent(current.overlayOpacityPercent)
        }
        if previous.hotkey != current.hotkey || previous.hotkeyMode != current.hotkeyMode {
            logger.info(
                "Перезапуск hotkey manager: вариант=\(current.hotkey), режим=\(current.hotkeyMode)"
            )
            hotkeyManager?.stop()
            // Та же фабрика, что на startup: не теряем hold/toggle-режим,
            // conversation hotkey, quick replay и conflict-reporting.
            hotkeyManager = makeHotkeyManager(settings: current)
            hotkeyManager?.start()
        }
    }

    // MARK: - Dynamic tooltip

    /// Build multi-line tooltip from live backend state.
    /// Format:
    ///   Krab Ear
    ///   STT: GigaAM (RNNT)
    ///   Last error: rewriter.timeout   (only if recent error exists)
    func buildStatusBarTooltip() async -> String {
        var lines: [String] = ["Krab Ear"]

        // Fetch diagnostics for STT engine.
        let diagResult = try? await ipcClient.callAsync(method: "get_diagnostics", params: [:])
        if let result = diagResult?["result"] as? [String: Any],
           let stt = result["stt"] as? [String: Any],
           let engine = stt["last_engine"] as? String {
            lines.append("STT: \(humanReadableSTTEngineShared(engine))")
        }

        // Fetch most recent error code (if any).
        let errResult = try? await ipcClient.callAsync(
            method: "list_recent_errors",
            params: ["limit": 1]
        )
        if let errData = errResult?["result"] as? [String: Any],
           let errList = errData["errors"] as? [[String: Any]],
           let firstErr = errList.first,
           let code = firstErr["code"] as? String,
           !code.isEmpty {
            lines.append("Last error: \(code)")
        }

        return lines.joined(separator: "\n")
    }

    /// Refresh tooltip every 10 s in a background Task.
    /// Call once from completeStartupAfterBackendReady (after status item created).
    func startTooltipRefresh() {
        Task.detached { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let tip = await self.buildStatusBarTooltip()
                await MainActor.run {
                    self.statusItem?.button?.toolTip = tip
                }
                try? await Task.sleep(nanoseconds: 10_000_000_000)  // 10 s
            }
        }
    }

    // MARK: - Menu construction

    func rebuildStatusMenu() {
        guard let statusItem else {
            return
        }

        let menu = NSMenu()

        // ── Сводка дня: вставляем В НАЧАЛО меню (ТЗ §Wiring) ─────────────────
        // MenuBarRecapView — self-contained карточка с IPC off-main (AGENT-3).
        // Ссылка сохраняется в menuBarRecapView для обновления через NSMenuDelegate.
        let recapView = MenuBarRecapView()
        let recapItem = NSMenuItem()
        recapItem.view = recapView
        menu.addItem(recapItem)           // первый пункт меню
        // B3: инфо-строка «кто держит LM Studio» (main+BrainLease.swift) —
        // disabled-пункт (action nil), скрыт при llm_brain_lease_enabled=false,
        // обновляется в menuWillOpen. Иконка — SF Symbol (класс AGENT-J/M).
        let brainItem = NSMenuItem(title: "LM Studio: —", action: nil, keyEquivalent: "")
        brainItem.image = NSImage(
            systemSymbolName: "brain.head.profile", accessibilityDescription: nil)
        menu.addItem(brainItem)
        self.brainLeaseMenuItem = brainItem
        // T8 (Memory Conductor): «Память: …» СРАЗУ ПОСЛЕ brain-lease строки —
        // тот же паттерн (disabled-пункт, обновление в menuWillOpen,
        // main+MemoryLine.swift). Скрыт при memory_conductor_enabled=false.
        let memoryItem = NSMenuItem(title: "Память: —", action: nil, keyEquivalent: "")
        memoryItem.image = NSImage(
            systemSymbolName: "memorychip", accessibilityDescription: nil)
        menu.addItem(memoryItem)
        self.memoryLineMenuItem = memoryItem
        menu.addItem(.separator())
        self.menuBarRecapView = recapView
        menu.delegate = self
        recapView.refresh(ipcClient: ipcClient)   // первичный fetch при построении меню
        refreshBrainLeaseMenuItem()               // первичный fetch brain-lease строки
        refreshMemoryLineMenuItem()                // первичный fetch строки «Память»
        // ─────────────────────────────────────────────────────────────────────

        let recordItem = NSMenuItem(
            title: isRecording ? "Остановить запись" : "Начать запись",
            action: #selector(onRecordToggle),
            keyEquivalent: ""
        )
        recordItem.target = self
        recordItem.isEnabled = !isProcessing
        menu.addItem(recordItem)

        // C3a: быстрая голосовая заметка (Cmd+Shift+N) — запись БЕЗ вставки
        // в активное окно, результат уходит в коллекцию «Быстрые заметки».
        let quickCaptureItem = NSMenuItem(
            title: quickCaptureActive ? "Остановить заметку" : "Быстрая заметка",
            action: #selector(onQuickCaptureToggle),
            keyEquivalent: "n"
        )
        quickCaptureItem.target = self
        quickCaptureItem.keyEquivalentModifierMask = [.command, .shift]
        quickCaptureItem.isEnabled = !isProcessing && !(isRecording && !quickCaptureActive)
        menu.addItem(quickCaptureItem)

        let quickNotesMenuItem = NSMenuItem(title: "Быстрые заметки", action: nil, keyEquivalent: "")
        menu.addItem(quickNotesMenuItem)
        let quickNotesMenu = NSMenu()
        menu.setSubmenu(quickNotesMenu, for: quickNotesMenuItem)
        self.quickNotesSubmenu = quickNotesMenu

        // C3b Task 2: панель-скретчпад — ручной показ независимо от состояния
        // записи (авто-показ по настройке живёт в onQuickCaptureToggle).
        let openScratchpadItem = NSMenuItem(
            title: "Открыть скретчпад",
            action: #selector(onOpenQuickCapturePanel),
            keyEquivalent: ""
        )
        openScratchpadItem.target = self
        menu.addItem(openScratchpadItem)

        let historyItem = NSMenuItem(
            title: "Открыть историю",
            action: #selector(onOpenHistory),
            keyEquivalent: "h"
        )
        historyItem.target = self
        historyItem.keyEquivalentModifierMask = [.command, .option]
        menu.addItem(historyItem)

        // C2c: живая панель встречи — старт/показ одним пунктом (backend идемпотентен).
        let meetingItem = NSMenuItem(
            title: "Встреча",
            action: #selector(onMeetingPanelToggle),
            keyEquivalent: "")
        meetingItem.target = self
        meetingItem.image = NSImage(systemSymbolName: "person.2.fill",
                                    accessibilityDescription: nil)  // уже используется в +MeetingMode
        menu.addItem(meetingItem)

        let showPanelItem = NSMenuItem(
            title: "Показать панель",
            action: #selector(onOpenHistory),
            keyEquivalent: "k"
        )
        showPanelItem.target = self
        showPanelItem.keyEquivalentModifierMask = [.command, .shift]
        menu.addItem(showPanelItem)

        let openTranscriptsItem = NSMenuItem(
            title: "Открыть транскрипты",
            action: #selector(onOpenTranscriptsInFinder),
            keyEquivalent: ""
        )
        openTranscriptsItem.target = self
        menu.addItem(openTranscriptsItem)

        menu.addItem(.separator())

        // Live субтитры (Cmd+Option+Shift+L — избегаем конфликт с Safari location bar Cmd+Shift+L)
        let liveSubsItem = NSMenuItem(
            title: systemAudioCapture.isCapturing ? "Остановить Live субтитры" : "Live субтитры (захват аудио)",
            action: #selector(onToggleLiveSubs),
            keyEquivalent: "l"
        )
        liveSubsItem.target = self
        liveSubsItem.keyEquivalentModifierMask = [.command, .option, .shift]
        menu.addItem(liveSubsItem)

        menu.addItem(.separator())

        let modeItem = NSMenuItem(
            title: settings.mode == "menubar" ? "Переключить в headless" : "Переключить в menu bar",
            action: #selector(onModeToggle),
            keyEquivalent: ""
        )
        modeItem.target = self
        menu.addItem(modeItem)

        let qualityMenuItem = NSMenuItem(title: "Качество транскрибации", action: nil, keyEquivalent: "")
        menu.addItem(qualityMenuItem)

        let qualitySubmenu = NSMenu()
        let balancedItem = NSMenuItem(
            title: "Balanced (turbo)",
            action: #selector(onQualityBalanced),
            keyEquivalent: ""
        )
        balancedItem.target = self
        balancedItem.state = settings.qualityProfile == "balanced" ? .on : .off
        qualitySubmenu.addItem(balancedItem)

        let maxItem = NSMenuItem(
            title: "Max (настраиваемый)",
            action: #selector(onQualityMax),
            keyEquivalent: ""
        )
        maxItem.target = self
        maxItem.state = settings.qualityProfile == "max" ? .on : .off
        qualitySubmenu.addItem(maxItem)

        menu.setSubmenu(qualitySubmenu, for: qualityMenuItem)

        let autoPasteItem = NSMenuItem(
            title: settings.autoPaste ? "Автовставка: вкл" : "Автовставка: выкл",
            action: #selector(onAutoPasteToggle),
            keyEquivalent: ""
        )
        autoPasteItem.target = self
        menu.addItem(autoPasteItem)

        let translationLabel: String
        switch settings.translationMode {
        case "ru_to_es":
            translationLabel = "RU -> ES"
        case "es_to_ru":
            translationLabel = "ES -> RU"
        case "en_to_ru":
            translationLabel = "EN -> RU"
        case "auto":
            translationLabel = "Auto"
        case "auto_to_ru":
            translationLabel = "Auto -> RU"
        case "bilingual_ru_es":
            translationLabel = "Bilingual RU<->ES"
        default:
            translationLabel = "Off"
        }
        let translationItem = NSMenuItem(title: "Перевод", action: nil, keyEquivalent: "")
        menu.addItem(translationItem)

        let translationSubmenu = NSMenu()
        let translationInfoItem = NSMenuItem(
            title: settings.translateAndPaste
                ? "Текущий: \(translationLabel) / вставка перевода"
                : "Текущий: \(translationLabel) / вставка оригинала",
            action: nil,
            keyEquivalent: ""
        )
        translationInfoItem.isEnabled = false
        translationSubmenu.addItem(translationInfoItem)
        translationSubmenu.addItem(.separator())

        let modeOffItem = NSMenuItem(title: "Off", action: #selector(onTranslationModeOff), keyEquivalent: "")
        modeOffItem.target = self
        modeOffItem.state = settings.translationMode == "off" ? .on : .off
        translationSubmenu.addItem(modeOffItem)

        let modeRuToEsItem = NSMenuItem(title: "RU -> ES", action: #selector(onTranslationModeRuToEs), keyEquivalent: "")
        modeRuToEsItem.target = self
        modeRuToEsItem.state = settings.translationMode == "ru_to_es" ? .on : .off
        translationSubmenu.addItem(modeRuToEsItem)

        let modeEsToRuItem = NSMenuItem(title: "ES -> RU", action: #selector(onTranslationModeEsToRu), keyEquivalent: "")
        modeEsToRuItem.target = self
        modeEsToRuItem.state = settings.translationMode == "es_to_ru" ? .on : .off
        translationSubmenu.addItem(modeEsToRuItem)

        let modeEnToRuItem = NSMenuItem(title: "EN -> RU", action: #selector(onTranslationModeEnToRu), keyEquivalent: "")
        modeEnToRuItem.target = self
        modeEnToRuItem.state = settings.translationMode == "en_to_ru" ? .on : .off
        translationSubmenu.addItem(modeEnToRuItem)

        let modeAutoItem = NSMenuItem(title: "Auto", action: #selector(onTranslationModeAuto), keyEquivalent: "")
        modeAutoItem.target = self
        modeAutoItem.state = settings.translationMode == "auto" ? .on : .off
        translationSubmenu.addItem(modeAutoItem)

        let modeBilingualItem = NSMenuItem(
            title: "Bilingual RU<->ES",
            action: #selector(onTranslationModeBilingualRuEs),
            keyEquivalent: ""
        )
        modeBilingualItem.target = self
        modeBilingualItem.state = settings.translationMode == "bilingual_ru_es" ? .on : .off
        translationSubmenu.addItem(modeBilingualItem)

        translationSubmenu.addItem(.separator())
        let livePresetItem = NSMenuItem(
            title: "Live Translation preset",
            action: #selector(onApplyLiveTranslationPreset),
            keyEquivalent: ""
        )
        livePresetItem.target = self
        translationSubmenu.addItem(livePresetItem)

        let swapRuEsItem = NSMenuItem(
            title: "Swap RU <-> ES",
            action: #selector(onSwapRuEsDirection),
            keyEquivalent: ""
        )
        swapRuEsItem.target = self
        swapRuEsItem.isEnabled = settings.translationMode == "ru_to_es"
            || settings.translationMode == "es_to_ru"
            || settings.translationMode == "auto"
            || settings.translationMode == "bilingual_ru_es"
        translationSubmenu.addItem(swapRuEsItem)

        translationSubmenu.addItem(.separator())
        let translateAndPasteItem = NSMenuItem(
            title: settings.translateAndPaste ? "Перевод + вставка: вкл" : "Перевод + вставка: выкл",
            action: #selector(onTranslateAndPasteToggle),
            keyEquivalent: ""
        )
        translateAndPasteItem.target = self
        translationSubmenu.addItem(translateAndPasteItem)

        translationSubmenu.addItem(.separator())
        let styleMenuItem = NSMenuItem(title: "Стиль перевода", action: nil, keyEquivalent: "")
        translationSubmenu.addItem(styleMenuItem)
        let styleSubmenu = NSMenu()
        let styleNeutral = NSMenuItem(title: "Neutral", action: #selector(onTranslationStyleNeutral), keyEquivalent: "")
        styleNeutral.target = self
        styleNeutral.state = settings.translationStyle == "neutral" ? .on : .off
        styleSubmenu.addItem(styleNeutral)
        let styleChat = NSMenuItem(title: "Chat", action: #selector(onTranslationStyleChat), keyEquivalent: "")
        styleChat.target = self
        styleChat.state = settings.translationStyle == "chat" ? .on : .off
        styleSubmenu.addItem(styleChat)
        let styleFormal = NSMenuItem(title: "Formal", action: #selector(onTranslationStyleFormal), keyEquivalent: "")
        styleFormal.target = self
        styleFormal.state = settings.translationStyle == "formal" ? .on : .off
        styleSubmenu.addItem(styleFormal)
        translationSubmenu.setSubmenu(styleSubmenu, for: styleMenuItem)

        menu.setSubmenu(translationSubmenu, for: translationItem)

        let clipboardMenuItem = NSMenuItem(title: "Буфер обмена", action: nil, keyEquivalent: "")
        menu.addItem(clipboardMenuItem)
        let clipboardSubmenu = NSMenu()
        let clipAlways = NSMenuItem(title: "Always copy", action: #selector(onClipboardModeAlways), keyEquivalent: "")
        clipAlways.target = self
        clipAlways.state = settings.clipboardMode == "always_copy" ? .on : .off
        // S34: риск-предупреждение — пароли и другой защищённый контент не затираются.
        clipAlways.toolTip = "Каждая диктовка заменяет буфер обмена транскриптом. "
            + "Пароли и другой защищённый контент не затираются."
        clipboardSubmenu.addItem(clipAlways)
        let clipOnFail = NSMenuItem(title: "Copy on fail", action: #selector(onClipboardModeOnFail), keyEquivalent: "")
        clipOnFail.target = self
        clipOnFail.state = settings.clipboardMode == "copy_on_fail" ? .on : .off
        clipOnFail.toolTip = "Буфер заменяется только если вставка в приложение не удалась. "
            + "Пароли и другой защищённый контент не затираются."
        clipboardSubmenu.addItem(clipOnFail)
        let clipNever = NSMenuItem(title: "Never copy", action: #selector(onClipboardModeNever), keyEquivalent: "")
        clipNever.target = self
        clipNever.state = settings.clipboardMode == "never_copy" ? .on : .off
        clipboardSubmenu.addItem(clipNever)
        menu.setSubmenu(clipboardSubmenu, for: clipboardMenuItem)

        let networkMenuItem = NSMenuItem(title: "Сетевой режим", action: nil, keyEquivalent: "")
        menu.addItem(networkMenuItem)

        let networkSubmenu = NSMenu()
        let offlineDefaultItem = NSMenuItem(
            title: "Offline default",
            action: #selector(onNetworkOfflineDefault),
            keyEquivalent: ""
        )
        offlineDefaultItem.target = self
        offlineDefaultItem.state = settings.networkMode == "offline_default" ? .on : .off
        networkSubmenu.addItem(offlineDefaultItem)

        let offlineStrictItem = NSMenuItem(
            title: "Offline strict",
            action: #selector(onNetworkOfflineStrict),
            keyEquivalent: ""
        )
        offlineStrictItem.target = self
        offlineStrictItem.state = settings.networkMode == "offline_strict" ? .on : .off
        networkSubmenu.addItem(offlineStrictItem)

        let onlineOptInItem = NSMenuItem(
            title: "Online opt-in",
            action: #selector(onNetworkOnlineOptIn),
            keyEquivalent: ""
        )
        onlineOptInItem.target = self
        onlineOptInItem.state = settings.networkMode == "online_opt_in" ? .on : .off
        networkSubmenu.addItem(onlineOptInItem)

        menu.setSubmenu(networkSubmenu, for: networkMenuItem)

        let hotkeyProfileItem = NSMenuItem(title: "Hotkey Profile", action: nil, keyEquivalent: "")
        menu.addItem(hotkeyProfileItem)
        let hotkeyProfileSubmenu = NSMenu()
        let profileDefaultItem = NSMenuItem(title: "Default", action: #selector(onHotkeyProfileDefault), keyEquivalent: "")
        profileDefaultItem.target = self
        profileDefaultItem.state = settings.hotkeyProfile == "default" ? .on : .off
        hotkeyProfileSubmenu.addItem(profileDefaultItem)
        let profileMeetingItem = NSMenuItem(title: "Meeting", action: #selector(onHotkeyProfileMeeting), keyEquivalent: "")
        profileMeetingItem.target = self
        profileMeetingItem.state = settings.hotkeyProfile == "meeting" ? .on : .off
        hotkeyProfileSubmenu.addItem(profileMeetingItem)
        let profileTranslationItem = NSMenuItem(title: "Translation", action: #selector(onHotkeyProfileTranslation), keyEquivalent: "")
        profileTranslationItem.target = self
        profileTranslationItem.state = settings.hotkeyProfile == "translation" ? .on : .off
        hotkeyProfileSubmenu.addItem(profileTranslationItem)
        menu.setSubmenu(hotkeyProfileSubmenu, for: hotkeyProfileItem)

        // Быстрое переключение пресетов записи
        addPresetMenuEntry(to: menu)

        let updateChannelItem = NSMenuItem(title: "Update Channel", action: nil, keyEquivalent: "")
        menu.addItem(updateChannelItem)
        let updateChannelSubmenu = NSMenu()
        let stableChannelItem = NSMenuItem(title: "Stable", action: #selector(onUpdateChannelStable), keyEquivalent: "")
        stableChannelItem.target = self
        stableChannelItem.state = settings.updateChannel == "stable" ? .on : .off
        updateChannelSubmenu.addItem(stableChannelItem)
        let betaChannelItem = NSMenuItem(title: "Beta", action: #selector(onUpdateChannelBeta), keyEquivalent: "")
        betaChannelItem.target = self
        betaChannelItem.state = settings.updateChannel == "beta" ? .on : .off
        updateChannelSubmenu.addItem(betaChannelItem)
        menu.setSubmenu(updateChannelSubmenu, for: updateChannelItem)

        let checkUpdatesItem = NSMenuItem(
            title: "Проверить обновления…",
            action: #selector(onCheckForUpdates),
            keyEquivalent: ""
        )
        checkUpdatesItem.target = self
        // Dev-запуск (бандл в репо): Sparkle не инициализирован — пункт серый.
        checkUpdatesItem.isEnabled = sparkleUpdaterController != nil
        menu.addItem(checkUpdatesItem)

        let quickActionsItem = NSMenuItem(title: "Быстрые действия", action: nil, keyEquivalent: "")
        menu.addItem(quickActionsItem)

        let quickActionsSubmenu = NSMenu()
        let copyLastItem = NSMenuItem(
            title: "Копировать последний",
            action: #selector(onCopyLastResult),
            keyEquivalent: "c"
        )
        copyLastItem.target = self
        copyLastItem.keyEquivalentModifierMask = [.command, .option]
        copyLastItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(copyLastItem)

        let pasteLastItem = NSMenuItem(
            title: "Вставить последний",
            action: #selector(onPasteLastResult),
            keyEquivalent: "v"
        )
        pasteLastItem.target = self
        pasteLastItem.keyEquivalentModifierMask = [.command, .option]
        pasteLastItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(pasteLastItem)

        let pasteOriginalItem = NSMenuItem(
            title: "Вставить оригинал",
            action: #selector(onPasteLastOriginal),
            keyEquivalent: ""
        )
        pasteOriginalItem.target = self
        pasteOriginalItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(pasteOriginalItem)

        let pasteTranslationItem = NSMenuItem(
            title: "Вставить перевод",
            action: #selector(onPasteLastTranslation),
            keyEquivalent: ""
        )
        pasteTranslationItem.target = self
        pasteTranslationItem.isEnabled = (lastResult?.translatedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false)
        quickActionsSubmenu.addItem(pasteTranslationItem)

        let pastePlainItem = NSMenuItem(
            title: "Вставить plain (1 строка)",
            action: #selector(onPasteLastPlainText),
            keyEquivalent: ""
        )
        pastePlainItem.target = self
        pastePlainItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(pastePlainItem)

        quickActionsSubmenu.addItem(.separator())
        let templateRuItem = NSMenuItem(
            title: "Шаблон RU follow-up",
            action: #selector(onApplyTemplateRu),
            keyEquivalent: ""
        )
        templateRuItem.target = self
        templateRuItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(templateRuItem)

        let templateEsItem = NSMenuItem(
            title: "Шаблон ES follow-up",
            action: #selector(onApplyTemplateEs),
            keyEquivalent: ""
        )
        templateEsItem.target = self
        templateEsItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(templateEsItem)

        menu.setSubmenu(quickActionsSubmenu, for: quickActionsItem)

        let startSoundItem = NSMenuItem(
            title: settings.playStartSound ? "Звук старта: вкл" : "Звук старта: выкл",
            action: #selector(onStartSoundToggle),
            keyEquivalent: ""
        )
        startSoundItem.target = self
        menu.addItem(startSoundItem)

        let duckingMenuItem = NSMenuItem(title: "Приглушение звука", action: nil, keyEquivalent: "")
        menu.addItem(duckingMenuItem)
        let duckingSubmenu = NSMenu()
        let duckEnabledItem = NSMenuItem(
            title: settings.audioDuckingEnabled ? "При записи: вкл" : "При записи: выкл",
            action: #selector(onAudioDuckingToggle),
            keyEquivalent: ""
        )
        duckEnabledItem.target = self
        duckingSubmenu.addItem(duckEnabledItem)
        duckingSubmenu.addItem(.separator())
        let duck25 = NSMenuItem(title: "25%", action: #selector(onAudioDuckingPercent25), keyEquivalent: "")
        duck25.target = self
        duck25.state = settings.audioDuckingPercent == 25 ? .on : .off
        duckingSubmenu.addItem(duck25)
        let duck50 = NSMenuItem(title: "50%", action: #selector(onAudioDuckingPercent50), keyEquivalent: "")
        duck50.target = self
        duck50.state = settings.audioDuckingPercent == 50 ? .on : .off
        duckingSubmenu.addItem(duck50)
        let duck75 = NSMenuItem(title: "75%", action: #selector(onAudioDuckingPercent75), keyEquivalent: "")
        duck75.target = self
        duck75.state = settings.audioDuckingPercent == 75 ? .on : .off
        duckingSubmenu.addItem(duck75)
        let duck100 = NSMenuItem(title: "100% (mute)", action: #selector(onAudioDuckingPercent100), keyEquivalent: "")
        duck100.target = self
        duck100.state = settings.audioDuckingPercent == 100 ? .on : .off
        duckingSubmenu.addItem(duck100)
        menu.setSubmenu(duckingSubmenu, for: duckingMenuItem)

        let overlayMenuItem = NSMenuItem(title: "Прозрачность realtime", action: nil, keyEquivalent: "")
        menu.addItem(overlayMenuItem)
        let overlaySubmenu = NSMenu()
        let overlay25 = NSMenuItem(title: "25%", action: #selector(onOverlayOpacity25), keyEquivalent: "")
        overlay25.target = self
        overlay25.state = settings.overlayOpacityPercent == 25 ? .on : .off
        overlaySubmenu.addItem(overlay25)
        let overlay45 = NSMenuItem(title: "45%", action: #selector(onOverlayOpacity45), keyEquivalent: "")
        overlay45.target = self
        overlay45.state = settings.overlayOpacityPercent == 45 ? .on : .off
        overlaySubmenu.addItem(overlay45)
        let overlay65 = NSMenuItem(title: "65%", action: #selector(onOverlayOpacity65), keyEquivalent: "")
        overlay65.target = self
        overlay65.state = settings.overlayOpacityPercent == 65 ? .on : .off
        overlaySubmenu.addItem(overlay65)
        menu.setSubmenu(overlaySubmenu, for: overlayMenuItem)

        let autostartItem = NSMenuItem(
            title: settings.autoStartEnabled ? "Автозапуск: вкл" : "Автозапуск: выкл",
            action: #selector(onAutostartToggle),
            keyEquivalent: ""
        )
        autostartItem.target = self
        menu.addItem(autostartItem)

        let dockItem = NSMenuItem(
            title: settings.showDockIcon ? "Иконка в Dock: вкл" : "Иконка в Dock: выкл",
            action: #selector(onDockIconToggle),
            keyEquivalent: ""
        )
        dockItem.target = self
        menu.addItem(dockItem)

        let compactItem = NSMenuItem(
            title: "Оптимизировать историю",
            action: #selector(onCompactHistory),
            keyEquivalent: ""
        )
        compactItem.target = self
        menu.addItem(compactItem)

        menu.addItem(.separator())

        let restartItem = NSMenuItem(
            title: "Перезапустить агент",
            action: #selector(onRestartAgent),
            keyEquivalent: ""
        )
        restartItem.target = self
        menu.addItem(restartItem)

        let stopItem = NSMenuItem(
            title: "Остановить агент",
            action: #selector(onStopAgent),
            keyEquivalent: ""
        )
        stopItem.target = self
        menu.addItem(stopItem)

        let quitItem = NSMenuItem(title: "Выход", action: #selector(onQuit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu
    }
}
