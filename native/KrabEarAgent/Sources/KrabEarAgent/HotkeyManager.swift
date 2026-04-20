/*
 Глобальная обработка горячей клавиши Right Option для Krab Ear.

 Связи модуля:
 1) main.swift: получает callback toggle записи.
 2) HotkeyDoubleTapDetector: детект двойного тапа → triggerConversationStart() (PR 1.5).
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
/// Также управляет DoubleTapDetector для запуска «Разговора с AI» (PR 1.5).
@MainActor
final class HotkeyManager {
    private var globalMonitor: Any?
    private var localMonitor: Any?
    private var isPressed = false
    private let variant: HotkeyVariant
    private let onToggle: @MainActor () -> Void

    // MARK: PR 1.5 — double-tap detector для Разговора с AI

    /// Детектор двойного нажатия Right Option (300 мс окно).
    /// Callback: переключить вкладку Разговор с AI и запустить/остановить сессию.
    private var doubleTapDetector: HotkeyDoubleTapDetector?

    /// Колбэк на double-tap (задаётся при запуске из main.swift).
    var onConversationDoubleTap: (@MainActor () -> Void)?

    init(variant: String, onToggle: @escaping @MainActor () -> Void) {
        self.variant = HotkeyVariant(rawValue: variant) ?? .rightOption
        self.onToggle = onToggle
    }

    func start() {
        stop() // Safety check
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            Task { @MainActor [weak self] in
                self?.handle(event: event)
            }
        }
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            Task { @MainActor [weak self] in
                self?.handle(event: event)
            }
            return event
        }

        // PR 1.5: Запустить детектор двойного нажатия Right Option
        // (только для right_option и right_option_toggle вариантов)
        if variant == .rightOption || variant == .rightOptionToggle {
            let detector = HotkeyDoubleTapDetector(windowMs: 0.3) { [weak self] in
                self?.onConversationDoubleTap?()
            }
            detector.start()
            doubleTapDetector = detector
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
        doubleTapDetector?.stop()
        doubleTapDetector = nil
    }

    private func handle(event: NSEvent) {
        let isTargetKey: Bool

        switch variant {
        case .rightOption, .rightOptionToggle:
            isTargetKey = (event.keyCode == Keycode.rightOption.rawValue)
        case .leftOption:
            isTargetKey = (event.keyCode == Keycode.leftOption.rawValue)
        case .anyOption:
            isTargetKey = (event.keyCode == Keycode.rightOption.rawValue || event.keyCode == Keycode.leftOption.rawValue)
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

    // MARK: - Тест-хук

    /// Инжектировать синтетическое событие клавиши в логику фильтрации.
    /// Используется только из тестового таргета — имитирует флаги keyCode и option.
    @MainActor
    func injectEventLogic(keyCode: UInt16, isOptionDown: Bool) {
        let isTargetKey: Bool
        switch variant {
        case .rightOption, .rightOptionToggle:
            isTargetKey = (keyCode == Keycode.rightOption.rawValue)
        case .leftOption:
            isTargetKey = (keyCode == Keycode.leftOption.rawValue)
        case .anyOption:
            isTargetKey = (keyCode == Keycode.rightOption.rawValue || keyCode == Keycode.leftOption.rawValue)
        }

        guard isTargetKey else { return }

        if isOptionDown && !isPressed {
            isPressed = true
            onToggle()
            return
        }

        if !isOptionDown && isPressed {
            isPressed = false
        }
    }
}
