# NSAlert AppHang Investigation (Sentry KRAB-EAR-AGENT-E)

**Sentry issue**: https://po-zm.sentry.io/issues/KRAB-EAR-AGENT-E
**Pattern**: `App hanging for at least 2000 ms.`
**Stacktrace top**: `-[NSAlert runModal] → -[NSApplication runModalForWindow:] → _NSTryRunModal`
**Frequency**: 9 events / 4 days, 1 user (DB18D650-...)
**Environment**: daily-use, macOS 26.5.0, Mac16,6 (M4 Max)

## Project context

- 785 Swift файлов / ~28 194 LOC
- 453 вхождения `async`/`await`/`Task {` — async/await активно используется для IPC, но **NSAlert вызовы из этого паттерна исключены**
- Последний коммит на момент исследования: `690a2ae` 06.05.2026

## Все места `runModal()` (синхронный, блокирует main thread)

| File:Line | Контекст | Статус |
|-----------|----------|--------|
| `PermissionWizard.swift:52,69,92` | 3× в onboarding wizard, `@MainActor` класс | **Неактивен** (см. `main.swift:246` — заменён на QuickStartWindowController) |
| `main+QuickReplace.swift:60` | Hotkey Cmd+Shift+R, alert с двумя text fields | Безопасен — нет IPC до alert |
| `main+QuickReplace.swift:121` | Error result alert после IPC | Безопасен — быстрый |
| `main.swift:1015` | `showFatalAndTerminate` | Только при крэше |
| `HistoryPanelController+History.swift:595` | `sendHistoryItemToImessage` — alert ДО IPC | **⚠️ главный кандидат на hang** |
| `HistoryPanelController+History.swift:626` | Result alert после IPC `send_imessage` | OK |
| `HistoryPanelController+History.swift:476, 515, 564` | Result alerts для Apple Notes/Reminders/Calendar | OK |
| `HistoryPanelController+HistoryEnhancements.swift:45, 211, 325, 357, 375` | Confirmation alerts/selection dialogs | OK (IPC на background перед alert) |
| `HistoryPanelController.swift:2101, 2152, 2193, 2270, 2318, 2344` | Export/import format selection | 2152, 2193 имеют комментарий `Async IPC чтобы NSAlert dismiss не блокировал main thread (AppHang risk)` — то есть разработчик уже знал о риске |
| `GlossarySuggestions.swift:388/404` | `if let w = window { beginSheetModal } else { runModal() }` | **else-ветка может срабатывать когда окно не на переднем плане** |
| `CallAutomationController.swift:1054/1056` | Та же паттерн | **⚠️ кандидат — call automation работает в фоне** |
| `DiagnosticsTabView.swift:536/538` | Та же паттерн | OK (UI-only) |
| `Settings+ClaudeDesign.swift:581/583` | Та же паттерн | OK (UI-only) |
| `KeyboardShortcuts.swift:95/97` | Та же паттерн | OK (UI-only) |

## Главные подозреваемые

### 1. `HistoryPanelController+History.swift:595` — `sendHistoryItemToImessage`

Функция показывает `runModal()` диалог получателя **до** IPC. Если система подтормаживает на отрисовке modal window (NSApp переходит в modal run loop), `runModalForWindow` ждёт системных ресурсов → стек из Sentry. UI пользователя замирает.

### 2. Ветки `else runModal()` в паттерне `beginSheetModal else runModal`

Срабатывают когда окно `nil` или невидимо. AppKit создаёт **новое modal window через `NSApplication.runModalForWindow`** — именно этот метод фигурирует в Sentry stacktrace.

`CallAutomationController.swift:1056` особенно подозрителен — call automation сценарии могут происходить в фоне когда окно не на переднем плане → window=nil → срабатывает `else runModal()` → app hangs.

## Рекомендации (text-only, не редактировались исходники)

1. **Заменить все `runModal()` на `beginSheetModal(for:completionHandler:)`** с `@MainActor` closure — это не блокирует run loop.

2. **`CallAutomationController.swift:1056` и `GlossarySuggestions.swift:390/404`** — убрать `else runModal()` ветку. Если `window == nil`, использовать:
   - `NSUserNotification` (deprecated, но работает)
   - `UNUserNotificationCenter` (modern)
   - StatusBar item с badge
   - `NSApp.activate(ignoringOtherApps: true)` + retry получения window

3. **`sendHistoryItemToImessage`** — заменить modal alert на inline `NSTextField` в самой панели. Альтернатива: guard `window?.isVisible == true` перед alert; если нет — сначала показать окно через статусбар.

4. **`PermissionWizard`** — уже не используется. Можно удалить весь класс.

5. **Универсальный helper** — обернуть все нужные ввода через async API:

```swift
@MainActor
func showInputAlert(title: String, message: String) async -> String? {
    await withCheckedContinuation { continuation in
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        if let window = NSApp.keyWindow ?? NSApp.mainWindow {
            alert.beginSheetModal(for: window) { response in
                continuation.resume(returning: response == .alertFirstButtonReturn ? input : nil)
            }
        } else {
            // Без окна вообще не показываем — возвращаем nil, caller fallback
            continuation.resume(returning: nil)
        }
    }
}
```

## Test/repro plan

1. Воспроизведение: использовать call automation или `Send to iMessage` функцию когда окно закрыто/свёрнуто.
2. Проверка через Instruments → Time Profiler во время реального hang.
3. Sentry filter после fix: убедиться что `KRAB-EAR-AGENT-E` не получает новых events 7+ дней.

## Источники

- Sentry: KRAB-EAR-AGENT-E (frequency 9 events / 4d, 1 user, daily-use)
- Investigation by Krab repo session 39 (research-only sonnet agent)
