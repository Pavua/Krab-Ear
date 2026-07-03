/*
 Профили резюмирования — секция настроек для SummaryProfileManager (backend).

 IPC-контракты (сверено буква-в-букву с KrabEar/backend/summary_profiles.py +
 KrabEar/backend/history_service.py строки ~4170-4207 — ТОЛЬКО ЭТИ ДВА МЕТОДА
 СУЩЕСТВУЮТ, нет delete/update):

   - list_summary_profiles {}
       → result.profiles ([{name, system_prompt, max_tokens, format_instructions, builtin}])
         5 встроенных (builtin: true): brief, detailed, bullet_points, meeting_notes,
         telegram. Плюс кастомные (builtin: false) если есть.

   - add_summary_profile {name: String, prompt: String, max_tokens: Int?, format_instructions: String?}
       → result.profile {name, system_prompt, max_tokens, format_instructions, builtin: false}
         Это фактически upsert среди кастомных профилей (совпадение имени с builtin
         отклоняется бэкендом как RuntimeError/ValueError — "зарезервировано").
         НЕТ отдельного delete-метода — кнопку удаления строить некуда.

 Архитектура (зеркало HistoryPanelController+ConfigPresets.swift / +STTVocabulary.swift):
   - buildSummaryProfilesSection()   — Gemini-вариант (settingsBar, ThemeCardView).
   - cdBuildSummaryProfilesSection() — Claude Design (settingsBarCD, CDSettingsCardView).
   - fetchAndRebuildSummaryProfilesCard(isClaudeDesign:) — грузит list_summary_profiles
     off-main, перестраивает карточку.
   - onCreateSummaryProfile(_:) / onCreateSummaryProfileCD(_:) — читают inline
     name/prompt/max_tokens/format_instructions поля → add_summary_profile.

 Правила AGENT-3 (AppHang-класс): IPC строго в DispatchQueue.global,
 мутации UI — строго в DispatchQueue.main.
 НЕ runModal() — эта секция не показывает alert/panel диалогов вообще.
 Глифы: только ASCII + установленные SF Symbols (text.badge.star, star.fill,
 plus.circle).
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum SummaryProfilesAssocKeys {
    nonisolated(unsafe) static var card: UInt8 = 0
    nonisolated(unsafe) static var cdCard: UInt8 = 0
    nonisolated(unsafe) static var nameField: UInt8 = 0
    nonisolated(unsafe) static var cdNameField: UInt8 = 0
    nonisolated(unsafe) static var promptTextView: UInt8 = 0
    nonisolated(unsafe) static var cdPromptTextView: UInt8 = 0
    nonisolated(unsafe) static var maxTokensField: UInt8 = 0
    nonisolated(unsafe) static var cdMaxTokensField: UInt8 = 0
    nonisolated(unsafe) static var formatInstructionsField: UInt8 = 0
    nonisolated(unsafe) static var cdFormatInstructionsField: UInt8 = 0
}

// MARK: - Модель профиля (internal, single-source)

struct SummaryProfileData {
    let name: String
    let systemPrompt: String
    let maxTokens: Int
    let formatInstructions: String
    let builtin: Bool
}

// MARK: - HistoryPanelController+SummaryProfiles

extension HistoryPanelController {

    // MARK: - Gemini variant: секция для settingsBar

    /// Строит секцию «Профили резюмирования» (Gemini-дизайн, settingsBar).
    /// Внутренняя карточка наполняется асинхронно при первом показе (off-main).
    @MainActor
    func buildSummaryProfilesSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_summary_profiles",
            title: "Профили резюмирования",
            isExpanded: false,
            iconSymbol: "text.badge.star"
        )

        let card = ThemeCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(
            self,
            &SummaryProfilesAssocKeys.card,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        section.contentStackView.addArrangedSubview(card)

        // Загрузка данных (off-main, AGENT-3).
        fetchAndRebuildSummaryProfilesCard(isClaudeDesign: false)

        return section
    }

    // MARK: - Claude Design variant: секция для settingsBarCD

    /// Строит секцию «Профили резюмирования» (Claude Design, settingsBarCD).
    @MainActor
    func cdBuildSummaryProfilesSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_summary_profiles",
            title: "Профили резюмирования",
            isExpanded: false
        )

        let card = CDSettingsCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(
            self,
            &SummaryProfilesAssocKeys.cdCard,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        section.contentStackView.addArrangedSubview(card)
        fetchAndRebuildSummaryProfilesCard(isClaudeDesign: true)

        return section
    }

    // MARK: - Загрузка данных с бэкенда

    /// Запрашивает list_summary_profiles строго off-main (AGENT-3), обновляет
    /// карточку на main.
    func fetchAndRebuildSummaryProfilesCard(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            var profiles: [SummaryProfileData] = []
            do {
                let resp = try ipc.call(method: "list_summary_profiles", params: [:])
                let result = resp["result"] as? [String: Any] ?? [:]
                let rawProfiles = result["profiles"] as? [[String: Any]] ?? []
                profiles = rawProfiles.compactMap { dict in
                    guard let name = dict["name"] as? String else { return nil }
                    return SummaryProfileData(
                        name: name,
                        systemPrompt: (dict["system_prompt"] as? String) ?? "",
                        maxTokens: (dict["max_tokens"] as? Int) ?? 300,
                        formatInstructions: (dict["format_instructions"] as? String) ?? "",
                        builtin: (dict["builtin"] as? Bool) ?? false
                    )
                }
            } catch {
                profiles = []
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if isClaudeDesign {
                    self.rebuildCDSummaryProfilesCard(profiles: profiles)
                } else {
                    self.rebuildGeminiSummaryProfilesCard(profiles: profiles)
                }
            }
        }
    }

    // MARK: - Перестройка карточки (Gemini)

    @MainActor
    private func rebuildGeminiSummaryProfilesCard(profiles: [SummaryProfileData]) {
        guard let card = objc_getAssociatedObject(
            self, &SummaryProfilesAssocKeys.card
        ) as? ThemeCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        // Список профилей.
        let subhead = makeSubhead("ДОСТУПНЫЕ ПРОФИЛИ")
        card.contentStackView.addArrangedSubview(subhead)

        if profiles.isEmpty {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = KrabEarTheme.Typography.caption
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
        } else {
            for profile in profiles {
                card.contentStackView.addArrangedSubview(summaryProfilesSeparator())
                card.contentStackView.addArrangedSubview(makeGeminiProfileRow(profile))
            }
        }

        // Форма «Создать новый профиль».
        card.contentStackView.addArrangedSubview(summaryProfilesSeparator())
        card.contentStackView.addArrangedSubview(makeSubhead("СОЗДАТЬ НОВЫЙ ПРОФИЛЬ"))
        card.contentStackView.addArrangedSubview(makeGeminiCreateForm())
    }

    // MARK: - Перестройка карточки (Claude Design)

    @MainActor
    private func rebuildCDSummaryProfilesCard(profiles: [SummaryProfileData]) {
        guard let card = objc_getAssociatedObject(
            self, &SummaryProfilesAssocKeys.cdCard
        ) as? CDSettingsCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        let subhead = NSTextField(labelWithString: "ДОСТУПНЫЕ ПРОФИЛИ")
        subhead.font = .systemFont(ofSize: 10, weight: .semibold)
        subhead.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(subhead)

        if profiles.isEmpty {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = .systemFont(ofSize: 12, weight: .regular)
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
        } else {
            for profile in profiles {
                card.contentStackView.addArrangedSubview(cdMakeSeparator())
                card.contentStackView.addArrangedSubview(makeCDProfileRow(profile))
            }
        }

        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        let createLabel = NSTextField(labelWithString: "СОЗДАТЬ НОВЫЙ ПРОФИЛЬ")
        createLabel.font = .systemFont(ofSize: 10, weight: .semibold)
        createLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(createLabel)
        card.contentStackView.addArrangedSubview(makeCDCreateForm())
    }

    // MARK: - Строка профиля (Gemini)

    @MainActor
    private func makeGeminiProfileRow(_ profile: SummaryProfileData) -> NSView {
        let tokensBadge = makeBadge(
            text: "\(profile.maxTokens) ток.",
            color: KrabEarTheme.Colors.textSecondary,
            tooltip: "Максимальное количество токенов ответа LLM"
        )

        var badges: [NSView] = []
        if profile.builtin {
            badges.append(
                makeBadge(
                    text: "builtin",
                    color: KrabEarTheme.Colors.accent,
                    tooltip: "Встроенный профиль — нельзя удалить",
                    symbol: "star.fill"
                )
            )
        }
        badges.append(tokensBadge)

        let badgeStack = NSStackView(views: badges)
        badgeStack.orientation = .horizontal
        badgeStack.spacing = KrabEarTheme.Metrics.tight
        badgeStack.alignment = .centerY

        return makeSettingRow(
            label: profile.name,
            description: profile.formatInstructions.isEmpty ? nil : profile.formatInstructions,
            control: badgeStack
        )
    }

    // MARK: - Строка профиля (Claude Design)

    @MainActor
    private func makeCDProfileRow(_ profile: SummaryProfileData) -> NSView {
        let tokensLabel = NSTextField(labelWithString: "\(profile.maxTokens) ток.")
        tokensLabel.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular)
        tokensLabel.textColor = KrabEarTheme.Colors.textSecondary
        tokensLabel.toolTip = "Максимальное количество токенов ответа LLM"
        tokensLabel.setContentHuggingPriority(.required, for: .horizontal)

        let badge: NSView? = profile.builtin
            ? makeBadge(text: "builtin", color: KrabEarTheme.Colors.accent, tooltip: "Встроенный профиль", symbol: "star.fill")
            : nil

        return cdMakeRow(label: profile.name, control: tokensLabel, badge: badge)
    }

    // MARK: - Форма «Создать новый профиль» (Gemini)

    @MainActor
    private func makeGeminiCreateForm() -> NSView {
        let nameField = NSTextField(frame: .zero)
        nameField.placeholderString = "Имя профиля (например, my_custom_style)…"
        nameField.font = KrabEarTheme.Typography.body
        nameField.bezelStyle = .roundedBezel
        nameField.isBordered = true
        nameField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        objc_setAssociatedObject(
            self, &SummaryProfilesAssocKeys.nameField, nameField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let promptTextView = NSTextView(frame: NSRect(x: 0, y: 0, width: 380, height: 80))
        promptTextView.isEditable = true
        promptTextView.isRichText = false
        promptTextView.font = KrabEarTheme.Typography.body
        promptTextView.textContainerInset = NSSize(width: 4, height: 4)
        let promptScroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 380, height: 80))
        promptScroll.documentView = promptTextView
        promptScroll.hasVerticalScroller = true
        promptScroll.autohidesScrollers = true
        promptScroll.borderType = .lineBorder
        promptScroll.heightAnchor.constraint(equalToConstant: 80).isActive = true
        objc_setAssociatedObject(
            self, &SummaryProfilesAssocKeys.promptTextView, promptTextView, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let maxTokensField = NSTextField(frame: .zero)
        maxTokensField.placeholderString = "300"
        maxTokensField.stringValue = "300"
        maxTokensField.font = KrabEarTheme.Typography.body
        maxTokensField.bezelStyle = .roundedBezel
        maxTokensField.isBordered = true
        maxTokensField.alignment = .right
        maxTokensField.widthAnchor.constraint(equalToConstant: 70).isActive = true
        objc_setAssociatedObject(
            self, &SummaryProfilesAssocKeys.maxTokensField, maxTokensField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let formatInstructionsField = NSTextField(frame: .zero)
        formatInstructionsField.placeholderString = "Описание формата (необязательно)…"
        formatInstructionsField.font = KrabEarTheme.Typography.body
        formatInstructionsField.bezelStyle = .roundedBezel
        formatInstructionsField.isBordered = true
        formatInstructionsField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        objc_setAssociatedObject(
            self,
            &SummaryProfilesAssocKeys.formatInstructionsField,
            formatInstructionsField,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let createButton = ThemePrimaryButton(
            title: "Создать",
            target: self,
            action: #selector(onCreateSummaryProfile(_:))
        )
        createButton.setAccessibilityLabel("Создать профиль резюмирования")
        createButton.setContentHuggingPriority(.required, for: .horizontal)

        let nameRow = makeSettingRow(label: "Имя", control: nameField)
        let promptRow = makeSettingRow(label: "Промпт", control: promptScroll)
        let maxTokensRow = makeSettingRow(label: "Макс. токенов", control: maxTokensField)
        let formatRow = makeSettingRow(label: "Описание формата", control: formatInstructionsField)

        let buttonRow = NSStackView(views: [createButton])
        buttonRow.orientation = .horizontal
        buttonRow.alignment = .centerY

        let vStack = NSStackView()
        vStack.orientation = .vertical
        vStack.alignment = .leading
        vStack.spacing = KrabEarTheme.Metrics.tight
        vStack.addArrangedSubview(nameRow)
        vStack.addArrangedSubview(promptRow)
        vStack.addArrangedSubview(maxTokensRow)
        vStack.addArrangedSubview(formatRow)
        vStack.addArrangedSubview(buttonRow)
        return vStack
    }

    // MARK: - Форма «Создать новый профиль» (Claude Design)

    @MainActor
    private func makeCDCreateForm() -> NSView {
        let nameField = NSTextField(frame: .zero)
        nameField.placeholderString = "Имя профиля…"
        nameField.font = .systemFont(ofSize: 12, weight: .regular)
        nameField.bezelStyle = .roundedBezel
        nameField.isBordered = true
        nameField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        objc_setAssociatedObject(
            self, &SummaryProfilesAssocKeys.cdNameField, nameField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let promptTextView = NSTextView(frame: NSRect(x: 0, y: 0, width: 380, height: 70))
        promptTextView.isEditable = true
        promptTextView.isRichText = false
        promptTextView.font = .systemFont(ofSize: 12, weight: .regular)
        promptTextView.textContainerInset = NSSize(width: 4, height: 4)
        let promptScroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 380, height: 70))
        promptScroll.documentView = promptTextView
        promptScroll.hasVerticalScroller = true
        promptScroll.autohidesScrollers = true
        promptScroll.borderType = .lineBorder
        promptScroll.heightAnchor.constraint(equalToConstant: 70).isActive = true
        objc_setAssociatedObject(
            self, &SummaryProfilesAssocKeys.cdPromptTextView, promptTextView, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let maxTokensField = NSTextField(frame: .zero)
        maxTokensField.placeholderString = "300"
        maxTokensField.stringValue = "300"
        maxTokensField.font = .systemFont(ofSize: 12, weight: .regular)
        maxTokensField.bezelStyle = .roundedBezel
        maxTokensField.isBordered = true
        maxTokensField.alignment = .right
        maxTokensField.widthAnchor.constraint(equalToConstant: 60).isActive = true
        objc_setAssociatedObject(
            self, &SummaryProfilesAssocKeys.cdMaxTokensField, maxTokensField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let formatInstructionsField = NSTextField(frame: .zero)
        formatInstructionsField.placeholderString = "Описание формата…"
        formatInstructionsField.font = .systemFont(ofSize: 12, weight: .regular)
        formatInstructionsField.bezelStyle = .roundedBezel
        formatInstructionsField.isBordered = true
        formatInstructionsField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        objc_setAssociatedObject(
            self,
            &SummaryProfilesAssocKeys.cdFormatInstructionsField,
            formatInstructionsField,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let createButton = NSButton(
            title: "Создать",
            target: self,
            action: #selector(onCreateSummaryProfileCD(_:))
        )
        createButton.bezelStyle = .rounded
        createButton.setAccessibilityLabel("Создать профиль резюмирования")
        createButton.setContentHuggingPriority(.required, for: .horizontal)

        let nameRow = cdMakeRow(label: "Имя", control: nameField)
        let promptRow = cdMakeRow(label: "Промпт", control: promptScroll)
        let maxTokensRow = cdMakeRow(label: "Макс. токенов", control: maxTokensField)
        let formatRow = cdMakeRow(label: "Описание формата", control: formatInstructionsField)

        let buttonRow = NSStackView(views: [createButton])
        buttonRow.orientation = .horizontal
        buttonRow.alignment = .centerY

        let vStack = NSStackView()
        vStack.orientation = .vertical
        vStack.alignment = .leading
        vStack.spacing = 0
        vStack.addArrangedSubview(nameRow)
        vStack.addArrangedSubview(promptRow)
        vStack.addArrangedSubview(maxTokensRow)
        vStack.addArrangedSubview(formatRow)
        vStack.addArrangedSubview(buttonRow)
        return vStack
    }

    // MARK: - Обработчик: Создать новый профиль (Gemini)

    @objc func onCreateSummaryProfile(_ sender: Any) {
        guard let nameField = objc_getAssociatedObject(
            self, &SummaryProfilesAssocKeys.nameField
        ) as? NSTextField else { return }
        let promptTextView = objc_getAssociatedObject(
            self, &SummaryProfilesAssocKeys.promptTextView
        ) as? NSTextView
        let maxTokensField = objc_getAssociatedObject(
            self, &SummaryProfilesAssocKeys.maxTokensField
        ) as? NSTextField
        let formatInstructionsField = objc_getAssociatedObject(
            self, &SummaryProfilesAssocKeys.formatInstructionsField
        ) as? NSTextField
        createSummaryProfile(
            nameField: nameField,
            promptTextView: promptTextView,
            maxTokensField: maxTokensField,
            formatInstructionsField: formatInstructionsField,
            isClaudeDesign: false
        )
    }

    // MARK: - Обработчик: Создать новый профиль (Claude Design)

    @objc func onCreateSummaryProfileCD(_ sender: Any) {
        guard let nameField = objc_getAssociatedObject(
            self, &SummaryProfilesAssocKeys.cdNameField
        ) as? NSTextField else { return }
        let promptTextView = objc_getAssociatedObject(
            self, &SummaryProfilesAssocKeys.cdPromptTextView
        ) as? NSTextView
        let maxTokensField = objc_getAssociatedObject(
            self, &SummaryProfilesAssocKeys.cdMaxTokensField
        ) as? NSTextField
        let formatInstructionsField = objc_getAssociatedObject(
            self, &SummaryProfilesAssocKeys.cdFormatInstructionsField
        ) as? NSTextField
        createSummaryProfile(
            nameField: nameField,
            promptTextView: promptTextView,
            maxTokensField: maxTokensField,
            formatInstructionsField: formatInstructionsField,
            isClaudeDesign: true
        )
    }

    /// Общая логика создания/замены кастомного профиля резюмирования.
    /// add_summary_profile — фактически upsert; backend отклоняет совпадение
    /// имени со встроенным профилем (RuntimeError/ValueError) — показываем
    /// error.localizedDescription через BackendToast, без клиентской проверки.
    private func createSummaryProfile(
        nameField: NSTextField,
        promptTextView: NSTextView?,
        maxTokensField: NSTextField?,
        formatInstructionsField: NSTextField?,
        isClaudeDesign: Bool
    ) {
        let name = nameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else {
            BackendToast.shared.show("Введите имя профиля", duration: 2.5)
            return
        }

        let prompt = (promptTextView?.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else {
            BackendToast.shared.show("Введите текст промпта", duration: 2.5)
            return
        }

        let maxTokensText = (maxTokensField?.stringValue ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let maxTokens = Int(maxTokensText) ?? 300
        let formatInstructions = (formatInstructionsField?.stringValue ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        var params: [String: Any] = [
            "name": name,
            "prompt": prompt,
            "max_tokens": maxTokens,
        ]
        if !formatInstructions.isEmpty {
            params["format_instructions"] = formatInstructions
        }

        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var success = false
            var errorText = ""
            do {
                _ = try ipc.call(method: "add_summary_profile", params: params)
                success = true
            } catch {
                success = false
                errorText = error.localizedDescription
            }
            DispatchQueue.main.async {
                if success {
                    nameField.stringValue = ""
                    promptTextView?.string = ""
                    maxTokensField?.stringValue = "300"
                    formatInstructionsField?.stringValue = ""
                    BackendToast.shared.show("Профиль «\(name)» создан", duration: 2.5)
                } else {
                    BackendToast.shared.show(
                        "Не удалось создать профиль: \(errorText)",
                        duration: 4.0
                    )
                }
            }
            self.fetchAndRebuildSummaryProfilesCard(isClaudeDesign: false)
            self.fetchAndRebuildSummaryProfilesCard(isClaudeDesign: true)
        }
    }

    // MARK: - Вспомогательный separator

    /// NSBox separator (аналог приватного makeSeparator()) — только для этого extension.
    @MainActor
    private func summaryProfilesSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }
}
