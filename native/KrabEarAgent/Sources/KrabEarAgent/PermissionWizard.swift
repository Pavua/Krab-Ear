/*
 Онбординг Krab Ear по критичным системным правам и автозапуску.

 Связи модуля:
 1) AutostartManaging: включает/отключает launchd автозапуск; тесты используют spy.
 2) AgentSettings: сохраняет факт завершения онбординга.
*/

import AppKit
import Foundation

/// Пошаговый onboarding по правам и автозапуску.
@MainActor
final class PermissionWizard {
    func runIfNeeded(
        settings: AgentSettings,
        persistSettings: ([String: Any]) -> Void,
        launchAgentManager: any AutostartManaging
    ) -> AgentSettings {
        guard !settings.onboardingCompleted else {
            return settings
        }

        var updated = settings

        showPermissionsStep(
            title: "Krab Ear: доступ к микрофону",
            body: "Разрешите доступ к микрофону для записи и транскрибации.",
            openURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
        )

        showPermissionsStep(
            title: "Krab Ear: Accessibility",
            body: "Разрешите Accessibility для горячей клавиши и вставки текста.",
            openURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        )

        showPermissionsStep(
            title: "Krab Ear: Input Monitoring",
            body: "Разрешите Input Monitoring для обработки глобального hotkey.",
            openURL: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
        )

        showDiagnosticsStep()

        let autostartAlert = NSAlert()
        autostartAlert.messageText = "Включить автозапуск Krab Ear после входа в систему?"
        autostartAlert.informativeText = "Можно изменить позже в настройках агента."
        autostartAlert.addButton(withTitle: "Включить")
        autostartAlert.addButton(withTitle: "Пока нет")

        let response = autostartAlert.runModal()
        let shouldEnableAutostart = response == .alertFirstButtonReturn
        launchAgentManager.setAutostart(enabled: shouldEnableAutostart)

        updated.autoStartEnabled = shouldEnableAutostart
        updated.onboardingCompleted = true
        persistSettings(updated.toPayload())
        return updated
    }

    private func showPermissionsStep(title: String, body: String, openURL: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        alert.addButton(withTitle: "Открыть настройки")
        alert.addButton(withTitle: "Пропустить")

        let response = alert.runModal()
        guard response == .alertFirstButtonReturn else {
            return
        }

        if let url = URL(string: openURL) {
            NSWorkspace.shared.open(url)
        }
    }

    private func showDiagnosticsStep() {
        let alert = NSAlert()
        alert.messageText = "Проверка готовности (Onboarding 2.0)"
        alert.informativeText = """
        Нажмите тест-кнопку:
        • Проверить микрофон — откроет раздел Privacy > Microphone.
        • Проверить вставку — откроет Privacy > Accessibility.
        После проверки закройте окно и продолжите.
        """
        alert.addButton(withTitle: "Проверить микрофон")
        alert.addButton(withTitle: "Проверить вставку")
        alert.addButton(withTitle: "Продолжить")

        let response = alert.runModal()
        if response == .alertFirstButtonReturn {
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
                NSWorkspace.shared.open(url)
            }
            showDiagnosticsStep()
            return
        }
        if response == .alertSecondButtonReturn {
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
                NSWorkspace.shared.open(url)
            }
            showDiagnosticsStep()
        }
    }

    // MARK: - Тест-хук

    /// Применить финальные мутации настроек без показа UI-диалогов.
    /// Используется только из тестового таргета.
    @MainActor
    func applyCompletionState(
        to settings: AgentSettings,
        autostart: Bool,
        persistSettings: ([String: Any]) -> Void,
        launchAgentManager: any AutostartManaging
    ) -> AgentSettings {
        var updated = settings
        launchAgentManager.setAutostart(enabled: autostart)
        updated.autoStartEnabled = autostart
        updated.onboardingCompleted = true
        persistSettings(updated.toPayload())
        return updated
    }
}
