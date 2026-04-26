/*
 Глобальная обработка горячей клавиши Right Option для Krab Ear.

 Связи модуля:
 1) main.swift: получает callback toggle записи.
 2) HotkeyDoubleTapDetector: детект двойного тапа → triggerConversationStart() (PR 1.5).

 Режимы hotkey (hotkeyMode):
 - "toggle": однократное нажатие — старт, следующее — стоп (существующее поведение).
 - "hold": зажал — пишет, отпустил — стоп. Нажатия < holdMinDurationMs игнорируются.
 3) Quick replay (Cmd+Option+V): onQuickReplay callback → PasteService.repastLast().
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

/// Режим срабатывания hotkey.
enum HotkeyMode: String {
    case toggle = "toggle"
    case hold = "hold"
}

/// Нативный hotkey менеджер для Option key toggle / hold.
/// Также управляет DoubleTapDetector для запуска «Разговора с AI» (PR 1.5)
/// и глобальным Cmd+Option+V для быстрого повтора вставки.
@MainActor
final class HotkeyManager {
    private var globalMonitor: Any?
    private var localMonitor: Any?
    private var isPressed = false
    private let variant: HotkeyVariant
    private let onToggle: @MainActor () -> Void

    // MARK: Hold mode

    /// Режим: toggle (по умолчанию) или hold (зажал-отпустил).
    let mode: HotkeyMode

    /// Минимальная длительность удержания для hold-режима (мс).
    let holdMinDurationMs: Int

    /// Момент нажатия — для фильтрации коротких (<holdMinDurationMs) нажатий.
    private var holdPressedAt: Date?

    /// Callback на старт записи в hold-режиме (DOWN).
    var onHoldStart: (@MainActor () -> Void)?

    /// Callback на стоп записи в hold-режиме (UP после достаточной длительности).
    var onHoldStop: (@MainActor () -> Void)?

    // MARK: PR 1.5 — double-tap detector для Разговора с AI

    /// Детектор двойного нажатия Right Option (300 мс окно).
    /// Callback: переключить вкладку Разговор с AI и запустить/остановить сессию.
    private var doubleTapDetector: HotkeyDoubleTapDetector?

    /// Колбэк на double-tap (задаётся при запуске из main.swift).
    var onConversationDoubleTap: (@MainActor () -> Void)?

    /// Pending single-tap action — ждёт окно double-tap. Cancelled когда
    /// detector ловит second tap. Если timer истёк — fires onToggle().
    private var pendingSingleTapTimer: DispatchSourceTimer?

    private func schedulePendingSingleTap() {
        // Cancel предыдущий если был — типичный case при rapid taps.
        cancelPendingSingleTap()
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + .milliseconds(310))  // 300ms detector window + 10ms slack
        timer.setEventHandler { [weak self] in
            self?.pendingSingleTapTimer = nil
            self?.onToggle()
        }
        timer.resume()
        pendingSingleTapTimer = timer
    }

    private func cancelPendingSingleTap() {
        pendingSingleTapTimer?.cancel()
        pendingSingleTapTimer = nil
    }

    // MARK: Quick replay — Cmd+Option+V

    /// Глобальный монитор keyDown для обнаружения Cmd+Option+V (быстрый повтор вставки).
    private var replayMonitor: Any?

    /// Callback на Cmd+Option+V — задаётся из main.swift.
    var onQuickReplay: (@MainActor () -> Void)?

    init(
        variant: String,
        onToggle: @escaping @MainActor () -> Void,
        mode: String = "toggle",
        holdMinDurationMs: Int = 200
    ) {
        self.variant = HotkeyVariant(rawValue: variant) ?? .rightOption
        self.onToggle = onToggle
        self.mode = HotkeyMode(rawValue: mode) ?? .toggle
        self.holdMinDurationMs = holdMinDurationMs
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
        // (только для right_option и right_option_toggle вариантов).
        // При detection — отменяем pending single-tap action и запускаем
        // conversation. См. processKeyEvent .toggle case.
        if variant == .rightOption || variant == .rightOptionToggle {
            let detector = HotkeyDoubleTapDetector(windowMs: 0.3) { [weak self] in
                self?.cancelPendingSingleTap()
                self?.onConversationDoubleTap?()
            }
            detector.start()
            doubleTapDetector = detector
        }

        // Quick replay: глобальный монитор Cmd+Option+V
        replayMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            Task { @MainActor [weak self] in
                self?.handleKeyDown(event: event)
            }
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
        if let replayMonitor {
            NSEvent.removeMonitor(replayMonitor)
            self.replayMonitor = nil
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
        processKeyEvent(isDown: isDown)
    }

    // MARK: - Обработка события клавиши (общая логика)

    private func processKeyEvent(isDown: Bool) {
        switch mode {
        case .toggle:
            if isDown && !isPressed {
                isPressed = true
                // Defer single-tap action when conversation double-tap is wired —
                // даём 300ms окно чтобы detector мог cancel single-tap если
                // user сделал double-tap. Иначе single-tap toggle'ит запись
                // до того как detector видит второй нажим (ранее эта race
                // делала double-tap "невидимым" — user видел только recording
                // start/stop).
                if onConversationDoubleTap != nil {
                    schedulePendingSingleTap()
                } else {
                    onToggle()
                }
            } else if !isDown && isPressed {
                isPressed = false
            }

        case .hold:
            if isDown && !isPressed {
                isPressed = true
                holdPressedAt = Date()
                onHoldStart?()
            } else if !isDown && isPressed {
                isPressed = false
                let pressDuration = holdPressedAt.map { Date().timeIntervalSince($0) * 1000 } ?? 0
                holdPressedAt = nil
                guard pressDuration >= Double(holdMinDurationMs) else {
                    // Слишком короткое нажатие — игнорируем, останавливаем запись без результата
                    return
                }
                onHoldStop?()
            }
        }
    }

    // MARK: - Quick replay key handling

    private func handleKeyDown(event: NSEvent) {
        guard isQuickReplayHotkey(event) else { return }
        onQuickReplay?()
    }

    /// Возвращает true если событие — Cmd+Option+V (быстрый повтор вставки).
    /// Cmd+Shift+V — системный «Paste and Match Style», намеренно не использован.
    func isQuickReplayHotkey(_ event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        return flags == [.command, .option] && event.keyCode == Keycode.v.rawValue
    }

    // MARK: - Тест-хуки

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
        processKeyEvent(isDown: isOptionDown)
    }

    /// Тест-хук: симулировать DOWN с явным overrideПрессTime — позволяет тестировать 200ms debounce.
    /// После simulateHoldDown используй simulateHoldUp с нужным releaseTime.
    @MainActor
    func simulateHoldDown(keyCode: UInt16, overridePressTime: Date = Date()) {
        let isTargetKey: Bool
        switch variant {
        case .rightOption, .rightOptionToggle:
            isTargetKey = (keyCode == Keycode.rightOption.rawValue)
        case .leftOption:
            isTargetKey = (keyCode == Keycode.leftOption.rawValue)
        case .anyOption:
            isTargetKey = (keyCode == Keycode.rightOption.rawValue || keyCode == Keycode.leftOption.rawValue)
        }
        guard isTargetKey, !isPressed else { return }
        isPressed = true
        holdPressedAt = overridePressTime
        onHoldStart?()
    }

    @MainActor
    func simulateHoldUp(keyCode: UInt16, overrideReleaseTime: Date = Date()) {
        let isTargetKey: Bool
        switch variant {
        case .rightOption, .rightOptionToggle:
            isTargetKey = (keyCode == Keycode.rightOption.rawValue)
        case .leftOption:
            isTargetKey = (keyCode == Keycode.leftOption.rawValue)
        case .anyOption:
            isTargetKey = (keyCode == Keycode.rightOption.rawValue || keyCode == Keycode.leftOption.rawValue)
        }
        guard isTargetKey, isPressed else { return }
        isPressed = false
        let pressDuration = holdPressedAt.map { overrideReleaseTime.timeIntervalSince($0) * 1000 } ?? 0
        holdPressedAt = nil
        guard pressDuration >= Double(holdMinDurationMs) else { return }
        onHoldStop?()
    }

    /// Инжектировать синтетическое keyDown событие — для тестирования быстрого повтора.
    @MainActor
    func injectKeyDownLogic(keyCode: UInt16, flags: NSEvent.ModifierFlags) {
        guard let event = NSEvent.keyEvent(
            with: .keyDown,
            location: .zero,
            modifierFlags: flags,
            timestamp: 0,
            windowNumber: 0,
            context: nil,
            characters: "",
            charactersIgnoringModifiers: "",
            isARepeat: false,
            keyCode: keyCode
        ) else { return }
        handleKeyDown(event: event)
    }
}
