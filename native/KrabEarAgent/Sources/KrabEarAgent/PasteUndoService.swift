/*
 PasteUndoService.swift — Глобальный hotkey Cmd+Control+Z для отката последней вставки.

 Связи модуля:
 1) main.swift: создаётся в completeStartupAfterBackendReady, рядом с SelectionTranslator.
 2) AgentSettings.pasteUndoEnabled: флаг «Откат вставки» из Settings.
 3) HistoryPanelController+Settings.swift: syncPasteUndoToggle обновляет checkbox и этот флаг.

 Логика:
 - Hotkey Cmd+Control+Z зарегистрирован ВСЕГДА (через NSEvent global monitor).
 - Если pasteUndoEnabled == false — handler делает return (no-op).
 - При срабатывании: захватывает target и резервирует общую input FIFO для Cmd+Z.
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
    private let pasteService: PasteService
    init(pasteService: PasteService = PasteService()) { self.pasteService = pasteService }

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

    /// Target/snapshot/deadline и место общей очереди фиксируются до Task/await.
    /// Unknown target не разрешает глобальный HID fallback в произвольное приложение.
    @discardableResult func sendCmdZ() -> Task<PasteAttemptResult, Never>? {
        guard let pid = pasteService.captureTargetPID(), pid != ProcessInfo.processInfo.processIdentifier else { return nil }
        let snapshot = pasteService.captureInput(pid: pid), queue = pasteService.inputQueue
        guard case .success(let ticket) = queue.reserve(deadline: pasteService.deadlineForPaste()) else { return nil }
        return Task { @MainActor in
            defer { queue.release(ticket) }
            switch await queue.awaitReady(ticket) {
            case .failure(let why): return PasteAttemptResult(ok: false, reason: "input_" + why.rawValue)
            case .success(let lease): return await pasteService.undo(snapshot: snapshot, lease: lease)
            }
        }
    }
}
