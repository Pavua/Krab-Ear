# C2c «Swift-панель встречи» Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Плавающая панель «Встреча»: живые action items, чипы спикеров и хвост транскрипта во время идущей встречи; старт/повышение из меню-бара и панели истории; финализация в существующий отчёт.

**Architecture:** Спека `docs/superpowers/specs/2026-07-10-c2-live-meeting-overlay-design.md` §2.6-§2.7 **+ амендмент §2.7a** (привязки к прецедентам; канал = SSE `/v1/events` + poll-фоллбэк). Backend полностью готов (C2a+C2b): IPC `meeting_start/meeting_stop/get_meeting_live_state`, события `meeting.transcript_appended/items_updated/speakers_updated/finalizing/finished`, event-мост живой.

**Tech Stack:** Swift 6 toolchain / language mode v5, AppKit (NSPanel), SwiftPM; тесты XCTest (`swift test`).

**Конвенции проекта (ОБЯЗАТЕЛЬНЫ, CLAUDE.md):**
- IPC строго off-main (AGENT-3): канон — `HistoryPanelController+ExportSelection.swift:108-129` (`DispatchQueue.global(qos: .userInitiated).async` → `ipcClient.call` → `nonisolated(unsafe) let` → `DispatchQueue.main.async`).
- НИКАКОГО `runModal()` (CI-гейт `test_nsAlertRunModal_onlyInAllowlistedFiles`).
- Глифы: ТОЛЬКО SF Symbols/символы, уже встречающиеся в native/ (глиф-гейт AGENT-J/M: перед использованием нового глифа — `grep -rF "<глиф>" native/Sources`; 0 вхождений → взять другой).
- Тема: только токены `KrabEarTheme` (Colors/Typography/Metrics/Motion), компоненты `ThemeCardView`/`ThemeSecondaryButton`.
- Комментарии/докстринги по-русски. Визуальную полировку НЕ делать — только функциональная раскладка (полировка уйдёт agy-брифом после мержа, §2.7a п.5).
- Сборка/тесты из `native/KrabEarAgent/`: `swift build -c release` и `swift test --filter <TestClass>`.

---

## Карта файлов

| Файл | Что |
|---|---|
| `native/KrabEarAgent/Sources/KrabEarAgent/MeetingLivePanelController.swift` | НОВЫЙ: панель + рендер state + SSE/poll lifecycle |
| `native/KrabEarAgent/Sources/KrabEarAgent/main+MeetingPanel.swift` | НОВЫЙ: пункт меню «Встреча», владение панелью, @objc-хендлер |
| `native/KrabEarAgent/Sources/KrabEarAgent/main+StatusMenu.swift` | пункт меню в `rebuildStatusMenu()` |
| `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift` | кнопка «Встреча» в `topActionsRow` |
| `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+MeetingMode.swift` | экстракция `makeMeetingReportVC(from:)` + standalone-окно отчёта |
| `native/KrabEarAgent/Tests/KrabEarAgentTests/MeetingLivePanelTests.swift` | НОВЫЙ: unit панели/рендера/SSE-парсера/фоллбэка |
| `native/KrabEarAgent/Tests/KrabEarAgentTests/MeetingPanelWiringTests.swift` | НОВЫЙ: source-contract проводки |

Задачи строго последовательны (Task 2 расширяет файл Task 1; Task 3 использует API Task 1/2).

---

### Task 1: MeetingLivePanelController — панель + чистый рендер state

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/MeetingLivePanelController.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/MeetingLivePanelTests.swift` (создать)

**Прецеденты (ПРОЧИТАЙ перед кодом):** `ConversationStatusOverlay.swift` целиком (panel-boilerplate: styleMask/level/collectionBehavior/drag/savePosition/restorePosition/isOnScreen≥80%/test-hooks) и рендер карточек в `HistoryPanelController+ActionItems.swift`. Правило: НЕ изобретать panel-код — скопировать паттерн ConversationStatusOverlay с новым positionKey.

- [ ] **Step 1: Написать падающий тест** (`MeetingLivePanelTests.swift`)

```swift
import XCTest
@testable import KrabEarAgent

