# C3a: Quick Capture (меню-бар + хоткей + авто-отправка) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Быстрая голосовая заметка: хоткей/пункт меню-бара → запись БЕЗ какой-либо вставки в активное окно → history item в коллекции «Быстрые заметки» + подменю последних заметок + opt-in отправка в Apple Notes / Obsidian.

**Architecture:** Спека `docs/superpowers/specs/2026-07-16-c3-quick-capture-design.md` (читать §2-§3 перед стартом). Заметка переиспользует штатный record-флоу (те же `start_recording`/`stop_recording` IPC и `start/stopRealtimeOverlayPolling` — там живут wake-word пауза и оверлей), с ДВУМЯ отличиями: streaming-paste не подключается (точечный гард) и результат уходит не в paste-пайплайн, а в свой обработчик (коллекция + toast + отправки).

**Tech Stack:** Swift 6 (native/KrabEarAgent), unittest-style swift test (source-contract паттерн `MeetingPanelWiringTests.swift`), существующие IPC: `start_recording`, `stop_recording`, `list_collections`, `create_collection`, `add_to_collection`, `get_collection_items`, `set_paste_status`, `create_apple_note`, `set_settings`/`get_settings`.

---

## Жёсткие правила для воркера

- Repo `/Users/pablito/Antigravity_AGENTS/Krab Ear`, база `codex/krab-ear-v2`, изолированный worktree, первым действием `git checkout -b feature/c3a-quick-capture`.
- IPC СТРОГО off-main (`DispatchQueue.global(qos:).async { ipcClient.call(...) }` или `Task.detached` + `callAsync`) — синхронный IPC на main = AppHang-класс AGENT-3.
- Глиф-гейт: любой новый non-ASCII символ/эмодзи в UI-строках — grep по `native/KrabEarAgent/Sources`; 0 вхождений → заменить установленным или SF Symbol (CoreText-hang класс AGENT-J/M).
- Никаких `runModal()` (CI-тест это ловит). Тосты — `BackendToast.shared.show(message)` (severity-API ErrorToastPresenter НЕ трогать).
- Сборка: `cd native/KrabEarAgent && swift build -c release`; тесты: `swift test --filter QuickCapture`.
- Ключи IPC — буква-в-букву из этого плана (класс #1791: выдуманный ключ ловится только гейтом).
- Коммиты с трейлером `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Якоря существующего кода (проверены 2026-07-16)

| Якорь | Файл:строка |
|---|---|
| `handleRecordToggleRequest()` — вход диктовки, debounce+`isProcessing` guard | `main+HotkeyRecording.swift:13` |
| `startRecording()` / `stopRecording()` | `main+HotkeyRecording.swift:91,145` |
| Поля результата stop: `status, history_id, text, ...`; dup-ветка: `skipped=duplicate, history_id=null` | `main+HotkeyRecording.swift:180-187` |
| `handleTranscriptionResult(text:historyId:)` — ПАСТ-пайплайн (заметке НЕ вызывать) | `main+PasteHandling.swift:13` |
| `startRealtimeOverlayPolling()`: стр.17 `streamingPasteController?.recordingDidStart()` (безусловно!), стр.20 `wakeWordPoller?.pause(.recording)` | `main+RealtimeOverlay.swift:13` |
| `stopRealtimeOverlayPolling()`: стр.53 `recordingDidStop()`, стр.54 `resume(.recording)` | `main+RealtimeOverlay.swift:38` |
| `isRecording`/`isProcessing` флаги делегата | `main.swift:160-161` |
| Ad-hoc хоткей-образец (Cmd+Shift+P, keyCode 35) — ⚠️ НЕ копировать его баг: монитор не сохраняется → не снять | `main+QuickPresets.swift:25` |
| Меню: `rebuildStatusMenu()` (полная пересборка), recordItem-паттерн | `main+StatusMenu.swift:176,196-203` |
| `menuWillOpen` → refresh (образец) | `main+MenuBarRecap.swift:306-308` |
| Settings-секция образец: `buildHotkeySection()`, хелперы `makeSettingRow`/`makeSwitchRow` | `HistoryPanelController+Settings.swift:1056,689,814` |
| Чекбокс→set_settings off-main образец | `HistoryPanelController+GigaAMToggle.swift:32-79` |
| Source-contract тест-образец | `native/KrabEarAgent/Tests/KrabEarAgentTests/MeetingPanelWiringTests.swift` |

Backend-факты: `add_to_collection {collection_name, item_id}`; `create_collection {name, description?}` (падает `RuntimeError` без name; при существующей — см. Task 1 Step 3 идемпотентный паттерн list→create); `get_collection_items {collection_name}` → `{items, count, collection_name}` (privacy внутри); `set_paste_status` (service.py:1712) — параметры сверить grep'ом `_handle_set_paste_status` в `KrabEar/backend/service.py` (ожидаемо `{item_id, status}` — подтверди и используй фактические); `create_apple_note {title, body, folder?}` → `{ok, note_id?, error?}` (privacy-гейт УЖЕ внутри); Obsidian: точный IPC найди grep'ом `obsidian` по `self\.` в `KrabEar/backend/service.py` (ожидаемо форс-синк; если per-item метода нет — зови общий sync-now метод).

Имя коллекции — константа: `Быстрые заметки`.

---

### Task 1: Флоу заметки + подавление streaming-paste + взаимные гарды

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/main+QuickCapture.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` (флаг делегата), `main+RealtimeOverlay.swift` (гард стр.17/53), `main+HotkeyRecording.swift` (гард входа), `main+MeetingPanel.swift` (гард старта встречи)
- Test (create): `native/KrabEarAgent/Tests/KrabEarAgentTests/QuickCaptureWiringTests.swift`

- [ ] **Step 1: Source-contract тесты (падающие)** — по образцу MeetingPanelWiringTests (тест читает исходник как текст и проверяет наличие контрактных строк):

```swift
import XCTest

/// C3a source-contract: инварианты проводки быстрой заметки (спека §2a).
final class QuickCaptureWiringTests: XCTestCase {
    private func source(_ name: String) throws -> String {
        // helper скопируй из MeetingPanelWiringTests (поиск файла от #filePath вверх)
        try loadSource(named: name)
    }

    func test_streamingPaste_guarded_by_quickCapture() throws {
        let src = try source("main+RealtimeOverlay.swift")
        // recordingDidStart обязан быть за гардом quickCaptureActive
        XCTAssertTrue(src.contains("if !quickCaptureActive"),
                      "streaming-paste должен подавляться в режиме заметки")
    }

    func test_quickCapture_never_calls_paste_pipeline() throws {
        let src = try source("main+QuickCapture.swift")
        XCTAssertFalse(src.contains("handleTranscriptionResult"),
                       "заметка не должна входить в paste-пайплайн")
        XCTAssertFalse(src.contains("pasteToFrontmostApp"))
    }

    func test_dictation_guarded_against_quickCapture() throws {
        let src = try source("main+HotkeyRecording.swift")
        XCTAssertTrue(src.contains("quickCaptureActive"),
                      "Right Option обязан отвергаться при активной заметке")
    }

    func test_meeting_guarded_against_quickCapture() throws {
        let src = try source("main+MeetingPanel.swift")
        XCTAssertTrue(src.contains("quickCaptureActive"))
    }

    func test_quickCapture_uses_overlay_polling_hooks() throws {
        let src = try source("main+QuickCapture.swift")
        XCTAssertTrue(src.contains("startRealtimeOverlayPolling()"),
                      "wake-word пауза/оверлей живут в этом хуке — обязателен")
        XCTAssertTrue(src.contains("stopRealtimeOverlayPolling()"))
        XCTAssertTrue(src.contains("set_paste_status"))
        XCTAssertTrue(src.contains("add_to_collection"))
    }
}
```

- [ ] **Step 2: Прогнать — убедиться, что падают** (`swift test --filter QuickCaptureWiring`; ожидаемо: файла main+QuickCapture.swift нет / гардов нет).

- [ ] **Step 3: Реализация**

1. `main.swift` рядом с `var isRecording = false` (стр.160): `var quickCaptureActive = false`.
2. `main+RealtimeOverlay.swift`: обе строки streaming-paste за гард:
   ```swift
   if !quickCaptureActive { streamingPasteController?.recordingDidStart() }
   ...
   if !quickCaptureActive { streamingPasteController?.recordingDidStop() }
   ```
   (wake-word pause/resume и оверлей — БЕЗ гарда, они нужны заметке).
3. `main+HotkeyRecording.swift:13` в начало `handleRecordToggleRequest()`:
   ```swift
   if quickCaptureActive {
       BackendToast.shared.show("Идёт быстрая заметка — сначала завершите её")
       return
   }
   ```
4. `main+MeetingPanel.swift` в начало start-ветки `onMeetingPanelToggle()` — тот же гард с тем же текстом-паттерном («Идёт быстрая заметка…»).
5. Новый `main+QuickCapture.swift`:

```swift
import AppKit

/// C3a (спека 2026-07-16-c3-quick-capture-design §2-§3): быстрая голосовая
/// заметка — запись БЕЗ вставки в активное окно; результат в history +
/// коллекцию «Быстрые заметки», opt-in дублирование в Notes/Obsidian.
extension AgentAppDelegate {
    static let quickCaptureCollectionName = "Быстрые заметки"

    @objc func onQuickCaptureToggle() {
        if quickCaptureActive { stopQuickCapture(); return }
        // взаимное исключение: диктовка/обработка. Встреча/чужая запись
        // отсекается ответом backend (recorder занят → status != ok).
        if isRecording || isProcessing {
            BackendToast.shared.show("Уже идёт запись — заметка недоступна")
            return
        }
        quickCaptureActive = true
        rebuildStatusMenu()
        Task.detached { [weak self] in
            guard let self else { return }
            do {
                let resp = try await self.ipcClient.callAsync(method: "start_recording", params: [:], timeoutSec: 10)
                let status = resp["status"] as? String ?? ""
                await MainActor.run {
                    if status == "ok" || status == "already_recording" {
                        self.startRealtimeOverlayPolling() // wake-word пауза + оверлей; streaming-paste подавлен гардом
                        BackendToast.shared.show("Быстрая заметка: запись…")
                    } else {
                        self.quickCaptureActive = false
                        self.rebuildStatusMenu()
                        BackendToast.shared.show("Не удалось начать заметку")
                    }
                }
            } catch {
                await MainActor.run {
                    self.quickCaptureActive = false
                    self.rebuildStatusMenu()
                    BackendToast.shared.show("Не удалось начать заметку")
                }
            }
        }
    }

    func stopQuickCapture() {
        stopRealtimeOverlayPolling()
        Task.detached { [weak self] in
            guard let self else { return }
            defer {
                Task { await MainActor.run {
                    self.quickCaptureActive = false
                    self.rebuildStatusMenu()
                } }
            }
            do {
                let resp = try await self.ipcClient.callAsync(method: "stop_recording", params: [:], timeoutSec: 120)
                await self.handleQuickCaptureResult(resp)
            } catch {
                await MainActor.run { BackendToast.shared.show("Ошибка завершения заметки") }
            }
        }
    }

    private func handleQuickCaptureResult(_ resp: [String: Any]) async {
        let status = resp["status"] as? String ?? ""
        if resp["skipped"] as? String == "duplicate" {
            await MainActor.run { BackendToast.shared.show("Заметка совпала с недавней записью — пропущена") }
            return
        }
        guard status == "ok", let historyId = resp["history_id"] as? String, !historyId.isEmpty else {
            await MainActor.run { BackendToast.shared.show("Заметка не сохранилась") }
            return
        }
        // 1) коллекция (лениво создать), 2) нейтральный paste_status, 3) отправки
        _ = try? await ipcClient.callAsync(
            method: "create_collection",
            params: ["name": Self.quickCaptureCollectionName,
                     "description": "Быстрые голосовые заметки"], timeoutSec: 10)
        _ = try? await ipcClient.callAsync(
            method: "add_to_collection",
            params: ["collection_name": Self.quickCaptureCollectionName, "item_id": historyId],
            timeoutSec: 10)
        _ = try? await ipcClient.callAsync(
            method: "set_paste_status",
            params: ["item_id": historyId, "status": "skipped"], timeoutSec: 10)
        await sendQuickCaptureCopies(text: resp["text"] as? String ?? "", historyId: historyId)
        await MainActor.run { BackendToast.shared.show("Заметка сохранена") }
    }
}
```

⚠️ Уточнения для воркера: (а) сигнатуру `callAsync` сверь с `IPCClient.swift:321`; (б) `create_collection` при существующей коллекции кидает ошибку ДРУГОГО класса, чем «нет name» — если ошибка «уже существует» не отличается от прочих, сначала `list_collections` и создавай только при отсутствии (паттерн bulk-действий в `HistoryPanelController+ExportSelection.swift`); (в) параметры `set_paste_status` сверь с `_handle_set_paste_status`; (г) `sendQuickCaptureCopies` — заглушка `func sendQuickCaptureCopies(text: String, historyId: String) async {}` в этой задаче, наполняется в Task 3.

6. Бейдж «skipped» в истории: `HistoryPanelController+History.swift:1160` — в switch добавить:
   ```swift
   case "skipped":
       statusColor = KrabEarTheme.Colors.textSecondary
       statusSymbol = "note.text"
   ```

- [ ] **Step 4: Тесты + сборка** — `swift test --filter QuickCaptureWiring` (все зелёные), `swift build -c release`.
- [ ] **Step 5: Коммит** (`feat(quick-capture): C3a Task 1 — флоу заметки, streaming-paste гард, взаимные гарды`).

---

### Task 2: Хоткей + пункт меню + подменю последних заметок

**Files:**
- Modify: `main+QuickCapture.swift` (хоткей-монитор + подменю), `main+StatusMenu.swift` (пункты), `main.swift` (старт монитора в `applicationDidFinishLaunching`, рядом с `selectionTranslator.start()` ~стр.434-439)
- Test: `QuickCaptureWiringTests.swift` (дополнить)

- [ ] **Step 1: Падающие тесты**

```swift
    func test_hotkey_monitor_is_stored_and_stoppable() throws {
        let src = try source("main+QuickCapture.swift")
        // урок main+QuickPresets.swift: монитор ОБЯЗАН сохраняться
        XCTAssertTrue(src.contains("quickCaptureHotkeyMonitor"))
        XCTAssertTrue(src.contains("removeMonitor"))
    }

    func test_status_menu_has_quick_capture_items() throws {
        let src = try source("main+StatusMenu.swift")
        XCTAssertTrue(src.contains("onQuickCaptureToggle"))
        XCTAssertTrue(src.contains("Быстрые заметки"))
    }

    func test_menu_open_refreshes_quick_notes() throws {
        let src = try source("main+MenuBarRecap.swift")
        XCTAssertTrue(src.contains("refreshQuickNotesSubmenu"),
                      "menuWillOpen обязан обновлять подменю заметок")
    }
```

- [ ] **Step 2: Реализация**

1. Хоткей-монитор в `main+QuickCapture.swift` (образец `SelectionTranslator.swift:126` — с СОХРАНЕНИЕМ монитора):
   ```swift
   // main.swift: var quickCaptureHotkeyMonitor: Any?
   func startQuickCaptureHotkeyMonitor() {
       guard quickCaptureHotkeyMonitor == nil else { return }
       quickCaptureHotkeyMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
           guard let self,
                 event.modifierFlags.intersection(.deviceIndependentFlagsMask) == [.command, .shift],
                 event.keyCode == 45 /* kVK_ANSI_N */ else { return }
           DispatchQueue.main.async { self.onQuickCaptureToggle() }
       }
   }
   func stopQuickCaptureHotkeyMonitor() {
       if let m = quickCaptureHotkeyMonitor { NSEvent.removeMonitor(m); quickCaptureHotkeyMonitor = nil }
   }
   ```
   Комбинация v1 фиксированная Cmd+Shift+N (настройка-дропдаун — Task 3); монитор стартует в `applicationDidFinishLaunching`, снимается в `applicationWillTerminate` (найди существующий teardown-блок).
2. `main+StatusMenu.swift` после recordItem (стр.~203):
   - пункт `quickCaptureActive ? "Остановить заметку" : "Быстрая заметка"`, action `#selector(onQuickCaptureToggle)`, `keyEquivalent: "n"` + `.keyEquivalentModifierMask = [.command, .shift]`, `isEnabled = !isProcessing && !(isRecording && !quickCaptureActive)`;
   - пункт «Быстрые заметки» с пустым submenu, ссылку сохрани: `self.quickNotesSubmenu = sub` (property в main.swift).
3. Подменю: в `main+MenuBarRecap.swift:306` `menuWillOpen` добавить `refreshQuickNotesSubmenu()`; реализация в `main+QuickCapture.swift`:
   ```swift
   func refreshQuickNotesSubmenu() {
       DispatchQueue.global(qos: .userInitiated).async { [weak self] in
           guard let self,
                 let resp = try? self.ipcClient.call(method: "get_collection_items",
                     params: ["collection_name": Self.quickCaptureCollectionName]),
                 let items = resp["items"] as? [[String: Any]] else { return }
           let latest = Array(items.suffix(7).reversed())
           DispatchQueue.main.async { self.rebuildQuickNotesSubmenu(latest) }
       }
   }
   ```
   `rebuildQuickNotesSubmenu`: для каждого item пункт «первые 40 символов… · HH:mm» (`text`/`ts` поля item — сверь фактические ключи item-словаря по `get_collection_items` ответу), action → копировать полный текст в `NSPasteboard.general` + toast «Скопировано»; representedObject = полный текст; последним — separator + «Показать все…» → `showHistoryPanel()` (найди существующий селектор открытия панели истории в main+StatusMenu.swift — historyItem action). Пустая коллекция/privacy → один disabled пункт «Заметок пока нет».
- [ ] **Step 3: Тесты + сборка зелёные; коммит** (`feat(quick-capture): C3a Task 2 — хоткей Cmd+Shift+N, пункт меню, подменю заметок`).

---

### Task 3: Настройки + авто-отправка Notes/Obsidian

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+QuickCaptureSettings.swift`
- Modify: `main+QuickCapture.swift` (наполнить `sendQuickCaptureCopies`), `HistoryPanelController+Settings.swift` (вставить секцию в общий стек настроек — найди место, где добавляются `buildHotkeySection()` и сиблинги), `KrabEar/backend/settings_validator.py` (+2 ключа в `_BOOL_FIELDS`), `KrabEar/core/config.py` (`DEFAULT_SETTINGS`: `quick_capture_send_to_notes: False`, `quick_capture_obsidian_sync: False`)
- Test: `QuickCaptureWiringTests.swift` + `KrabEar/tests/test_settings_quick_capture_C3a.py` (новый, 2 ключа в валидаторе/дефолтах)

- [ ] **Step 1: Падающие тесты**

Python (`test_settings_quick_capture_C3a.py`): `DEFAULT_SETTINGS["quick_capture_send_to_notes"] is False`, `..._obsidian_sync is False`, оба ключа в `settings_validator._BOOL_FIELDS`. Swift: `sendQuickCaptureCopies` содержит `create_apple_note` и obsidian-вызов; секция настроек существует (`buildQuickCaptureSection`).

- [ ] **Step 2: Реализация**

1. Backend: 2 ключа в `DEFAULT_SETTINGS` + `_BOOL_FIELDS` (образец — соседние bool-ключи; ubuntu-parity прогнать на новый тест-файл).
2. `sendQuickCaptureCopies`: читает НАСТРОЙКИ ЖИВЬЁМ (`get_settings` off-main — не кэш агента, чтобы чекбокс действовал сразу):
   - notes включён → `create_apple_note {title: первые ~60 символов первой строки, body: полный текст, folder: "Krab Ear"}`; ответ `ok:false` → toast с `user_msg`/`error` (privacy-гейт backend'а сам вернёт отказ в privacy-mode);
   - obsidian включён → точный IPC из grep'а (см. «Backend-факты»); нет vault'а/выключен синк — молча пропустить (ответ backend'а скажет).
3. Settings-секция `buildQuickCaptureSection()` (образцы: `buildHotkeySection` для структуры, `makeSwitchRow` для чекбоксов, GigaAMToggle для off-main записи): два чекбокса («Дублировать в Apple Notes», «Синхронизировать в Obsidian») + дропдаун «Хоткей заметки» (`makeSettingRow` + NSPopUpButton) из трёх фикс-комбинаций: «⌘⇧N» (`cmd_shift_n`, default), «⌘⌥N» (`cmd_opt_n`), «⌃⇧N» (`ctrl_shift_n`) — ключ `quick_capture_hotkey` (string) в `DEFAULT_SETTINGS` (+ allowlist-валидация значения в `settings_validator.py` по образцу соседних enum-ключей; py-тест дополни третьим ключом). Монитор из Task 2 читает комбинацию при старте: расширь `startQuickCaptureHotkeyMonitor()` маппингом `cmd_shift_n → ([.command,.shift], 45)`, `cmd_opt_n → ([.command,.option], 45)`, `ctrl_shift_n → ([.control,.shift], 45)`; выбор в дропдауне → `set_settings` off-main → `stopQuickCaptureHotkeyMonitor()` + `startQuickCaptureHotkeyMonitor()` (пере-арм на новую комбинацию).
- [ ] **Step 3: Прогоны** — swift build/test; `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_settings_quick_capture_C3a.py -v -p no:cacheprovider`; `scripts/pre_merge_py312_check.sh KrabEar/tests/test_settings_quick_capture_C3a.py`.
- [ ] **Step 4: Коммит** (`feat(quick-capture): C3a Task 3 — настройки + отправка в Notes/Obsidian`).

---

### Task 4: Гейты волны + живой смок

- [ ] **Step 1:** Полные прогоны: `swift build -c release && swift test` (весь набор); flake8 на новые py-файлы; `make audit-all`.
- [ ] **Step 2: Живой смок (обязателен, DoD спеки §7):** собрать бинарь, задеплоить в dev-инстанс по ритуалу проекта (`build_and_deploy.command` или ручной cp+codesign по CLAUDE.md), затем: открыть TextEdit с фокусом в документе → Cmd+Shift+N → надиктовать фразу (`say -v Milena "проверка быстрой заметки"` в колонки, если автономно) → Cmd+Shift+N. Проверить: (а) в TextEdit НЕ появилось НИ ОДНОГО символа (ни партиалов, ни финала — гард streaming-paste работает); (б) заметка видна в подменю «Быстрые заметки» и копируется кликом; (в) при включённом чекбоксе Notes появилась заметка в Notes.app; (г) во время заметки Right Option отвергается тостом; (д) `paste_status` item'а = «skipped» (нейтральный бейдж в истории).
- [ ] **Step 3:** Отчёт STATUS/headSha/итоги смока (скриншоты приветствуются) + NOTE-список отклонений от спеки.
