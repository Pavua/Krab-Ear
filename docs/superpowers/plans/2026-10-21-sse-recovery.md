# SSE recovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** активный Swift subscriber восстанавливает SSE после EOF/REST restart,
а stop/hide/cancel запрещает новые попытки и события прежнего поколения.
**Architecture:** общий Foundation-only owner транспорта поверх существующего
`SSESessionDelegate`; reuse текущих fake session/task границ и проверенного
connection UUID/generation/backoff паттерна LiveSubtitlesOverlay. UI-владельцы
сохраняют собственный start/stop lifetime. Без replay аудио/текста.
**Tech Stack:** Swift/Foundation/URLSession/XCTest, существующие зависимости.
**Spec:** требования ниже; граница runtime — соседний план
`2026-10-21-tts-observability-bootstrap.md`.
**База:** origin/codex/krab-ear-v2 @ 3f08c9dc; isolated ear-sse-recovery-plan.

## Global Constraints

- Source/tests only. Нет production Swift/backend/REST lifecycle, моделей,
  настроек, микрофона/clipboard, Main/Gateway/clone/OrbStack действий.
- `SSESessionDelegate` остаётся единственным parser с HTTP/MIME/UTF8 проверкой.
- Один current connection и один pending retry на subscription owner.
- EOF с nil error — disconnect, как transport error. После stop/hide/cancel
  новые запросы и обработка старых callbacks запрещены.
- Delay 0.5/1/2/4/8/16/30 секунд, затем потолок30 пока owner активен;
  бюджет попыток не обрывает подписку до восстановления REST. После принятой
  полной SSE строки backoff сбрасывается. Нет busy loop и periodic model calls.
- Закрывать task/session на каждом terminal path; не держать model/audio/text
  payload в reconnect state. Event ID/replay/dedup бизнес-событий не вводятся.
- Source PASS не доказывает recovery живого Swift20131 или отсутствие TTS.

## Проверенная карта source consumers

| Owner / source file | Текущий source recovery | Решение |
|---|---|---|
| `HealthMonitor.swift` / ProbeSSEBox | ждёт cancellation; EOF не освобождает ожидание | исправить первым, actual fake transport tests |
| `RealtimeOverlayController+PartialSSE.swift` | отдельный parser; EOF пустой | общий parser/owner, lifetime start/stop |
| `StreamingPasteController.swift` | отдельный parser; EOF пустой; новый stream при recording start | общий owner; recording generation сохранить |
| `GlobalStatusBar.swift` | start/stop, нет onComplete | общий owner; visibility lifetime |
| `TranslationStreamView.swift` | dataTask остаётся non-nil после EOF | общий owner; явный start/stop lifetime |
| `LiveSubtitlesOverlay.swift` | UUID/generation/retry/cancel существуют | reference + regression, не заменять работающий owner |
| `QuickCapturePanelController.swift` | generation/retry/visibility существуют | reference + regression |
| `MeetingLivePanelController.swift` | silence watchdog + polling fallback | сохранить, отдельно фиксировать вид recovery |
| `HistoryPanelController+SecuritySettings.swift` | terminal migration watcher, counters fallback | gate migration idle; не объявлять auto-reconnect |

Девять исходных consumers не означают девять живых connections. Три TCP
соединения Swift20131 не атрибутированы по filter/owner. Source map не является
доказательством состава/семантики уже запущенного app build.

## Review Focus

- Синхронный EOF при resume и cancel до установки continuation: без зависания.
- Поздние line/EOF G1 после G2: не закрывать G2, не менять UI/counters G2.
- Hide/stop во время backoff или между factory/resume: без повторного connect.
- HTTP401/403/non-SSE/MIME и split UTF8: не принимать ошибку за event payload.
- Старый REST принимает TTS, новый counter ещё не существует: source recovery
  не обходит этот bootstrap gate и не разрешает restart.

### Task 1: reusable transport owner и ProbeSSEBox

**22.10 source status:** реализовано в `codex/ear-sse-recovery-1022`.
Actual Probe clean EOF и queued-retry cancellation: RED → GREEN.
31 PASS в ограниченном Foundation harness (HealthMonitor + shared owner +
SSESessionDelegate; StatusIndicatorView leaf stub), включая настоящий localhost
TCP/HTTP/URLSession reconnect. Независимый повторный source review PASS после
исправления cancellation race. Полный AppKit build/selected330 PASS, exact-head CI PR2109
`f0b9f568`: 27 SUCCESS / 0 failures; runtime acceptance не выполнен. Tasks 2/3 остаются в очереди.


**Files:** Create `native/KrabEarAgent/Sources/KrabEarAgent/SSESubscriptionTransport.swift`;
Modify `HealthMonitor.swift`; Create
`native/KrabEarAgent/Tests/KrabEarAgentTests/SSESubscriptionRecoveryTests.swift`;
Modify `HealthMonitorTests.swift`.

- [ ] RED: actual owner/fake session tests: `testEOFReconnectsWhileActive`,
  `testCleanEOFReconnects`, `testStopDuringBackoffDoesNotConnect`,
  `testCancellationBeforeWaitReturns`, `testLateG1CallbacksDoNotAffectG2`,
  `testRepeatedCompletionSchedulesOneRetry`, `testInvalidResponseDoesNotEmit`.
  Assert connection counts, pending retry count≤1, cancel/invalidate exactly once,
  delay sequence above; manually advanced scheduler, no sleeps/network/UI.