@MainActor
final class MeetingLivePanelTests: XCTestCase {

    private func makeState(
        active: Bool = true,
        transcriptTail: String = "обсуждаем релиз ",
        items: [[String: Any]] = [["text": "подготовить документацию", "priority": "high"]],
        decisions: [String] = ["релиз в четверг"],
        questions: [String] = [],
        speakers: [[String: Any]] = [
            ["label": "Спикер 1", "talk_sec": 17.1, "last_active_ts": Date().timeIntervalSince1970],
            ["label": "Спикер 2", "talk_sec": 14.8, "last_active_ts": Date().timeIntervalSince1970 - 95],
        ],
        degradedLLM: Bool = false,
        degradedDiar: Bool = false
    ) -> [String: Any] {
        [
            "ok": true, "active": active,
            "started_at": Date().timeIntervalSince1970 - 120,
            "transcript_len": 640, "transcript_tail": transcriptTail,
            "items": items, "decisions": decisions, "questions": questions,
            "speakers": speakers,
            "degraded": ["llm": degradedLLM, "diarization": degradedDiar],
            "last_updated_ts": Date().timeIntervalSince1970,
        ]
    }

    func test_panel_is_nonactivating_floating_draggable() {
        let c = MeetingLivePanelController()
        XCTAssertEqual(c._testPanelLevel, .floating)
        XCTAssertTrue(c._testPanelIsDraggable)
    }

    func test_render_active_state_populates_sections() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        XCTAssertEqual(c._testSpeakerChipCount, 2)
        XCTAssertEqual(c._testItemRowCount, 2)  // 1 item + 1 decision (questions пусто)
        XCTAssertTrue(c._testTranscriptTailText.contains("обсуждаем релиз"))
        XCTAssertFalse(c._testDegradedBadgeVisible)
        XCTAssertEqual(c._testUIState, .live)
    }

    func test_render_degraded_flags_show_badge() {
        let c = MeetingLivePanelController()
        c.render(state: makeState(degradedDiar: true))
        XCTAssertTrue(c._testDegradedBadgeVisible)
    }

    func test_render_inactive_state_switches_to_idle() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c.render(state: ["ok": true, "active": false])
        XCTAssertEqual(c._testUIState, .idle)
    }

    func test_render_privacy_state() {
        let c = MeetingLivePanelController()
        c.render(state: ["ok": true, "active": false, "privacy_mode_active": true])
        XCTAssertEqual(c._testUIState, .privacy)
    }

    func test_finalizing_state_is_sticky_until_finished() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c.enterFinalizing()
        XCTAssertEqual(c._testUIState, .finalizing)
        // Пока финализация не завершена, обычный active-рендер её НЕ сбивает
        c.render(state: makeState())
        XCTAssertEqual(c._testUIState, .finalizing)
        // inactive (запись остановлена) — тоже остаёмся в finalizing до finished/отчёта
        c.render(state: ["ok": true, "active": false])
        XCTAssertEqual(c._testUIState, .finalizing)
    }

    func test_speaker_chip_shows_staleness() {
        let c = MeetingLivePanelController()
        let old = Date().timeIntervalSince1970 - 200
        c.render(state: makeState(speakers: [["label": "Спикер 1", "talk_sec": 60.0,
                                              "last_active_ts": old]]))
        XCTAssertTrue(c._testSpeakerChipTitles[0].contains("Спикер 1"))
        // возраст данных отображается (точный формат не пинится — только факт наличия «с» или «мин»)
        XCTAssertTrue(c._testSpeakerChipTitles[0].contains("с") || c._testSpeakerChipTitles[0].contains("мин"))
    }
}
```

- [ ] **Step 2: Убедиться, что падает**

Run: `cd native/KrabEarAgent && swift test --filter MeetingLivePanelTests 2>&1 | tail -5`
Expected: compile error — `cannot find 'MeetingLivePanelController' in scope`.

- [ ] **Step 3: Реализация — каркас контроллера**

`MeetingLivePanelController.swift` (структура; panel-boilerplate копируй из ConversationStatusOverlay, здесь — контракт):

```swift
import AppKit

