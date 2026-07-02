/*
 Пресеты конфигурации — секция настроек для ConfigPresetsLibrary (backend).

 IPC-контракты (сверено буква-в-букву с KrabEar/backend/config_presets_library.py):
   - list_config_presets {}
       → result.presets ([{name, description, builtin, settings_patch}])
   - apply_config_preset {name: String}
       → result {name, settings_patch, applied, saved} | throws IPCError.backendError
         (KeyError "Пресет не найден" на бэкенде становится error.message)
   - create_config_preset {name: String, description: String, settings_patch: [String: Any]}
       → result.preset {name, description, builtin: false, settings_patch}
         (settings_patch ОБЯЗАН быть непустым dict — иначе ValueError на бэкенде)
   - delete_config_preset {name: String}
       → result {name, deleted: Bool}  (builtin пресеты → deleted: false, не ошибка)
   - export_config_preset {name: String}
       → result {name, json: String}   (поле называется "json", не "preset_json")
   - import_config_preset {json: String}
       → result.preset {name, description, builtin: false, settings_patch}

 Архитектура (зеркало HistoryPanelController+STTVocabulary.swift):
   - buildConfigPresetsSection()   — Gemini-вариант (settingsBar, ThemeCardView).
   - cdBuildConfigPresetsSection() — Claude Design (settingsBarCD, CDSettingsCardView).
   - fetchAndRebuildConfigPresetsCard(isClaudeDesign:) — грузит list_config_presets
     off-main, перестраивает карточку.
   - onApplyConfigPreset / onDeleteConfigPreset / onExportConfigPreset — построчные
     обработчики (идентификатор кнопки несёт имя пресета).
   - onCreateConfigPresetFromCurrent(CD:) — читает inline name/description поля +
     подмножество текущих AgentSettings (quality_profile, cleanup_profile,
     translation_mode, diarization_enabled) как settings_patch.
   - onImportConfigPreset(CD:) — NSOpenPanel → читает файл → import_config_preset.

 Правила AGENT-3 (AppHang-класс): IPC строго в DispatchQueue.global,
 мутации UI — строго в DispatchQueue.main.
 НЕ runModal() — только presentPanelSheet/presentAlertSheet из AlertHelpers.swift.
 Глифы: только ASCII + установленные SF Symbols (slider.horizontal.3, star.fill,
 plus.circle, xmark.circle, square.and.arrow.up/down, checkmark.circle.fill).
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum ConfigPresetsAssocKeys {
    nonisolated(unsafe) static var card: UInt8 = 0
    nonisolated(unsafe) static var cdCard: UInt8 = 0
    nonisolated(unsafe) static var nameField: UInt8 = 0
    nonisolated(unsafe) static var cdNameField: UInt8 = 0
    nonisolated(unsafe) static var descriptionField: UInt8 = 0
    nonisolated(unsafe) static var cdDescriptionField: UInt8 = 0
}

// MARK: - Модель пресета (internal, single-source)

struct ConfigPresetData {
    let name: String
    let description: String
    let builtin: Bool
    let settingsPatch: [String: Any]
}

// MARK: - HistoryPanelController+ConfigPresets

extension HistoryPanelController {

    // MARK: - Gemini variant: секция для settingsBar

    /// Строит секцию «Пресеты конфигурации» (Gemini-дизайн, settingsBar).
    /// Внутренняя карточка наполняется асинхронно при первом показе (off-main).
    @MainActor
    func buildConfigPresetsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_config_presets",
            title: "Пресеты конфигурации",
            isExpanded: false,
            iconSymbol: "slider.horizontal.3"
        )

        let card = ThemeCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(
            self,
            &ConfigPresetsAssocKeys.card,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        section.contentStackView.addArrangedSubview(card)

        // Загрузка данных (off-main, AGENT-3).
        fetchAndRebuildConfigPresetsCard(isClaudeDesign: false)

        return section
    }

    // MARK: - Claude Design variant: секция для settingsBarCD

    /// Строит секцию «Пресеты конфигурации» (Claude Design, settingsBarCD).
    @MainActor
    func cdBuildConfigPresetsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_config_presets",
            title: "Пресеты конфигурации",
            isExpanded: false
        )

        let card = CDSettingsCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(
            self,
            &ConfigPresetsAssocKeys.cdCard,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        section.contentStackView.addArrangedSubview(card)
        fetchAndRebuildConfigPresetsCard(isClaudeDesign: true)

        return section
    }

    // MARK: - Загрузка данных с бэкенда

    /// Запрашивает list_config_presets строго off-main (AGENT-3), обновляет
    /// карточку на main.
    func fetchAndRebuildConfigPresetsCard(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            var presets: [ConfigPresetData] = []
            do {
                let resp = try ipc.call(method: "list_config_presets", params: [:])
                let result = resp["result"] as? [String: Any] ?? [:]
                let rawPresets = result["presets"] as? [[String: Any]] ?? []
                presets = rawPresets.compactMap { dict in
                    guard let name = dict["name"] as? String else { return nil }
                    return ConfigPresetData(
                        name: name,
                        description: (dict["description"] as? String) ?? "",
                        builtin: (dict["builtin"] as? Bool) ?? false,
                        settingsPatch: (dict["settings_patch"] as? [String: Any]) ?? [:]
                    )
                }
            } catch {
                presets = []
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if isClaudeDesign {
                    self.rebuildCDConfigPresetsCard(presets: presets)
                } else {
                    self.rebuildGeminiConfigPresetsCard(presets: presets)
                }
            }
        }
    }

    // MARK: - Перестройка карточки (Gemini)

    @MainActor
    private func rebuildGeminiConfigPresetsCard(presets: [ConfigPresetData]) {
        guard let card = objc_getAssociatedObject(
            self, &ConfigPresetsAssocKeys.card
        ) as? ThemeCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        // Список пресетов.
        let subhead = makeSubhead("ДОСТУПНЫЕ ПРЕСЕТЫ")
        card.contentStackView.addArrangedSubview(subhead)

        if presets.isEmpty {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = KrabEarTheme.Typography.caption
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
        } else {
            for preset in presets {
                card.contentStackView.addArrangedSubview(configPresetsSeparator())
                card.contentStackView.addArrangedSubview(makeGeminiPresetRow(preset))
            }
        }

        // Форма «Создать из текущих».
        card.contentStackView.addArrangedSubview(configPresetsSeparator())
        card.contentStackView.addArrangedSubview(makeSubhead("СОЗДАТЬ ИЗ ТЕКУЩИХ НАСТРОЕК"))
        card.contentStackView.addArrangedSubview(makeGeminiCreateForm())

        // Кнопка импорта.
        card.contentStackView.addArrangedSubview(configPresetsSeparator())
        let importButton = ThemeSecondaryButton(
            title: "Импортировать пресет…",
            target: self,
            action: #selector(onImportConfigPreset(_:))
        )
        importButton.setAccessibilityLabel("Импортировать пресет конфигурации из файла")
        let importRow = NSStackView(views: [importButton])
        importRow.orientation = .horizontal
        importRow.alignment = .centerY
        card.contentStackView.addArrangedSubview(importRow)
    }

    // MARK: - Перестройка карточки (Claude Design)

    @MainActor
    private func rebuildCDConfigPresetsCard(presets: [ConfigPresetData]) {
        guard let card = objc_getAssociatedObject(
            self, &ConfigPresetsAssocKeys.cdCard
        ) as? CDSettingsCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        let subhead = NSTextField(labelWithString: "ДОСТУПНЫЕ ПРЕСЕТЫ")
        subhead.font = .systemFont(ofSize: 10, weight: .semibold)
        subhead.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(subhead)

        if presets.isEmpty {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = .systemFont(ofSize: 12, weight: .regular)
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
        } else {
            for preset in presets {
                card.contentStackView.addArrangedSubview(cdMakeSeparator())
                card.contentStackView.addArrangedSubview(makeCDPresetRow(preset))
            }
        }

        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        let createLabel = NSTextField(labelWithString: "СОЗДАТЬ ИЗ ТЕКУЩИХ НАСТРОЕК")
        createLabel.font = .systemFont(ofSize: 10, weight: .semibold)
        createLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(createLabel)
        card.contentStackView.addArrangedSubview(makeCDCreateForm())

        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        let importButton = NSButton(
            title: "Импортировать пресет…",
            target: self,
            action: #selector(onImportConfigPresetCD(_:))
        )
        importButton.bezelStyle = .rounded
        importButton.setAccessibilityLabel("Импортировать пресет конфигурации из файла")
        let importRow = NSStackView(views: [importButton])
        importRow.orientation = .horizontal
        importRow.alignment = .centerY
        card.contentStackView.addArrangedSubview(importRow)
    }

    // MARK: - Строка пресета (Gemini)

    @MainActor
    private func makeGeminiPresetRow(_ preset: ConfigPresetData) -> NSView {
        let applyButton = NSButton(frame: .zero)
        applyButton.bezelStyle = .inline
        applyButton.isBordered = false
        if let img = NSImage(systemSymbolName: "checkmark.circle.fill", accessibilityDescription: "Применить") {
            applyButton.image = img
            applyButton.imageScaling = .scaleProportionallyDown
            applyButton.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
        }
        applyButton.contentTintColor = KrabEarTheme.Colors.success
        applyButton.toolTip = "Применить пресет «\(preset.name)»"
        applyButton.setAccessibilityLabel("Применить пресет «\(preset.name)»")
        applyButton.identifier = NSUserInterfaceItemIdentifier(preset.name)
        applyButton.target = self
        applyButton.action = #selector(onApplyConfigPreset(_:))
        applyButton.setContentHuggingPriority(.required, for: .horizontal)

        let exportButton = NSButton(frame: .zero)
        exportButton.bezelStyle = .inline
        exportButton.isBordered = false
        if let img = NSImage(systemSymbolName: "square.and.arrow.up", accessibilityDescription: "Экспортировать") {
            exportButton.image = img
            exportButton.imageScaling = .scaleProportionallyDown
            exportButton.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
        }
        exportButton.contentTintColor = KrabEarTheme.Colors.textSecondary
        exportButton.toolTip = "Экспортировать пресет «\(preset.name)» в файл"
        exportButton.setAccessibilityLabel("Экспортировать пресет «\(preset.name)»")
        exportButton.identifier = NSUserInterfaceItemIdentifier(preset.name)
        exportButton.target = self
        exportButton.action = #selector(onExportConfigPreset(_:))
        exportButton.setContentHuggingPriority(.required, for: .horizontal)

        var trailingViews: [NSView] = [applyButton, exportButton]

        if !preset.builtin {
            let deleteButton = NSButton(frame: .zero)
            deleteButton.bezelStyle = .inline
            deleteButton.isBordered = false
            if let img = NSImage(systemSymbolName: "xmark.circle", accessibilityDescription: "Удалить") {
                deleteButton.image = img
                deleteButton.imageScaling = .scaleProportionallyDown
                deleteButton.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
            }
            deleteButton.contentTintColor = KrabEarTheme.Colors.error
            deleteButton.toolTip = "Удалить пресет «\(preset.name)»"
            deleteButton.setAccessibilityLabel("Удалить пресет «\(preset.name)»")
            deleteButton.identifier = NSUserInterfaceItemIdentifier(preset.name)
            deleteButton.target = self
            deleteButton.action = #selector(onDeleteConfigPreset(_:))
            deleteButton.setContentHuggingPriority(.required, for: .horizontal)
            trailingViews.append(deleteButton)
        }

        let buttonsStack = NSStackView(views: trailingViews)
        buttonsStack.orientation = .horizontal
        buttonsStack.spacing = KrabEarTheme.Metrics.tight
        buttonsStack.alignment = .centerY

        let badge: NSView? = preset.builtin
            ? makeBadge(text: "builtin", color: KrabEarTheme.Colors.accent, tooltip: "Встроенный пресет — нельзя удалить", symbol: "star.fill")
            : nil

        return makeSettingRow(
            label: preset.name,
            description: preset.description.isEmpty ? nil : preset.description,
            control: buttonsStack,
            badge: badge
        )
    }

    // MARK: - Строка пресета (Claude Design)

    @MainActor
    private func makeCDPresetRow(_ preset: ConfigPresetData) -> NSView {
        let applyButton = NSButton(frame: .zero)
        applyButton.bezelStyle = .inline
        applyButton.isBordered = false
        if let img = NSImage(systemSymbolName: "checkmark.circle.fill", accessibilityDescription: "Применить") {
            applyButton.image = img
            applyButton.imageScaling = .scaleProportionallyDown
            applyButton.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 12, weight: .regular)
        }
        applyButton.contentTintColor = KrabEarTheme.Colors.success
        applyButton.toolTip = "Применить пресет «\(preset.name)»"
        applyButton.setAccessibilityLabel("Применить пресет «\(preset.name)»")
        applyButton.identifier = NSUserInterfaceItemIdentifier(preset.name)
        applyButton.target = self
        applyButton.action = #selector(onApplyConfigPresetCD(_:))
        applyButton.setContentHuggingPriority(.required, for: .horizontal)

        let exportButton = NSButton(frame: .zero)
        exportButton.bezelStyle = .inline
        exportButton.isBordered = false
        if let img = NSImage(systemSymbolName: "square.and.arrow.up", accessibilityDescription: "Экспортировать") {
            exportButton.image = img
            exportButton.imageScaling = .scaleProportionallyDown
            exportButton.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 12, weight: .regular)
        }
        exportButton.contentTintColor = KrabEarTheme.Colors.textSecondary
        exportButton.toolTip = "Экспортировать пресет «\(preset.name)»"
        exportButton.setAccessibilityLabel("Экспортировать пресет «\(preset.name)»")
        exportButton.identifier = NSUserInterfaceItemIdentifier(preset.name)
        exportButton.target = self
        exportButton.action = #selector(onExportConfigPresetCD(_:))
        exportButton.setContentHuggingPriority(.required, for: .horizontal)

        var trailingViews: [NSView] = [applyButton, exportButton]

        if !preset.builtin {
            let deleteButton = NSButton(frame: .zero)
            deleteButton.bezelStyle = .inline
            deleteButton.isBordered = false
            if let img = NSImage(systemSymbolName: "xmark.circle", accessibilityDescription: "Удалить") {
                deleteButton.image = img
                deleteButton.imageScaling = .scaleProportionallyDown
                deleteButton.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 12, weight: .regular)
            }
            deleteButton.contentTintColor = KrabEarTheme.Colors.error
            deleteButton.toolTip = "Удалить пресет «\(preset.name)»"
            deleteButton.setAccessibilityLabel("Удалить пресет «\(preset.name)»")
            deleteButton.identifier = NSUserInterfaceItemIdentifier(preset.name)
            deleteButton.target = self
            deleteButton.action = #selector(onDeleteConfigPresetCD(_:))
            deleteButton.setContentHuggingPriority(.required, for: .horizontal)
            trailingViews.append(deleteButton)
        }

        let buttonsStack = NSStackView(views: trailingViews)
        buttonsStack.orientation = .horizontal
        buttonsStack.spacing = KrabEarTheme.Metrics.tight
        buttonsStack.alignment = .centerY

        let badge: NSView? = preset.builtin
            ? makeBadge(text: "builtin", color: KrabEarTheme.Colors.accent, tooltip: "Встроенный пресет", symbol: "star.fill")
            : nil

        return cdMakeRow(label: preset.name, control: buttonsStack, badge: badge)
    }

    // MARK: - Форма «Создать из текущих» (Gemini)

    @MainActor
    private func makeGeminiCreateForm() -> NSView {
        let nameField = NSTextField(frame: .zero)
        nameField.placeholderString = "Имя пресета…"
        nameField.font = KrabEarTheme.Typography.body
        nameField.bezelStyle = .roundedBezel
        nameField.isBordered = true
        nameField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        objc_setAssociatedObject(
            self, &ConfigPresetsAssocKeys.nameField, nameField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let descField = NSTextField(frame: .zero)
        descField.placeholderString = "Описание (необязательно)…"
        descField.font = KrabEarTheme.Typography.body
        descField.bezelStyle = .roundedBezel
        descField.isBordered = true
        descField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        objc_setAssociatedObject(
            self, &ConfigPresetsAssocKeys.descriptionField, descField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let createButton = ThemePrimaryButton(
            title: "Создать",
            target: self,
            action: #selector(onCreateConfigPresetFromCurrent(_:))
        )
        createButton.setAccessibilityLabel("Создать пресет из текущих настроек")
        createButton.setContentHuggingPriority(.required, for: .horizontal)

        let nameRow = makeSettingRow(label: "Имя", control: nameField)
        let descRow = makeSettingRow(label: "Описание", control: descField)

        let buttonRow = NSStackView(views: [createButton])
        buttonRow.orientation = .horizontal
        buttonRow.alignment = .centerY

        let vStack = NSStackView()
        vStack.orientation = .vertical
        vStack.alignment = .leading
        vStack.spacing = KrabEarTheme.Metrics.tight
        vStack.addArrangedSubview(nameRow)
        vStack.addArrangedSubview(descRow)
        vStack.addArrangedSubview(buttonRow)
        return vStack
    }

    // MARK: - Форма «Создать из текущих» (Claude Design)

    @MainActor
    private func makeCDCreateForm() -> NSView {
        let nameField = NSTextField(frame: .zero)
        nameField.placeholderString = "Имя пресета…"
        nameField.font = .systemFont(ofSize: 12, weight: .regular)
        nameField.bezelStyle = .roundedBezel
        nameField.isBordered = true
        nameField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        objc_setAssociatedObject(
            self, &ConfigPresetsAssocKeys.cdNameField, nameField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let descField = NSTextField(frame: .zero)
        descField.placeholderString = "Описание (необязательно)…"
        descField.font = .systemFont(ofSize: 12, weight: .regular)
        descField.bezelStyle = .roundedBezel
        descField.isBordered = true
        descField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        objc_setAssociatedObject(
            self, &ConfigPresetsAssocKeys.cdDescriptionField, descField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let createButton = NSButton(
            title: "Создать",
            target: self,
            action: #selector(onCreateConfigPresetFromCurrentCD(_:))
        )
        createButton.bezelStyle = .rounded
        createButton.setAccessibilityLabel("Создать пресет из текущих настроек")
        createButton.setContentHuggingPriority(.required, for: .horizontal)

        let nameRow = cdMakeRow(label: "Имя", control: nameField)
        let descRow = cdMakeRow(label: "Описание", control: descField)

        let buttonRow = NSStackView(views: [createButton])
        buttonRow.orientation = .horizontal
        buttonRow.alignment = .centerY

        let vStack = NSStackView()
        vStack.orientation = .vertical
        vStack.alignment = .leading
        vStack.spacing = 0
        vStack.addArrangedSubview(nameRow)
        vStack.addArrangedSubview(descRow)
        vStack.addArrangedSubview(buttonRow)
        return vStack
    }

    // MARK: - Обработчики: Применить (Gemini + CD)

    @objc func onApplyConfigPreset(_ sender: NSButton) {
        applyConfigPreset(named: sender.identifier?.rawValue)
    }

    @objc func onApplyConfigPresetCD(_ sender: NSButton) {
        applyConfigPreset(named: sender.identifier?.rawValue)
    }

    private func applyConfigPreset(named name: String?) {
        guard let name, !name.isEmpty else { return }
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try ipc.call(method: "apply_config_preset", params: ["name": name])
                DispatchQueue.main.async {
                    BackendToast.shared.show("Пресет применён", duration: 2.0)
                }
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show(
                        "Не удалось применить пресет: \(error.localizedDescription)",
                        duration: 4.0
                    )
                }
            }
            self.fetchAndRebuildConfigPresetsCard(isClaudeDesign: false)
            self.fetchAndRebuildConfigPresetsCard(isClaudeDesign: true)
        }
    }

    // MARK: - Обработчики: Удалить (Gemini + CD)

    @objc func onDeleteConfigPreset(_ sender: NSButton) {
        deleteConfigPreset(named: sender.identifier?.rawValue)
    }

    @objc func onDeleteConfigPresetCD(_ sender: NSButton) {
        deleteConfigPreset(named: sender.identifier?.rawValue)
    }

    private func deleteConfigPreset(named name: String?) {
        guard let name, !name.isEmpty else { return }
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var deleted = false
            do {
                let resp = try ipc.call(method: "delete_config_preset", params: ["name": name])
                let result = resp["result"] as? [String: Any] ?? [:]
                deleted = result["deleted"] as? Bool ?? false
            } catch {
                deleted = false
            }
            DispatchQueue.main.async {
                BackendToast.shared.show(
                    deleted ? "Пресет удалён" : "Не удалось удалить пресет",
                    duration: 2.5
                )
            }
            self.fetchAndRebuildConfigPresetsCard(isClaudeDesign: false)
            self.fetchAndRebuildConfigPresetsCard(isClaudeDesign: true)
        }
    }

    // MARK: - Обработчики: Экспортировать (Gemini + CD)

    @objc func onExportConfigPreset(_ sender: NSButton) {
        exportConfigPreset(named: sender.identifier?.rawValue)
    }

    @objc func onExportConfigPresetCD(_ sender: NSButton) {
        exportConfigPreset(named: sender.identifier?.rawValue)
    }

    private func exportConfigPreset(named name: String?) {
        guard let name, !name.isEmpty else { return }
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let resp = try ipc.call(method: "export_config_preset", params: ["name": name])
                let result = resp["result"] as? [String: Any] ?? [:]
                let jsonStr = result["json"] as? String ?? ""
                DispatchQueue.main.async {
                    self.presentExportSavePanel(name: name, json: jsonStr)
                }
            } catch {
                DispatchQueue.main.async {
                    self.showInfoAlert(
                        title: "Экспорт пресета",
                        body: "Не удалось экспортировать пресет «\(name)»: \(error.localizedDescription)"
                    )
                }
            }
        }
    }

    /// Показывает NSSavePanel для сохранения JSON-строки пресета. Вызывается на main thread.
    @MainActor
    private func presentExportSavePanel(name: String, json: String) {
        guard !json.isEmpty else {
            showInfoAlert(title: "Экспорт пресета", body: "Бэкенд вернул пустой JSON.")
            return
        }

        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = "krab_ear_preset_\(name).json"
        panel.allowedContentTypes = [.json]
        panel.title = "Сохранить пресет «\(name)»"
        panel.prompt = "Сохранить"

        presentPanelSheet(panel, for: self.window) { [weak self] response in
            guard response == .OK, let url = panel.url else { return }
            do {
                try json.write(to: url, atomically: true, encoding: .utf8)
                self?.showInfoAlert(
                    title: "Экспорт пресета",
                    body: "Пресет «\(name)» сохранён:\n\(url.path)"
                )
            } catch {
                self?.showInfoAlert(
                    title: "Экспорт пресета",
                    body: "Не удалось записать файл: \(error.localizedDescription)"
                )
            }
        }
    }

    // MARK: - Обработчик: Создать из текущих настроек (Gemini)

    @objc func onCreateConfigPresetFromCurrent(_ sender: Any) {
        guard let nameField = objc_getAssociatedObject(
            self, &ConfigPresetsAssocKeys.nameField
        ) as? NSTextField else { return }
        let descField = objc_getAssociatedObject(
            self, &ConfigPresetsAssocKeys.descriptionField
        ) as? NSTextField
        createConfigPresetFromCurrent(nameField: nameField, descField: descField, isClaudeDesign: false)
    }

    // MARK: - Обработчик: Создать из текущих настроек (Claude Design)

    @objc func onCreateConfigPresetFromCurrentCD(_ sender: Any) {
        guard let nameField = objc_getAssociatedObject(
            self, &ConfigPresetsAssocKeys.cdNameField
        ) as? NSTextField else { return }
        let descField = objc_getAssociatedObject(
            self, &ConfigPresetsAssocKeys.cdDescriptionField
        ) as? NSTextField
        createConfigPresetFromCurrent(nameField: nameField, descField: descField, isClaudeDesign: true)
    }

    /// Общая логика создания пресета из подмножества текущих AgentSettings.
    /// Подмножество: quality_profile, cleanup_profile, translation_mode,
    /// diarization_enabled — часто настраиваемые поля, покрывающие все встроенные
    /// пресеты (interview/meeting/voice_memo/language_practice/podcast).
    private func createConfigPresetFromCurrent(
        nameField: NSTextField,
        descField: NSTextField?,
        isClaudeDesign: Bool
    ) {
        let name = nameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else {
            showInfoAlert(title: "Создать пресет", body: "Введите имя пресета.")
            return
        }
        let description = (descField?.stringValue ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        let current = settingsProvider()
        let settingsPatch: [String: Any] = [
            "quality_profile": current.qualityProfile,
            "cleanup_profile": current.cleanupProfile,
            "translation_mode": current.translationMode,
            "diarization_enabled": current.diarizationEnabled,
        ]

        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var success = false
            var errorText = ""
            do {
                _ = try ipc.call(
                    method: "create_config_preset",
                    params: [
                        "name": name,
                        "description": description,
                        "settings_patch": settingsPatch,
                    ]
                )
                success = true
            } catch {
                success = false
                errorText = error.localizedDescription
            }
            DispatchQueue.main.async {
                if success {
                    nameField.stringValue = ""
                    descField?.stringValue = ""
                    BackendToast.shared.show("Пресет «\(name)» создан", duration: 2.5)
                } else {
                    BackendToast.shared.show(
                        "Не удалось создать пресет: \(errorText)",
                        duration: 4.0
                    )
                }
            }
            self.fetchAndRebuildConfigPresetsCard(isClaudeDesign: false)
            self.fetchAndRebuildConfigPresetsCard(isClaudeDesign: true)
        }
    }

    // MARK: - Обработчики: Импортировать (Gemini + CD)

    @objc func onImportConfigPreset(_ sender: Any) {
        presentImportOpenPanel()
    }

    @objc func onImportConfigPresetCD(_ sender: Any) {
        presentImportOpenPanel()
    }

    /// Показывает NSOpenPanel для выбора JSON-файла пресета, затем вызывает
    /// import_config_preset off-main.
    @MainActor
    private func presentImportOpenPanel() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.json]
        panel.title = "Импортировать пресет конфигурации"
        panel.message = "Выберите JSON-файл, экспортированный через «Экспортировать»"
        panel.prompt = "Импортировать"

        presentPanelSheet(panel, for: self.window) { [weak self] response in
            guard let self, response == .OK, let url = panel.url else { return }
            guard let json = try? String(contentsOf: url, encoding: .utf8) else {
                self.showInfoAlert(title: "Импорт пресета", body: "Не удалось прочитать файл.")
                return
            }

            let ipc = self.ipcClient
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                guard let self else { return }
                var importedName = ""
                var errorText = ""
                var success = false
                do {
                    let resp = try ipc.call(method: "import_config_preset", params: ["json": json])
                    let result = resp["result"] as? [String: Any] ?? [:]
                    let preset = result["preset"] as? [String: Any] ?? [:]
                    importedName = (preset["name"] as? String) ?? ""
                    success = true
                } catch {
                    success = false
                    errorText = error.localizedDescription
                }
                DispatchQueue.main.async {
                    if success {
                        self.showInfoAlert(
                            title: "Импорт пресета",
                            body: "Пресет «\(importedName)» импортирован."
                        )
                    } else {
                        self.showInfoAlert(
                            title: "Импорт пресета",
                            body: "Не удалось импортировать пресет: \(errorText)"
                        )
                    }
                }
                self.fetchAndRebuildConfigPresetsCard(isClaudeDesign: false)
                self.fetchAndRebuildConfigPresetsCard(isClaudeDesign: true)
            }
        }
    }

    // MARK: - Вспомогательный separator

    /// NSBox separator (аналог приватного makeSeparator()) — только для этого extension.
    @MainActor
    private func configPresetsSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }
}
