# C3b: Плавающее мини-окно скретчпада Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Плавающая панель скретчпада: live-текст текущей быстрой заметки + список последних заметок + кнопки Старт/Стоп, Копировать, →Notes.

**Architecture:** Спека `docs/superpowers/specs/2026-07-16-c3-quick-capture-design.md` §4. База — паттерн `MeetingLivePanelController.swift` (панельные свойства, render, SSE через общий `SSESessionDelegate`), НО проще: состояние панели ЛОКАЛЬНОЕ (`quickCaptureActive` делегата, старт/стоп — методы C3a), партиалы — SSE `realtime.partial_transcript`; SSE умер → просто нет live-текста (деградация приемлема, panel-state от backend-событий не зависит → silence-watchdog и sticky-выходы панели встречи тут НЕ нужны — зафиксировано осознанно).

**Prerequisite:** волна C3a смёржена (использует `onQuickCaptureToggle`, `quickCaptureActive`, коллекцию, `sendQuickCaptureCopies`-настройки).

**Tech Stack:** Swift 6; SSE-образцы: `RealtimeOverlayController+PartialSSE.swift:61-64` (фильтр `realtime.partial_transcript,realtime.final_transcript`), `MeetingLivePanelController.swift` (URLSession+SSESessionDelegate, `Task { @MainActor }`).

## Жёсткие правила

Те же, что в плане C3a (worktree + ветка `feature/c3b-scratchpad-panel`, IPC off-main, глиф-гейт, без runModal, KrabEarTheme-токены). Плюс уроки C2c, зашитые в спеку §4: `.closable` обязателен, `isReleasedWhenClosed = false`, off-screen guard, у КАЖДОЙ точки показа есть путь закрытия.

### Task 1: QuickCapturePanelController — панель + рендер + test-hooks

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/QuickCapturePanelController.swift`
- Test (create): `native/KrabEarAgent/Tests/KrabEarAgentTests/QuickCapturePanelTests.swift`

- [ ] **Step 1: Падающие тесты** (инстанцирование контроллера в тестах — образец `MeetingLivePanelTests.swift`, там же helper'ы):

```swift
final class QuickCapturePanelTests: XCTestCase {
    @MainActor func test_panel_properties() {
        let c = QuickCapturePanelController()
        XCTAssertTrue(c.window?.styleMask.contains(.closable) ?? false)
        XCTAssertFalse(c.window?.isReleasedWhenClosed ?? true)
        XCTAssertEqual(c.window?.level, .floating)
    }
    @MainActor func test_render_idle_and_recording() {
        let c = QuickCapturePanelController()
        c._testSetRecording(false)
        XCTAssertTrue(c._testStatusText.contains("не идёт") || c._testStatusText.contains("Готов"))
        c._testSetRecording(true)
        XCTAssertTrue(c._testHeaderTimerActive)
    }
    @MainActor func test_partial_appends_to_live_text() {
        let c = QuickCapturePanelController()
        c._testSetRecording(true)
        c._testIngestPartial("привет мир")
        XCTAssertTrue(c._testLiveText.contains("привет мир"))
    }
    @MainActor func test_notes_list_renders() {
        let c = QuickCapturePanelController()
        c._testSetNotes([["text": "первая заметка", "ts": "2026-07-16T10:00:00"]])
        XCTAssertEqual(c._testNoteRowCount, 1)
    }
}
```

- [ ] **Step 2: Реализация.** Скопируй каркас с `MeetingLivePanelController.swift` (НЕ наследуй — отдельный класс): NSPanel `.nonactivatingPanel/.hudWindow/.utilityWindow/.titled/.closable/.resizable`, `level = .floating`, `isReleasedWhenClosed = false`, `delegate = self`, minSize 300×320, positionKey `KrabEar_QuickCapturePanelPosition`, off-screen guard (перенос ≥80% алгоритма оттуда). Разделы UI (KrabEarTheme-токены): header (статус ⏺/таймер), зона live-текста (NSTextView read-only в NSScrollView), список заметок (vertical stack, до 7 строк: текст-превью + кнопка «Копировать»), нижний ряд кнопок: ⏺/■ (зовёт `appDelegate.onQuickCaptureToggle()`), «Копировать всё» (live-текст → NSPasteboard + toast), «→ Notes» (виден только при включённом `quick_capture_send_to_notes`; шлёт текст через `create_apple_note` off-main). Test-hooks `_test*` — по образцу панели встречи (обёртки над внутренним состоянием, `@MainActor`).
- [ ] **Step 3:** `swift test --filter QuickCapturePanel` зелёный; `swift build -c release`; коммит.

### Task 2: SSE-партиалы + проводка точек входа

**Files:**
- Modify: `QuickCapturePanelController.swift` (SSE), `main+QuickCapture.swift` (показ панели + submenu-пункт), `main.swift` (property `quickCapturePanelController`), `HistoryPanelController+QuickCaptureSettings.swift` (+чекбокс «Показывать скретчпад при записи», ключ `quick_capture_show_panel` → `DEFAULT_SETTINGS`+`_BOOL_FIELDS`+мини-тест py как в C3a Task 3)
- Test: `QuickCapturePanelTests.swift` + `QuickCaptureWiringTests.swift` (дополнить)

- [ ] **Step 1: Падающие source-contract тесты:** `main+QuickCapture.swift` содержит `ensureQuickCapturePanelController` и показ панели за гардом `quick_capture_show_panel`; submenu содержит пункт «Открыть скретчпад»; `QuickCapturePanelController.swift` содержит `/v1/events?filter=realtime.partial_transcript` и `windowWillClose`.
- [ ] **Step 2: Реализация.** SSE: подписка на `http://127.0.0.1:5005/v1/events?filter=realtime.partial_transcript,realtime.final_transcript` через общий `SSESessionDelegate` (образец панели встречи; парсинг ПЛОСКОГО payload — урок C2c: сверься с `event_bus.py`-сериализатором, тест корми wire-форматом). SSE стартует при показе панели, останавливается в `windowWillClose` (запись при этом ЖИВЁТ — закрытие панели ≠ стоп заметки). `partial` заменяет live-текст, `final` фиксирует строку. Точки входа: (а) `onQuickCaptureToggle` при старте заметки — если `quick_capture_show_panel` включён (живое чтение настроек, как `sendQuickCaptureCopies`) → `ensureQuickCapturePanelController().show()`; (б) пункт подменю «Открыть скретчпад». При показе — обновить список заметок (`get_collection_items` off-main).
- [ ] **Step 3:** Полный `swift test` + сборка + py-тест ключа; коммит.

### Task 3: Гейты + живой смок + agy-бриф

- [ ] **Step 1:** `swift build -c release && swift test`; flake8/parity на py-тест; `make audit-all`; глиф-гейт диффа.
- [ ] **Step 2: Живой смок:** задеплоить dev-бинарь → включить `quick_capture_show_panel` → Cmd+Shift+N: панель всплыла, live-текст идёт при диктовке (`say -v Milena` в колонки для автономного прогона), стоп → заметка появилась в списке панели и в подменю; закрыть панель крестиком во время записи → запись продолжается, стоп из меню работает; повторное открытие панели не крешит (isReleasedWhenClosed-урок).
- [ ] **Step 3: agy-бриф** на визуальную полировку панели — по образцу `docs/design-briefs/2026-07-16-meeting-panel-polish.md` (инварианты: test-hooks, панельные свойства, только Theme-токены, глифы, оба тест-фильтра зелёные). Бриф пишет Claude, исполняет agy, дифф гейтится построчно.
- [ ] **Step 4:** Отчёт STATUS/headSha/смок-итоги.