/// C2c (спека §2.7 + §2.7a): плавающая панель живой встречи.
/// Владение: AgentAppDelegate (main+MeetingPanel.swift). Панель показывает
/// live-состояние meeting-сессии backend'а; закрытие панели сессию НЕ трогает.
@MainActor
final class MeetingLivePanelController: NSObject {

    enum UIState: Equatable {
        case idle        // сессии нет
        case live        // сессия активна, рендерим state
        case finalizing  // meeting_stop отправлен, ждём отчёт (sticky)
        case privacy     // privacy_mode_active
    }

    private let panel: NSPanel
    private let positionKey = "KrabEar_MeetingLivePanelPosition"

    // --- секции UI (все на KrabEarTheme-токенах) ---
    private let headerTimerLabel = NSTextField(labelWithString: "00:00")
    private let degradedBadge = NSTextField(labelWithString: "деградация")
    private let speakersRow = NSStackView()
    private let itemsStack = NSStackView()
    private let transcriptTailLabel = NSTextField(wrappingLabelWithString: "")
    private let stopButton = ThemeSecondaryButton(title: "Завершить встречу")
    private let statusLabel = NSTextField(labelWithString: "")

    private(set) var uiState: UIState = .idle
    private var startedAt: TimeInterval?
    private var timerTick: Timer?

    override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 360, height: 520),
            styleMask: [.nonactivatingPanel, .hudWindow, .utilityWindow],
            backing: .buffered, defer: false)
        super.init()
        setupPanel()   // паттерн ConversationStatusOverlay: level/.floating,
                       // collectionBehavior, drag+savePosition, ThemeCardView-контент,
                       // restorePosition c isOnScreen ≥80%.
                       // ОТЛИЧИЕ от прецедента (§2.7): панель resizable —
                       // panel.styleMask.insert(.resizable); panel.minSize = NSSize(width: 300, height: 360)
        buildLayout()  // header / speakersRow / itemsStack(scroll) / tail / stopButton
                       // stopButton.target/action → requestStop() (тело придёт в Task 2;
                       // в Task 1 — пустая заглушка requestStop() { })
    }

    func show() { /* restorePosition + orderFront(nil); НЕ активирует приложение */ }
    func hide() { /* orderOut */ }

    /// Единственная точка входа данных: полный снапшот get_meeting_live_state
    /// ИЛИ склеенный из SSE-событий (Task 2). Чистая функция состояния → UI.
    func render(state: [String: Any]) {
        if uiState == .finalizing { return }  // sticky до отчёта/reset
        if (state["privacy_mode_active"] as? Bool) == true { setUIState(.privacy); return }
        guard (state["active"] as? Bool) == true else { setUIState(.idle); return }
        setUIState(.live)
        startedAt = state["started_at"] as? TimeInterval
        renderSpeakers(state["speakers"] as? [[String: Any]] ?? [])
        renderItems(items: state["items"] as? [[String: Any]] ?? [],
                    decisions: state["decisions"] as? [String] ?? [],
                    questions: state["questions"] as? [String] ?? [])
        transcriptTailLabel.stringValue = state["transcript_tail"] as? String ?? ""
        let degraded = state["degraded"] as? [String: Any] ?? [:]
        degradedBadge.isHidden = !((degraded["llm"] as? Bool ?? false)
                                   || (degraded["diarization"] as? Bool ?? false))
    }

    func enterFinalizing() { setUIState(.finalizing) }
    func resetToIdle() { setUIState(.idle) }   // после показа отчёта/ошибки

    // renderSpeakers: чип = "«label» · Xм Yс · N с назад" (staleness из last_active_ts);
    // renderItems: строки с префиксом типа — item "◦", decision "✓", question "?"
    //   (все три символа уже встречаются в native/ — проверь grep'ом, иначе замени
    //   на найденные аналоги); рендер-образец карточек — +ActionItems.swift.
    // updateTimer(): Timer 1с обновляет headerTimerLabel от startedAt (только .live).

    // --- test hooks (паттерн ConversationStatusOverlay) ---
    var _testPanelLevel: NSWindow.Level { panel.level }
    var _testPanelIsDraggable: Bool { panel.isMovableByWindowBackground }
    var _testUIState: UIState { uiState }
    var _testSpeakerChipCount: Int { speakersRow.arrangedSubviews.count }
    var _testSpeakerChipTitles: [String] { /* тексты чипов */ }
    var _testItemRowCount: Int { itemsStack.arrangedSubviews.count }
    var _testTranscriptTailText: String { transcriptTailLabel.stringValue }
    var _testDegradedBadgeVisible: Bool { !degradedBadge.isHidden }
}
```

Требования к реализации (сверяй с тестами Step 1):
- `setUIState(_:)` переключает видимость секций: `.idle` — заглушка «Встреча не идёт»; `.privacy` — «Privacy-режим»; `.finalizing` — статус «Финализирую…», stopButton disabled; `.live` — все секции.
- `render` при `.finalizing` — no-op (sticky, пинится тестом).
- Таймер header'а: `mm:ss` от `started_at`; при выходе из `.live` — инвалидировать.
- Никаких прямых IPC/сетевых вызовов в Task 1 — только чистый рендер (данные придут в Task 2).

- [ ] **Step 4: Тесты зелёные**

Run: `cd native/KrabEarAgent && swift test --filter MeetingLivePanelTests 2>&1 | tail -3`
Expected: PASS (7 тестов).

- [ ] **Step 5: Полная сборка**

Run: `cd native/KrabEarAgent && swift build -c release 2>&1 | tail -3`
Expected: Build complete.

- [ ] **Step 6: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/MeetingLivePanelController.swift native/KrabEarAgent/Tests/KrabEarAgentTests/MeetingLivePanelTests.swift
git commit -m "feat(meeting-panel): MeetingLivePanelController — панель + чистый рендер live-state

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Данные — SSE + poll-фоллбэк + финализация в отчёт

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/MeetingLivePanelController.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+MeetingMode.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/MeetingLivePanelTests.swift` (дописать)

