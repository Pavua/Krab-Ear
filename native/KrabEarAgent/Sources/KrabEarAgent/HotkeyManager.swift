/*
 Глобальная обработка горячей клавиши Right Option для Krab Ear.

 Связи модуля:
 1) main.swift: получает callback toggle записи.
 2) HotkeyDoubleTapDetector: детект двойного тапа → triggerConversationToggle().

 Режимы hotkey (hotkeyMode):
 - "toggle": однократное нажатие — старт, следующее — стоп (существующее поведение).
 - "hold": зажал — пишет, отпустил — стоп. Нажатия < holdMinDurationMs игнорируются.
 3) Quick replay (Cmd+Option+V): onQuickReplay callback → PasteService.repastLast().
*/

import AppKit
import Carbon
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

    /// Отложенный старт hold-записи. Короткое нажатие отменяет таймер и поэтому
    /// вообще не открывает обычную диктовку.
    private var pendingHoldStartTimer: DispatchSourceTimer?

    /// Успел ли отложенный hold реально вызвать onHoldStart. Нужен, чтобы UP
    /// вызывал onHoldStop только для действительно начатой записи.
    private var holdActionStarted = false

    /// Обработчик старта записи после подтверждённого удержания клавиши.
    var onHoldStart: (@MainActor () -> Void)?

    /// Обработчик остановки записи на UP после состоявшегося старта.
    var onHoldStop: (@MainActor () -> Void)?

    // MARK: PR 1.5 — double-tap detector для Разговора с AI

    /// Детектор двойного нажатия Right Option (300 мс окно).
    /// Callback: переключить вкладку Разговор с AI и запустить/остановить сессию.
    private var doubleTapDetector: HotkeyDoubleTapDetector?

    /// Колбэк на double-tap (задаётся при запуске из main.swift).
    var onConversationDoubleTap: (@MainActor () -> Void)?

    /// Отложенное действие одиночного тапа ждёт окно double-tap. Детектор
    /// отменяет его на втором тапе; после тайм-аута вызывается onToggle().
    private var pendingSingleTapTimer: DispatchSourceTimer?

    /// Момент последнего поглощённого double-tap. Нужен, чтобы не поставить новый
    /// таймер на второй DOWN, если detector обработал событие раньше менеджера.
    private var recentDoubleTapAt: Date?
    /// Окно после double-tap, в течение которого менеджер игнорирует tap event.
    private static let doubleTapDebounceMs: Double = 500
    /// Окно detector 300 мс + 10 мс на порядок доставки независимых мониторов.
    private static let doubleTapDecisionDelayMs = 310

    /// Double-tap поддерживается только вариантами, для которых реально создаётся
    /// HotkeyDoubleTapDetector. Left/Any Option не должны платить задержкой 310 мс.
    static func supportsConversationDoubleTap(variant rawVariant: String) -> Bool {
        let parsed = HotkeyVariant(rawValue: rawVariant) ?? .rightOption
        return parsed == .rightOption || parsed == .rightOptionToggle
    }

    private var conversationDoubleTapIsActive: Bool {
        onConversationDoubleTap != nil
            && Self.supportsConversationDoubleTap(variant: variant.rawValue)
    }

    private var isInsideConsumedDoubleTapWindow: Bool {
        guard let recentDoubleTapAt else { return false }
        return Date().timeIntervalSince(recentDoubleTapAt) * 1000 < Self.doubleTapDebounceMs
    }

    private func schedulePendingSingleTap() {
        // Второй DOWN может прийти сюда после detector callback — не вооружаем
        // одиночный тап повторно внутри окна уже поглощённого double-tap.
        guard !isInsideConsumedDoubleTapWindow else { return }
        cancelPendingSingleTap()
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + .milliseconds(Self.doubleTapDecisionDelayMs))
        timer.setEventHandler { [weak self] in
            self?.completePendingSingleTap()
        }
        timer.resume()
        pendingSingleTapTimer = timer
    }

    private func cancelPendingSingleTap() {
        pendingSingleTapTimer?.cancel()
        pendingSingleTapTimer = nil
    }

    private func completePendingSingleTap() {
        guard pendingSingleTapTimer != nil else { return }
        cancelPendingSingleTap()
        onToggle()
    }

    private func schedulePendingHoldStart() {
        guard !isInsideConsumedDoubleTapWindow else { return }
        cancelPendingHoldStart()
        let delayMs = conversationDoubleTapIsActive
            ? max(holdMinDurationMs, Self.doubleTapDecisionDelayMs)
            : holdMinDurationMs
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + .milliseconds(delayMs))
        timer.setEventHandler { [weak self] in
            self?.completePendingHoldStart()
        }
        timer.resume()
        pendingHoldStartTimer = timer
    }

    private func cancelPendingHoldStart() {
        pendingHoldStartTimer?.cancel()
        pendingHoldStartTimer = nil
    }

    private func completePendingHoldStart() {
        guard pendingHoldStartTimer != nil else { return }
        cancelPendingHoldStart()
        guard isPressed, !holdActionStarted else { return }
        holdActionStarted = true
        onHoldStart?()
    }

    /// Единая точка поглощения double-tap для реального detector и тест-хука.
    /// Отменяет обе возможные обычные операции ДО запуска conversation callback.
    private func consumeConversationDoubleTap() {
        guard conversationDoubleTapIsActive else { return }
        cancelPendingSingleTap()
        cancelPendingHoldStart()
        recentDoubleTapAt = Date()
        onConversationDoubleTap?()
    }

    // MARK: Quick replay — Cmd+Option+V

    /// Глобальный монитор keyDown для обнаружения Cmd+Option+V (быстрый повтор вставки).
    private var replayMonitor: Any?

    /// Callback на Cmd+Option+V — задаётся из main.swift.
    var onQuickReplay: (@MainActor () -> Void)?

    // MARK: Phase B.2 — hotkey conflict reporting

    /// Fire-and-forget callback вызывается когда RegisterEventHotKey возвращает
    /// eventHotKeyExistsErr (-9878), т.е. другое приложение уже держит chord.
    /// Задаётся из main.swift после того как ipcClient готов.
    var reportHotkeyConflictHandler: ((String) -> Void)?

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

        // Phase B.2: Probe whether the chord is already registered by another app.
        // RegisterEventHotKey with a temporary EventHotKeyID is the only reliable
        // way to detect kSystemUIServer / Spotlight / other global shortcuts before
        // our own NSEvent monitors are set up.
        probeChordConflict()

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
        // При detection единая точка consumeConversationDoubleTap отменяет и
        // одиночный toggle, и ещё не начавшийся hold.
        if Self.supportsConversationDoubleTap(variant: variant.rawValue) {
            let detector = HotkeyDoubleTapDetector(windowMs: 0.3) { [weak self] in
                self?.consumeConversationDoubleTap()
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

    /// Проверяет занят ли chord другим приложением через RegisterEventHotKey.
    /// Если RegisterEventHotKey возвращает eventHotKeyExistsErr (-9878) —
    /// вызывает reportHotkeyConflictHandler с chord identifier.
    /// При успехе — немедленно отменяет регистрацию (UnregisterEventHotKey).
    private func probeChordConflict() {
        // Определяем keyCode и modifiers для текущего варианта.
        // Для right_option / right_option_toggle используем Option key.
        let (keyCode, modifiers): (UInt32, UInt32)
        switch variant {
        case .rightOption, .rightOptionToggle:
            keyCode = UInt32(Keycode.rightOption.rawValue)
            modifiers = UInt32(optionKey)
        case .leftOption:
            keyCode = UInt32(Keycode.leftOption.rawValue)
            modifiers = UInt32(optionKey)
        case .anyOption:
            keyCode = UInt32(Keycode.rightOption.rawValue)
            modifiers = UInt32(optionKey)
        }

        let probeID = EventHotKeyID(signature: OSType(0x4B524142), id: UInt32(9878))  // 'KRAB', probe sentinel
        var hotKeyRef: EventHotKeyRef?
        let status = RegisterEventHotKey(
            keyCode,
            modifiers,
            probeID,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )

        if status == OSStatus(eventHotKeyExistsErr) {
            // Another app holds the chord — fire error bus report.
            let chordName = variant.rawValue
            AgentLogger.shared.warn("HotkeyManager: chord '\(chordName)' занят другим приложением (eventHotKeyExistsErr)")
            reportHotkeyConflictHandler?(chordName)
        } else if status == noErr, let ref = hotKeyRef {
            // Successfully registered — immediately unregister, this was only a probe.
            UnregisterEventHotKey(ref)
        }
        // Other non-zero statuses (e.g. paramErr) are silently ignored —
        // they don't indicate a conflict, just that the probe wasn't supported.
    }

    func stop() {
        cancelPendingSingleTap()
        cancelPendingHoldStart()
        recentDoubleTapAt = nil
        isPressed = false
        if holdActionStarted {
            holdActionStarted = false
            onHoldStop?()
        }
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
                // Задерживаем одиночный тап только когда для текущего варианта
                // действительно есть detector и назначен conversation callback.
                if conversationDoubleTapIsActive {
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
                holdActionStarted = false
                // До порога удержания не начинаем запись. Для Right Option с
                // conversation callback ждём всё 300-мс окно double-tap: тогда
                // короткий первый тап не оставляет параллельную диктовку.
                schedulePendingHoldStart()
            } else if !isDown && isPressed {
                isPressed = false
                cancelPendingHoldStart()
                if holdActionStarted {
                    holdActionStarted = false
                    onHoldStop?()
                }
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

    /// Тест-хук: симулировать DOWN через ту же production-ветку hold lifecycle.
    @MainActor
    func simulateHoldDown(keyCode: UInt16) {
        injectEventLogic(keyCode: keyCode, isOptionDown: true)
    }

    @MainActor
    func simulateHoldUp(keyCode: UInt16) {
        injectEventLogic(keyCode: keyCode, isOptionDown: false)
    }

    /// Тест-хук: синхронно завершить вооружённый одиночный тап без ожидания 310 мс.
    var hasPendingSingleTapForTests: Bool { pendingSingleTapTimer != nil }

    @MainActor
    func firePendingSingleTapForTests() {
        completePendingSingleTap()
    }

    /// Тест-хуки для hold-таймера: проверяют тот же объект, что production callback.
    var hasPendingHoldStartForTests: Bool { pendingHoldStartTimer != nil }

    @MainActor
    func firePendingHoldStartForTests() {
        completePendingHoldStart()
    }

    /// Тест-хук double-tap использует единую production-точку поглощения.
    @MainActor
    func injectConversationDoubleTapLogic() {
        consumeConversationDoubleTap()
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
