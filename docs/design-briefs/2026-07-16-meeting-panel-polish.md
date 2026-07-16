# Design Brief: визуальная полировка панели «Встреча» (C2c post-merge)

Дата: 2026-07-16. Исполнитель: Gemini 3.1 Pro (agy). Скоуп: ТОЛЬКО внешний вид
`native/KrabEarAgent/Sources/KrabEarAgent/MeetingLivePanelController.swift`.

## Контекст

Плавающая NSPanel живой встречи (спека `docs/superpowers/specs/2026-07-10-c2-live-meeting-overlay-design.md`
§2.7, вариант B из макетов): header (⏺ таймер + «Встреча» + degraded-бейдж), ряд чипов
спикеров, скролл-список items (задачи/решения/вопросы), хвост транскрипта, кнопка
«Завершить встречу». Функциональная раскладка собрана инженерно на KrabEarTheme-токенах —
нужна визуальная доводка до уровня остального приложения (Liquid Glass, образцы:
`ConversationStatusOverlay.swift`, `ConversationViewController+UI.swift`,
`MeetingReportViewController` в `HistoryPanelController+MeetingMode.swift`).

## Что улучшить (твоя зона)

1. **Чипы спикеров**: сейчас plain-текст ««Спикер 1» · 2м 5с · 10с назад». Сделай настоящие
   чипы-капсулы: скруглённый фон (cardBackground/accent-тонирование по индексу спикера),
   компактная типографика, staleness — вторичным цветом/меньшим кеглем; активный недавно
   спикер визуально «живее» (например, точка-индикатор).
2. **Иерархия header'а**: таймер — моноширинный/крупнее, ⏺-индикатор записи с мягкой
   пульсацией (Motion-токены, Reduce Motion уважать), degraded-бейдж — как warning-чип,
   не голый текст.
3. **Список items**: визуально разделить типы (задача/решение/вопрос) — иконка или
   цветовая полоска слева вместо текстового префикса; интерлиньяж/отступы по Metrics.
4. **Хвост транскрипта**: визуально «фоновая» зона (secondary, лёгкая подложка),
   не конкурирует с items.
5. **Пустые/статусные состояния** (idle/privacy/finalizing): по центру, с иконкой,
   аккуратной типографикой; finalizing — со спиннером (NSProgressIndicator).
6. Общие отступы/выравнивание секций, читаемость на тёмной/светлой теме.

## Что НЕЛЬЗЯ ломать (жёсткие инварианты — за нарушение дифф отклоняется)

- **Всю логику**: состояния UIState и `setUIState`, `render(state:)`, SSE/poll-слой,
  `deliverFinished`/`finishedDelivered`, таймеры, `windowWillClose`, `requestStop()`.
  Меняй ТОЛЬКО построение/стилизацию вьюх.
- **Test-hooks и их семантику** (`_test*`): `_testSpeakerChipCount` = число arranged
  subviews в `speakersRow`; `_testSpeakerChipTitles` обязан возвращать строки, содержащие
  label и «с»/«мин» (staleness); `_testItemRowCount` = items+decisions+questions;
  `_testTranscriptTailText`, `_testDegradedBadgeVisible`, `_testHeaderTimerActive` и
  остальные — поведение неизменно. После правок прогони:
  `cd native/KrabEarAgent && swift test --filter MeetingLivePanelTests` (18 тестов) и
  `swift test --filter MeetingPanelWiringTests` (8) — все зелёные.
- **Панельные свойства**: styleMask (включая `.closable`/`.resizable`), `.floating`,
  `isReleasedWhenClosed=false`, delegate, minSize 300×360, positionKey, drag/off-screen guard.
- **Токены**: только `KrabEarTheme` (Colors/Typography/Metrics/Motion/Interaction);
  никаких хардкод-цветов/шрифтов/магических чисел вне Metrics.
- **Глифы**: только SF Symbols (`NSImage(systemSymbolName:)`) или символы, уже
  встречающиеся в `native/KrabEarAgent/Sources` (проверяй grep'ом) — CoreText-hang
  класс AGENT-J/M.
- Никаких `runModal()`, никакого прямого IPC/сетевого кода, никаких новых файлов
  (всё в MeetingLivePanelController.swift), никаких переименований публичных API.

## Definition of Done

`swift build -c release` зелёный; оба тест-фильтра зелёные; дифф затрагивает только
MeetingLivePanelController.swift; краткое резюме изменений в конце ответа.