**Прецеденты (ПРОЧИТАЙ):** `LiveSubtitlesOverlay.swift:380-460` (SSE через общий `SSESessionDelegate`, `handleSSELine`, `_testHandleSSELine`); `HistoryPanelController+MeetingMode.swift:530-615` (`onOpenMeeting` → `get_meeting_report` off-main → `presentMeetingReport`).

**Контракт данных (backend, C2a/C2b — точные имена):**
- SSE endpoint: `http://127.0.0.1:5005/v1/events?filter=meeting.transcript_appended,meeting.items_updated,meeting.speakers_updated,meeting.finalizing,meeting.finished` (comma-list поддержан, `rest_server.py:1649`).
- Payload'ы: `transcript_appended {chunk_text, total_len}`; `items_updated {items, decisions, questions}`; `speakers_updated {speakers}`; `finalizing {}`; `finished {item_id}`.
- Poll: IPC `get_meeting_live_state {}` → полный снапшот (schema — `docs/IPC_API_REFERENCE.md`, секция meeting).
- Stop: IPC `meeting_stop {}` → `{ok, item_id?}`.

- [ ] **Step 1: Дописать падающие тесты**

```swift
    // === Task 2: SSE/poll/финализация ===

    func test_sse_event_updates_partial_sections() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c._testHandleSSELine("event: meeting.speakers_updated")
        c._testHandleSSELine(#"data: {"type":"meeting.speakers_updated","data":{"speakers":[{"label":"Спикер 1","talk_sec":5.0,"last_active_ts":0}]}}"#)
        XCTAssertEqual(c._testSpeakerChipCount, 1)
    }

    func test_sse_transcript_appended_appends_tail() {
        let c = MeetingLivePanelController()
        c.render(state: makeState(transcriptTail: "начало. "))
        c._testHandleSSELine("event: meeting.transcript_appended")
        c._testHandleSSELine(#"data: {"data":{"chunk_text":"продолжение","total_len":700}}"#)
        XCTAssertTrue(c._testTranscriptTailText.hasSuffix("продолжение "))
    }

    func test_foreign_sse_event_ignored() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        let before = c._testSpeakerChipCount
        c._testHandleSSELine("event: live_subs.result")
        c._testHandleSSELine(#"data: {"data":{"speakers":[]}}"#)
        XCTAssertEqual(c._testSpeakerChipCount, before)
    }

    func test_sse_finished_triggers_report_callback() {
        let c = MeetingLivePanelController()
        var received: String?
        c.onFinished = { itemID in received = itemID }
        c.render(state: makeState())
        c.enterFinalizing()
        c._testHandleSSELine("event: meeting.finished")
        c._testHandleSSELine(#"data: {"data":{"item_id":"abc-123"}}"#)
        XCTAssertEqual(received, "abc-123")
    }

    func test_silence_watchdog_arms_poll_fallback() {
        let c = MeetingLivePanelController()
        c.render(state: makeState())
        c._testSimulateSSESilence(seconds: 16)
        XCTAssertTrue(c._testPollFallbackActive)
        // Любая живая SSE-строка снимает фоллбэк
        c._testHandleSSELine("event: meeting.items_updated")
        c._testHandleSSELine(#"data: {"data":{"items":[],"decisions":[],"questions":[]}}"#)
        XCTAssertFalse(c._testPollFallbackActive)
    }
```