- [ ] Define `@MainActor final class SSESubscriptionTransport` with
  `start(request: URLRequest, onConnectionReset: @escaping () -> Void,
  onLine: @escaping (String) -> Void)` and `stop()`.
  Constructor injects existing task/session factory boundaries and scheduler
  `(TimeInterval, DispatchWorkItem) -> Void`. Preserve compatibility aliases for
  ProbeSSETask/ProbeSSESession tests if interfaces move. State: active flag,
  generation, connection UUID, current task/session/delegate, one retry token.
- [ ] GREEN: identity checked on MainActor before every callback; detach current
  connection before cancel/invalidate. Stop advances generation before cleanup.
  Before each connection can emit any line, synchronously call onConnectionReset
  on MainActor to clear consumer event state (Probe pendingEventType, paste eventType,
  partial buffers). Call reset also on terminal stop. A serial delegate queue feeds
  one ordered callback drain onto MainActor; do not launch an independent Task per
  line. Reset/line/EOF preserve transport order and each item carries connection
  identity. No reuse of a cancelled task.
- [ ] RED/GREEN boundary regression: G1 EOF between event: and data:, then G2
  data without event; no inherited event type or partial fragment. Verify ordered
  event/data/reset callbacks even when MainActor execution is delayed.
- [ ] Wire HealthMonitor production subscription to actual owner. Cancellation
  completes async lifetime and closes transport, not only the test injection path.
  Existing ping/wedge logic and escalation callbacks unchanged.
- [ ] Local bounded Foundation-only harness compiles actual shared owner + actual
  SSESessionDelegate + XCTest fakes in a temporary package (no app entry point).
  Full `swift test -j 1 --filter 'SSESubscriptionRecoveryTests|HealthMonitorTests'`
  only in a separately qualified build window/hosted macOS job. No dependencies
  downloaded or full local build during memory HOLD. A skipped command is recorded.

### Task 2: partial/paste owners

**22.10 partial slice:** isolated stacked `codex/ear-partial-sse-1022` @
`f0b9f568` base. Actual partial extension localhost clean EOF RED → GREEN;
35 PASS bounded harness (UI leaf stub), including stop/backoff, stale callbacks,
parser reset and replacement subscription. Full AppKit CI and tail regression
await next coordinated window; filters explicitly include both suites.
Task 2 remains incomplete: StreamingPasteController not migrated yet.


**Files:** Modify `RealtimeOverlayController+PartialSSE.swift`,
`StreamingPasteController.swift`, `StreamingPasteControllerTests.swift`;
Create `RealtimePartialSSERecoveryTests.swift`.

- [ ] RED actual controllers with fake transport/scheduler: EOF followed by a new
  accepted stream resumes current owner; hide/recording stop cancels retry;
  old recording G1 cannot paste/update G2; no duplicate final paste after reconnect.
- [ ] GREEN replace private PartialSSEDelegate/StreamingSSEDelegate with existing
  shared parser/transport. Keep text parsing and paste admission/FIFO untouched.
  Explicit active generation survives transport replacement, not recording stop.
- [ ] Run actual controller regressions plus RealtimeOverlayTailTests and
  StreamingPasteControllerTests in qualified build environment; source grep alone
  is not wiring acceptance. Clipboard/keyboard production actions forbidden.

### Task 3: status/translation owners and full source gate

**Files:** Modify `GlobalStatusBar.swift`, `TranslationStreamView.swift`;
Create `StatusTranslationSSERecoveryTests.swift`; conditional owning-hook change
in `HistoryPanelController+LiveTranslation.swift` as specified below; update
`docs/NOW.md` after GREEN.

- [ ] RED actual status/translation owners: disconnect with visible/active owner
  retries; hidden/stopped owner stays stopped; stale lines cannot append duplicate
  entries to a later session. Simulate >30 seconds outage with virtual clock.
- [ ] Translation currently starts in both init methods and stops only in deinit;
  there is no existing hide/show contract to preserve. Move subscription ownership
  into TranslationStreamView.updateSubscriptionLifetime(): eligible iff window is
  present/visible, not miniaturized, and !isHiddenOrHasHiddenAncestor. Invoke from
  viewDidMoveToWindow, viewDidHide/viewDidUnhide and window-scoped
  didChangeOcclusionState/didMiniaturize/didDeminiaturize notifications. Replace
  observers when window changes, cancel/remove on detach/deinit. Reparenting during
  theme rebuild must stop old transport and start at most one new connection.
  Verify actual AppKit hide/orderOut/tab-switch/reparent hooks with fixture windows
  in qualified hosted tests; if a path emits no hook, add an explicit owning hook
  in HistoryPanelController+LiveTranslation.swift before claiming visibility PASS.
- [ ] GREEN use shared transport; GlobalStatusBar keeps existing start/stop and
  heartbeat semantics. Do not infer visibility from receipt of an SSE line.
- [ ] Regression existing LiveSubtitles/QuickCapture/Meeting paths; no forced
  migration watcher restart or replay of migration events.
- [ ] Ruff for changed scripts if any, diff-check, static Swift/audit-all guards,
  independent strong whole-diff auth/audio/cancellation review. Exact-head hosted
  Swift CI + tests before source acceptance; Main serial CI ACK before push.
- [ ] PR and source receipt, with runtime recovery explicitly NOT_TESTED.

## Runtime boundary

Only a subsequently qualified Swift release can activate these fixes. For the
currently running build, use an already verified UI resubscribe path or separately
reviewed Ear-only Swift lifecycle in a fresh owner window. If neither exists,
REST cutover stays HOLD. Do not claim that editing source repairs live sockets.
