# Design Brief: визуальная полировка панели «Быстрая заметка» (C3b)

Дата: 2026-07-19. Исполнитель: Gemini 3.1 Pro (agy). Скоуп: ТОЛЬКО внешний вид
`native/KrabEarAgent/Sources/KrabEarAgent/QuickCapturePanelController.swift`.

## Контекст

Плавающая NSPanel-скретчпад (спека `docs/superpowers/specs/2026-07-16-c3-quick-capture-design.md`
§4): header (⏺-индикатор + mm:ss таймер + статус-текст), карточка живого текста
партиалов, скролл-список последних заметок (превью + «Копировать»), ряд кнопок
(старт/стоп, «Копировать всё», «→ Notes»). Функциональная раскладка собрана
инженерно на KrabEarTheme-токенах — нужна визуальная доводка до уровня
остального приложения (Liquid Glass, образцы: `MeetingLivePanelController.swift`
после его собственной agy-полировки, `ConversationStatusOverlay.swift`,
`ConversationViewController+UI.swift`).

## Что улучшить (твоя зона)

1. **Header**: сейчас плоский ряд `recordIndicator + headerTimerLabel + statusLabel`.
   Таймер — крупнее/моноширинный (уже `.tabular()`, но проверь читаемость на
   тёмной/светлой теме), статус-текст — вторичным цветом, компактнее рядом с
   таймером (сейчас может визуально «спорить» с таймером за внимание).
   Recording-индикатор уже пульсирует (`RecordingIndicator`, Motion-токены,
   Reduce Motion уважается его собственным кодом — не трогай) — просто убедись,
   что раскладка вокруг него аккуратна.
2. **Карточка живого текста** (`liveTextContainer`): уже есть скруглённый фон
   + бордер — доведи до уровня остального приложения (внутренние отступы,
   типографика live-текста, состояние «пусто» — сейчас просто пустая карточка,
   добавь placeholder-текст по центру серым цветом типа «Говорите — текст
   появится здесь», который скрывается, как только приходит первый партиал).
3. **Список заметок** (`notesStack`/`QuickNoteRowView`): сейчас голые строки
   текст+кнопка. Сделай визуально настоящий список — тонкий разделитель или
   лёгкий card-background на каждой строке, компактная типографика превью,
   кнопка «Копировать» — как вторичное действие (можно меньше/иконка-only на
   hover, если не усложняет). Пустое состояние «Заметок пока нет» — по центру,
   с лёгкой иконкой, не голый текст в углу.
4. **Ряд кнопок**: `toggleButton`/`copyAllButton`/`sendToNotesButton` — сейчас
   `ThemePrimaryButton`/`ThemeSecondaryButton` как есть, это нормально; проверь
   выравнивание/отступы между ними и что toggleButton визуально доминирует
   (primary-действие), остальные — вторичные.
5. Общие отступы/выравнивание секций (`contentStack`), читаемость на тёмной/
   светлой теме, ничего не должно обрезаться при minSize 300×320.

## Что НЕЛЬЗЯ ломать (жёсткие инварианты — за нарушение дифф отклоняется)

- **Всю логику**: `show()`/`hide()`/`windowWillClose`, `setRecording(_:)`,
  `ingestPartial(_:)` (ЗАМЕНА текста, не конкатенация — это был живой баг,
  зафикшенный этой же волной, не трогай семантику), SSE-слой
  (`startSSE`/`stopSSE`/`handleSSELine`/`dispatchSSEEvent`), таймер
  (`startTimer`/`stopTimer`/`updateTimer`), `renderNotes(_:)`/`makeNoteRow`,
  `copyToClipboard`, `onSendToNotesTapped` (IPC-вызов off-main), position
  persistence (`restorePosition`/`savePosition`/`isOnScreen`/`placeTopRight`),
  `handleDrag`. Меняй ТОЛЬКО построение/стилизацию вьюх в `buildLayout()`,
  `makeNoteRow`, `setupPanel()` (стили, НЕ styleMask-флаги — см. ниже).
- **Test-hooks и их семантику** (`_test*`): `_testStatusText`,
  `_testHeaderTimerActive`, `_testLiveText`, `_testNoteRowCount`,
  `_testSetRecording`, `_testIngestPartial`, `_testSetNotes`,
  `_testHandleSSELine` — сигнатуры и возвращаемые значения неизменны.
  После правок прогони: `cd native/KrabEarAgent && swift test --filter
  QuickCapturePanelTests` (13 тестов) и `swift test --filter
  QuickCaptureWiringTests` (11 тестов, грепают ДРУГИЕ файлы — просто
  подтверди, что они всё ещё зелёные) — все зелёные.
- **Панельные свойства**: `styleMask` (включая `.closable` + `.titled` —
  ОБА обязательны, `.titled` даёт реальную кнопку закрытия, без него крестика
  физически нет, это был живой баг этой же волны; `titlebarAppearsTransparent`
  и `titleVisibility=.hidden` остаются, чтобы полоса тайтлбара не была видна),
  `.floating` level, `isReleasedWhenClosed=false`, delegate, minSize 300×320,
  `positionKey`, drag/off-screen guard (`isOnScreen` 80%-порог).
- **Токены**: только `KrabEarTheme` (Colors/Typography/Metrics/Motion/
  Interaction); никаких хардкод-цветов/шрифтов/магических чисел вне Metrics.
- **Глифы**: только SF Symbols (`NSImage(systemSymbolName:)`) или символы,
  уже встречающиеся в `native/KrabEarAgent/Sources` (проверяй grep'ом) —
  CoreText-hang класс AGENT-J/M.
- Никаких `runModal()`, никакого прямого IPC/сетевого кода сверх уже
  существующего `onSendToNotesTapped`, никаких новых файлов (всё в
  QuickCapturePanelController.swift), никаких переименований публичных API
  (`window`, `ipcClient`, `onToggleRecording`, `setSendToNotesVisible`).

## Definition of Done

`swift build -c release` зелёный; оба тест-фильтра зелёные (13+11); дифф
затрагивает только QuickCapturePanelController.swift; краткое резюме
изменений в конце ответа.