И source-contract в `MeetingPanelWiringTests.swift` (создать; паттерн чтения исходника — `MainErrorsWiringTests.swift:298-334`):

```swift
import XCTest

final class MeetingPanelWiringTests: XCTestCase {
    private func source(_ name: String) throws -> String {
        // резолв пути как в MainErrorsWiringTests (walk-up от #file до Sources/)
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // → Tests/KrabEarAgentTests/
            .deletingLastPathComponent()   // → Tests/
            .deletingLastPathComponent()   // → корень пакета KrabEarAgent/
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    func test_stop_button_actually_calls_meeting_stop() throws {
        let src = try source("MeetingLivePanelController.swift")
        XCTAssertTrue(src.contains("\"meeting_stop\""),
                      "кнопка Завершить обязана реально звать meeting_stop (anti test-validates-the-hole)")
    }

    func test_sse_filter_lists_all_five_meeting_events() throws {
        let src = try source("MeetingLivePanelController.swift")
        for ev in ["meeting.transcript_appended", "meeting.items_updated",
                   "meeting.speakers_updated", "meeting.finalizing", "meeting.finished"] {
            XCTAssertTrue(src.contains(ev), "SSE-фильтр без \(ev)")
        }
    }
}
```

- [ ] **Step 2: Убедиться, что падают** (compile errors на новых hooks/onFinished; source-contract красный).

- [ ] **Step 3: Реализация**

(a) В `MeetingLivePanelController` добавить data-слой:

```swift
    // --- данные (Task 2) ---
    var ipcClient: IPCClient?                 // инжектится владельцем (Task 3)
    var onFinished: ((String?) -> Void)?      // item_id; владелец открывает отчёт
    private let restBaseURL = "http://127.0.0.1:5005"
    private var sseTask: URLSessionDataTask?
    private var pendingSSEEventType: String?
    private var lastSSEActivity: TimeInterval = 0
    private var pollTimer: Timer?
    private var silenceTimer: Timer?
    private(set) var pollFallbackActive = false
    private lazy var sseDelegate = SSESessionDelegate { [weak self] line in
        Task { @MainActor [weak self] in self?.handleSSELine(line) }
    }
    private lazy var sseSession = URLSession(configuration: .default,
                                             delegate: sseDelegate, delegateQueue: nil)
```

