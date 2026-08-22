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

// MARK: - Call Observer settings (w1 T9, доп.скоуп шаг 5)
//
// В отличие от секции выше (локальные UserDefaults), call_observer_hud_enabled/
// call_observer_autoplay_audio — настройки бэкенд-правды (читает
// IPCCallObserverSettings.refresh в main+CallObserver.swift). Чекбоксы читают
// текущее значение через get_settings при построении секции (off-main,
// AGENT-3), пишут через applySettingsPatch — тот же set_settings-хелпер,
// которым уже пользуется каждый чекбокс Settings-таба (см.
// HistoryPanelController+VoiceCommands.swift onVoiceCommandsEnabledChanged).
// После записи — coordinator.settingsDidChange() (generic-нотификации в
// агенте нет, spec §5; координатор и так перечитывает на каждом полл-тике,
// прямой вызов — best-effort свежесть, не источник правды).

private enum CallObserverSettingsAssocKeys {
    nonisolated(unsafe) static var hudToggle: UInt8 = 0
    nonisolated(unsafe) static var autoplayToggle: UInt8 = 0
}

extension HistoryPanelController {

    /// Секция «Наблюдатель звонков агента» (Gemini-вариант, settingsBar —
    /// см. вызов в HistoryPanelController.swift).
    @MainActor
    func buildCallObserverSettingsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "call_observer_settings",
            title: "Наблюдатель звонков агента",
            isExpanded: false
        )
        let card = ThemeCardView()

        let hudToggle = NSButton(checkboxWithTitle: "", target: self,
                                 action: #selector(onCallObserverHudEnabledChanged(_:)))
        hudToggle.state = .on  // прод-дефолт, см. IPCCallObserverSettings.refresh
        objc_setAssociatedObject(self, &CallObserverSettingsAssocKeys.hudToggle, hudToggle,
                                 .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let hudRow = makeSettingRow(
            label: "Панель звонка агента при звонке",
            description: "Плавающая плашка с живым транскриптом поверх остальных окон.",
            control: hudToggle
        )

        let autoplayToggle = NSButton(checkboxWithTitle: "", target: self,
                                      action: #selector(onCallObserverAutoplayChanged(_:)))
        autoplayToggle.state = .off
        objc_setAssociatedObject(self, &CallObserverSettingsAssocKeys.autoplayToggle, autoplayToggle,
                                 .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let autoplayRow = makeSettingRow(
            label: "Сразу включать звук звонка",
            description: "Автопрослушка звонка агента, как только он начался.",
            control: autoplayToggle
        )

        card.contentStackView.addArrangedSubview(hudRow)
        card.contentStackView.addArrangedSubview(autoplayRow)
        section.contentStackView.addArrangedSubview(card)

        refreshCallObserverSettingsToggles()
        return section
    }

    /// Гидратирует чекбоксы реальным состоянием бэкенда (off-main, AGENT-3;
    /// паттерн fetchAndRebuildVoiceCommandsList).
    func refreshCallObserverSettingsToggles() {
        let ipc = ipcClient
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let resp = try? ipc.call(method: "get_settings", params: [:]),
                  let result = resp["result"] as? [String: Any] else { return }
            let hudEnabled = result["call_observer_hud_enabled"] as? Bool ?? true
            let autoplay = result["call_observer_autoplay_audio"] as? Bool ?? false
            DispatchQueue.main.async {
                guard let self else { return }
                if let t = objc_getAssociatedObject(
                    self, &CallObserverSettingsAssocKeys.hudToggle) as? NSButton {
                    t.state = hudEnabled ? .on : .off
                }
                if let t = objc_getAssociatedObject(
                    self, &CallObserverSettingsAssocKeys.autoplayToggle) as? NSButton {
                    t.state = autoplay ? .on : .off
                }
            }
        }
    }

    @objc func onCallObserverHudEnabledChanged(_ sender: NSButton) {
        applySettingsPatch(["call_observer_hud_enabled": sender.state == .on])
        (NSApp.delegate as? AgentAppDelegate)?.callObserverCoordinator?.settingsDidChange()
    }

    @objc func onCallObserverAutoplayChanged(_ sender: NSButton) {
        applySettingsPatch(["call_observer_autoplay_audio": sender.state == .on])
        (NSApp.delegate as? AgentAppDelegate)?.callObserverCoordinator?.settingsDidChange()
    }
}
