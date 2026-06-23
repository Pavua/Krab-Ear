/*
 Текстовые сниппеты — секция настроек.
 */

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum TextSnippetsAssocKeys {
    nonisolated(unsafe) static var enabledToggle: UInt8 = 0
    nonisolated(unsafe) static var cdEnabledToggle: UInt8 = 0
    nonisolated(unsafe) static var snippetsCard: UInt8 = 0
    nonisolated(unsafe) static var cdSnippetsCard: UInt8 = 0
    
    nonisolated(unsafe) static var addTriggerField: UInt8 = 0
    nonisolated(unsafe) static var addTextField: UInt8 = 0
    nonisolated(unsafe) static var cdAddTriggerField: UInt8 = 0
    nonisolated(unsafe) static var cdAddTextField: UInt8 = 0
}

// MARK: - HistoryPanelController+TextSnippets

extension HistoryPanelController {

    // MARK: - Gemini variant

    @MainActor
    func buildTextSnippetsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_text_snippets",
            title: "Текстовые сниппеты",
            isExpanded: false,
            iconSymbol: "text.quote"
        )

        let card = ThemeCardView()

        // Тоггл «Включить текстовые сниппеты»
        let enabledToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onTextSnippetsEnabledChanged))
        enabledToggle.state = AgentSettings.default.textSnippetsEnabled ? .on : .off
        objc_setAssociatedObject(self, &TextSnippetsAssocKeys.enabledToggle, enabledToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let enabledRow = makeSettingRow(
            label: "Включить текстовые сниппеты",
            description: "Триггеры для быстрой вставки заготовленных текстов. Скажите \"вставь <триггер>\".",
            control: enabledToggle
        )

        // Строка добавления
        let addTriggerField = NSTextField(frame: .zero)
        addTriggerField.placeholderString = "Триггер (напр. моя почта)"
        addTriggerField.font = KrabEarTheme.Typography.body
        addTriggerField.bezelStyle = .roundedBezel
        addTriggerField.isBordered = true
        addTriggerField.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        objc_setAssociatedObject(self, &TextSnippetsAssocKeys.addTriggerField, addTriggerField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let addTextField = NSTextField(frame: .zero)
        addTextField.placeholderString = "Текст (напр. user@example.com)"
        addTextField.font = KrabEarTheme.Typography.body
        addTextField.bezelStyle = .roundedBezel
        addTextField.isBordered = true
        addTextField.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        objc_setAssociatedObject(self, &TextSnippetsAssocKeys.addTextField, addTextField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let addButton = ThemePrimaryButton(title: "Добавить", target: self, action: #selector(onAddTextSnippet(_:)))

        let addStack = NSStackView(views: [addTriggerField, addTextField, addButton])
        addStack.orientation = .horizontal
        addStack.spacing = KrabEarTheme.Metrics.tight
        addStack.alignment = .centerY

        let addRow = makeSettingRow(
            label: "Новый сниппет",
            control: addStack
        )

        // Карточка со списком
        let snipCard = ThemeCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        snipCard.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(self, &TextSnippetsAssocKeys.snippetsCard, snipCard, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let listSubhead = makeSubhead("СУЩЕСТВУЮЩИЕ СНИППЕТЫ")

        card.contentStackView.addArrangedSubview(enabledRow)
        card.contentStackView.addArrangedSubview(sttVocabMakeSeparator())
        card.contentStackView.addArrangedSubview(addRow)

        section.contentStackView.addArrangedSubview(card)
        section.contentStackView.addArrangedSubview(listSubhead)
        section.contentStackView.addArrangedSubview(snipCard)

        fetchAndRebuildTextSnippetsList(isClaudeDesign: false)

        return section
    }

    // MARK: - Claude Design variant

    @MainActor
    func cdBuildTextSnippetsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_text_snippets",
            title: "Текстовые сниппеты",
            isExpanded: false
        )

        let card = CDSettingsCardView()

        let enabledToggle = NSButton(checkboxWithTitle: "Включить", target: self, action: #selector(onTextSnippetsEnabledChangedCD))
        enabledToggle.state = AgentSettings.default.textSnippetsEnabled ? .on : .off
        enabledToggle.toolTip = "Триггеры для быстрой вставки заготовленных текстов. Скажите \"вставь <триггер>\"."
        objc_setAssociatedObject(self, &TextSnippetsAssocKeys.cdEnabledToggle, enabledToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let enabledRow = cdMakeRow(label: "Сниппеты", control: enabledToggle)

        // Добавление
        let addTriggerField = NSTextField(frame: .zero)
        addTriggerField.placeholderString = "Триггер…"
        addTriggerField.font = .systemFont(ofSize: 12, weight: .regular)
        addTriggerField.bezelStyle = .roundedBezel
        addTriggerField.isBordered = true
        addTriggerField.widthAnchor.constraint(greaterThanOrEqualToConstant: 80).isActive = true
        objc_setAssociatedObject(self, &TextSnippetsAssocKeys.cdAddTriggerField, addTriggerField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let addTextField = NSTextField(frame: .zero)
        addTextField.placeholderString = "Текст…"
        addTextField.font = .systemFont(ofSize: 12, weight: .regular)
        addTextField.bezelStyle = .roundedBezel
        addTextField.isBordered = true
        addTextField.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        objc_setAssociatedObject(self, &TextSnippetsAssocKeys.cdAddTextField, addTextField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let addButton = NSButton(title: "Добавить", target: self, action: #selector(onAddTextSnippetCD(_:)))
        addButton.bezelStyle = .rounded

        let addStack = NSStackView(views: [addTriggerField, addTextField, addButton])
        addStack.orientation = .horizontal
        addStack.spacing = KrabEarTheme.Metrics.tight
        addStack.alignment = .centerY

        let addRow = cdMakeRow(label: "Добавить", control: addStack)

        let snipCard = CDSettingsCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        snipCard.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(self, &TextSnippetsAssocKeys.cdSnippetsCard, snipCard, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        card.contentStackView.addArrangedSubview(enabledRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(addRow)

        section.contentStackView.addArrangedSubview(card)
        section.contentStackView.addArrangedSubview(snipCard)

        fetchAndRebuildTextSnippetsList(isClaudeDesign: true)

        return section
    }

    // MARK: - Toggle Actions

    @objc func onTextSnippetsEnabledChanged() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.enabledToggle) as? NSButton else { return }
        applySettingsPatch(["text_snippets_enabled": toggle.state == .on])
    }

    @objc func onTextSnippetsEnabledChangedCD() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.cdEnabledToggle) as? NSButton else { return }
        applySettingsPatch(["text_snippets_enabled": toggle.state == .on])
    }

    @MainActor
    func syncTextSnippetsToggles(enabled: Bool) {
        if let t = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.enabledToggle) as? NSButton {
            t.state = enabled ? .on : .off
        }
        if let t = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.cdEnabledToggle) as? NSButton {
            t.state = enabled ? .on : .off
        }
    }

    // MARK: - Add/Remove Actions

    @objc func onAddTextSnippet(_ sender: NSButton) {
        guard let triggerField = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.addTriggerField) as? NSTextField,
              let textField = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.addTextField) as? NSTextField else { return }
        addSnippetIPC(trigger: triggerField.stringValue, text: textField.stringValue, isClaudeDesign: false)
    }

    @objc func onAddTextSnippetCD(_ sender: NSButton) {
        guard let triggerField = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.cdAddTriggerField) as? NSTextField,
              let textField = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.cdAddTextField) as? NSTextField else { return }
        addSnippetIPC(trigger: triggerField.stringValue, text: textField.stringValue, isClaudeDesign: true)
    }

    @objc func onRemoveTextSnippet(_ sender: NSButton) {
        guard let trigger = sender.identifier?.rawValue, !trigger.isEmpty else { return }
        removeSnippetIPC(trigger: trigger, isClaudeDesign: false)
    }

    @objc func onRemoveTextSnippetCD(_ sender: NSButton) {
        guard let trigger = sender.identifier?.rawValue, !trigger.isEmpty else { return }
        removeSnippetIPC(trigger: trigger, isClaudeDesign: true)
    }

    // MARK: - IPC

    private func addSnippetIPC(trigger: String, text: String, isClaudeDesign: Bool) {
        let t = trigger.trimmingCharacters(in: .whitespacesAndNewlines)
        let txt = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, !txt.isEmpty else { return }

        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try ipc.call(method: "add_text_snippet", params: ["trigger": t, "expansion": txt])
            } catch {
                print("[Agent] add_text_snippet failed: \(error)")
            }
            DispatchQueue.main.async {
                // Clear fields
                if isClaudeDesign {
                    if let tf = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.cdAddTriggerField) as? NSTextField { tf.stringValue = "" }
                    if let tf = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.cdAddTextField) as? NSTextField { tf.stringValue = "" }
                } else {
                    if let tf = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.addTriggerField) as? NSTextField { tf.stringValue = "" }
                    if let tf = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.addTextField) as? NSTextField { tf.stringValue = "" }
                }
                self.fetchAndRebuildTextSnippetsList(isClaudeDesign: isClaudeDesign)
            }
        }
    }

    private func removeSnippetIPC(trigger: String, isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try ipc.call(method: "remove_text_snippet", params: ["trigger": trigger])
            } catch {
                print("[Agent] remove_text_snippet failed: \(error)")
            }
            DispatchQueue.main.async {
                self.fetchAndRebuildTextSnippetsList(isClaudeDesign: isClaudeDesign)
            }
        }
    }

    func fetchAndRebuildTextSnippetsList(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }

            let snippets: [[String: Any]]
            do {
                let resp = try ipc.call(method: "list_text_snippets", params: [:])
                let result = resp["result"] as? [String: Any]
                snippets = result?["snippets"] as? [[String: Any]] ?? []
            } catch {
                snippets = []
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if isClaudeDesign {
                    self.rebuildCDTextSnippetsList(snippets: snippets)
                } else {
                    self.rebuildGeminiTextSnippetsList(snippets: snippets)
                }
            }
        }
    }

    // MARK: - Rebuild Lists

    @MainActor
    private func rebuildGeminiTextSnippetsList(snippets: [[String: Any]]) {
        guard let card = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.snippetsCard) as? ThemeCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        guard !snippets.isEmpty else {
            let unavailable = NSTextField(labelWithString: "Нет сниппетов")
            unavailable.font = KrabEarTheme.Typography.caption
            unavailable.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(unavailable)
            return
        }

        for snip in snippets {
            let trigger = snip["trigger"] as? String ?? ""
            let text = snip["expansion"] as? String ?? ""

            let triggerLabel = NSTextField(labelWithString: "[\(trigger)]")
            triggerLabel.font = KrabEarTheme.Typography.body
            triggerLabel.textColor = KrabEarTheme.Colors.textPrimary
            
            let textLabel = NSTextField(labelWithString: text)
            textLabel.font = KrabEarTheme.Typography.body
            textLabel.textColor = KrabEarTheme.Colors.textSecondary
            textLabel.lineBreakMode = .byTruncatingTail
            textLabel.maximumNumberOfLines = 1
            
            let delButton = ThemeSecondaryButton(title: "Удалить", target: self, action: #selector(onRemoveTextSnippet(_:)))
            delButton.identifier = NSUserInterfaceItemIdentifier(trigger)

            let stack = NSStackView(views: [triggerLabel, textLabel, delButton])
            stack.orientation = .horizontal
            stack.spacing = KrabEarTheme.Metrics.tight
            stack.alignment = .centerY
            
            triggerLabel.setContentHuggingPriority(.required, for: .horizontal)
            delButton.setContentHuggingPriority(.required, for: .horizontal)
            textLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

            card.contentStackView.addArrangedSubview(stack)
        }
    }

    @MainActor
    private func rebuildCDTextSnippetsList(snippets: [[String: Any]]) {
        guard let card = objc_getAssociatedObject(self, &TextSnippetsAssocKeys.cdSnippetsCard) as? CDSettingsCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        guard !snippets.isEmpty else {
            let unavailable = NSTextField(labelWithString: "Нет сниппетов")
            unavailable.font = .systemFont(ofSize: 12, weight: .regular)
            unavailable.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(unavailable)
            return
        }

        for (idx, snip) in snippets.enumerated() {
            if idx > 0 {
                card.contentStackView.addArrangedSubview(cdMakeSeparator())
            }

            let trigger = snip["trigger"] as? String ?? ""
            let text = snip["expansion"] as? String ?? ""

            let triggerLabel = NSTextField(labelWithString: "[\(trigger)]")
            triggerLabel.font = .systemFont(ofSize: 12, weight: .bold)
            triggerLabel.textColor = KrabEarTheme.Colors.textPrimary
            
            let textLabel = NSTextField(labelWithString: text)
            textLabel.font = .systemFont(ofSize: 12, weight: .regular)
            textLabel.textColor = KrabEarTheme.Colors.textSecondary
            textLabel.lineBreakMode = .byTruncatingTail
            textLabel.maximumNumberOfLines = 1
            
            let delButton = NSButton(title: "Удалить", target: self, action: #selector(onRemoveTextSnippetCD(_:)))
            delButton.bezelStyle = .rounded
            delButton.identifier = NSUserInterfaceItemIdentifier(trigger)

            let stack = NSStackView(views: [triggerLabel, textLabel, delButton])
            stack.orientation = .horizontal
            stack.spacing = KrabEarTheme.Metrics.tight
            stack.alignment = .centerY
            
            triggerLabel.setContentHuggingPriority(.required, for: .horizontal)
            delButton.setContentHuggingPriority(.required, for: .horizontal)
            textLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

            card.contentStackView.addArrangedSubview(stack)
        }
    }
}
