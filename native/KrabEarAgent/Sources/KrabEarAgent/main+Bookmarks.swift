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
        // 1. Получаем текущее состояние записи (session_id + elapsed_sec)
        guard let stateData = try? callWithRecovery(method: "get_recording_state", params: [:]) else {
            logger.warn("Не удалось получить состояние записи для закладки")
            return
        }

        let sessionId = (stateData["session_id"] as? String) ?? "__live__"
        let offsetSec = (stateData["elapsed_sec"] as? Double) ?? 0.0

        // 2. Создаём закладку
        let params: [String: Any] = [
            "session_id": sessionId,
            "offset_sec": offsetSec,
            "note": "",
        ]
        guard (try? callWithRecovery(method: "add_bookmark", params: params)) != nil else {
            logger.warn("Ошибка создания закладки")
            return
        }

        let offsetFormatted = Self.formatOffsetSec(offsetSec)
        logger.info("Закладка создана в \(offsetFormatted) (session: \(sessionId))")
        DispatchQueue.main.async { [weak self] in
            self?.showTemporaryBookmarkMessage(offsetFormatted)
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
