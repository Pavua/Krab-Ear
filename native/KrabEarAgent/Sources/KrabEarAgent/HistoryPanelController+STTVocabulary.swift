/*
 Словарь STT (hotwords) — секция настроек.

 IPC-контракты (все существуют в бэкенде):
   - list_stt_hotwords {}            → result.hotwords ([String])
   - add_stt_hotword {word: String}  → result.hotwords ([String]), result.truncated (Bool)
   - remove_stt_hotword {word: String} → result.hotwords ([String])
   - get_vocabulary_suggestions {}   → result.suggestions ([{word: String, count: Int}]),
                                        result.reason (String, напр. "privacy_mode_active")

 Архитектура:
   - buildSTTVocabularySection()   — секция для Gemini-дизайна (settingsBar).
   - cdBuildSTTVocabularySection() — секция для Claude Design (settingsBarCD).
   - fetchAndRebuildSTTVocabularyCard(isClaudeDesign:) — загрузка с бэкенда, перестройка карточки.
   - onAddSTTHotword(_:) — обработчик кнопки «Добавить».
   - onRemoveSTTHotword(_:) — обработчик кнопки удаления (SF Symbol xmark.circle).
   - onAddSuggestion(_:) — добавляет предложенный термин в словарь.

 Правила AGENT-3 (AppHang-класс): IPC строго в DispatchQueue.global,
 мутации UI — строго в DispatchQueue.main.
 Никакого runModal() — вставка/удаление не требуют подтверждения.
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum STTVocabAssocKeys {
    nonisolated(unsafe) static var vocabCard: UInt8 = 0
    nonisolated(unsafe) static var cdVocabCard: UInt8 = 0
    nonisolated(unsafe) static var addTextField: UInt8 = 0
    nonisolated(unsafe) static var cdAddTextField: UInt8 = 0
}

// MARK: - HistoryPanelController+STTVocabulary

extension HistoryPanelController {

    // MARK: - Gemini variant: секция для settingsBar

    /// Строит секцию «Словарь STT» для Gemini-дизайна (settingsBar).
    @MainActor
    func buildSTTVocabularySection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_stt_vocabulary",
            title: "Словарь STT",
            isExpanded: false,
            iconSymbol: "text.book.closed"
        )

        let card = ThemeCardView()

        // Строка добавления: NSTextField + кнопка «Добавить».
        let addField = NSTextField(frame: .zero)
        addField.placeholderString = "Новый термин…"
        addField.font = KrabEarTheme.Typography.body
        addField.bezelStyle = .roundedBezel
        addField.isBordered = true
        addField.focusRingType = .default
        addField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        addField.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        // Enter в поле тоже запускает добавление.
        addField.target = self
        addField.action = #selector(onAddSTTHotwordFromField(_:))

        objc_setAssociatedObject(
            self,
            &STTVocabAssocKeys.addTextField,
            addField,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let addButton = ThemePrimaryButton(title: "Добавить", target: self, action: #selector(onAddSTTHotword(_:)))
        addButton.setContentHuggingPriority(.required, for: .horizontal)
        addButton.setAccessibilityLabel("Добавить термин в словарь STT")

        let addRow = makeSettingRow(label: "Добавить термин", control: addField)

        // Складываем строку с кнопкой вручную (makeSettingRow поддерживает один control справа,
        // а нам нужны поле + кнопка). Используем горизонтальный NSStackView.
        let addFieldStack = NSStackView(views: [addField, addButton])
        addFieldStack.orientation = .horizontal
        addFieldStack.spacing = KrabEarTheme.Metrics.tight
        addFieldStack.alignment = .centerY
        _ = addRow // Unused — строим напрямую через makeSettingRow с составным control.

        let addCompositeRow = makeSettingRow(
            label: "Добавить термин",
            description: "Термины повышают точность распознавания собственных имён и специализированной лексики.",
            control: addFieldStack
        )

        // Загрузочная карточка — наполняется асинхронно.
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary

        objc_setAssociatedObject(
            self,
            &STTVocabAssocKeys.vocabCard,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        card.contentStackView.addArrangedSubview(addCompositeRow)
        card.contentStackView.addArrangedSubview(sttVocabMakeSeparator())
        card.contentStackView.addArrangedSubview(loadingLabel)

        section.contentStackView.addArrangedSubview(card)

        // Запускаем загрузку словаря с бэкенда (off-main, AGENT-3).
        fetchAndRebuildSTTVocabularyCard(isClaudeDesign: false)

        return section
    }

    // MARK: - Claude Design variant: секция для settingsBarCD

    /// Строит секцию «Словарь STT» для Claude Design (settingsBarCD).
    @MainActor
    func cdBuildSTTVocabularySection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_stt_vocabulary",
            title: "Словарь STT",
            isExpanded: false
        )

        let card = CDSettingsCardView()

        // Строка добавления (CD стиль: compact field + кнопка).
        let addField = NSTextField(frame: .zero)
        addField.placeholderString = "Новый термин…"
        addField.font = .systemFont(ofSize: 12, weight: .regular)
        addField.bezelStyle = .roundedBezel
        addField.isBordered = true
        addField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        addField.widthAnchor.constraint(greaterThanOrEqualToConstant: 110).isActive = true
        addField.target = self
        addField.action = #selector(onAddSTTHotwordFromFieldCD(_:))

        objc_setAssociatedObject(
            self,
            &STTVocabAssocKeys.cdAddTextField,
            addField,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let addButton = NSButton(title: "Добавить", target: self, action: #selector(onAddSTTHotwordCD(_:)))
        addButton.bezelStyle = .rounded
        addButton.setContentHuggingPriority(.required, for: .horizontal)
        addButton.setAccessibilityLabel("Добавить термин в словарь STT")

        let addFieldStack = NSStackView(views: [addField, addButton])
        addFieldStack.orientation = .horizontal
        addFieldStack.spacing = KrabEarTheme.Metrics.tight
        addFieldStack.alignment = .centerY

        let addRow = cdMakeRow(label: "Добавить", control: addFieldStack)

        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary

        objc_setAssociatedObject(
            self,
            &STTVocabAssocKeys.cdVocabCard,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        card.contentStackView.addArrangedSubview(addRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(loadingLabel)

        section.contentStackView.addArrangedSubview(card)

        fetchAndRebuildSTTVocabularyCard(isClaudeDesign: true)

        return section
    }

    // MARK: - Загрузка данных с бэкенда

    /// Запрашивает list_stt_hotwords + get_vocabulary_suggestions строго off-main (AGENT-3).
    /// Перестраивает нужную карточку на main thread.
    func fetchAndRebuildSTTVocabularyCard(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            // 1. Текущий словарь.
            let hotwords: [String]
            let truncated: Bool
            do {
                let resp = try ipc.call(method: "list_stt_hotwords", params: [:])
                let result = resp["result"] as? [String: Any]
                hotwords = result?["hotwords"] as? [String] ?? []
                truncated = result?["truncated"] as? Bool ?? false
            } catch {
                hotwords = []
                truncated = false
            }

            // 2. Предложения из истории транскрибаций.
            let suggestions: [[String: Any]]
            let privacyBlocked: Bool
            do {
                let resp = try ipc.call(method: "get_vocabulary_suggestions", params: [:])
                let result = resp["result"] as? [String: Any]
                let reason = result?["reason"] as? String ?? ""
                privacyBlocked = (reason == "privacy_mode_active")
                suggestions = result?["suggestions"] as? [[String: Any]] ?? []
            } catch {
                suggestions = []
                privacyBlocked = false
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if isClaudeDesign {
                    self.rebuildCDSTTVocabularyCard(
                        hotwords: hotwords,
                        truncated: truncated,
                        suggestions: suggestions,
                        privacyBlocked: privacyBlocked
                    )
                } else {
                    self.rebuildGeminiSTTVocabularyCard(
                        hotwords: hotwords,
                        truncated: truncated,
                        suggestions: suggestions,
                        privacyBlocked: privacyBlocked
                    )
                }
            }
        }
    }

    // MARK: - Перестройка карточки (Gemini)

    @MainActor
    private func rebuildGeminiSTTVocabularyCard(
        hotwords: [String],
        truncated: Bool,
        suggestions: [[String: Any]],
        privacyBlocked: Bool
    ) {
        guard let card = objc_getAssociatedObject(
            self, &STTVocabAssocKeys.vocabCard
        ) as? ThemeCardView else { return }

        // Убираем все строки кроме первой (поле добавления + разделитель).
        let arrangedViews = card.contentStackView.arrangedSubviews
        // Первые 2 — addCompositeRow + separator — сохраняем; остальные удаляем.
        for v in arrangedViews.dropFirst(2) {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        // Текущий список hotwords.
        if hotwords.isEmpty {
            let empty = NSTextField(labelWithString: "Словарь пуст")
            empty.font = KrabEarTheme.Typography.caption
            empty.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(empty)
        } else {
            for word in hotwords {
                let row = makeGeminiHotwordRow(word: word)
                card.contentStackView.addArrangedSubview(row)
            }
        }

        // Сообщение о лимите.
        if truncated {
            let limitNote = NSTextField(labelWithString: "Список заполнен (лимит)")
            limitNote.font = KrabEarTheme.Typography.caption
            limitNote.textColor = KrabEarTheme.Colors.warning
            card.contentStackView.addArrangedSubview(limitNote)
        }

        // Раздел предложений.
        card.contentStackView.addArrangedSubview(sttVocabMakeSeparator())

        let suggestionsSubhead = makeSubhead("ПРЕДЛОЖЕНИЯ")
        card.contentStackView.addArrangedSubview(suggestionsSubhead)

        if privacyBlocked {
            let privLabel = NSTextField(labelWithString: "Недоступно в режиме приватности")
            privLabel.font = KrabEarTheme.Typography.caption
            privLabel.textColor = KrabEarTheme.Colors.textDisabled
            card.contentStackView.addArrangedSubview(privLabel)
        } else {
            // Фильтруем предложения — исключаем уже добавленные.
            let hotwordSet = Set(hotwords)
            let filteredSuggestions = suggestions.filter { dict in
                guard let w = dict["word"] as? String else { return false }
                return !hotwordSet.contains(w)
            }

            if filteredSuggestions.isEmpty {
                let noSug = NSTextField(labelWithString: "Нет предложений")
                noSug.font = KrabEarTheme.Typography.caption
                noSug.textColor = KrabEarTheme.Colors.textDisabled
                card.contentStackView.addArrangedSubview(noSug)
            } else {
                for dict in filteredSuggestions {
                    guard let word = dict["word"] as? String else { continue }
                    let count = dict["count"] as? Int ?? 0
                    let row = makeGeminiSuggestionRow(word: word, count: count)
                    card.contentStackView.addArrangedSubview(row)
                }
            }
        }
    }

    // MARK: - Перестройка карточки (Claude Design)

    @MainActor
    private func rebuildCDSTTVocabularyCard(
        hotwords: [String],
        truncated: Bool,
        suggestions: [[String: Any]],
        privacyBlocked: Bool
    ) {
        guard let card = objc_getAssociatedObject(
            self, &STTVocabAssocKeys.cdVocabCard
        ) as? CDSettingsCardView else { return }

        let arrangedViews = card.contentStackView.arrangedSubviews
        // Первые 2 — addRow + separator — сохраняем.
        for v in arrangedViews.dropFirst(2) {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        if hotwords.isEmpty {
            let empty = NSTextField(labelWithString: "Словарь пуст")
            empty.font = .systemFont(ofSize: 12, weight: .regular)
            empty.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(empty)
        } else {
            for word in hotwords {
                let row = makeCDHotwordRow(word: word)
                card.contentStackView.addArrangedSubview(row)
            }
        }

        if truncated {
            let limitNote = NSTextField(labelWithString: "Список заполнен (лимит)")
            limitNote.font = .systemFont(ofSize: 11, weight: .regular)
            limitNote.textColor = KrabEarTheme.Colors.warning
            card.contentStackView.addArrangedSubview(limitNote)
        }

        card.contentStackView.addArrangedSubview(cdMakeSeparator())

        let sugLabel = NSTextField(labelWithString: "ПРЕДЛОЖЕНИЯ")
        sugLabel.font = .systemFont(ofSize: 10, weight: .semibold)
        sugLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(sugLabel)

        if privacyBlocked {
            let privLabel = NSTextField(labelWithString: "Недоступно в режиме приватности")
            privLabel.font = .systemFont(ofSize: 12, weight: .regular)
            privLabel.textColor = KrabEarTheme.Colors.textDisabled
            card.contentStackView.addArrangedSubview(privLabel)
        } else {
            let hotwordSet = Set(hotwords)
            let filteredSuggestions = suggestions.filter { dict in
                guard let w = dict["word"] as? String else { return false }
                return !hotwordSet.contains(w)
            }

            if filteredSuggestions.isEmpty {
                let noSug = NSTextField(labelWithString: "Нет предложений")
                noSug.font = .systemFont(ofSize: 12, weight: .regular)
                noSug.textColor = KrabEarTheme.Colors.textDisabled
                card.contentStackView.addArrangedSubview(noSug)
            } else {
                for dict in filteredSuggestions {
                    guard let word = dict["word"] as? String else { continue }
                    let count = dict["count"] as? Int ?? 0
                    let row = makeCDSuggestionRow(word: word, count: count)
                    card.contentStackView.addArrangedSubview(row)
                }
            }
        }
    }

    // MARK: - Строки для Gemini-дизайна

    @MainActor
    private func makeGeminiHotwordRow(word: String) -> NSView {
        let label = NSTextField(labelWithString: word)
        label.font = KrabEarTheme.Typography.body
        label.textColor = KrabEarTheme.Colors.textPrimary
        label.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let removeButton = NSButton(frame: .zero)
        removeButton.bezelStyle = .inline
        removeButton.isBordered = false
        if let img = NSImage(systemSymbolName: "xmark.circle", accessibilityDescription: "Удалить") {
            removeButton.image = img
            removeButton.imageScaling = .scaleProportionallyDown
            let cfg = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
            removeButton.symbolConfiguration = cfg
        }
        removeButton.contentTintColor = KrabEarTheme.Colors.textDisabled
        removeButton.toolTip = "Удалить \"\(word)\" из словаря"
        removeButton.setAccessibilityLabel("Удалить \"\(word)\" из словаря STT")
        removeButton.identifier = NSUserInterfaceItemIdentifier(word)
        removeButton.target = self
        removeButton.action = #selector(onRemoveSTTHotword(_:))
        removeButton.setContentHuggingPriority(.required, for: .horizontal)

        let row = NSStackView(views: [label, removeButton])
        row.orientation = .horizontal
        row.distribution = .fill
        row.alignment = .centerY
        row.spacing = KrabEarTheme.Metrics.tight
        row.edgeInsets = NSEdgeInsets(top: 3, left: 0, bottom: 3, right: 0)
        return row
    }

    @MainActor
    private func makeGeminiSuggestionRow(word: String, count: Int) -> NSView {
        let label = NSTextField(labelWithString: word)
        label.font = KrabEarTheme.Typography.body
        label.textColor = KrabEarTheme.Colors.textPrimary
        label.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let countBadge = makeBadge(
            text: "×\(count)",
            color: KrabEarTheme.Colors.textSecondary,
            tooltip: "Встречается \(count) раз в истории"
        )

        let addButton = NSButton(frame: .zero)
        addButton.bezelStyle = .inline
        addButton.isBordered = false
        if let img = NSImage(systemSymbolName: "plus.circle", accessibilityDescription: "Добавить") {
            addButton.image = img
            addButton.imageScaling = .scaleProportionallyDown
            let cfg = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
            addButton.symbolConfiguration = cfg
        }
        addButton.contentTintColor = KrabEarTheme.Colors.accent
        addButton.toolTip = "Добавить «\(word)» в словарь STT"
        addButton.setAccessibilityLabel("Добавить «\(word)» в словарь STT")
        addButton.identifier = NSUserInterfaceItemIdentifier(word)
        addButton.target = self
        addButton.action = #selector(onAddSuggestion(_:))
        addButton.setContentHuggingPriority(.required, for: .horizontal)

        let row = NSStackView(views: [label, countBadge, addButton])
        row.orientation = .horizontal
        row.distribution = .fill
        row.alignment = .centerY
        row.spacing = KrabEarTheme.Metrics.tight
        row.edgeInsets = NSEdgeInsets(top: 3, left: 0, bottom: 3, right: 0)
        return row
    }

    // MARK: - Строки для Claude Design

    @MainActor
    private func makeCDHotwordRow(word: String) -> NSView {
        let removeButton = NSButton(frame: .zero)
        removeButton.bezelStyle = .inline
        removeButton.isBordered = false
        if let img = NSImage(systemSymbolName: "xmark.circle", accessibilityDescription: "Удалить") {
            removeButton.image = img
            removeButton.imageScaling = .scaleProportionallyDown
            removeButton.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 12, weight: .regular)
        }
        removeButton.contentTintColor = KrabEarTheme.Colors.textDisabled
        removeButton.toolTip = "Удалить \"\(word)\""
        removeButton.setAccessibilityLabel("Удалить \"\(word)\" из словаря STT")
        removeButton.identifier = NSUserInterfaceItemIdentifier(word)
        removeButton.target = self
        removeButton.action = #selector(onRemoveSTTHotwordCD(_:))
        removeButton.setContentHuggingPriority(.required, for: .horizontal)

        return cdMakeRow(label: word, control: removeButton)
    }

    @MainActor
    private func makeCDSuggestionRow(word: String, count: Int) -> NSView {
        let addButton = NSButton(frame: .zero)
        addButton.bezelStyle = .inline
        addButton.isBordered = false
        if let img = NSImage(systemSymbolName: "plus.circle", accessibilityDescription: "Добавить") {
            addButton.image = img
            addButton.imageScaling = .scaleProportionallyDown
            addButton.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 12, weight: .regular)
        }
        addButton.contentTintColor = KrabEarTheme.Colors.accent
        addButton.toolTip = "Добавить «\(word)»"
        addButton.setAccessibilityLabel("Добавить «\(word)» в словарь STT (упоминается \(count) раз)")
        addButton.identifier = NSUserInterfaceItemIdentifier(word)
        addButton.target = self
        addButton.action = #selector(onAddSuggestionCD(_:))
        addButton.setContentHuggingPriority(.required, for: .horizontal)

        let countLabel = NSTextField(labelWithString: "×\(count)")
        countLabel.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular)
        countLabel.textColor = KrabEarTheme.Colors.textSecondary
        countLabel.setContentHuggingPriority(.required, for: .horizontal)

        let controlStack = NSStackView(views: [countLabel, addButton])
        controlStack.orientation = .horizontal
        controlStack.spacing = KrabEarTheme.Metrics.tight
        controlStack.alignment = .centerY

        return cdMakeRow(label: word, control: controlStack)
    }

    // MARK: - Обработчики кнопок (Gemini)

    /// Обработчик Enter-в-поле (Gemini variant).
    @objc func onAddSTTHotwordFromField(_ sender: NSTextField) {
        addHotwordFromText(sender.stringValue, isClaudeDesign: false)
    }

    /// Обработчик кнопки «Добавить» (Gemini variant).
    @objc func onAddSTTHotword(_ sender: Any) {
        guard let field = objc_getAssociatedObject(
            self, &STTVocabAssocKeys.addTextField
        ) as? NSTextField else { return }
        addHotwordFromText(field.stringValue, isClaudeDesign: false)
    }

    /// Обработчик кнопки удаления (Gemini variant).
    @objc func onRemoveSTTHotword(_ sender: NSButton) {
        guard let word = sender.identifier?.rawValue, !word.isEmpty else { return }
        removeHotword(word, isClaudeDesign: false)
    }

    /// Обработчик кнопки «+» рядом с предложением (Gemini variant).
    @objc func onAddSuggestion(_ sender: NSButton) {
        guard let word = sender.identifier?.rawValue, !word.isEmpty else { return }
        addHotwordFromText(word, isClaudeDesign: false)
    }

    // MARK: - Обработчики кнопок (Claude Design)

    @objc func onAddSTTHotwordFromFieldCD(_ sender: NSTextField) {
        addHotwordFromText(sender.stringValue, isClaudeDesign: true)
    }

    @objc func onAddSTTHotwordCD(_ sender: Any) {
        guard let field = objc_getAssociatedObject(
            self, &STTVocabAssocKeys.cdAddTextField
        ) as? NSTextField else { return }
        addHotwordFromText(field.stringValue, isClaudeDesign: true)
    }

    @objc func onRemoveSTTHotwordCD(_ sender: NSButton) {
        guard let word = sender.identifier?.rawValue, !word.isEmpty else { return }
        removeHotword(word, isClaudeDesign: true)
    }

    @objc func onAddSuggestionCD(_ sender: NSButton) {
        guard let word = sender.identifier?.rawValue, !word.isEmpty else { return }
        addHotwordFromText(word, isClaudeDesign: true)
    }

    // MARK: - Общая логика (off-main IPC)

    /// Добавляет hotword через IPC строго off-main (AGENT-3).
    /// После успеха: очищает поле ввода, перестраивает карточку,
    /// показывает тост «Список заполнен» если truncated.
    private func addHotwordFromText(_ raw: String, isClaudeDesign: Bool) {
        let word = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !word.isEmpty else { return }

        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            let truncated: Bool
            do {
                let resp = try ipc.call(method: "add_stt_hotword", params: ["word": word])
                let result = resp["result"] as? [String: Any]
                truncated = result?["truncated"] as? Bool ?? false
            } catch {
                return
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }

                // Очищаем поле ввода.
                if isClaudeDesign {
                    if let field = objc_getAssociatedObject(
                        self, &STTVocabAssocKeys.cdAddTextField
                    ) as? NSTextField {
                        field.stringValue = ""
                    }
                } else {
                    if let field = objc_getAssociatedObject(
                        self, &STTVocabAssocKeys.addTextField
                    ) as? NSTextField {
                        field.stringValue = ""
                    }
                }

                if truncated {
                    BackendToast.shared.show(
                        "Словарь STT: список заполнен (достигнут лимит)",
                        duration: 3.0
                    )
                }
            }

            // Перезагружаем обе карточки — бэкенд source of truth.
            self.fetchAndRebuildSTTVocabularyCard(isClaudeDesign: false)
            self.fetchAndRebuildSTTVocabularyCard(isClaudeDesign: true)
        }
    }

    /// Удаляет hotword через IPC строго off-main (AGENT-3).
    private func removeHotword(_ word: String, isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try ipc.call(method: "remove_stt_hotword", params: ["word": word])
            } catch {
                return
            }
            // Перезагружаем обе карточки.
            self.fetchAndRebuildSTTVocabularyCard(isClaudeDesign: false)
            self.fetchAndRebuildSTTVocabularyCard(isClaudeDesign: true)
        }
    }

    // MARK: - Вспомогательный separator

    /// NSBox separator — только для этого extension.
    @MainActor
    private func sttVocabMakeSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }
}