- `startUpdates()`: разовый off-main poll (`get_meeting_live_state` → `render`), затем `startSSE()` + silence-watchdog (Timer 5с: `now - lastSSEActivity > 15` при `.live` → `activatePollFallback()`).
- `startSSE()`: URL с фильтром из ПЯТИ событий (см. контракт), `Accept: text/event-stream`, `timeoutInterval: 600`, паттерн LiveSubtitlesOverlay.
- `handleSSELine(_:)`: паттерн LiveSubs (`event: ` → pendingSSEEventType; `data: ` → dispatch по типу). `lastSSEActivity = now` на КАЖДОЙ строке; если фоллбэк был активен — `deactivatePollFallback()`. Диспатч:
  - `transcript_appended` → `appendTranscriptChunk(chunk_text)` (append + trim до ~600 симв.);
  - `items_updated` → `renderItems(...)`; `speakers_updated` → `renderSpeakers(...)`;
  - `finalizing` → `enterFinalizing()`; `finished` → `onFinished?(item_id)`.
  Конверт: `obj["data"] ?? obj` (как `parseSSEData` LiveSubs).
- `activatePollFallback()`: `pollFallbackActive = true`; Timer 5с → off-main `get_meeting_live_state` → `render`; плюс пересоздание SSE-стрима (`stopSSE(); startSSE()`).
- `requestStop()` (вешается на stopButton в Task 1-каркасе): `enterFinalizing()` → off-main `ipcClient.call("meeting_stop")`; в ответе есть `item_id` → `onFinished?(item_id)` сразу (не ждём SSE); ошибка IPC → `statusLabel` с текстом ошибки + `resetToIdle()`.
- `stopUpdates()`: снять SSE/таймеры (вызывается из `hide()`); показ панели снова → `startUpdates()`.
- Test-hooks: `_testHandleSSELine(_:)` (прямой вызов handleSSELine), `_testSimulateSSESilence(seconds:)` (сдвигает `lastSSEActivity` в прошлое и дёргает watchdog-тик напрямую — БЕЗ реального ожидания), `_testPollFallbackActive`.

(b) `HistoryPanelController+MeetingMode.swift` — экстракция и standalone-окно:
- Вынести из `presentMeetingReport(_:)` построение VC в `static func makeMeetingReportVC(from result: [String: Any]) -> MeetingReportViewController?` (парсинг полей БЕЗ изменений — только перенос; sheet-путь `onOpenMeeting` переключить на хелпер).
- Добавить standalone-презентацию (для панели, у которой нет host-окна истории):

```swift
    /// C2c: отчёт встречи в отдельном titled-окне (панель — не NSWindowController-хост).
    @MainActor
    static func presentMeetingReportStandalone(result: [String: Any]) {
        guard let vc = makeMeetingReportVC(from: result) else { return }
        let window = NSWindow(contentViewController: vc)
        window.styleMask = [.titled, .closable, .resizable]
        window.title = "Встреча"
        window.setContentSize(NSSize(width: 640, height: 580))
        window.center()
        window.makeKeyAndOrderFront(nil)
        // держим ссылку до закрытия (иначе ARC закроет окно немедленно)
        _standaloneReportWindows.append(window)
        NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification, object: window, queue: .main
        ) { note in
            MainActor.assumeIsolated {
                _standaloneReportWindows.removeAll { $0 === note.object as? NSWindow }
            }
        }
    }
    @MainActor private static var _standaloneReportWindows: [NSWindow] = []
```

Сверь парсинг полей с фактическим телом `presentMeetingReport` (строки ~565-615) — переносить буква-в-букву, ничего не «улучшать».

- [ ] **Step 4: Тесты зелёные**

Run: `cd native/KrabEarAgent && swift test --filter MeetingLivePanelTests 2>&1 | tail -3 && swift test --filter MeetingPanelWiringTests 2>&1 | tail -3`
Expected: PASS (12 + 2).

- [ ] **Step 5: Регрессия MeetingMode + сборка**

Run: `cd native/KrabEarAgent && swift test --filter MeetingMode 2>&1 | tail -3` (если тесты на +MeetingMode есть — найти: `grep -rl "MeetingReport" Tests/ | head`) и `swift build -c release 2>&1 | tail -3`.
Expected: PASS / Build complete.

