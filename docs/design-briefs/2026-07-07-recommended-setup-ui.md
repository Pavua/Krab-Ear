# Design brief — A1 «Рекомендованная настройка» (онбординг-шаг + Settings-секция + wake word consent)

## Контекст
Три новых Swift-компонента для волны A1 (см. план
`docs/superpowers/plans/2026-07-07-recommended-setup.md`, Задачи 3/5/6). Механика
(wiring, IPC, associated-object паттерн, Auto Layout skeleton) уже написана Sonnet и
компилируется — здесь нужен ТОЛЬКО визуал.

1. `RecommendedSetupStep.swift` — шаг онбординга (sheet), показывает превью (dry_run)
   списка «включим/пропустим» перед завершением настройки.
2. `WakeWordConsentStep.swift` — отдельный consent-экран (sheet) для голосового
   триггера «Краб» — always-listening микрофон, показывается ПОСЛЕ шага 1.
3. `HistoryPanelController+RecommendedSetup.swift` — секция в Настройках, показывает
   тот же превью + кнопки «Применить рекомендуемое» / «Отменить последнее».

## 🔴 НЕЛЬЗЯ ЛОМАТЬ (поведение/проводка — иначе регрессия)
1. **Auto Layout skeleton** (NSStackView-иерархия, constraints) — можно менять
   spacing/padding/шрифты/цвета, НЕЛЬЗЯ убирать constraints, ломающие resize/adaptive
   layout.
2. **IPC-контракт**: `apply_recommended_setup {dry_run}` и `set_settings
   {wake_word_engine: "openwakeword"}` вызываются строго off-main (DispatchQueue.global
   / `Task` + `await MainActor.run`), обновление UI — строго на main (AGENT-3 AppHang-
   класс). НЕ переносить IPC-вызовы на main thread ради визуальных экспериментов.
3. **associated-object паттерн** (`objc_setAssociatedObject`/`objc_getAssociatedObject`)
   в `HistoryPanelController+RecommendedSetup.swift` — используется для хранения
   последнего dry_run снапшота между перестройками карточки; не заменять на другое
   хранилище без согласования.
4. **`sectionId`-строки** (ключи персистентности UserDefaults — переименование сбросит
   состояние свёрнутости у пользователя): `"recommended_setup"`, `"cd_recommended_setup"`.
   НЕ переименовывай.
5. **@objc-селекторы** (вызываются по `#selector`): `onApplyRecommendedSetup(_:)`,
   `onUndoLastRecommendedSetup(_:)`. Не переименовывать.
6. Кнопки «Применить»/«Пропустить»/«Отменить последнее»/«Включить»/«Не сейчас» должны
   остаться семантически теми же действиями — можно менять титры/иконки, не логику
   нажатий.
7. **Глифы**: ТОЛЬКО ASCII + установленные SF Symbols (см. существующий набор в
   `HistoryPanelController+Calibration.swift`: `speedometer`, `checkmark.circle.fill`,
   `exclamationmark.triangle`) — перед добавлением НОВОГО символа сверить с
   `native/` (0 вхождений нового глифа означает нужно взять уже установленный SF
   Symbol, не изобретать новый). Кириллица — ок (основной язык проекта).
8. Не создавать новых scratch/test `.swift` файлов сверх трёх перечисленных выше.
9. Должно компилироваться: `cd native/KrabEarAgent && swift build -c release` →
   «Build complete» без ошибок.

## Что улучшить (визуал — твоя зона)
1. **Карточка превью** (оба шага онбординга + Settings-секция): два визуально
   различимых блока — «Будет включено» (зелёный/accent-акцент, список ключей
   человеко-читаемыми названиями, не raw `snake_case`) и «Будет пропущено»
   (нейтральный/серый акцент, причина рядом с каждым пунктом).
2. **Иконки на пункт**: подобрать SF Symbols по смыслу (пауза/тишина, дедупликация,
   автосохранение, фонетика, сниппеты, авто-обучение, quick edit, undo, календарь,
   LLM-полировка, action items, SenseVoice) — единый визуальный язык с существующими
   секциями Настроек (`HistoryPanelController+Calibration.swift`,
   `HistoryPanelController+STTEnginesPicker.swift`).
3. **tier-бейдж (low/mid/high)** — переиспользовать существующий `calibTierBadge`-
   паттерн цветов (success/accent/textDisabled) из
   `HistoryPanelController+Calibration.swift`, не изобретать новую цветовую схему.
4. **Кнопка «Отменить последнее применение»** — должна визуально читаться как менее
   «опасная», чем деструктивные действия (не красная/warning-стилистика) — это откат
   настроек через существующий backup-механизм, не потеря данных.
5. **Пустое состояние** («ничего не пропущено, всё безопасное уже включено») —
   отдельный дружелюбный текст, не пустая карточка.
6. **WakeWordConsentStep** — иконка микрофона (`mic.fill` / `waveform`), явный акцент
   на «локально, без сети» (например небольшой значок `lock.shield` рядом с текстом
   про приватность), чтобы визуально отличаться от обычных feature-тогглов —
   consent-экран должен читаться как более «весомое» решение, чем обычный шаг мастера.
7. Применяй улучшения консистентно к обоим вариантам Settings-секции (Gemini
   `buildRecommendedSetupSection`/`rebuildGeminiRecommendedSetupCard` и Claude-Design
   `cdBuildRecommendedSetupSection`/`rebuildCDRecommendedSetupCard`).

## Результат
Правки только в трёх файлах Задач 3/5/6 (или новых Swift extension-файлах, если
решишь разбить визуал на дополнительные файлы по тому же паттерну, что и
`HistoryPanelController+Calibration.swift` — в этом случае перечисли новые файлы в
отчёте). `swift build -c release` должен проходить после правок. Ревью диффа —
Claude, ПЕРЕД коммитом (см. `reference_gemini_cli_delegation` в памяти проекта: brief
→ agy → ревью диффа контролёром → `swift build -c release` → commit
`Co-Authored-By: Gemini 3.1 Pro (Antigravity)`). НЕ коммить самостоятельно — оставь
изменения в рабочем дереве, отчёт положи в `/tmp/krab-ear-gemini/run.log`.
