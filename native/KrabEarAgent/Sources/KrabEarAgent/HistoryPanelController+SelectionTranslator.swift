/*
 HistoryPanelController+SelectionTranslator.swift
 Секция «Авто-перевод» в панели настроек.

 Связи модуля:
 1) SelectionTranslator: читает/применяет SelectionTranslatorConfig.
 2) HistoryPanelController: использует makeSettingRow / makeSwitchRow / makeSeparator.
 3) AgentAppDelegate: доступ к selectionTranslator через NSApp.delegate.
*/

import AppKit
import Foundation

// MARK: - HistoryPanelController controls для секции

extension HistoryPanelController {

    // MARK: - UI controls (lazy init via buildSelectionTranslatorSection)

    // MARK: - Associated object keys (using nonisolated(unsafe) static to avoid String-pointer warnings)

    private nonisolated(unsafe) static var toggleKey: UInt8 = 0
    private nonisolated(unsafe) static var hotkeyKey: UInt8 = 0
    private nonisolated(unsafe) static var targetLangKey: UInt8 = 0

    /// Toggle «Включить selection translate»
    var selectionTranslateToggle: NSButton {
        if let btn = objc_getAssociatedObject(self, &Self.toggleKey) as? NSButton { return btn }
        let btn = NSButton(checkboxWithTitle: "", target: nil, action: nil)
        btn.setButtonType(.switch)
        btn.setAccessibilityLabel("Включить авто-перевод выделенного текста (Cmd+Shift+T)")
        objc_setAssociatedObject(self, &Self.toggleKey, btn, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return btn
    }

    /// Hotkey picker
    var selectionHotkeySelector: NSPopUpButton {
        if let sel = objc_getAssociatedObject(self, &Self.hotkeyKey) as? NSPopUpButton { return sel }
        let sel = NSPopUpButton(frame: .zero, pullsDown: false)
        sel.setAccessibilityLabel("Горячая клавиша для авто-перевода выделенного текста")
        objc_setAssociatedObject(self, &Self.hotkeyKey, sel, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return sel
    }

    /// Target lang picker
    var selectionTargetLangSelector: NSPopUpButton {
        if let sel = objc_getAssociatedObject(self, &Self.targetLangKey) as? NSPopUpButton { return sel }
        let sel = NSPopUpButton(frame: .zero, pullsDown: false)
        sel.setAccessibilityLabel("Целевой язык авто-перевода выделенного текста")
        objc_setAssociatedObject(self, &Self.targetLangKey, sel, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return sel
    }

    // MARK: - Section builder

    /// Секция «Авто-перевод» в Settings tab (settings bar).
    func buildSelectionTranslatorSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "settings_selection_translate",
            title: "Авто-перевод",
            isExpanded: false
        )

        let card = ThemeCardView()

        // 1. Enable toggle
        let toggle = selectionTranslateToggle
        toggle.target = self
        toggle.action = #selector(onSelectionTranslateToggleChanged)
        let toggleRow = makeSwitchRow(
            label: "Включить selection translate",
            description: "Переводит выделенный текст на месте. Сначала через Accessibility API, затем через буфер обмена.",
            button: toggle
        )

        // 2. Hotkey picker
        let hotkeyPicker = selectionHotkeySelector
        hotkeyPicker.removeAllItems()
        hotkeyPicker.addItems(withTitles: ["Cmd+Shift+T", "Cmd+Option+T"])
        hotkeyPicker.target = self
        hotkeyPicker.action = #selector(onSelectionHotkeyChanged)
        let hotkeyRow = makeSettingRow(
            label: "Горячая клавиша",
            control: hotkeyPicker
        )

        // 3. Target lang picker
        let langPicker = selectionTargetLangSelector
        langPicker.removeAllItems()
        langPicker.addItems(withTitles: ["Auto", "ES", "RU", "EN"])
        langPicker.target = self
        langPicker.action = #selector(onSelectionTargetLangChanged)
        let langRow = makeSettingRow(
            label: "Целевой язык",
            description: "Auto определяет направление автоматически: кириллица → ES, латиница → RU.",
            control: langPicker
        )

        card.contentStackView.addArrangedSubview(toggleRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(hotkeyRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(langRow)

        section.contentStackView.addArrangedSubview(card)

        // Sync initial state
        syncSelectionTranslatorControls()

        return section
    }

    // MARK: - Sync controls

    func syncSelectionTranslatorControls() {
        let config = SelectionTranslatorConfig.load()
        selectionTranslateToggle.state = config.enabled ? .on : .off
        switch config.hotkey {
        case "cmd_opt_t":
            selectionHotkeySelector.selectItem(at: 1)
        default:
            selectionHotkeySelector.selectItem(at: 0)
        }
        switch config.targetLang {
        case "es":
            selectionTargetLangSelector.selectItem(at: 1)
        case "ru":
            selectionTargetLangSelector.selectItem(at: 2)
        case "en":
            selectionTargetLangSelector.selectItem(at: 3)
        default:
            selectionTargetLangSelector.selectItem(at: 0)
        }
    }

    // MARK: - Handlers

    @objc func onSelectionTranslateToggleChanged() {
        var config = SelectionTranslatorConfig.load()
        config.enabled = selectionTranslateToggle.state == .on
        config.save()
        applySelectionTranslatorConfig(config)
    }

    @objc func onSelectionHotkeyChanged() {
        var config = SelectionTranslatorConfig.load()
        config.hotkey = selectionHotkeySelector.indexOfSelectedItem == 1 ? "cmd_opt_t" : "cmd_shift_t"
        config.save()
        applySelectionTranslatorConfig(config)
    }

    @objc func onSelectionTargetLangChanged() {
        var config = SelectionTranslatorConfig.load()
        switch selectionTargetLangSelector.indexOfSelectedItem {
        case 1: config.targetLang = "es"
        case 2: config.targetLang = "ru"
        case 3: config.targetLang = "en"
        default: config.targetLang = "auto"
        }
        config.save()
        applySelectionTranslatorConfig(config)
    }

    // MARK: - Apply

    private func applySelectionTranslatorConfig(_ config: SelectionTranslatorConfig) {
        guard let appDelegate = NSApp.delegate as? AgentAppDelegate else { return }
        appDelegate.selectionTranslator?.config = config
    }

    // MARK: - CD Builders

    @MainActor
    func cdBuildSelectionTranslatorSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_settings_selection_translate",
            title: "Авто-перевод",
            isExpanded: false
        )
        let card = CDSettingsCardView()

        selectionTranslateToggle.title = ""
        selectionTranslateToggle.setButtonType(.switch)
        let toggleRow = cdMakeRow(label: "Включить", control: selectionTranslateToggle)

        let hotkeyRow = cdMakeRow(label: "Горячая клавиша", control: selectionHotkeySelector)
        let langRow = cdMakeRow(label: "Целевой язык", control: selectionTargetLangSelector)

        card.contentStackView.addArrangedSubview(toggleRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(hotkeyRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(langRow)

        section.contentStackView.addArrangedSubview(card)

        // Sync initial state
        syncSelectionTranslatorControls()

        return section
    }

}