- [ ] **Step 6: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/MeetingLivePanelController.swift native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+MeetingMode.swift native/KrabEarAgent/Tests/KrabEarAgentTests/MeetingLivePanelTests.swift native/KrabEarAgent/Tests/KrabEarAgentTests/MeetingPanelWiringTests.swift
git commit -m "feat(meeting-panel): SSE+poll-фоллбэк, финализация в standalone-отчёт

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Точки входа — меню-бар + кнопка истории + проводка

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/main+MeetingPanel.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+StatusMenu.swift` (пункт меню в `rebuildStatusMenu()`, рядом с «Открыть историю», ~строка 205)
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift` (кнопка в `topActionsRow`, рядом с `helpButton` ~строка 805)
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/MeetingPanelWiringTests.swift` (дописать)

- [ ] **Step 1: Дописать падающие source-contract тесты**

```swift
    func test_menu_item_wired_in_rebuildStatusMenu() throws {
        let src = try source("main+StatusMenu.swift")
        XCTAssertTrue(src.contains("onMeetingPanelToggle"),
                      "пункт меню «Встреча» обязан быть реально добавлен в rebuildStatusMenu")
    }

    func test_meeting_panel_handler_calls_meeting_start() throws {
        let src = try source("main+MeetingPanel.swift")
        XCTAssertTrue(src.contains("\"meeting_start\""))
        XCTAssertTrue(src.contains("DispatchQueue.global"),
                      "IPC строго off-main (AGENT-3)")
    }

    func test_history_panel_button_routes_to_delegate() throws {
        let src = try source("HistoryPanelController.swift")
        XCTAssertTrue(src.contains("onOpenMeetingPanel") || src.contains("meetingPanelButton"),
                      "кнопка «Встреча» в topActionsRow обязана существовать и роутить в делегат")
    }
```

- [ ] **Step 2: Убедиться, что падают.**

- [ ] **Step 3: Реализация**

(a) `main+MeetingPanel.swift`:

```swift
import AppKit

/// C2c: владение панелью встречи + точки входа (спека §2.7/§2.7a п.3).
extension AgentAppDelegate {

    /// Единый вход: сессии нет → meeting_start (backend идемпотентен:
    /// already_active/promoted) → показать панель; сессия есть → просто показать.
    @objc func onMeetingPanelToggle() {
        let controller = ensureMeetingPanelController()
        controller.show()
        controller.startUpdates()
        let client = ipcClient
        // AGENT-3: ipcClient.call строго off-main.
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                nonisolated(unsafe) let response = try client.call(
                    method: "meeting_start", params: [:])
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    if (result["skipped"] as? String) == "privacy_mode" {
                        controller.render(state: ["ok": true, "active": false,
                                                  "privacy_mode_active": true])
                    }
                    // успех/already_active: ближайший poll/SSE наполнит панель
                }
            } catch {
                DispatchQueue.main.async {
                    controller.showTransientError("Не удалось начать встречу: \(error.localizedDescription)")
                }
            }
        }
    }

    func ensureMeetingPanelController() -> MeetingLivePanelController {
        if let existing = meetingPanelController { return existing }
        let c = MeetingLivePanelController()
        c.ipcClient = ipcClient
        c.onFinished = { [weak self] itemID in
            self?.openMeetingReportAfterFinish(itemID: itemID)
        }
        meetingPanelController = c
        return c
    }

    /// finished → get_meeting_report → standalone-окно; без item_id — панель в idle.
    func openMeetingReportAfterFinish(itemID: String?) {
        guard let itemID, !itemID.isEmpty else {
            meetingPanelController?.resetToIdle(); return
        }
        let client = ipcClient
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                nonisolated(unsafe) let response = try client.call(
                    method: "get_meeting_report", params: ["id": itemID])
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async { [weak self] in
                    self?.meetingPanelController?.resetToIdle()
                    HistoryPanelController.presentMeetingReportStandalone(result: result)
                }
            } catch {
                DispatchQueue.main.async { [weak self] in
                    self?.meetingPanelController?.showTransientError(
                        "Отчёт не построился: \(error.localizedDescription)")
                    self?.meetingPanelController?.resetToIdle()
                }
            }
        }
    }
}
```

Property `var meetingPanelController: MeetingLivePanelController?` — объявить в `main.swift` рядом с другими owned-контроллерами (найди блок свойств AgentAppDelegate; НЕ associated objects — это класс в основном файле). `showTransientError(_:)` — маленький хелпер в контроллере панели (statusLabel + авто-очистка через 5с), добавить в Task 3 если не сделан раньше.

(b) `main+StatusMenu.swift`, в `rebuildStatusMenu()` после пункта «Открыть историю»:

```swift
        let meetingItem = NSMenuItem(
            title: "Встреча",
            action: #selector(onMeetingPanelToggle),
            keyEquivalent: "")
        meetingItem.target = self
        meetingItem.image = NSImage(systemSymbolName: "person.2.fill",
                                    accessibilityDescription: nil)  // уже используется в +MeetingMode
        menu.addItem(meetingItem)
