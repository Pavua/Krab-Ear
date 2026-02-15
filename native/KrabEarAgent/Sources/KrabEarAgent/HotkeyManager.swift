/*
 Глобальная обработка горячей клавиши Right Option для Krab Ear.

 Связи модуля:
 1) main.swift: получает callback toggle записи.
*/

import AppKit
import Foundation


/// Варианты горячих клавиш.
enum HotkeyVariant: String {
    case rightOption = "right_option"
    case rightOptionToggle = "right_option_toggle"
    case leftOption = "left_option"
    case anyOption = "any_option"
}

/// Нативный hotkey менеджер для Option key toggle.
final class HotkeyManager {
    private var globalMonitor: Any?
    private var localMonitor: Any?
    private var isPressed = false
    private let variant: HotkeyVariant
    private let onToggle: () -> Void

    init(variant: String, onToggle: @escaping () -> Void) {
        self.variant = HotkeyVariant(rawValue: variant) ?? .rightOption
        self.onToggle = onToggle
    }

    func start() {
        stop() // Safety check
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            self?.handle(event: event)
        }
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            self?.handle(event: event)
            return event
        }
    }

    func stop() {
        if let globalMonitor {
            NSEvent.removeMonitor(globalMonitor)
            self.globalMonitor = nil
        }
        if let localMonitor {
            NSEvent.removeMonitor(localMonitor)
            self.localMonitor = nil
        }
    }

    private func handle(event: NSEvent) {
        // keyCode 61 = Right Option, 58 = Left Option
        let isTargetKey: Bool
        
        switch variant {
        case .rightOption, .rightOptionToggle:
            isTargetKey = (event.keyCode == 61)
        case .leftOption:
            isTargetKey = (event.keyCode == 58)
        case .anyOption:
            isTargetKey = (event.keyCode == 61 || event.keyCode == 58)
        }

        guard isTargetKey else { return }

        let isDown = event.modifierFlags.contains(.option)

        if isDown && !isPressed {
            isPressed = true
            onToggle()
            return
        }

        if !isDown && isPressed {
            isPressed = false
        }
    }
}

