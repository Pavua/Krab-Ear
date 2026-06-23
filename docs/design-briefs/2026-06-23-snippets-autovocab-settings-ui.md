# Design brief — Settings UI: Auto-vocab toggle + Text Snippets editor (2026-06-23)

Два бэкенда уже отгружены и работают, но НЕ видны юзеру (gated off, UI нет). Задача — добавить две Settings-поверхности в Krab Ear `.app` (Swift). ВИЗУАЛ — твой; проводка/IPC ниже точная (anti-rebuild).

## Контекст / правила (НЕ нарушать)
- Паттерн расширений: новый файл `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+TextSnippets.swift` (для секции сниппетов) по образцу существующих `HistoryPanelController+STTVocabulary.swift` / `+VoiceCommands.swift` (associated objects, `setupX()` → `CollapsibleSectionView`, `sectionId` для UserDefaults-персистентности).
- 🔴 IPC СТРОГО off-main thread (паттерн AGENT-3): любой `ipcClient.call(...)` — в `DispatchQueue.global().async`, результат обратно на main через `DispatchQueue.main.async`. Никогда не звать IPC на main.
- 🔴 НИКАКОГО `runModal()` — только `presentAlertSheet(_:for:)` / `presentPanelSheet(_:for:)` из `AlertHelpers.swift` (Sequoia AppHang convention).
- 🔴 Глиф-гейт: НЕ вводи новые экзотические non-ASCII глифы/эмодзи в UI-строки (CoreText hang класс AGENT-J/M). Кириллица — ок. Иконки — только SF Symbols (`NSImage(systemSymbolName:)`).
- Используй токены `KrabEarTheme` (цвета/шрифты/отступы), `ThemePrimaryButton`/`ThemeSecondaryButton`, `CollapsibleSectionView`. Reduce-Motion respected (через `KrabEarTheme.Motion.animate`).
- Скомпилируй: `cd native/KrabEarAgent && swift build -c release` должен пройти без ошибок. НЕ делай git commit / push / codesign / копирование в bundle — оставь изменения в рабочем дереве, их заберёт ревьюер.

## Поверхность A — тоггл «Авто-словарь из правок»
Маленький тоггл. Лучшее место — ВНУТРИ существующей секции «Словарь STT» (`HistoryPanelController+STTVocabulary.swift`), добавь одну строку-тоггл сверху или снизу списка.
- Подпись: «Авто-словарь из правок» + подсказка (tooltip/caption): «Когда вы исправляете неверно распознанное слово, оно автоматически добавляется в словарь STT».
- Привязка: setting-ключ `auto_learn_corrections_enabled` (Bool, default false).
- Чтение: `get_settings` (или существующий механизм синхронизации тогглов в этой секции) → выставить состояние. Запись: `set_settings {"auto_learn_corrections_enabled": <Bool>}` (off-main).

## Поверхность B — новая секция «Текстовые сниппеты»
Новая `CollapsibleSectionView` (sectionId напр. `text_snippets_settings`).
- Тоггл вверху: «Включить сниппеты» → setting `text_snippets_enabled` (Bool, default false), читать/писать через `set_settings`/`get_settings` (off-main).
- Редактор пар trigger → expansion: таблица или вертикальный список строк, каждая показывает `trigger` и (усечённый) `expansion`. Кнопки «Добавить» и «Удалить» (per-row или выбранную).
  - Загрузка списка: IPC `list_text_snippets` → ответ `{ok, snippets: [{trigger, expansion}, ...]}` (off-main, заполнить таблицу на main).
  - Добавление: показать `presentPanelSheet`/`presentAlertSheet` с двумя полями (trigger, expansion; expansion многострочный) → IPC `add_text_snippet {"trigger": String, "expansion": String}` → перезагрузить список.
  - Удаление: IPC `remove_text_snippet {"trigger": String}` → перезагрузить.
- Пустое состояние: короткая подпись-пример «Напр.: "вставь подпись" → ваш текст подписи».
- Подсказка секции: «Произнесённая триггер-фраза заменяется на заданный текст перед вставкой».

## Где зарегистрировать секции
Секция «Текстовые сниппеты» должна появиться во вкладке Settings рядом с «Словарь STT» / «Голосовые команды» (найди, где они добавляются в layout Settings-вкладки — тот же `setupX()` вызов / stackView — и встрой по аналогии). Тоггл авто-словаря — внутри секции «Словарь STT».

## Готовность
Когда закончишь: оставь отчёт в `/tmp/krab-ear-gemini/run.log` (какие файлы создал/изменил, прошёл ли `swift build -c release`). НЕ коммить.
