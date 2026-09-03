/*
 HistoryPanelController+LiveSubsSettings — секция "Live субтитры" в Settings panel.

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
    // MARK: - Helpers

    @MainActor
    private func makeCallObserverHudToggle() -> NSButton {
        let hudToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onCallObserverHudEnabledChanged))
        hudToggle.state = .on
        objc_setAssociatedObject(self, &CallObserverSettingsAssocKeys.hudToggle, hudToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return hudToggle
    }

    @MainActor
    private func makeCallObserverAutoplayToggle() -> NSButton {
        let autoplayToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onCallObserverAutoplayChanged))
        autoplayToggle.state = .off
        objc_setAssociatedObject(self, &CallObserverSettingsAssocKeys.autoplayToggle, autoplayToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return autoplayToggle
    }

    @MainActor
    func buildCallObserverSettingsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "call_observer_settings",
            title: "Наблюдатель звонков агента",
            isExpanded: false
        )
        let card = ThemeCardView()

        let hudToggle = makeCallObserverHudToggle()
        let hudRow = makeSettingRow(
            label: "Панель звонка агента при звонке",
            description: "Плавающая плашка с живым транскриптом поверх остальных окон.",
            control: hudToggle
        )

        let autoplayToggle = makeCallObserverAutoplayToggle()
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

    // MARK: - CD Builders

    @MainActor
    func cdBuildCallObserverSettingsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_call_observer_settings",
            title: "Наблюдатель звонков агента",
            isExpanded: false
        )
        let card = CDSettingsCardView()

        let hudToggle = makeCallObserverHudToggle()
        hudToggle.title = ""
        hudToggle.setButtonType(.switch)
        let hudRow = cdMakeRow(label: "Панель при звонке", control: hudToggle)

        let autoplayToggle = makeCallObserverAutoplayToggle()
        autoplayToggle.title = ""
        autoplayToggle.setButtonType(.switch)
        let autoplayRow = cdMakeRow(label: "Сразу включать звук", control: autoplayToggle)

        card.contentStackView.addArrangedSubview(hudRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(autoplayRow)

        section.contentStackView.addArrangedSubview(card)
        refreshCallObserverSettingsToggles()
        return section
    }

}