```

(c) `HistoryPanelController.swift`: кнопка `meetingPanelButton = ThemeSecondaryButton(title: "Встреча")` в `topActionsRow` (рядом с `helpButton`, `addArrangedSubview`); `@objc func onOpenMeetingPanel()` → `(NSApp.delegate as? AgentAppDelegate)?.onMeetingPanelToggle()`.

- [ ] **Step 4: Тесты зелёные + сборка**

Run: `cd native/KrabEarAgent && swift test --filter MeetingPanelWiringTests 2>&1 | tail -3 && swift build -c release 2>&1 | tail -3`
Expected: PASS (5) / Build complete.

- [ ] **Step 5: Глиф-гейт**

Run: `grep -rFo "person.2.fill" native/KrabEarAgent/Sources | head -2` (должен встречаться и вне нового кода). Любой ДРУГОЙ новый глиф/символ в диффе — тем же grep'ом; 0 вхождений вне нового кода → заменить.

- [ ] **Step 6: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/main+MeetingPanel.swift native/KrabEarAgent/Sources/KrabEarAgent/main+StatusMenu.swift native/KrabEarAgent/Sources/KrabEarAgent/main.swift native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift native/KrabEarAgent/Sources/KrabEarAgent/MeetingLivePanelController.swift native/KrabEarAgent/Tests/KrabEarAgentTests/MeetingPanelWiringTests.swift
git commit -m "feat(meeting-panel): точки входа — меню-бар «Встреча» + кнопка истории + проводка отчёта

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Финальные гейты волны

**Files:** только фиксы, если гейты красные.

- [ ] **Step 1:** `cd native/KrabEarAgent && swift test 2>&1 | tail -5` — ВСЯ Swift-сьюта зелёная (UITests-слой скипается штатно).
- [ ] **Step 2:** `swift build -c release 2>&1 | tail -3` — Build complete.
- [ ] **Step 3:** Глиф-аудит диффа: `git diff 87ae84db..HEAD -- native/ | grep -oP '(?<=\+).*' | grep -oE '[^\x00-\x7F]' | sort -u` — каждый non-ASCII символ проверить grep'ом по native/Sources вне диффа.
- [ ] **Step 4:** `grep -rn "runModal()" native/KrabEarAgent/Sources/KrabEarAgent/MeetingLivePanelController.swift native/KrabEarAgent/Sources/KrabEarAgent/main+MeetingPanel.swift` — пусто.
- [ ] **Step 5:** Commit фиксов (если были).

---

## Вне плана (координатор, после мержа)

Adversarial-ревью целого диффа; PR/CI по точному SHA/мерж; **parity-бинари** (`swift build -c release` → `cp` в `Krab Ear.app/Contents/MacOS/` + `native/runtime/` + `codesign`, коммит с `git add -f`); рестарт агента; живой смок панели на реальной встрече; agy-бриф на визуальную полировку; ROADMAP/память; релиз v2.9.0 — по сигналу владельца.
