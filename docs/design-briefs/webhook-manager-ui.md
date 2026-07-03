# Бриф: Swift UI для «Webhooks» (WebhookManager)

## Контекст
Backend `backend/webhook_manager.py` (`WebhookManager`) полностью готов, вживую работает: регистрация внешних URL-получателей событий с HMAC-подписью, SSRF-защитой, retry, статистикой доставки. IPC-хендлеры уже зарегистрированы в dispatch table `service.py` (строки ~1849-1851). Swift UI ОТСУТСТВУЕТ полностью.

## Контракт IPC (сверено буква-в-букву с реальным кодом `backend/webhook_manager.py` — НЕ выдумывай другие поля; **ТОЛЬКО ЭТИ ТРИ МЕТОДА СУЩЕСТВУЮТ** — нет update/toggle/enable-disable, только register/unregister/list)

### `register_webhook`
Params: `{"url": "https://example.com/hook", "events": ["transcription.completed"], "secret": "минимум-16-символов-или-пусто"}`
- `url` (String, ОБЯЗАТЕЛЕН) — должен начинаться с `http://` или `https://`. Backend САМ проверяет SSRF (блокирует localhost/RFC1918/cloud-metadata) — не дублируй эту проверку на Swift-стороне, просто покажи ошибку если backend отклонит.
- `events` (массив String, опционально) — пустой массив `[]` = подписка на ВСЕ события. Дай юзеру простое текстовое поле с событиями через запятую (напр. `transcription.completed, recording.started`) — парси по запятой в Swift, НЕ делай сложный picker, событий много и они не документированы централизованно.
- `secret` (String, опционально) — HMAC-ключ. Если непустой — ДОЛЖЕН быть ≥16 символов (backend бросит ValueError короче). Пустая строка = без подписи (легитимно).

Response (успех): `{"webhook_id": "<uuid>"}`.
Response (лимит вебхуков достигнут, НЕ ошибка-исключение): `{"ok": false, "reason": "webhook_limit_reached"}` — покажи «Достигнут лимит webhook-ов».
Response (пустой URL / SSRF-отклонение / короткий secret — ЭТО IPC error envelope, не `ok:false` внутри result): при этих условиях backend бросает исключение → IPC-клиент throw'ает `IPCError.backendError(message)` — лови через обычный `catch`, `error.localizedDescription` уже содержит понятное сообщение.

### `unregister_webhook`
Params: `{"webhook_id": "<uuid>"}` (обязателен).
Response: `{"removed": true}` или `{"removed": false}` (если id не найден).

### `list_webhooks`
Params: `{}`.
Response: `{"webhooks": [{"webhook_id": "...", "url": "...", "events": [...], "has_secret": true, "enabled": true, "created_at": "...", "deliveries": 5, "failures": 1, "last_status": 200}]}`.
- `has_secret` — bool, реальный секрет НИКОГДА не возвращается (правильно, не пытайся его показать/редактировать).
- `last_status` — может быть `null` (ещё не было доставок) или Int (HTTP-код последней попытки).

## Что построить
Новый файл `HistoryPanelController+WebhookManager.swift`, по аналогии с `HistoryPanelController+ConfigPresets.swift` (открой для образца — недавно построенная секция с list+create-формой в обоих settings-вариантах Gemini/Claude Design, если такое разделение существует и в других недавних секциях; если увидишь что большинство новых секций сейчас идут только в ОДНОМ (Gemini) варианте — ориентируйся на более свежий пример, напр. `+RecordingScheduler.swift` или `+TimelineExport.swift`).

1. Новая CollapsibleSectionView секция **«Webhooks»** в Settings-табе.
2. **Список** зарегистрированных webhook-ов: URL (обрезать длинные по центру или tail), бейдж 🔒/значок замка (SF Symbol `lock.fill`) если `has_secret`, счётчик `deliveries`/`failures`, `last_status` цветом (зелёный 2xx, красный иначе, серый если null). Кнопка «Удалить» на каждой строке → `unregister_webhook`.
3. **Форма добавления**: поле URL, поле «События (через запятую, пусто = все)», поле «Секрет (опционально, мин. 16 символов)» (NSSecureTextField или обычное — секрет тут не супер-чувствителен для отображения, но раз это ключ, используй SecureTextField для консистентности с другими секретными полями в проекте). Кнопка «Зарегистрировать» → `register_webhook`. После успеха — очисти форму, обнови список.
4. Обработка `{"ok": false, "reason": "webhook_limit_reached"}` — отдельное сообщение через `BackendToast`, не как generic-ошибка.

## Жёсткие правила (иначе ревью/сборка завернёт)
- IPC СТРОГО off-main (AGENT-3): `DispatchQueue.global().async { ipcClient.call(...) }`, назад на UI через `DispatchQueue.main.async`.
- НЕ `runModal()`.
- Glyph-gate: не вводи новые non-ASCII глифы кроме уже используемых в native/; иконки — SF Symbols (`lock.fill`, `xmark.circle`, `plus.circle` и т.п., все уже используются в проекте — не выдумывай).
- `KrabEarTheme` токены как в соседних секциях.
- В конце ОБЯЗАТЕЛЬНО `swift build -c release` из native/KrabEarAgent — зелёный, без ошибок. Почини сам если есть ошибки компиляции.

## Граница
Только новый файл `HistoryPanelController+WebhookManager.swift` + ОДНА строка wiring в существующий Settings-таб (там же где регистрируются другие секции, напр. рядом с `buildRecordingSchedulerSection()`/`buildConfigPresetsSection()` — найди этот код через grep). НЕ трогай Python/backend (уже готов и полностью протестирован), НЕ другие Swift-файлы кроме точки wiring.
