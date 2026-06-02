// main+Bookmarks.swift — Recording bookmarks hotkey (Cmd+Shift+B)
// Регистрирует глобальный NSEvent-монитор.
// При нажатии Cmd+Shift+B во время активной записи создаёт закладку через IPC.

import AppKit
import Foundation

extension AgentAppDelegate {

    // MARK: - Hotkey registration

    /// Регистрирует глобальный монитор клавиатуры для Cmd+Shift+B.
    func startBookmarkHotkeyMonitor() {
        NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return }
            // keyCode 11 = 'b'; проверяем Cmd+Shift модификаторы
            let mods = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            guard event.keyCode == 11,
                  mods == [.command, .shift] else { return }
            DispatchQueue.main.async {
                self.handleBookmarkHotkey()
            }
        }
    }

    // MARK: - Hotkey handler

    @MainActor
    func handleBookmarkHotkey() {
        guard isRecording else {
            logger.info("Bookmark hotkey нажат вне записи — игнорируем")
            return
        }
        createBookmarkDuringRecording()
    }

    // MARK: - Bookmark creation

    func createBookmarkDuringRecording() {
        // Offload both IPC calls off the main thread to prevent >2s AppHang
        // (AGENT-3 pattern: sync callWithRecovery on main thread blocks runloop).
        // Wave 188 fix: mirror the Task.detached pattern from main+HotkeyRecording.swift.
        let ipc = self.ipcClient
        let log = self.logger
        Task.detached { [weak self] in
            // 1. Получаем текущее состояние записи (session_id + elapsed_sec)
            guard let stateData = try? await ipc.callAsync(method: "get_recording_state", params: [:]) else {
                log.warn("Не удалось получить состояние записи для закладки")
                return
            }

            let result = (stateData["result"] as? [String: Any]) ?? stateData
            let sessionId = (result["session_id"] as? String) ?? "__live__"
            let offsetSec = (result["elapsed_sec"] as? Double) ?? 0.0

            // 2. Создаём закладку
            let bookmarkParams: [String: Any] = [
                "session_id": sessionId,
                "offset_sec": offsetSec,
                "note": "",
            ]
            guard (try? await ipc.callAsync(method: "add_bookmark", params: bookmarkParams)) != nil else {
                log.warn("Ошибка создания закладки")
                return
            }

            log.info("Закладка создана в \(offsetSec)s (session: \(sessionId))")
            if let self = self {
                await MainActor.run {
                    let offsetFormatted = AgentAppDelegate.formatOffsetSec(offsetSec)
                    self.showTemporaryBookmarkMessage(offsetFormatted)
                }
            }
        }
    }

    // MARK: - Helpers

    /// Форматирует секунды как "M:SS" или "H:MM:SS".
    static func formatOffsetSec(_ sec: Double) -> String {
        let total = Int(sec)
        let h = total / 3600
        let m = (total % 3600) / 60
        let s = total % 60
        if h > 0 {
            return String(format: "%d:%02d:%02d", h, m, s)
        }
        return String(format: "%d:%02d", m, s)
    }

    // MARK: - Temporary status message

    @MainActor
    func showTemporaryBookmarkMessage(_ offsetFormatted: String) {
        let message = "📌 \(offsetFormatted)"
        if let btn = statusItem?.button {
            let original = btn.title
            btn.title = message
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                btn.title = original
            }
        } else {
            logger.info("Закладка: \(message)")
        }
    }
}
