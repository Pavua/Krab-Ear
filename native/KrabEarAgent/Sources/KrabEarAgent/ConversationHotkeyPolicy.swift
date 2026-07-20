/*
 ConversationHotkeyPolicy — единая чистая политика двойного Right Option.

 Нужна, чтобы startup, экран настроек и lifecycle разговора одинаково трактовали
 сохранённый флаг KrabEar_ConversationHotkeyEnabled. UI и глобальные NSEvent-
 мониторы остаются в main.swift/HotkeyManager, а здесь нет AppKit и побочных
 эффектов, поэтому переходы idle → start и active → stop тестируются headless.
*/

import Foundation

enum ConversationHotkeyPolicy {
    /// Ключ уже используется экраном настроек; централизуем его, чтобы startup
    /// не расходился с UI после следующего переименования или рефакторинга.
    static let defaultsKey = "KrabEar_ConversationHotkeyEnabled"

    /// Исторический дефолт — hotkey включён на первом запуске. Важно отличать
    /// отсутствующий ключ от явно сохранённого false: bool(forKey:) сам этого
    /// различия не показывает.
    static func isEnabled(in defaults: UserDefaults = .standard) -> Bool {
        guard defaults.object(forKey: defaultsKey) != nil else { return true }
        return defaults.bool(forKey: defaultsKey)
    }

    /// Выполняет ровно одну ветку lifecycle: idle запускаем, active останавливаем.
    /// Замыкания не сохраняются — production может безопасно передавать UI-вызовы,
    /// а тесты подставляют счётчики без реального окна, WebSocket и микрофона.
    static func performToggle(
        isSessionActive: Bool,
        onStart: () -> Void,
        onStop: () -> Void
    ) {
        if isSessionActive {
            onStop()
        } else {
            onStart()
        }
    }
}
