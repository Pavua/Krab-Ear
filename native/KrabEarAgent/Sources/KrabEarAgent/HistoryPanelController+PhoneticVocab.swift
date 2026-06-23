/*
 Фонетический словарь — секция настроек.
 */

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum PhoneticVocabAssocKeys {
    nonisolated(unsafe) static var enabledToggle: UInt8 = 0
    nonisolated(unsafe) static var cdEnabledToggle: UInt8 = 0
    nonisolated(unsafe) static var entriesCard: UInt8 = 0
    nonisolated(unsafe) static var cdEntriesCard: UInt8 = 0

    nonisolated(unsafe) static var addCanonicalField: UInt8 = 0
    nonisolated(unsafe) static var addVariantsField: UInt8 = 0
    nonisolated(unsafe) static var cdAddCanonicalField: UInt8 = 0
    nonisolated(unsafe) static var cdAddVariantsField: UInt8 = 0
}

// MARK: - HistoryPanelController+PhoneticVocab

extension HistoryPanelController {

    // MARK: - Gemini variant

    @MainActor
    func buildPhoneticVocabSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_phonetic_vocab",
            title: "Фонетический словарь",
            isExpanded: false,
            iconSymbol: "character.book.closed"
        )

        let card = ThemeCardView()

        // Тоггл «Включить фонетический словарь»
        let enabledToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onPhoneticVocabEnabledChanged))
        enabledToggle.state = AgentSettings.default.phoneticVocabEnabled ? .on : .off
        objc_setAssociatedObject(self, &PhoneticVocabAssocKeys.enabledToggle, enabledToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let enabledRow = makeSettingRow(
            label: "Включить фонетический словарь",
            description: "Сопоставляет произносимые варианты слов с правильным написанием при транскрибации.",
            control: enabledToggle
        )

        // Строка добавления: поле «Правильно» + поле «Варианты» (через запятую)
        let addCanonicalField = NSTextField(frame: .zero)
        addCanonicalField.placeholderString = "Правильно (напр. Павел)"
        addCanonicalField.font = KrabEarTheme.Typography.body
        addCanonicalField.bezelStyle = .roundedBezel
        addCanonicalField.isBordered = true
        addCanonicalField.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        objc_setAssociatedObject(self, &PhoneticVocabAssocKeys.addCanonicalField, addCanonicalField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let addVariantsField = NSTextField(frame: .zero)
        addVariantsField.placeholderString = "Варианты: пашел, павэл"
        addVariantsField.font = KrabEarTheme.Typography.body
        addVariantsField.bezelStyle = .roundedBezel
        addVariantsField.isBordered = true
        addVariantsField.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        objc_setAssociatedObject(self, &PhoneticVocabAssocKeys.addVariantsField, addVariantsField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let addButton = ThemePrimaryButton(title: "Добавить", target: self, action: #selector(onAddPhoneticEntry(_:)))

        let addStack = NSStackView(views: [addCanonicalField, addVariantsField, addButton])
        addStack.orientation = .horizontal
        addStack.spacing = KrabEarTheme.Metrics.tight
        addStack.alignment = .centerY

        let addRow = makeSettingRow(
            label: "Новая запись",
            control: addStack
        )

        // Карточка со списком
        let entriesCard = ThemeCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        entriesCard.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(self, &PhoneticVocabAssocKeys.entriesCard, entriesCard, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let listSubhead = makeSubhead("СУЩЕСТВУЮЩИЕ ЗАПИСИ")

        card.contentStackView.addArrangedSubview(enabledRow)
        card.contentStackView.addArrangedSubview(sttVocabMakeSeparator())
        card.contentStackView.addArrangedSubview(addRow)

        section.contentStackView.addArrangedSubview(card)
        section.contentStackView.addArrangedSubview(listSubhead)
        section.contentStackView.addArrangedSubview(entriesCard)

        fetchAndRebuildPhoneticVocabList(isClaudeDesign: false)

        return section
    }

    // MARK: - Claude Design variant

    @MainActor
    func cdBuildPhoneticVocabSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_phonetic_vocab",
            title: "Фонетический словарь",
            isExpanded: false
        )

        let card = CDSettingsCardView()

        let enabledToggle = NSButton(checkboxWithTitle: "Включить", target: self, action: #selector(onPhoneticVocabEnabledChangedCD))
        enabledToggle.state = AgentSettings.default.phoneticVocabEnabled ? .on : .off
        enabledToggle.toolTip = "Сопоставляет произносимые варианты слов с правильным написанием при транскрибации."
        objc_setAssociatedObject(self, &PhoneticVocabAssocKeys.cdEnabledToggle, enabledToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let enabledRow = cdMakeRow(label: "Фонетика", control: enabledToggle)

        // Добавление
        let addCanonicalField = NSTextField(frame: .zero)
        addCanonicalField.placeholderString = "Правильно..."
        addCanonicalField.font = .systemFont(ofSize: 12, weight: .regular)
        addCanonicalField.bezelStyle = .roundedBezel
        addCanonicalField.isBordered = true
        addCanonicalField.widthAnchor.constraint(greaterThanOrEqualToConstant: 80).isActive = true
        objc_setAssociatedObject(self, &PhoneticVocabAssocKeys.cdAddCanonicalField, addCanonicalField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let addVariantsField = NSTextField(frame: .zero)
        addVariantsField.placeholderString = "Варианты..."
        addVariantsField.font = .systemFont(ofSize: 12, weight: .regular)
        addVariantsField.bezelStyle = .roundedBezel
        addVariantsField.isBordered = true
        addVariantsField.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        objc_setAssociatedObject(self, &PhoneticVocabAssocKeys.cdAddVariantsField, addVariantsField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let addButton = NSButton(title: "Добавить", target: self, action: #selector(onAddPhoneticEntryCD(_:)))
        addButton.bezelStyle = .rounded

        let addStack = NSStackView(views: [addCanonicalField, addVariantsField, addButton])
        addStack.orientation = .horizontal
        addStack.spacing = KrabEarTheme.Metrics.tight
        addStack.alignment = .centerY

        let addRow = cdMakeRow(label: "Добавить", control: addStack)

        let entriesCard = CDSettingsCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка...")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        entriesCard.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(self, &PhoneticVocabAssocKeys.cdEntriesCard, entriesCard, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        card.contentStackView.addArrangedSubview(enabledRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(addRow)

        section.contentStackView.addArrangedSubview(card)
        section.contentStackView.addArrangedSubview(entriesCard)

        fetchAndRebuildPhoneticVocabList(isClaudeDesign: true)

        return section
    }

    // MARK: - Toggle Actions

    @objc func onPhoneticVocabEnabledChanged() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.enabledToggle) as? NSButton else { return }
        applySettingsPatch(["phonetic_vocab_enabled": toggle.state == .on])
    }

    @objc func onPhoneticVocabEnabledChangedCD() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.cdEnabledToggle) as? NSButton else { return }
        applySettingsPatch(["phonetic_vocab_enabled": toggle.state == .on])
    }

    @MainActor
    func syncPhoneticVocabToggles(enabled: Bool) {
        if let t = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.enabledToggle) as? NSButton {
            t.state = enabled ? .on : .off
        }
        if let t = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.cdEnabledToggle) as? NSButton {
            t.state = enabled ? .on : .off
        }
    }

    // MARK: - Add/Remove Actions

    @objc func onAddPhoneticEntry(_ sender: NSButton) {
        guard let canonicalField = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.addCanonicalField) as? NSTextField,
              let variantsField = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.addVariantsField) as? NSTextField else { return }
        addPhoneticEntryIPC(canonical: canonicalField.stringValue, variantsRaw: variantsField.stringValue, isClaudeDesign: false)
    }

    @objc func onAddPhoneticEntryCD(_ sender: NSButton) {
        guard let canonicalField = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.cdAddCanonicalField) as? NSTextField,
              let variantsField = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.cdAddVariantsField) as? NSTextField else { return }
        addPhoneticEntryIPC(canonical: canonicalField.stringValue, variantsRaw: variantsField.stringValue, isClaudeDesign: true)
    }

    @objc func onRemovePhoneticEntry(_ sender: NSButton) {
        guard let canonical = sender.identifier?.rawValue, !canonical.isEmpty else { return }
        removePhoneticEntryIPC(canonical: canonical, isClaudeDesign: false)
    }

    @objc func onRemovePhoneticEntryCD(_ sender: NSButton) {
        guard let canonical = sender.identifier?.rawValue, !canonical.isEmpty else { return }
        removePhoneticEntryIPC(canonical: canonical, isClaudeDesign: true)
    }

    // MARK: - IPC

    private func addPhoneticEntryIPC(canonical: String, variantsRaw: String, isClaudeDesign: Bool) {
        let c = canonical.trimmingCharacters(in: .whitespacesAndNewlines)
        // Split variants on commas, trim each, drop empties
        let variants = variantsRaw
            .split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !c.isEmpty, !variants.isEmpty else { return }

        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try ipc.call(method: "add_phonetic_entry", params: ["canonical": c, "variants": variants])
            } catch {
                print("[Agent] add_phonetic_entry failed: \(error)")
            }
            DispatchQueue.main.async {
                // Clear fields
                if isClaudeDesign {
                    if let tf = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.cdAddCanonicalField) as? NSTextField { tf.stringValue = "" }
                    if let tf = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.cdAddVariantsField) as? NSTextField { tf.stringValue = "" }
                } else {
                    if let tf = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.addCanonicalField) as? NSTextField { tf.stringValue = "" }
                    if let tf = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.addVariantsField) as? NSTextField { tf.stringValue = "" }
                }
                self.fetchAndRebuildPhoneticVocabList(isClaudeDesign: isClaudeDesign)
            }
        }
    }

    private func removePhoneticEntryIPC(canonical: String, isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try ipc.call(method: "remove_phonetic_entry", params: ["canonical": canonical])
            } catch {
                print("[Agent] remove_phonetic_entry failed: \(error)")
            }
            DispatchQueue.main.async {
                self.fetchAndRebuildPhoneticVocabList(isClaudeDesign: isClaudeDesign)
            }
        }
    }

    func fetchAndRebuildPhoneticVocabList(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }

            let entries: [[String: Any]]
            do {
                let resp = try ipc.call(method: "list_phonetic_entries", params: [:])
                let result = resp["result"] as? [String: Any]
                entries = result?["entries"] as? [[String: Any]] ?? []
            } catch {
                entries = []
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if isClaudeDesign {
                    self.rebuildCDPhoneticVocabList(entries: entries)
                } else {
                    self.rebuildGeminiPhoneticVocabList(entries: entries)
                }
            }
        }
    }

    // MARK: - Rebuild Lists

    @MainActor
    private func rebuildGeminiPhoneticVocabList(entries: [[String: Any]]) {
        guard let card = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.entriesCard) as? ThemeCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        guard !entries.isEmpty else {
            let unavailable = NSTextField(labelWithString: "Нет записей")
            unavailable.font = KrabEarTheme.Typography.caption
            unavailable.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(unavailable)
            return
        }

        for entry in entries {
            let canonical = entry["canonical"] as? String ?? ""
            let variants = entry["variants"] as? [String] ?? []
            let variantsText = variants.joined(separator: ", ")

            let canonicalLabel = NSTextField(labelWithString: "[\(canonical)]")
            canonicalLabel.font = KrabEarTheme.Typography.body
            canonicalLabel.textColor = KrabEarTheme.Colors.textPrimary

            let variantsLabel = NSTextField(labelWithString: variantsText)
            variantsLabel.font = KrabEarTheme.Typography.body
            variantsLabel.textColor = KrabEarTheme.Colors.textSecondary
            variantsLabel.lineBreakMode = .byTruncatingTail
            variantsLabel.maximumNumberOfLines = 1

            let delButton = ThemeSecondaryButton(title: "Удалить", target: self, action: #selector(onRemovePhoneticEntry(_:)))
            delButton.identifier = NSUserInterfaceItemIdentifier(canonical)

            let stack = NSStackView(views: [canonicalLabel, variantsLabel, delButton])
            stack.orientation = .horizontal
            stack.spacing = KrabEarTheme.Metrics.tight
            stack.alignment = .centerY

            canonicalLabel.setContentHuggingPriority(.required, for: .horizontal)
            delButton.setContentHuggingPriority(.required, for: .horizontal)
            variantsLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

            card.contentStackView.addArrangedSubview(stack)
        }
    }

    @MainActor
    private func rebuildCDPhoneticVocabList(entries: [[String: Any]]) {
        guard let card = objc_getAssociatedObject(self, &PhoneticVocabAssocKeys.cdEntriesCard) as? CDSettingsCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        guard !entries.isEmpty else {
            let unavailable = NSTextField(labelWithString: "Нет записей")
            unavailable.font = .systemFont(ofSize: 12, weight: .regular)
            unavailable.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(unavailable)
            return
        }

        for (idx, entry) in entries.enumerated() {
            if idx > 0 {
                card.contentStackView.addArrangedSubview(cdMakeSeparator())
            }

            let canonical = entry["canonical"] as? String ?? ""
            let variants = entry["variants"] as? [String] ?? []
            let variantsText = variants.joined(separator: ", ")

            let canonicalLabel = NSTextField(labelWithString: "[\(canonical)]")
            canonicalLabel.font = .systemFont(ofSize: 12, weight: .bold)
            canonicalLabel.textColor = KrabEarTheme.Colors.textPrimary

            let variantsLabel = NSTextField(labelWithString: variantsText)
            variantsLabel.font = .systemFont(ofSize: 12, weight: .regular)
            variantsLabel.textColor = KrabEarTheme.Colors.textSecondary
            variantsLabel.lineBreakMode = .byTruncatingTail
            variantsLabel.maximumNumberOfLines = 1

            let delButton = NSButton(title: "Удалить", target: self, action: #selector(onRemovePhoneticEntryCD(_:)))
            delButton.bezelStyle = .rounded
            delButton.identifier = NSUserInterfaceItemIdentifier(canonical)

            let stack = NSStackView(views: [canonicalLabel, variantsLabel, delButton])
            stack.orientation = .horizontal
            stack.spacing = KrabEarTheme.Metrics.tight
            stack.alignment = .centerY

            canonicalLabel.setContentHuggingPriority(.required, for: .horizontal)
            delButton.setContentHuggingPriority(.required, for: .horizontal)
            variantsLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

            card.contentStackView.addArrangedSubview(stack)
        }
    }
}
