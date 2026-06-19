/*
 Шаблоны вывода — секция настроек.

 IPC-контракты (все существуют в бэкенде, TemplateManager):
   - get_templates {}                           → result.templates ([{name, text, category?, ...}])
   - add_template {name, text, category?}       → result.template ({name, text, category, ...})
   - remove_template {name}                     → result.removed (Bool), result.name (String)
   - apply_template {name, variables?}          → result.text (String), result.name (String)

 Архитектура:
   - buildOutputTemplatesSection()   — секция для Gemini-дизайна (settingsBar).
   - cdBuildOutputTemplatesSection() — секция для Claude Design (settingsBarCD).
   - fetchAndRebuildOutputTemplatesCard(isClaudeDesign:) — загрузка с бэкенда, перестройка карточки.
   - onAddOutputTemplate(_:)         — обработчик кнопки «Добавить».
   - onRemoveOutputTemplate(_:)      — обработчик кнопки удаления (SF Symbol xmark.circle).
   - onApplyOutputTemplate(_:)       — применить → скопировать в буфер обмена + BackendToast.

 Правила AGENT-3 (AppHang-класс): IPC строго в DispatchQueue.global,
 мутации UI — строго в DispatchQueue.main.
 Никакого runModal() — вставка/удаление не требуют подтверждения.
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum OutputTemplatesAssocKeys {
    nonisolated(unsafe) static var templateCard: UInt8 = 0
    nonisolated(unsafe) static var cdTemplateCard: UInt8 = 0
    nonisolated(unsafe) static var nameField: UInt8 = 0
    nonisolated(unsafe) static var cdNameField: UInt8 = 0
    nonisolated(unsafe) static var textField: UInt8 = 0
    nonisolated(unsafe) static var cdTextField: UInt8 = 0
}

// MARK: - HistoryPanelController+OutputTemplates

extension HistoryPanelController {

    // MARK: - Gemini variant: секция для settingsBar

    /// Строит секцию «Шаблоны вывода» для Gemini-дизайна (settingsBar).
    @MainActor
    func buildOutputTemplatesSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "output_templates",
            title: "Шаблоны вывода",
            isExpanded: false,
            iconSymbol: "doc.text"
        )

        let card = ThemeCardView()

        // Строка «Название»: NSTextField для имени шаблона.
        let nameField = NSTextField(frame: .zero)
        nameField.placeholderString = "Название шаблона…"
        nameField.font = KrabEarTheme.Typography.body
        nameField.bezelStyle = .roundedBezel
        nameField.isBordered = true
        nameField.focusRingType = .default
        nameField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        nameField.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true

        objc_setAssociatedObject(
            self,
            &OutputTemplatesAssocKeys.nameField,
            nameField,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        // Строка «Текст»: NSTextField для текста шаблона (однострочная, Enter→добавить).
        let textField = NSTextField(frame: .zero)
        textField.placeholderString = "Текст шаблона (поддерживает {переменные})…"
        textField.font = KrabEarTheme.Typography.body
        textField.bezelStyle = .roundedBezel
        textField.isBordered = true
        textField.focusRingType = .default
        textField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        textField.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        textField.target = self
        textField.action = #selector(onAddOutputTemplateFromField(_:))

        objc_setAssociatedObject(
            self,
            &OutputTemplatesAssocKeys.textField,
            textField,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let addButton = ThemePrimaryButton(title: "Добавить", target: self, action: #selector(onAddOutputTemplate(_:)))
        addButton.setContentHuggingPriority(.required, for: .horizontal)
        addButton.setAccessibilityLabel("Добавить шаблон вывода")

        let nameRow = makeSettingRow(
            label: "Название",
            description: nil,
            control: nameField
        )

        let textAndButton = NSStackView(views: [textField, addButton])
        textAndButton.orientation = .horizontal
        textAndButton.spacing = KrabEarTheme.Metrics.tight
        textAndButton.alignment = .centerY

        let textRow = makeSettingRow(
            label: "Текст",
            description: "Используйте {переменная} для подстановки. «Применить» скопирует результат в буфер.",
            control: textAndButton
        )

        // Загрузочная карточка — наполняется асинхронно.
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary

        objc_setAssociatedObject(
            self,
            &OutputTemplatesAssocKeys.templateCard,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        card.contentStackView.addArrangedSubview(nameRow)
        card.contentStackView.addArrangedSubview(textRow)
        card.contentStackView.addArrangedSubview(outputTemplatesMakeSeparator())
        card.contentStackView.addArrangedSubview(loadingLabel)

        section.contentStackView.addArrangedSubview(card)

        // Запускаем загрузку шаблонов с бэкенда (off-main, AGENT-3).
        fetchAndRebuildOutputTemplatesCard(isClaudeDesign: false)

        return section
    }

    // MARK: - Claude Design variant: секция для settingsBarCD

    /// Строит секцию «Шаблоны вывода» для Claude Design (settingsBarCD).
    @MainActor
    func cdBuildOutputTemplatesSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_output_templates",
            title: "Шаблоны вывода",
            isExpanded: false
        )

        let card = CDSettingsCardView()

        // Поле «Название».
        let nameField = NSTextField(frame: .zero)
        nameField.placeholderString = "Название…"
        nameField.font = .systemFont(ofSize: 12, weight: .regular)
        nameField.bezelStyle = .roundedBezel
        nameField.isBordered = true
        nameField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        nameField.widthAnchor.constraint(greaterThanOrEqualToConstant: 100).isActive = true

        objc_setAssociatedObject(
            self,
            &OutputTemplatesAssocKeys.cdNameField,
            nameField,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        // Поле «Текст» + кнопка «Добавить».
        let textField = NSTextField(frame: .zero)
        textField.placeholderString = "Текст шаблона…"
        textField.font = .systemFont(ofSize: 12, weight: .regular)
        textField.bezelStyle = .roundedBezel
        textField.isBordered = true
        textField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        textField.widthAnchor.constraint(greaterThanOrEqualToConstant: 110).isActive = true
        textField.target = self
        textField.action = #selector(onAddOutputTemplateFromFieldCD(_:))

        objc_setAssociatedObject(
            self,
            &OutputTemplatesAssocKeys.cdTextField,
            textField,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let addButton = NSButton(title: "Добавить", target: self, action: #selector(onAddOutputTemplateCD(_:)))
        addButton.bezelStyle = .rounded
        addButton.setContentHuggingPriority(.required, for: .horizontal)
        addButton.setAccessibilityLabel("Добавить шаблон вывода")

        let nameRow = cdMakeRow(label: "Название", control: nameField)

        let textAndButton = NSStackView(views: [textField, addButton])
        textAndButton.orientation = .horizontal
        textAndButton.spacing = KrabEarTheme.Metrics.tight
        textAndButton.alignment = .centerY

        let textRow = cdMakeRow(label: "Текст", control: textAndButton)

        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary

        objc_setAssociatedObject(
            self,
            &OutputTemplatesAssocKeys.cdTemplateCard,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        card.contentStackView.addArrangedSubview(nameRow)
        card.contentStackView.addArrangedSubview(textRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(loadingLabel)

        section.contentStackView.addArrangedSubview(card)

        fetchAndRebuildOutputTemplatesCard(isClaudeDesign: true)

        return section
    }

    // MARK: - Загрузка данных с бэкенда

    /// Запрашивает get_templates строго off-main (AGENT-3).
    /// Перестраивает нужную карточку на main thread.
    func fetchAndRebuildOutputTemplatesCard(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            let templates: [[String: Any]]
            do {
                let resp = try ipc.call(method: "get_templates", params: [:])
                let result = resp["result"] as? [String: Any]
                templates = result?["templates"] as? [[String: Any]] ?? []
            } catch {
                templates = []
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if isClaudeDesign {
                    self.rebuildCDOutputTemplatesCard(templates: templates)
                } else {
                    self.rebuildGeminiOutputTemplatesCard(templates: templates)
                }
            }
        }
    }

    // MARK: - Перестройка карточки (Gemini)

    @MainActor
    private func rebuildGeminiOutputTemplatesCard(templates: [[String: Any]]) {
        guard let card = objc_getAssociatedObject(
            self, &OutputTemplatesAssocKeys.templateCard
        ) as? ThemeCardView else { return }

        // Первые 3 — nameRow + textRow + separator — сохраняем; остальные удаляем.
        let arrangedViews = card.contentStackView.arrangedSubviews
        for v in arrangedViews.dropFirst(3) {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        if templates.isEmpty {
            let empty = NSTextField(labelWithString: "Шаблонов нет")
            empty.font = KrabEarTheme.Typography.caption
            empty.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(empty)
        } else {
            for tmpl in templates {
                guard let name = tmpl["name"] as? String else { continue }
                let text = tmpl["text"] as? String ?? ""
                let row = makeGeminiTemplateRow(name: name, text: text)
                card.contentStackView.addArrangedSubview(row)
            }
        }
    }

    // MARK: - Перестройка карточки (Claude Design)

    @MainActor
    private func rebuildCDOutputTemplatesCard(templates: [[String: Any]]) {
        guard let card = objc_getAssociatedObject(
            self, &OutputTemplatesAssocKeys.cdTemplateCard
        ) as? CDSettingsCardView else { return }

        // Первые 3 — nameRow + textRow + separator — сохраняем; остальные удаляем.
        let arrangedViews = card.contentStackView.arrangedSubviews
        for v in arrangedViews.dropFirst(3) {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        if templates.isEmpty {
            let empty = NSTextField(labelWithString: "Шаблонов нет")
            empty.font = .systemFont(ofSize: 12, weight: .regular)
            empty.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(empty)
        } else {
            for tmpl in templates {
                guard let name = tmpl["name"] as? String else { continue }
                let text = tmpl["text"] as? String ?? ""
                let row = makeCDTemplateRow(name: name, text: text)
                card.contentStackView.addArrangedSubview(row)
            }
        }
    }

    // MARK: - Строки для Gemini-дизайна

    @MainActor
    private func makeGeminiTemplateRow(name: String, text: String) -> NSView {
        // Название (жирное) + превью текста (серое, усечённое).
        let nameLabel = NSTextField(labelWithString: name)
        nameLabel.font = KrabEarTheme.Typography.body
        nameLabel.textColor = KrabEarTheme.Colors.textPrimary
        nameLabel.setContentHuggingPriority(.required, for: .horizontal)

        let preview = String(text.prefix(40)) + (text.count > 40 ? "…" : "")
        let previewLabel = NSTextField(labelWithString: preview)
        previewLabel.font = KrabEarTheme.Typography.caption
        previewLabel.textColor = KrabEarTheme.Colors.textSecondary
        previewLabel.setContentHuggingPriority(.defaultLow, for: .horizontal)
        previewLabel.lineBreakMode = .byTruncatingTail

        let labelsStack = NSStackView(views: [nameLabel, previewLabel])
        labelsStack.orientation = .horizontal
        labelsStack.spacing = KrabEarTheme.Metrics.tight
        labelsStack.alignment = .centerY
        labelsStack.setContentHuggingPriority(.defaultLow, for: .horizontal)

        // Кнопка «Применить → буфер».
        let applyButton = NSButton(frame: .zero)
        applyButton.bezelStyle = .inline
        applyButton.isBordered = false
        if let img = NSImage(systemSymbolName: "doc.on.clipboard", accessibilityDescription: "Применить") {
            applyButton.image = img
            applyButton.imageScaling = .scaleProportionallyDown
            let cfg = NSImage.SymbolConfiguration(pointSize: 13, weight: .regular)
            applyButton.symbolConfiguration = cfg
        }
        applyButton.contentTintColor = KrabEarTheme.Colors.accent
        applyButton.toolTip = "Применить «\(name)» → скопировать в буфер"
        applyButton.setAccessibilityLabel("Применить шаблон «\(name)» и скопировать в буфер")
        applyButton.identifier = NSUserInterfaceItemIdentifier(name)
        applyButton.target = self
        applyButton.action = #selector(onApplyOutputTemplate(_:))
        applyButton.setContentHuggingPriority(.required, for: .horizontal)

        // Кнопка удаления.
        let removeButton = NSButton(frame: .zero)
        removeButton.bezelStyle = .inline
        removeButton.isBordered = false
        if let img = NSImage(systemSymbolName: "xmark.circle", accessibilityDescription: "Удалить") {
            removeButton.image = img
            removeButton.imageScaling = .scaleProportionallyDown
            let cfg = NSImage.SymbolConfiguration(pointSize: 13, weight: .regular)
            removeButton.symbolConfiguration = cfg
        }
        removeButton.contentTintColor = KrabEarTheme.Colors.textDisabled
        removeButton.toolTip = "Удалить шаблон «\(name)»"
        removeButton.setAccessibilityLabel("Удалить шаблон «\(name)»")
        removeButton.identifier = NSUserInterfaceItemIdentifier(name)
        removeButton.target = self
        removeButton.action = #selector(onRemoveOutputTemplate(_:))
        removeButton.setContentHuggingPriority(.required, for: .horizontal)

        let row = NSStackView(views: [labelsStack, applyButton, removeButton])
        row.orientation = .horizontal
        row.distribution = .fill
        row.alignment = .centerY
        row.spacing = KrabEarTheme.Metrics.tight
        row.edgeInsets = NSEdgeInsets(top: 3, left: 0, bottom: 3, right: 0)
        return row
    }

    // MARK: - Строки для Claude Design

    @MainActor
    private func makeCDTemplateRow(name: String, text: String) -> NSView {
        // Кнопка «Применить → буфер».
        let applyButton = NSButton(frame: .zero)
        applyButton.bezelStyle = .inline
        applyButton.isBordered = false
        if let img = NSImage(systemSymbolName: "doc.on.clipboard", accessibilityDescription: "Применить") {
            applyButton.image = img
            applyButton.imageScaling = .scaleProportionallyDown
            applyButton.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 12, weight: .regular)
        }
        applyButton.contentTintColor = KrabEarTheme.Colors.accent
        applyButton.toolTip = "Применить «\(name)» → скопировать в буфер"
        applyButton.setAccessibilityLabel("Применить шаблон «\(name)» и скопировать в буфер")
        applyButton.identifier = NSUserInterfaceItemIdentifier(name)
        applyButton.target = self
        applyButton.action = #selector(onApplyOutputTemplateCD(_:))
        applyButton.setContentHuggingPriority(.required, for: .horizontal)

        // Кнопка удаления.
        let removeButton = NSButton(frame: .zero)
        removeButton.bezelStyle = .inline
        removeButton.isBordered = false
        if let img = NSImage(systemSymbolName: "xmark.circle", accessibilityDescription: "Удалить") {
            removeButton.image = img
            removeButton.imageScaling = .scaleProportionallyDown
            removeButton.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 12, weight: .regular)
        }
        removeButton.contentTintColor = KrabEarTheme.Colors.textDisabled
        removeButton.toolTip = "Удалить «\(name)»"
        removeButton.setAccessibilityLabel("Удалить шаблон «\(name)»")
        removeButton.identifier = NSUserInterfaceItemIdentifier(name)
        removeButton.target = self
        removeButton.action = #selector(onRemoveOutputTemplateCD(_:))
        removeButton.setContentHuggingPriority(.required, for: .horizontal)

        let controlStack = NSStackView(views: [applyButton, removeButton])
        controlStack.orientation = .horizontal
        controlStack.spacing = KrabEarTheme.Metrics.tight
        controlStack.alignment = .centerY

        // Превью ограничено 30 символами.
        let preview = String(text.prefix(30)) + (text.count > 30 ? "…" : "")
        let displayLabel = "\(name)  \(preview)"
        return cdMakeRow(label: displayLabel, control: controlStack)
    }

    // MARK: - Обработчики кнопок (Gemini)

    /// Обработчик Enter-в-поле-текста (Gemini variant).
    @objc func onAddOutputTemplateFromField(_ sender: NSTextField) {
        addTemplateFromFields(isClaudeDesign: false)
    }

    /// Обработчик кнопки «Добавить» (Gemini variant).
    @objc func onAddOutputTemplate(_ sender: Any) {
        addTemplateFromFields(isClaudeDesign: false)
    }

    /// Обработчик кнопки удаления (Gemini variant).
    @objc func onRemoveOutputTemplate(_ sender: NSButton) {
        guard let name = sender.identifier?.rawValue, !name.isEmpty else { return }
        removeTemplate(name, isClaudeDesign: false)
    }

    /// Обработчик кнопки «Применить → буфер» (Gemini variant).
    @objc func onApplyOutputTemplate(_ sender: NSButton) {
        guard let name = sender.identifier?.rawValue, !name.isEmpty else { return }
        applyTemplateToPasteboard(name, isClaudeDesign: false)
    }

    // MARK: - Обработчики кнопок (Claude Design)

    @objc func onAddOutputTemplateFromFieldCD(_ sender: NSTextField) {
        addTemplateFromFields(isClaudeDesign: true)
    }

    @objc func onAddOutputTemplateCD(_ sender: Any) {
        addTemplateFromFields(isClaudeDesign: true)
    }

    @objc func onRemoveOutputTemplateCD(_ sender: NSButton) {
        guard let name = sender.identifier?.rawValue, !name.isEmpty else { return }
        removeTemplate(name, isClaudeDesign: true)
    }

    @objc func onApplyOutputTemplateCD(_ sender: NSButton) {
        guard let name = sender.identifier?.rawValue, !name.isEmpty else { return }
        applyTemplateToPasteboard(name, isClaudeDesign: true)
    }

    // MARK: - Общая логика (off-main IPC)

    /// Добавляет шаблон через IPC строго off-main (AGENT-3).
    /// После успеха: очищает поля ввода, перестраивает карточку.
    private func addTemplateFromFields(isClaudeDesign: Bool) {
        let name: String
        let text: String

        if isClaudeDesign {
            guard let nf = objc_getAssociatedObject(
                self, &OutputTemplatesAssocKeys.cdNameField
            ) as? NSTextField,
                  let tf = objc_getAssociatedObject(
                      self, &OutputTemplatesAssocKeys.cdTextField
                  ) as? NSTextField else { return }
            name = nf.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            text = tf.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        } else {
            guard let nf = objc_getAssociatedObject(
                self, &OutputTemplatesAssocKeys.nameField
            ) as? NSTextField,
                  let tf = objc_getAssociatedObject(
                      self, &OutputTemplatesAssocKeys.textField
                  ) as? NSTextField else { return }
            name = nf.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            text = tf.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        }

        guard !name.isEmpty, !text.isEmpty else { return }

        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            do {
                _ = try ipc.call(method: "add_template", params: ["name": name, "text": text])
            } catch {
                return
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }

                // Очищаем поля ввода.
                if isClaudeDesign {
                    if let nf = objc_getAssociatedObject(
                        self, &OutputTemplatesAssocKeys.cdNameField
                    ) as? NSTextField { nf.stringValue = "" }
                    if let tf = objc_getAssociatedObject(
                        self, &OutputTemplatesAssocKeys.cdTextField
                    ) as? NSTextField { tf.stringValue = "" }
                } else {
                    if let nf = objc_getAssociatedObject(
                        self, &OutputTemplatesAssocKeys.nameField
                    ) as? NSTextField { nf.stringValue = "" }
                    if let tf = objc_getAssociatedObject(
                        self, &OutputTemplatesAssocKeys.textField
                    ) as? NSTextField { tf.stringValue = "" }
                }
            }

            // Перезагружаем обе карточки — бэкенд source of truth.
            self.fetchAndRebuildOutputTemplatesCard(isClaudeDesign: false)
            self.fetchAndRebuildOutputTemplatesCard(isClaudeDesign: true)
        }
    }

    /// Удаляет шаблон через IPC строго off-main (AGENT-3).
    private func removeTemplate(_ name: String, isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try ipc.call(method: "remove_template", params: ["name": name])
            } catch {
                return
            }
            // Перезагружаем обе карточки.
            self.fetchAndRebuildOutputTemplatesCard(isClaudeDesign: false)
            self.fetchAndRebuildOutputTemplatesCard(isClaudeDesign: true)
        }
    }

    /// Применяет шаблон через IPC и копирует результат в NSPasteboard (AGENT-3: off-main IPC).
    private func applyTemplateToPasteboard(_ name: String, isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async {
            let rendered: String
            do {
                let resp = try ipc.call(method: "apply_template", params: ["name": name])
                let result = resp["result"] as? [String: Any]
                rendered = result?["text"] as? String ?? ""
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Ошибка применения шаблона «\(name)»", duration: 3.0)
                }
                return
            }

            guard !rendered.isEmpty else {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Шаблон «\(name)» вернул пустой результат", duration: 3.0)
                }
                return
            }

            DispatchQueue.main.async {
                let pb = NSPasteboard.general
                pb.clearContents()
                pb.setString(rendered, forType: .string)
                BackendToast.shared.show("Скопировано", duration: 2.0)
            }
        }
    }

    // MARK: - Вспомогательный separator

    /// NSBox separator — только для этого extension.
    @MainActor
    private func outputTemplatesMakeSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }
}
