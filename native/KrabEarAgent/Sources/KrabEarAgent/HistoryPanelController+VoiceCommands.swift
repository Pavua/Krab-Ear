/*
 Голосовые команды — секция настроек.

 Surfacing двух флагов, уже живых в pipeline (core/voice_commands.py):
   voice_commands_enabled      (default: true)
   voice_commands_strict_mode  (default: true)

 IPC-контракты:
   - set_settings {voice_commands_enabled: Bool}
   - set_settings {voice_commands_strict_mode: Bool}
   - list_voice_commands {language?: String}
     → result.ok, result.languages ([String]),
       result.commands ([{language, phrase, action, description}])

 Архитектура:
   - buildVoiceCommandsSection()   — вариант для Gemini-дизайна (settingsBar).
   - cdBuildVoiceCommandsSection() — вариант для Claude Design (settingsBarCD).
   - syncVoiceCommandsToggles(enabled:strictMode:) — обновляет оба чекбокса при
     смене настроек (вызывается из syncSettingsControls).
   - fetchAndRebuildVoiceCommandsList(isClaudeDesign:) — загружает справочник
     команд с бэкенда и перестраивает список (AGENT-3: строго off-main).

 Правила AGENT-3 (AppHang-класс): IPC строго в DispatchQueue.global,
 мутации UI — строго в DispatchQueue.main.
 Никакого runModal() — переключатели не требуют подтверждения.
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum VCAssocKeys {
    nonisolated(unsafe) static var enabledToggle: UInt8 = 0
    nonisolated(unsafe) static var strictToggle: UInt8 = 0
    nonisolated(unsafe) static var cdEnabledToggle: UInt8 = 0
    nonisolated(unsafe) static var cdStrictToggle: UInt8 = 0
    nonisolated(unsafe) static var commandsCard: UInt8 = 0
    nonisolated(unsafe) static var cdCommandsCard: UInt8 = 0
}

// MARK: - HistoryPanelController+VoiceCommands

extension HistoryPanelController {

    // MARK: - Gemini variant

    /// Строит секцию «Голосовые команды» для Gemini-дизайна (settingsBar).
    @MainActor
    func buildVoiceCommandsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_voice_commands",
            title: "Голосовые команды",
            isExpanded: false,
            iconSymbol: "waveform.and.mic"
        )

        let card = ThemeCardView()

        // Тоггл «Включить голосовые команды»
        let enabledToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onVoiceCommandsEnabledChanged))
        enabledToggle.state = AgentSettings.default.voiceCommandsEnabled ? .on : .off
        objc_setAssociatedObject(self, &VCAssocKeys.enabledToggle, enabledToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let enabledRow = makeSettingRow(
            label: "Включить голосовые команды",
            description: "Команды диктовки (запятая, новая строка, удалить слово…) преобразуются в символы автоматически.",
            control: enabledToggle
        )

        // Тоггл «Строгий режим»
        let strictToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onVoiceCommandsStrictModeChanged))
        strictToggle.state = AgentSettings.default.voiceCommandsStrictMode ? .on : .off
        objc_setAssociatedObject(self, &VCAssocKeys.strictToggle, strictToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let strictRow = makeSettingRow(
            label: "Строгий режим (точные фразы)",
            description: "В строгом режиме однозначные слова-омонимы («вопрос», «точка») не трактуются как команды.",
            control: strictToggle
        )

        // Карточка со списком команд (заполняется асинхронно)
        let cmdsCard = ThemeCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        cmdsCard.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(self, &VCAssocKeys.commandsCard, cmdsCard, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let listSubhead = makeSubhead("СПИСОК КОМАНД")

        card.contentStackView.addArrangedSubview(enabledRow)
        card.contentStackView.addArrangedSubview(strictRow)

        section.contentStackView.addArrangedSubview(card)
        section.contentStackView.addArrangedSubview(listSubhead)
        section.contentStackView.addArrangedSubview(cmdsCard)

        fetchAndRebuildVoiceCommandsList(isClaudeDesign: false)

        return section
    }

    // MARK: - Claude Design variant

    /// Строит секцию «Голосовые команды» для Claude Design (settingsBarCD).
    @MainActor
    func cdBuildVoiceCommandsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_voice_commands",
            title: "Голосовые команды",
            isExpanded: false
        )

        let card = CDSettingsCardView()

        // Тоггл «Включить»
        let enabledToggle = NSButton(checkboxWithTitle: "Включить", target: self, action: #selector(onVoiceCommandsEnabledChangedCD))
        enabledToggle.state = AgentSettings.default.voiceCommandsEnabled ? .on : .off
        objc_setAssociatedObject(self, &VCAssocKeys.cdEnabledToggle, enabledToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let enabledRow = cdMakeRow(label: "Голосовые команды", control: enabledToggle)

        // Тоггл «Строгий режим»
        let strictToggle = NSButton(checkboxWithTitle: "Строгий режим", target: self, action: #selector(onVoiceCommandsStrictModeChangedCD))
        strictToggle.state = AgentSettings.default.voiceCommandsStrictMode ? .on : .off
        objc_setAssociatedObject(self, &VCAssocKeys.cdStrictToggle, strictToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let strictRow = cdMakeRow(label: "Точные фразы", control: strictToggle)

        // Карточка со списком команд
        let cmdsCard = CDSettingsCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        cmdsCard.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(self, &VCAssocKeys.cdCommandsCard, cmdsCard, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        card.contentStackView.addArrangedSubview(enabledRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(strictRow)

        section.contentStackView.addArrangedSubview(card)
        section.contentStackView.addArrangedSubview(cmdsCard)

        fetchAndRebuildVoiceCommandsList(isClaudeDesign: true)

        return section
    }

    // MARK: - Toggle actions (Gemini)

    @objc func onVoiceCommandsEnabledChanged() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(self, &VCAssocKeys.enabledToggle) as? NSButton else { return }
        let enabled = toggle.state == .on
        applySettingsPatch(["voice_commands_enabled": enabled])
    }

    @objc func onVoiceCommandsStrictModeChanged() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(self, &VCAssocKeys.strictToggle) as? NSButton else { return }
        let strict = toggle.state == .on
        applySettingsPatch(["voice_commands_strict_mode": strict])
    }

    // MARK: - Toggle actions (Claude Design)

    @objc func onVoiceCommandsEnabledChangedCD() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(self, &VCAssocKeys.cdEnabledToggle) as? NSButton else { return }
        let enabled = toggle.state == .on
        applySettingsPatch(["voice_commands_enabled": enabled])
    }

    @objc func onVoiceCommandsStrictModeChangedCD() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(self, &VCAssocKeys.cdStrictToggle) as? NSButton else { return }
        let strict = toggle.state == .on
        applySettingsPatch(["voice_commands_strict_mode": strict])
    }

    // MARK: - Sync from backend settings (called by syncSettingsControls)

    /// Обновляет чекбоксы из свежих настроек (вызывается из syncSettingsControls).
    @MainActor
    func syncVoiceCommandsToggles(enabled: Bool, strictMode: Bool) {
        if let t = objc_getAssociatedObject(self, &VCAssocKeys.enabledToggle) as? NSButton {
            t.state = enabled ? .on : .off
        }
        if let t = objc_getAssociatedObject(self, &VCAssocKeys.strictToggle) as? NSButton {
            t.state = strictMode ? .on : .off
        }
        if let t = objc_getAssociatedObject(self, &VCAssocKeys.cdEnabledToggle) as? NSButton {
            t.state = enabled ? .on : .off
        }
        if let t = objc_getAssociatedObject(self, &VCAssocKeys.cdStrictToggle) as? NSButton {
            t.state = strictMode ? .on : .off
        }
    }

    // MARK: - Загрузка справочника команд с бэкенда

    /// Загружает список команд через list_voice_commands (off-main, AGENT-3).
    /// Перестраивает карточку на main thread.
    func fetchAndRebuildVoiceCommandsList(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }

            let commands: [[String: Any]]
            do {
                let resp = try ipc.call(method: "list_voice_commands", params: [:])
                let result = resp["result"] as? [String: Any]
                commands = result?["commands"] as? [[String: Any]] ?? []
            } catch {
                commands = []
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if isClaudeDesign {
                    self.rebuildCDVoiceCommandsList(commands: commands)
                } else {
                    self.rebuildGeminiVoiceCommandsList(commands: commands)
                }
            }
        }
    }

    // MARK: - Перестройка карточки (Gemini)

    @MainActor
    private func rebuildGeminiVoiceCommandsList(commands: [[String: Any]]) {
        guard let card = objc_getAssociatedObject(
            self, &VCAssocKeys.commandsCard
        ) as? ThemeCardView else { return }

        // Сбрасываем текущее содержимое
        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        guard !commands.isEmpty else {
            let unavailable = NSTextField(labelWithString: "Список недоступен")
            unavailable.font = KrabEarTheme.Typography.caption
            unavailable.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(unavailable)
            return
        }

        // Группируем по языку
        let langOrder = ["ru", "es", "en"]
        let langNames = ["ru": "Русский", "es": "Español", "en": "English"]

        for lang in langOrder {
            let langCmds = commands.filter { ($0["language"] as? String) == lang }
            guard !langCmds.isEmpty else { continue }

            let header = makeSubhead(langNames[lang] ?? lang.uppercased())
            card.contentStackView.addArrangedSubview(header)

            for cmd in langCmds {
                let phrase = cmd["phrase"] as? String ?? ""
                let description = cmd["description"] as? String ?? ""
                let row = voiceCommandMakeGeminiRow(phrase: phrase, description: description)
                card.contentStackView.addArrangedSubview(row)
            }
        }
    }

    // MARK: - Перестройка карточки (Claude Design)

    @MainActor
    private func rebuildCDVoiceCommandsList(commands: [[String: Any]]) {
        guard let card = objc_getAssociatedObject(
            self, &VCAssocKeys.cdCommandsCard
        ) as? CDSettingsCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        guard !commands.isEmpty else {
            let unavailable = NSTextField(labelWithString: "Список недоступен")
            unavailable.font = .systemFont(ofSize: 12, weight: .regular)
            unavailable.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(unavailable)
            return
        }

        let langOrder = ["ru", "es", "en"]
        let langNames = ["ru": "Русский", "es": "Español", "en": "English"]

        for lang in langOrder {
            let langCmds = commands.filter { ($0["language"] as? String) == lang }
            guard !langCmds.isEmpty else { continue }

            if !card.contentStackView.arrangedSubviews.isEmpty {
                card.contentStackView.addArrangedSubview(cdMakeSeparator())
            }

            let headerLabel = NSTextField(labelWithString: langNames[lang] ?? lang.uppercased())
            headerLabel.font = .systemFont(ofSize: 11, weight: .semibold)
            headerLabel.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(headerLabel)

            for cmd in langCmds {
                let phrase = cmd["phrase"] as? String ?? ""
                let description = cmd["description"] as? String ?? ""
                let row = voiceCommandMakeCDRow(phrase: phrase, description: description)
                card.contentStackView.addArrangedSubview(row)
            }
        }
    }

    // MARK: - Строки списка команд (Gemini)

    @MainActor
    private func voiceCommandMakeGeminiRow(phrase: String, description: String) -> NSView {
        let phraseLabel = NSTextField(labelWithString: phrase)
        phraseLabel.font = KrabEarTheme.Typography.body
        phraseLabel.textColor = KrabEarTheme.Colors.textPrimary
        phraseLabel.setContentHuggingPriority(.defaultHigh, for: .horizontal)

        let descLabel = NSTextField(labelWithString: description)
        descLabel.font = KrabEarTheme.Typography.caption
        descLabel.textColor = KrabEarTheme.Colors.textSecondary
        descLabel.lineBreakMode = .byWordWrapping
        descLabel.maximumNumberOfLines = 2
        descLabel.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let stack = NSStackView(views: [phraseLabel, descLabel])
        stack.orientation = .horizontal
        stack.alignment = .firstBaseline
        stack.distribution = .fill
        stack.spacing = KrabEarTheme.Metrics.tight

        return stack
    }

    // MARK: - Строки списка команд (Claude Design)

    @MainActor
    private func voiceCommandMakeCDRow(phrase: String, description: String) -> NSView {
        let phraseLabel = NSTextField(labelWithString: phrase)
        phraseLabel.font = .systemFont(ofSize: 12, weight: .medium)
        phraseLabel.textColor = KrabEarTheme.Colors.textPrimary
        phraseLabel.setContentHuggingPriority(.defaultHigh, for: .horizontal)

        let descLabel = NSTextField(labelWithString: description)
        descLabel.font = .systemFont(ofSize: 11, weight: .regular)
        descLabel.textColor = KrabEarTheme.Colors.textSecondary
        descLabel.lineBreakMode = .byWordWrapping
        descLabel.maximumNumberOfLines = 2
        descLabel.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let stack = NSStackView(views: [phraseLabel, descLabel])
        stack.orientation = .horizontal
        stack.alignment = .firstBaseline
        stack.distribution = .fill
        stack.spacing = KrabEarTheme.Metrics.tight

        return stack
    }
}
