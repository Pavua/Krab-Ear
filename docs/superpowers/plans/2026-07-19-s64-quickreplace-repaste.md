# S64 re-paste (QuickReplace → буфер) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** После успешного Cmd+Shift+R исправленный текст попадает в буфер обмена с подсказкой «Скопировано — ⌘V»; заодно IPC-вызов уходит с main thread (AGENT-3 hardening).

**Architecture:** Спека `docs/superpowers/specs/2026-07-19-s64-quickreplace-repaste-design.md`. Один файл (`main+QuickReplace.swift`), ноль новых IPC/настроек — backend уже возвращает `new_text`. Off-main строго по паттерну Wave 188 (`main+Bookmarks.swift::createBookmarkDuringRecording`): `let ipc = self.ipcClient` → `Task.detached` → `ipc.callAsync` → `await MainActor.run { UI }`. `callWithRecovery` из функции удаляется целиком (принятый кодовой базой трейд-офф: без `restartIfDead`-recovery, самолечение — за BackendSupervisor/HealthMonitor).

**Tech Stack:** Swift 6 (`AgentAppDelegate` — `@MainActor`; IPCClient — Sendable). Тесты — source-contract greps (образец `QuickCaptureWiringTests.swift`).

## Жёсткие правила

Worktree `.worktrees/s64-repaste` + ветка `feature/s64-quickreplace-repaste` от `codex/krab-ear-v2`. Глиф-гейт: «⌘» УЖЕ установлен (`HistoryPanelController+KeyboardShortcuts.swift`) — новых глифов не вводить. Никаких `runModal()`. Буфер трогается ТОЛЬКО при `ok:true` (спека: ошибочные пути не меняются). Существующая обработка privacy-отказа (`reason` без `error` → «Неизвестная ошибка») — pre-existing, НЕ трогать (вне скоупа).

### Task 1: re-paste + off-main в main+QuickReplace.swift

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+QuickReplace.swift` (completion-блок `onReplaceWordRequested`, ~строки 60-106)
- Test (create): `native/KrabEarAgent/Tests/KrabEarAgentTests/QuickReplaceWiringTests.swift`

- [ ] **Step 1: Падающие source-contract тесты** — создать `QuickReplaceWiringTests.swift`:

```swift
/*
 QuickReplaceWiringTests — source-contract тесты S64 re-paste (спека
 2026-07-19-s64-quickreplace-repaste-design.md). Грепают реальный source
 (паттерн QuickCaptureWiringTests) — ловят декоративную проводку.
*/

import XCTest

final class QuickReplaceWiringTests: XCTestCase {
    private func source(_ name: String) throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // → Tests/KrabEarAgentTests/
            .deletingLastPathComponent()   // → Tests/
            .deletingLastPathComponent()   // → корень пакета KrabEarAgent/
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    /// S64 re-paste: успешная замена обязана класть исправленный ПОЛНЫЙ текст
    /// (new_text из ответа backend) в буфер обмена.
    func test_success_branch_copies_new_text_to_clipboard() throws {
        let src = try source("main+QuickReplace.swift")
        XCTAssertTrue(src.contains("putToClipboard"),
                      "успешная замена обязана копировать new_text в буфер")
        XCTAssertTrue(src.contains("new_text"),
                      "текст для буфера берётся из поля new_text ответа IPC")
    }

    /// Тост обязан подсказывать пользователю про вставку.
    func test_toast_mentions_clipboard_hint() throws {
        let src = try source("main+QuickReplace.swift")
        XCTAssertTrue(src.contains("Скопировано"),
                      "тост обязан сообщать про копирование в буфер")
    }

    /// AGENT-3: sync callWithRecovery на main thread из completion алерта —
    /// AppHang-класс на деградированном пути (рестарт backend внутри recovery).
    /// Обязан быть заменён паттерном Wave 188 (Task.detached + callAsync).
    func test_ipc_call_is_off_main() throws {
        let src = try source("main+QuickReplace.swift")
        XCTAssertTrue(src.contains("Task.detached"),
                      "IPC обязан уходить off-main (паттерн Wave 188)")
        XCTAssertTrue(src.contains("callAsync"))
        XCTAssertFalse(src.contains("callWithRecovery"),
                       "sync-путь обязан быть удалён из файла целиком (AGENT-3)")
    }
}
```

- [ ] **Step 2: RED** — `cd native/KrabEarAgent && swift test --filter QuickReplaceWiringTests`. Ожидание: 3 FAIL (в текущем source нет `putToClipboard`/`new_text`/`Скопировано`/`Task.detached`, и есть `callWithRecovery`) — падение по причине «фича отсутствует», не по опечатке в helper'е.

- [ ] **Step 3: Реализация.** В `main+QuickReplace.swift` заменить в `presentAlertSheet`-completion весь блок от комментария `// IPC call: word replacement is fast…` до конца `catch` (строки ~70-104) на off-main вызов, и добавить новый `@MainActor`-метод обработки ответа:

