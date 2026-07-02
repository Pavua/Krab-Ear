# Бриф: Swift UI для «Запланированные записи» (RecordingScheduler)

## Контекст
Backend `backend/recording_scheduler.py` (`RecordingScheduler`) полностью готов и вживую работает (фоновый триггер-поток каждые 30с проверяет расписание и стартует запись). IPC-хендлеры уже зарегистрированы в dispatch table `service.py`. Swift UI ОТСУТСТВУЕТ полностью — нужно создать с нуля.

## Контракт IPC (сверено буква-в-букву с реальным кодом `recording_scheduler.py` — НЕ выдумывай другие поля)

### `schedule_recording`
Params:
```json
{"start_time": "2026-07-05T15:00:00+02:00", "duration_sec": 1800, "label": "Планёрка"}
```
- `start_time` (String, ОБЯЗАТЕЛЕН) — ISO 8601 с таймзоной. Валидация на бэкенде: не в прошлом, не дальше 30 дней вперёд.
- `duration_sec` (Int, ОБЯЗАТЕЛЕН для полезного результата) — секунды, диапазон 1..7200 (макс 2 часа). Бэкенд бросает `ValueError` вне диапазона.
- `label` (String, опционально) — метка/описание.

Response (успех): `{"schedule": {"id": "...", "start_time": "...", "duration_sec": 1800, "label": "...", "status": "pending", "created_at": "..."}}`
Response (ошибка): IPC error envelope `{"ok": false, "error": {"message": "..."}}` — покажи message юзеру (напр. «Достигнут лимит ожидающих записей (50)» или «start_time не может быть в прошлом»).

### `cancel_scheduled_recording`
Params: `{"schedule_id": "<id>"}` (backend также принимает алиас `"id"`, но используй `schedule_id`).
Response: `{"cancelled": true}` или `{"cancelled": false}` (если id не найден или уже не pending). **НЕ `{"ok": ...}`** — поле называется `cancelled`.

### `list_scheduled_recordings`
Params: `{}` (без параметров).
Response: `{"schedules": [...], "count": 3}`. Каждый элемент schedules[i] = `{id, start_time, duration_sec, label, status, created_at}`. `status` ∈ `"pending"|"completed"|"cancelled"`.

## Что построить
Новый файл `HistoryPanelController+RecordingScheduler.swift`, по аналогии с `HistoryPanelController+CompareRecordings.swift` или `+RecordingInsights.swift` (открой их для образца структуры/стиля секции).

1. Новая CollapsibleSectionView секция **«Запланированные записи»** — разумно разместить в Settings-табе рядом с другими manager-панелями (`+STTVocabulary.swift`, `+VoiceCommands.swift` — открой один из них для образца wiring).
2. **Список** предстоящих (`status == "pending"`) заданий: время начала (локализованный формат), длительность (мин), label. Кнопка «Отменить» на каждой строке → `cancel_scheduled_recording`, оптимистично убрать из списка при `cancelled: true`.
3. **Форма добавления**: `NSDatePicker` (стиль `.textFieldAndStepper` или calendar — на твой вкус, главное чтобы выбирал и дату, и время) + `NSTextField` для длительности в минутах (конвертируй в секунды перед отправкой, умножь на 60) + `NSTextField` для label. Кнопка «Запланировать» → `schedule_recording`. Конвертируй `Date` в ISO8601 строку С таймзоной (`ISO8601DateFormatter` с `.withInternetDateTime` — включает timezone offset).
4. После успешного добавления/отмены — обнови список вызовом `list_scheduled_recordings`.
5. Ошибки от бэкенда (`ValueError` messages типа "лимит" или "в прошлом") показывай через `BackendToast` (не `runModal`!) — посмотри как это делается в соседних файлах.

## Жёсткие правила (иначе ревью/сборка завернёт)
- IPC СТРОГО off-main (паттерн AGENT-3): `DispatchQueue.global().async { ipcClient.call(...) }`, назад на UI через `DispatchQueue.main.async` или `@MainActor`.
- НЕ `runModal()` нигде.
- Glyph-gate: не вводи новые non-ASCII глифы кроме уже используемых в native/ (SF Symbols для иконок, не Unicode-эмодзи-литералы).
- `KrabEarTheme` токены как в соседних секциях.
- В конце ОБЯЗАТЕЛЬНО `swift build -c release` из `native/KrabEarAgent` — должен быть зелёным. Если есть ошибки компиляции — почини их сам перед завершением.

## Граница
Только новый файл `HistoryPanelController+RecordingScheduler.swift` + одна строка wiring в существующий Settings-таб (там где регистрируются другие CollapsibleSection). НЕ трогай Python/backend (уже готов), НЕ другие Swift-файлы кроме точки wiring.
