/*
 PasteUndoService.swift — Глобальный hotkey Cmd+Control+Z для отката последней вставки.

 Связи модуля:
 1) main.swift: создаётся в completeStartupAfterBackendReady, рядом с SelectionTranslator.
 2) AgentSettings.pasteUndoEnabled: флаг «Откат вставки» из Settings.
 3) HistoryPanelController+Settings.swift: syncPasteUndoToggle обновляет checkbox и этот флаг.

 Логика:
 - Hotkey Cmd+Control+Z зарегистрирован ВСЕГДА (через NSEvent global monitor).
 - Если pasteUndoEnabled == false — handler делает return (no-op).
 - При срабатывании: шлёт Cmd+Z во frontmost app через CGEvent.postToPid.
 - Не трогает clipboard, не делает IPC — pure keystroke replay.
*/

import AppKit
import Foundation

/// Сервис глобального hotkey «Откат вставки» (Cmd+Control+Z).
///
/// Hotkey всегда зарегистрирован. Чтобы избежать re-registration churn при изменении
/// настройки, handler просто делает no-op когда `pasteUndoEnabled == false`.
@MainActor
final class PasteUndoService {

    // MARK: - State

    /// Управляет включением/выключением отката. Обновляется из настроек.
    var pasteUndoEnabled: Bool = false

    private var globalMonitor: Any?
    private let logger = AgentLogger.shared

    // Задержка между key-down и key-up синтетического Cmd+Z (микросекунды)
    private let keyPressDelayUs: useconds_t = 30_000

    // MARK: - Lifecycle

    /// Регистрирует глобальный monitor. Вызывать один раз при старте.
    func start() {
        guard globalMonitor == nil else { return }
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            Task { @MainActor [weak self] in
                self?.handleKeyEvent(event)
            }
        }
        logger.info("PasteUndoService запущен (Cmd+Ctrl+Z). enabled=\(pasteUndoEnabled)")
    }

    /// Снимает registration monitor.
    func stop() {
        if let m = globalMonitor {
            NSEvent.removeMonitor(m)
            globalMonitor = nil
        }
        logger.info("PasteUndoService остановлен.")
    }

    // MARK: - Key event

    private func handleKeyEvent(_ event: NSEvent) {
        guard isUndoHotkey(event) else { return }
        guard pasteUndoEnabled else {
            // Зарегистрирован, но отключён — тихий no-op.
            return
        }
        logger.info("PasteUndoService: Cmd+Ctrl+Z — отправляем Cmd+Z в frontmost app")
        sendCmdZ()
    }

    /// Проверяет, соответствует ли event chord Cmd+Control+Z.
    func isUndoHotkey(_ event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        // Chord: Command + Control, клавиша Z (keyCode 6 = kVK_ANSI_Z)
        let zKeyCode: UInt16 = 6
        return flags == [.command, .control] && event.keyCode == zKeyCode
    }

    // MARK: - Send Cmd+Z

    /// Отправляет Cmd+Z во frontmost app через CGEvent.postToPid.
    /// Fallback на .cghidEventTap если frontmostApplication недоступен.
    private func sendCmdZ() {
        guard let source = CGEventSource(stateID: .hidSystemState) else {
            logger.warn("PasteUndoService: не удалось создать CGEventSource")
            return
        }

        let zKeyCode: CGKeyCode = 6 // kVK_ANSI_Z

        guard
            let keyDown = CGEvent(keyboardEventSource: source, virtualKey: zKeyCode, keyDown: true),
            let keyUp   = CGEvent(keyboardEventSource: source, virtualKey: zKeyCode, keyDown: false)
        else {
            logger.warn("PasteUndoService: не удалось создать CGEvent для Cmd+Z")
            return
        }

        // Только Command — посылаем стандартный Undo (Cmd+Z), не Cmd+Ctrl+Z.
        keyDown.flags = .maskCommand
        keyUp.flags   = .maskCommand

        if let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier,
           pid != ProcessInfo.processInfo.processIdentifier {
            keyDown.postToPid(pid)
            usleep(keyPressDelayUs)
            keyUp.postToPid(pid)
            logger.info("PasteUndoService: Cmd+Z отправлен в pid=\(pid)")
        } else {
            // Fallback: HID event tap (когда frontmost — сам агент или неизвестен).
            keyDown.post(tap: .cghidEventTap)
            usleep(keyPressDelayUs)
            keyUp.post(tap: .cghidEventTap)
            logger.info("PasteUndoService: Cmd+Z отправлен через cghidEventTap (fallback)")
        }
    }
}
