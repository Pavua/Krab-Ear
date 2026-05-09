// main+LiveSubsHotkey.swift — Глобальный hotkey для Live Subs (Cmd+Option+Shift+L)
// Регистрирует глобальный NSEvent-монитор.
// Работает при любом frontmost-приложении (Safari, etc.).

import AppKit
import Foundation

extension AgentAppDelegate {

    // MARK: - Global hotkey registration

    /// Регистрирует глобальный монитор клавиатуры для Cmd+Option+Shift+L.
    func startLiveSubsHotkeyMonitor() {
        NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return }
            // keyCode 37 = 'l'; проверяем Cmd+Option+Shift модификаторы
            let mods = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            guard event.keyCode == 37,
                  mods == [.command, .option, .shift] else { return }
            DispatchQueue.main.async {
                self.toggleLiveSubsCaptureFromMenu()
            }
        }
    }
}
