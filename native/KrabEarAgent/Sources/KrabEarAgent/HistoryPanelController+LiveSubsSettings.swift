/*
 HistoryPanelController+LiveSubsSettings — секция "Live субтитры" в Settings panel.

 Секция добавляется в оба дизайн-варианта (Gemini и Claude Design).
 Использует CDSettingsCardView + cdMakeRow паттерн Claude Design.

 Настройки:
 - Toggle "Включить Live субтитры"           → toggles SystemAudioCapture + overlay
 - Dropdown "Язык перевода"                   → ru / es / en
 - Toggle "Оригинал + перевод" / только перевод
 - Кнопка "Сброс позиции HUD"
*/

import AppKit
import Foundation

// MARK: - Live Subs property keys (UserDefaults)
extension UserDefaults {
    static let liveSubsEnabledKey   = "KrabEar_LiveSubsEnabled"
    static let liveSubsTargetLang   = "KrabEar_LiveSubsTargetLang"
    static let liveSubsShowOriginal = "KrabEar_LiveSubsShowOriginal"
}

// MARK: - HistoryPanelController extension

extension HistoryPanelController {

    // MARK: - Build Live Subs section

    @MainActor
    func buildLiveSubsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "live_subs_section",
            title: "Live субтитры",
            isExpanded: false
        )
        let card = CDSettingsCardView()

        // 1. Enable toggle
        let enableToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onLiveSubsEnabledChanged(_:)))
        enableToggle.setButtonType(.switch)
        enableToggle.state = UserDefaults.standard.bool(forKey: UserDefaults.liveSubsEnabledKey) ? .on : .off
        enableToggle.tag = 8801
        let enableRow = cdMakeRow(label: "Включить Live субтитры (Cmd+⌥+Shift+L)", control: enableToggle)

        card.contentStackView.addArrangedSubview(enableRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())

        // 2. Target language dropdown
        let langSelector = NSPopUpButton(frame: .zero, pullsDown: false)
        langSelector.addItems(withTitles: ["ru — Русский", "es — Español", "en — English"])
        let savedLang = UserDefaults.standard.string(forKey: UserDefaults.liveSubsTargetLang) ?? "ru"
        switch savedLang {
        case "es": langSelector.selectItem(at: 1)
        case "en": langSelector.selectItem(at: 2)
        default:   langSelector.selectItem(at: 0)
        }
        langSelector.target = self
        langSelector.action = #selector(onLiveSubsLangChanged(_:))
        langSelector.tag = 8802
        let langRow = cdMakeRow(label: "Язык перевода", control: langSelector)

        card.contentStackView.addArrangedSubview(langRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())

        // 3. Show original toggle
        let origToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onLiveSubsShowOriginalChanged(_:)))
        origToggle.setButtonType(.switch)
        let showOrig = UserDefaults.standard.object(forKey: UserDefaults.liveSubsShowOriginal) != nil
            ? UserDefaults.standard.bool(forKey: UserDefaults.liveSubsShowOriginal)
            : true
        origToggle.state = showOrig ? .on : .off
        origToggle.tag = 8803
        let origRow = cdMakeRow(label: "Показывать оригинал + перевод", control: origToggle)

        card.contentStackView.addArrangedSubview(origRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())

        // 4. Reset HUD position button
        let resetBtn = ThemeSecondaryButton(title: "Сброс позиции HUD", target: self, action: #selector(onLiveSubsResetPosition))
        resetBtn.applyThemeSecondary()
        let resetRow = cdMakeRow(label: "HUD", control: resetBtn)
        card.contentStackView.addArrangedSubview(resetRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Handlers

    @objc func onLiveSubsEnabledChanged(_ sender: NSButton) {
        let enabled = sender.state == .on
        UserDefaults.standard.set(enabled, forKey: UserDefaults.liveSubsEnabledKey)

        guard let delegate = NSApp.delegate as? AgentAppDelegate else { return }
        if enabled {
            delegate.startLiveSubsCapture()
        } else {
            delegate.stopLiveSubsCapture()
        }
    }

    @objc func onLiveSubsLangChanged(_ sender: NSPopUpButton) {
        let langs = ["ru", "es", "en"]
        let idx = max(0, min(sender.indexOfSelectedItem, langs.count - 1))
        let lang = langs[idx]
        UserDefaults.standard.set(lang, forKey: UserDefaults.liveSubsTargetLang)

        guard let delegate = NSApp.delegate as? AgentAppDelegate else { return }
        delegate.systemAudioCapture.targetLang = lang
    }

    @objc func onLiveSubsShowOriginalChanged(_ sender: NSButton) {
        let show = sender.state == .on
        UserDefaults.standard.set(show, forKey: UserDefaults.liveSubsShowOriginal)

        guard let delegate = NSApp.delegate as? AgentAppDelegate else { return }
        delegate.liveSubsOverlay.showOriginalAndTranslation = show
        delegate.systemAudioCapture.showOriginalAndTranslation = show
    }

    @objc func onLiveSubsResetPosition() {
        guard let delegate = NSApp.delegate as? AgentAppDelegate else { return }
        delegate.liveSubsOverlay.resetPosition()
    }
}
