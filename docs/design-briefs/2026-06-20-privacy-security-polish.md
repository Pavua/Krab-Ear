# Design brief — визуальная полировка Privacy/Security секций (2026-06-20)

## Цель
Сделать две Settings-секции — «Безопасность» (шифрование истории) и «Хранение истории» (авто-удаление) — визуально цельной, понятной privacy-группой. Сейчас контролы функциональны, но плоские: тоггл + подпись. Нужна ясная визуальная иерархия и статус-индикация состояния шифрования.

## Файлы (РОВНО эти два, оба имеют ДВА варианта — Gemini `build…` и Claude-Design `cd…`)
- `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+SecuritySettings.swift`
- `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+RetentionSettings.swift`

## 🔴 НЕЛЬЗЯ ЛОМАТЬ (поведение/проводка — иначе регрессия)
1. **sectionId-строки** (ключи персистентности UserDefaults — переименование сбросит состояние свёрнутости у пользователя): `"history_security_settings"`, `"cd_history_security"`, `"history_retention_settings"`, `"cd_history_retention"`. НЕ переименовывай.
2. **@objc-селекторы и их имена** (вызываются по `#selector`): `onEncryptionToggleChanged`, `onEncryptionToggleChangedCD`, `updateEncryptionAvailability`, `onAutoPurgeEnabledChanged`, `onAutoPurgeEnabledChangedCD`, `onRetentionDaysChanged`, `onRetentionDaysChangedCD`, `syncRetentionSettings`.
3. **IPC-методы и параметры** (не менять имена/ключи): `get_encryption_status` {}, `set_history_encryption` {enabled:Bool}, `set_settings` {auto_purge_enabled:Bool}, `set_settings` {auto_purge_retention_days:Int}.
4. **Логику** rollback тоггла шифрования при ошибке Keychain (`set_history_encryption` неуспех → откат тоггла) и текст предупреждения о потере ключа — СОХРАНИТЬ дословно по смыслу (это безопасность, не косметика).
5. Свойства-контролы, на которые ссылаются селекторы/sync (toggle, stepper, daysLabel и т.п.) — не переименовывать.

## 🔴 Жёсткие правила проекта (CI/конвенции отвергнут нарушения)
- ВСЕ `ipcClient.call` строго off-main: `DispatchQueue.global` → `DispatchQueue.main` (AGENT-3). Не двигать существующие вызовы на main.
- НИКОГДА `runModal()`. Только `presentAlertSheet`/`presentPanelSheet` (AlertHelpers.swift) с nil-guard окна, либо inline-контролы.
- ТОЛЬКО токены `KrabEarTheme` (Colors/Typography/Metrics/Interaction) — никаких хардкод NSColor/чисел/шрифтов.
- SF Symbols только через `if let icon = NSImage(systemSymbolName:…)` (без force-unwrap). Глиф-безопасно.
- Не создавать новых scratch/test `.swift`. Правки ТОЛЬКО в этих двух файлах.
- Должно компилироваться: `cd native/KrabEarAgent && swift build -c release` → «Build complete» без ошибок.

## Что улучшить (визуал — твоя зона)
1. **Иконография замка**: состояние шифрования отражать SF Symbol — `lock.fill` (вкл) / `lock.open` (выкл), цвет через KrabEarTheme (accent при вкл, secondary при выкл). Обновлять в `updateEncryptionAvailability`/после `get_encryption_status`.
2. **Статус-бейдж шифрования**: «Зашифровано» / «Выключено» / «Недоступно (Keychain)» — читаемый бейдж рядом с тогглом, цвет по состоянию (KrabEarTheme.Colors.accent/textSecondary/warning).
3. **Иерархия**: primary-строка (название + тоггл/бейдж) визуально выше вторичного пояснения (caption). Используй KrabEarTheme.Metrics для отступов.
4. **Когезия двух секций**: единый визуальный язык privacy (иконки-замок/часы, одинаковые отступы), чтобы «Безопасность» и «Хранение истории» читались как связанная группа.
5. **Retention**: понятнее показать `N дн.` + намёк на диапазон (1–3650); иконка `clock.arrow.circlepath` для авто-удаления.
6. Применяй улучшения к ОБОИМ вариантам (Gemini `build…` и CD `cd…`) консистентно.

## Результат
Только эти 2 файла изменены, `swift build -c release` зелёный, sectionId/селекторы/IPC нетронуты. Сообщи список изменений + статус сборки.