```swift
            // Off-main IPC (AGENT-3, паттерн Wave 188 из main+Bookmarks.swift):
            // прежний sync callWithRecovery на main из completion алерта на
            // деградированном пути (restartIfDead = полный цикл рестарта
            // backend внутри recovery) блокировал главный поток. IPCClient —
            // Sendable, AgentAppDelegate (@MainActor self) — нет, поэтому
            // локальный `let`. Трейд-офф паттерна (принят в миграциях
            // Bookmarks/HotkeyRecording): без restartIfDead-recovery — при
            // мёртвом backend разовое действие показывает ошибку, самолечение
            // остаётся за BackendSupervisor/HealthMonitor.
            let ipc = self.ipcClient
            Task.detached { [weak self] in
                do {
                    let response = try await ipc.callAsync(
                        method: "replace_word_in_last_transcript",
                        params: ["old_word": oldWord, "new_word": newWord],
                        timeoutSec: 10
                    )
                    let result = response["result"] as? [String: Any] ?? [:]
                    await MainActor.run { [weak self] in
                        self?.handleReplaceWordResponse(result, oldWord: oldWord, newWord: newWord)
                    }
                } catch {
                    await MainActor.run { [weak self] in
                        self?.showReplaceResult(success: false, message: "Ошибка IPC: \(error.localizedDescription)")
                    }
                }
            }
```

Новый метод (в том же extension, перед `showReplaceResult`; тело success/failure-веток — прежнее, добавлена ТОЛЬКО клипборд-вставка):

```swift
    /// Обработка ответа replace_word_in_last_transcript на main. Вынесена из
    /// completion алерта при переводе IPC off-main (S64 re-paste, спека
    /// 2026-07-19): success/failure-маппинг прежний, добавлено копирование
    /// new_text в буфер.
    @MainActor
    private func handleReplaceWordResponse(_ result: [String: Any], oldWord: String, newWord: String) {
        let ok = result["ok"] as? Bool ?? false
        let count = result["replaced_count"] as? Int ?? 0
        let error = result["error"] as? String

        if ok {
            let autoLearned = result["auto_learned"] as? Bool ?? false
            let noun = count == 1 ? "вхождение" : (count < 5 ? "вхождения" : "вхождений")
            var message = "Заменено \(count) \(noun): «\(oldWord)» → «\(newWord)»."
            // S64 re-paste: исправленный ПОЛНЫЙ текст — в буфер, чтобы
            // пользователь мог сразу вставить его в целевое приложение
            // (выделив там старый текст), без ручного похода в историю.
            // Только success-ветка — буфер пользователя не перезаписывается
            // на ошибках. Privacy: backend в privacy mode отказывает ДО
            // чтения истории, new_text сюда не доходит.
            if let newText = result["new_text"] as? String, !newText.isEmpty {
                pasteService.putToClipboard(newText)
                message += " Скопировано — ⌘V."
            }
            if autoLearned {
                message += " Слово «\(newWord)» выучено в словарь STT."
            }
            showReplaceResult(success: true, message: message)
        } else {
            let reason: String
            switch error {
            case "word_not_found":    reason = "Слово «\(oldWord)» не найдено в последней записи."
            case "no_recent_history": reason = "История пуста."
            case "item_not_found":    reason = "Запись не найдена."
            case "missing_words":     reason = "Укажите оба слова."
            default:                  reason = error ?? "Неизвестная ошибка."
            }
            showReplaceResult(success: false, message: reason)
        }
    }
```

Также обновить шапку-комментарий файла (строки 1-5): добавить строку про re-paste («после замены исправленный текст копируется в буфер — S64, спека 2026-07-19») — комментарий описывает поведение, которого раньше не было.

- [ ] **Step 4: GREEN** — `swift test --filter QuickReplaceWiringTests` (3 PASS), затем полный `swift test` (все зелёные, ~1252) и `swift build -c release`.

- [ ] **Step 5: Commit** — `git add native/KrabEarAgent/Sources/KrabEarAgent/main+QuickReplace.swift native/KrabEarAgent/Tests/KrabEarAgentTests/QuickReplaceWiringTests.swift && git commit` (`feat(quick-replace): S64 re-paste — new_text в буфер + off-main IPC (Wave 188 паттерн)`).

### Task 2: Гейты + мерж + деплой (стандартный ритуал)

- [ ] **Step 1:** Личный построчный гейт диффа против спеки (single-task волна — целостный adversarial-ревью отдельной моделью не требуется, дифф < 100 строк; гейт по чеклисту: буфер только в ok-ветке, callWithRecovery отсутствует, глифы установленные, error-пути byte-идентичны прежним).
- [ ] **Step 2:** Parity-бинари: `swift build -c release` → `cp` в оба места (`Krab Ear.app/Contents/MacOS/KrabEarAgent` + `native/runtime/KrabEarAgent`) → `codesign -s - -f` оба → commit.
- [ ] **Step 3:** Мерж: rebase на актуальный `codex/krab-ear-v2` при необходимости → stash-танец вокруг чужого WIP `telegram_bridge.py` в общем чекауте → `git merge --ff-only` → stash pop.
- [ ] **Step 4:** Деплой: `pgrep -x KrabEarAgent -l` (chip-риск SingleInstanceGuard) → kill старого → `open` СТРОГО по абсолютному пути `/Users/pablito/Antigravity_AGENTS/Krab Ear/Krab Ear.app` (урок worktree-shadow 2026-07-19) → сверка `ps -p <pid> -o comm=` с ожидаемым путём + IPC `ping`.
- [ ] **Step 5:** Живой смок: синтетический keystroke НЕ триггерит global monitor (урок C3b) и меню-пункта у QuickReplace нет — физическое нажатие Cmd+Shift+R остаётся за владельцем при первом использовании; зафиксировать это ограничение в отчёте волны. Доки: запись в `docs/ROADMAP-2026H2.md` журнал (+ пометить S64 закрытой в реконсиляции, если упоминается).
