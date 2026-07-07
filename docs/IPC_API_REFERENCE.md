# Krab Ear IPC API Reference

Unix socket JSON-RPC protocol. Default socket paths:
- **Production (launchd):** `~/Library/Application Support/KrabEar/krabear.sock`
- **Dev standalone:** `~/.krab_ear_data/backend.sock`

**Request format:** `{"id": "req-1", "method": "...", "params": {...}}`  
**Success response:** `{"id": "req-1", "ok": true, "result": {...}}`  
**Error response:** `{"id": "req-1", "ok": false, "error": {"code": "...", "message": "..."}}`

Live handler count: **285** (from `awk 'NR>=905&&NR<=1250' KrabEar/backend/service.py | grep -cE '"[a-z_]+":\s*(self\._|lambda)'`).  
Source of truth: `service.py` lookup table (lines 905–1233) + delegated service modules.  
Regen: Wave 745 (2026-05-26) — replaces 840-line stub doc with ~58% drift. Documented: **289 handlers** across 44 categories.

---

## Категории / Categories

1. [Recording](#recording)
2. [History — CRUD](#history--crud)
3. [History — Search & Filter](#history--search--filter)
4. [History — Tags & Favorites](#history--tags--favorites)
5. [History — Export](#history--export)
6. [History — Annotations & Enrichment](#history--annotations--enrichment)
7. [History — Backup & Restore](#history--backup--restore)
8. [History — Deduplication & Integrity](#history--deduplication--integrity)
9. [Settings & Profile Presets](#settings--profile-presets)
10. [Translation](#translation)
11. [Glossary & Vocabulary](#glossary--vocabulary)
12. [STT Management](#stt-management)
13. [Audio — Devices & Analysis](#audio--devices--analysis)
14. [Audio Import & Transcription Queue](#audio-import--transcription-queue)
15. [LLM / Rewriter](#llm--rewriter)
16. [Call Assist](#call-assist)
17. [Call Automation (Phase 3)](#call-automation-phase-3)
18. [Live Subtitles (Phase 2)](#live-subtitles-phase-2)
19. [Apple Integration](#apple-integration)
20. [Sentry / Observability / Error Bus](#sentry--observability--error-bus)
21. [Health, Diagnostics & Metrics](#health-diagnostics--metrics)
22. [Analytics & Trends](#analytics--trends)
23. [Collections & Chains](#collections--chains)
24. [Bookmarks](#bookmarks)
25. [Sharing & Webhooks](#sharing--webhooks)
26. [Transcript Versioning](#transcript-versioning)
27. [Paste Formatter & App Memory](#paste-formatter--app-memory)
28. [Templates & Quick Phrases](#templates--quick-phrases)
29. [Plugins & Feature Flags](#plugins--feature-flags)
30. [Speaker Manager](#speaker-manager)
31. [Semantic Search](#semantic-search)
32. [Scheduled Recordings](#scheduled-recordings)
33. [Obsidian Sync](#obsidian-sync)
34. [Playback Tracker](#playback-tracker)
35. [Search History](#search-history)
36. [Archive Manager](#archive-manager)
37. [Text Processing](#text-processing)
38. [Event Replay](#event-replay)
39. [Config Presets Library](#config-presets-library)
40. [Data Migrator](#data-migrator)
41. [Model Cache Manager](#model-cache-manager)
42. [Wake Word](#wake-word)
43. [TTS](#tts)
44. [Launch Readiness (2026-06-27)](#launch-readiness-2026-06-27)
45. [Event-мост IPC→REST (2026-07-07)](#event-мост-ipcrest-2026-07-07)
46. [Misc](#misc)

---

## Recording

| Метод | Описание |
|---|---|
| `ping` | Liveness check, возвращает uptime и состояние записи |
| `start_recording` | Начать захват микрофона |
| `stop_recording` | Остановить захват, запустить STT, сохранить в историю |
| `get_recording_state` | Текущее состояние записи и preview текст |
| `set_paste_status` | Обновить статус вставки для элемента истории |
| `handshake` | Инициализация соединения Swift→backend |
| `report_reconnect` | Телеметрия переподключения |
| `report_paste_failure` | Репорт ошибки вставки из Swift |
| `report_hotkey_conflict` | Репорт конфликта хоткея из Swift |

### `ping`
*(service.py → health_check_service.py)*  
Liveness check. Используется `HealthMonitor.swift` (3-секундный тик).  
Нет параметров.  
Returns: `{status, service, version, uptime_sec, is_recording, history_count}`

**Пример:**
```json
Request:  {"id":"1","method":"ping","params":{}}
Response: {"id":"1","ok":true,"result":{"status":"ok","service":"krab-ear-backend","version":"2.0.4","uptime_sec":3721,"is_recording":false,"history_count":142}}
```

### `start_recording`
*(service.py → recording_core_service.py)*  
Начать захват микрофона.  
Нет параметров.  
Returns: `{status: "recording" | "already_recording", is_recording, duration_sec, preview_text}`

**Пример:**
```json
Request:  {"id":"2","method":"start_recording","params":{}}
Response: {"id":"2","ok":true,"result":{"status":"recording","is_recording":true,"duration_sec":0.0,"preview_text":""}}
```

### `stop_recording`
*(service.py → recording_core_service.py)*  
Остановить запись, запустить STT pipeline (Whisper → cleanup → diarization → translate → LLM rewrite → paste → save).  
Params (все опциональные, fallback на runtime settings):

| Param | Type | Default | Description |
|---|---|---|---|
| `quality_profile` | string | `"balanced"` | `"fast"`, `"balanced"`, `"max"` |
| `cleanup_profile` | string | `"soft"` | `"soft"`, `"strict"` |
| `lang_hint` | string | null | ISO 639-1 language hint |
| `translation_mode` | string | from settings | `"off"`, `"ru_to_es"`, `"es_to_ru"`, `"en_to_ru"`, `"auto"`, `"bilingual_ru_es"` |
| `translate_and_paste` | bool | from settings | Вставить переведённый текст |
| `stop_tail_trim_ms` | int | `180` | Обрезать N мс с конца аудио (0–1200) |

Returns: `{status, text, original_text, translated_text, translation_status, translation_mode, source_lang, target_lang, history_id, ts, duration_sec, silence_detected, background_guard_rejected, ...}`  
Статусы: `"ok"`, `"empty_audio"`, `"empty_text"`, `"already_stopped"`

### `get_recording_state`
*(service.py → recording_core_service.py)*  
Текущее состояние записи и preview текст.  
Нет параметров.  
Returns: `{is_recording, duration_sec, preview_text}`

### `set_paste_status`
*(service.py)*  
Обновить результат вставки для элемента истории (сообщает backend успех/ошибку paste).  
Params: `{id}` (str, required), `{paste_status}` (str: `"ok"` | `"failed"`)  
Returns: `{updated, id, paste_status}`

### `handshake`
*(service.py)*  
Swift→backend инициализация при подключении. Обмен версиями и capabilities.  
Params: `{agent_version?, capabilities?}`  
Returns: `{backend_version, protocol_version, capabilities}`

### `report_reconnect`
*(service.py)*  
Swift→backend телеметрия переподключения. Публикует `ipc.reconnect` info event.  
Params: `{reason?}`  
Returns: `{ok: true}`

### `report_paste_failure`
*(service.py)*  
Репорт из Swift когда paste не удался (AX denied / app unsupported).  
Params: `{reason, app_bundle_id?, error_code?}`  
Returns: `{ok: true}`

### `report_hotkey_conflict`
*(service.py)*  
Репорт из Swift когда `RegisterEventHotKey` возвращает `eventHotKeyExistsErr`.  
Params: `{hotkey_description?}`  
Returns: `{ok: true}`

---

## History — CRUD

| Метод | Описание |
|---|---|
| `get_history_page` | Страница истории с фильтрами |
| `get_history_item` | Полные детали записи по ID |
| `add_history_item` | Добавить запись вручную |
| `delete_history_item` | Мягкое удаление по ID (tombstone) |
| `cleanup_old_history` | Удалить записи старше N дней |
| `compact_history` | Уплотнить NDJSON, удалить tombstone |
| `import_history_ndjson` | Импорт из внешнего NDJSON файла |

### `get_history_page`
*(history_service.py)*  
Paginated history с фильтрами по языку, дате, confidence.  
Params: `{page?, page_size?, lang?, date_from?, date_to?, min_confidence?}`  
Returns: `{items: [...], total, page, page_size, has_more}`

### `get_history_item`
*(history_service.py)*  
Возвращает полные детали одной записи истории по ID.  
Params: `{id}` (str, required)  
Returns: HistoryItem object (все поля включая speaker_turns, action_items, tags)

### `add_history_item`
*(history_service.py)*  
Вручную добавить запись в историю.  
Params: `{text, ts?, lang?, duration_sec?, paste_status?, ...}`  
Returns: полный dict добавленной записи (`item.to_dict()`) — содержит `id`, `ts`, `text`, `lang`, `duration_sec`, `paste_status` и прочие поля HistoryItem (не только `{id, ts}`).

### `delete_history_item`
*(history_service.py)*  
Мягкое удаление записи (tombstone в NDJSON; реальная запись остаётся до compact).  
Params: `{id}` (str, required)  
Returns: `{deleted, id}`

### `cleanup_old_history`
*(history_service.py)*  
Удаляет записи истории старше указанного числа дней (tombstone).  
Params: `{days}` (int, required) — записи с ts старше days удаляются  
Returns: `{deleted}`

### `compact_history`
*(history_service.py)*  
Уплотнить NDJSON файл — удалить tombstones, переписать активные записи.  
Нет params.  
Returns: `{ok, before_count, after_count}`

### `import_history_ndjson`
*(history_service.py)*  
Импортирует историю из внешнего NDJSON файла с merge (дубли по ID пропускаются).  
Params: `{path}` (str, absolute path)  
Returns: `{imported, skipped, errors}`

---

## History — Search & Filter

| Метод | Описание |
|---|---|
| `search_history` | Полнотекстовый поиск |
| `fuzzy_search` | Нечёткий поиск |
| `search_with_highlights` | Поиск с подсветкой совпадений |
| `search_by_speaker` | Фильтр по спикеру диаризации |
| `filter_by_confidence` | Фильтр по STT confidence |
| `get_history_overview` | Обзорный срез истории |
| `get_history_stats` | Размер файлов и счётчики |
| `get_history_statistics` | Агрегированная статистика |
| `word_frequency_analysis` | Частотный анализ слов |
| `find_duplicates` | Поиск дублей по текстовому сходству |
| `get_clipboard_history` | Последние N вставленных транскрипций |
| `repaste_item` | Повторная вставка элемента clipboard |
| `get_storage_info` | Размер файлов данных |
| `get_transcripts_path` | Путь к папке транскриптов |

### `search_history`
*(history_service.py)*  
Полнотекстовый поиск по транскрипциям.  
Params: `{query, limit?, date_from?, date_to?, lang?}`  
Returns: `{items: [...], total}`

### `fuzzy_search`
*(history_service.py)*  
Нечёткий поиск по истории транскрипций (approximate string matching).  
Params: `{query, limit?, threshold?}` (threshold: float 0–1, default 0.7)  
Returns: `{items: [...], scores: [...]}`

### `search_with_highlights`
*(history_service.py)*  
Поиск по истории с подсветкой совпадений в результатах.  
Params: `{query, limit?}`  
Returns: `{items: [{...item, highlights: [...]}], total}`

### `search_by_speaker`
*(history_service.py)*  
Возвращает записи истории, в которых участвует указанный спикер.  
Params: `{speaker_id}` (str)  
Returns: `{items: [...]}`

### `filter_by_confidence`
*(history_service.py)*  
Возвращает записи истории, отфильтрованные по STT confidence score.  
Params: `{min_confidence?, max_confidence?, limit?}` (float 0–1)  
Returns: `{items: [...]}`

### `get_history_overview`
*(history_service.py)*  
Возвращает обзорный срез истории для панели управления.  
Params: `{limit?}` (опционально)  
Returns: `{total, recent: [...], oldest_ts, newest_ts, avg_duration_sec}`

### `get_history_stats`
*(history_service.py)*  
Возвращает состояние журналов истории и оценку размера.  
Нет params.  
Returns: `{active_count, deleted_count, file_size_bytes, ...}`

### `get_history_statistics`
*(history_service.py)*  
Агрегирует статистику по всем активным записям истории за один проход.  
Params: `{date_from?, date_to?}`  
Returns: `{total_recordings, total_duration_sec, total_words, avg_confidence, languages: {...}, ...}`

### `word_frequency_analysis`
*(history_service.py)*  
Анализирует частоту слов по истории транскрипций.  
Params: `{limit?, lang?, date_from?, date_to?}`  
Returns: `{words: [{word, count}, ...], total_unique}`

### `find_duplicates`
*(history_service.py)*  
Находит дублирующиеся транскрипции в истории по текстовому сходству.  
Params: `{threshold?, limit?}` (threshold: float 0–1)  
Returns: `{groups: [[id1, id2, ...], ...], total_groups}`

### `get_clipboard_history`
*(history_service.py)*  
Возвращает последние N вставленных транскрипций из in-memory clipboard_history (≤20).  
Нет params.  
Returns: `{items: [{id, text, ts}, ...]}`

### `repaste_item`
*(history_service.py)*  
Находит текст по history_id в clipboard_history и возвращает для повторной вставки.  
Params: `{id}` (str)  
Returns: `{text, id}`

### `get_storage_info`
*(history_service.py)*  
Возвращает информацию о размере файлов данных Krab Ear.  
Нет params.  
Returns: `{history_bytes, settings_bytes, transcripts_dir_bytes, total_bytes}`

### `get_transcripts_path`
*(history_service.py)*  
Возвращает путь к папке транскриптов и создаёт её при необходимости.  
Нет params.  
Returns: `{path}` (str, абсолютный путь)

---

## History — Tags & Favorites

| Метод | Описание |
|---|---|
| `add_tag` | Добавить тег к записи |
| `remove_tag` | Удалить тег из записи |
| `get_tags` | Теги конкретной записи |
| `search_by_tag` | Записи с указанным тегом |
| `list_all_tags` | Все уникальные теги с count |
| `toggle_favorite` | Переключить флаг избранного |
| `get_favorites` | Все избранные записи |
| `is_favorite` | Проверить, находится ли запись в избранном |

### `add_tag`
*(history_service.py)*  
Добавляет тег к записи истории.  
Params: `{id, tag}` (str, str)  
Returns: `{ok, id, tags: [...]}`

### `remove_tag`
*(history_service.py)*  
Удаляет тег из записи истории.  
Params: `{id, tag}` (str, str)  
Returns: `{ok, id, tags: [...]}`

### `get_tags`
*(history_service.py)*  
Возвращает все теги для конкретной записи.  
Params: `{id}` (str)  
Returns: `{id, tags: [...]}`

### `search_by_tag`
*(history_service.py)*  
Возвращает записи истории с указанным тегом.  
Params: `{tag}` (str)  
Returns: `{items: [...], total}`

### `list_all_tags`
*(history_service.py)*  
Возвращает все уникальные теги с количеством использований.  
Нет params.  
Returns: `{tags: [{tag, count}, ...]}`

### `toggle_favorite`
*(history_service.py)*  
Переключает флаг избранного для записи истории.  
Params: `{id}` (str)  
Returns: `{id, is_favorite}`

### `get_favorites`
*(history_service.py)*  
Возвращает все избранные записи, отсортированные по времени (новые первыми).  
Нет params.  
Returns: `{items: [...]}`

### `is_favorite`
*(history_service.py)*  
Проверяет, находится ли запись в избранном.  
Params: `{id}` (str)  
Returns: `{id, is_favorite}`

---

## History — Export

| Метод | Описание |
|---|---|
| `export_history` | Экспорт в Markdown |
| `export_history_srt` | Экспорт в формат SRT субтитров |
| `export_history_csv` | Экспорт в CSV |
| `export_history_json` | Экспорт в JSON |
| `export_history_markdown` | Экспорт в Markdown (явный) |
| `export_obsidian` | Экспорт в Obsidian-совместимый Markdown |
| `export_html_report` | Экспорт в автономный HTML отчёт |
| `generate_html_report` | Алиас для Swift UI Analytics Dashboard |
| `batch_export` | Пакетный экспорт в нескольких форматах |

### `export_history`
*(history_service.py)*  
Экспортирует всю историю в формате Markdown с метаданными и диаризацией.  
Params: `{date_from?, date_to?, limit?}`  
Returns: `{content}` (str, Markdown)

### `export_history_srt`
*(history_service.py)*  
Экспортирует запись истории в формате SRT-субтитров (по speaker_turns).  
Params: `{id}` (str)  
Returns: `{content}` (str, SRT)

### `export_history_csv`
*(history_service.py)*  
Экспорт истории в CSV формат.  
Params: `{date_from?, date_to?}`  
Returns: `{content}` (str, CSV)

### `export_history_json`
*(history_service.py)*  
Экспортирует историю транскрипций в структурированный JSON.  
Params: `{date_from?, date_to?, limit?}`  
Returns: `{content}` (str, JSON)

### `export_history_markdown`
*(history_service.py)*  
Экспортирует историю транскрипций в формате Markdown.  
Params: `{date_from?, date_to?}`  
Returns: `{content}` (str, Markdown)

### `export_obsidian`
*(history_service.py)*  
Экспортирует транскрипции в формат Obsidian-совместимого Markdown с YAML frontmatter.  
Params: `{date_from?, date_to?}`  
Returns: `{content}` (str)

### `export_html_report`
*(history_service.py)*  
Экспортирует историю транскрипций в автономный HTML-отчёт с аналитикой.  
Params: `{date_from?, date_to?}` (опциональные)  
Returns: `{content}` (str, HTML)

### `generate_html_report`
*(history_service.py)*  
Алиас для `export_html_report` — используется из Analytics Dashboard в Swift UI.  
_(documented in history_service.py)_

### `batch_export`
*(history_service.py)*  
Экспортирует историю в нескольких форматах одновременно.  
Params: `{formats: ["json","csv","srt",...], date_from?, date_to?}`  
Returns: `{exports: {format: content, ...}}`

---

## History — Annotations & Enrichment

| Метод | Описание |
|---|---|
| `set_annotation` | Сохранить заметку к записи |
| `get_annotation` | Получить заметку |
| `search_annotations` | Полнотекстовый поиск по заметкам |
| `enrich_recording` | Авто-заполнение метаданных записи |
| `auto_summarize_batch` | LLM резюме для пакета транскрипций |
| `list_summary_profiles` | Список профилей резюмирования |
| `add_summary_profile` | Добавить профиль резюмирования |
| `generate_auto_title` | Автоматический заголовок |

### `set_annotation`
*(history_service.py)*  
Сохраняет текстовую заметку к записи истории.  
Params: `{id, note}` (str, str)  
Returns: `{ok, id}`

### `get_annotation`
*(history_service.py)*  
Возвращает заметку для записи истории.  
Params: `{id}` (str)  
Returns: `{id, note}` (note может быть null)

### `search_annotations`
*(history_service.py)*  
Полнотекстовый поиск по пользовательским заметкам.  
Params: `{query, limit?}`  
Returns: `{items: [...]}`

### `enrich_recording`
*(metadata_enricher.py)*  
Авто-заполняет поля метаданных: language, sentence_count, word_count, keywords.  
Params: `{id}` (str)  
Returns: `{ok, id, enriched_fields: {...}}`

### `auto_summarize_batch`
*(history_service.py)*  
Генерирует ОДНО сводное LLM-резюме для пакета транскрипций (агрегат, не per-item).  
Params: `{ids?: [...], limit?, profile?}` — пустой/невалидный `ids` → `ok=False`.  
Returns: `{summary, key_points: [...], items_processed, total_words, llm, fallback, error, profile}`  
*(не per-item `summaries` — это единый дайджест по всему пакету.)*

### `list_summary_profiles`
*(history_service.py)*  
Возвращает список всех профилей резюмирования (встроенных + кастомных).  
Нет params.  
Returns: `{profiles: [{name, description, prompt_template}, ...]}`

### `add_summary_profile`
*(history_service.py)*  
Добавляет или заменяет кастомный профиль резюмирования.  
Params: `{name, description, prompt_template}`  
Returns: `{ok, name}`

### `generate_auto_title`
*(service.py)*  
Генерирует автоматический заголовок для транскрибации эвристически.  
Params: `{id?, text?}` — одно из двух required  
Returns: `{title}` (str)

---

## History — Backup & Restore

| Метод | Описание |
|---|---|
| `backup_history` | Создать timestamped резервную копию |
| `restore_history` | Восстановить из резервной копии |
| `list_backups` | Список резервных копий |
| `configure_auto_export` | Настроить расписание авто-экспорта |
| `list_auto_exports` | Список файлов авто-экспорта |

### `backup_history`
*(history_service.py)*  
Создаёт timestamped-резервную копию `history.ndjson` и `settings.json`.  
Нет params.  
Returns: `{path, size_bytes, ts}`

### `restore_history`
*(history_service.py)*  
Восстанавливает историю из резервной копии (текущий файл заменяется).  
Params: `{backup_name}` (str)  
Returns: `{ok, restored_count}`

### `list_backups`
*(history_service.py)*  
Возвращает список доступных резервных копий с метаданными.  
Нет params.  
Returns: `{backups: [{name, ts, size_bytes}, ...]}`

### `configure_auto_export`
*(service.py)*  
Настраивает расписание авто-экспорта транскрипций.  
Params: `{enabled, interval_sec?, format?, output_dir?}`  
Returns: `{ok, config}`

### `list_auto_exports`
*(service.py)*  
Список файлов авто-экспорта.  
Нет params.  
Returns: `{exports: [...]}`

---

## History — Deduplication & Integrity

| Метод | Описание |
|---|---|
| `check_duplicate` | Проверить одну транскрипцию на дублирование |
| `run_deduplication` | Полное сканирование истории на дубли |
| `get_dedup_stats` | Статистика дедупликатора |
| `check_integrity` | Проверка целостности данных |
| `repair_integrity` | Исправление проблем целостности |

### `check_duplicate`
*(service.py)*  
Проверяет, является ли текст дубликатом существующей записи в истории.  
Params: `{text, threshold?}` (float 0–1, default 0.9)  
Returns: `{is_duplicate, matched_id?, similarity?}`

### `run_deduplication`
*(service.py)*  
Сканирует всю историю и возвращает отчёт о дублирующихся транскрипциях.  
Params: `{threshold?, auto_delete?}`  
Returns: `{groups: [...], total_groups, deleted?}`

### `get_dedup_stats`
*(service.py)*  
Возвращает статистику дедупликатора за текущую сессию.  
Нет params.  
Returns: `{checked, duplicates_found, chars_saved}`

### `check_integrity`
*(health_check_service.py)*  
Проверяет целостность файлов данных Krab Ear (history.ndjson, settings.json).  
Нет params.  
Returns: `{ok, issues: [...], checked_files}`

### `repair_integrity`
*(service.py)*  
Исправляет автоматически устраняемые проблемы целостности данных.  
Нет params.  
Returns: `{ok, repaired, issues_remaining}`

---

## Settings & Profile Presets

| Метод | Описание |
|---|---|
| `get_settings` | Все текущие настройки |
| `set_settings` | Обновить настройки (partial) |
| `apply_profile_preset` | Применить пресет профиля |
| `list_profile_presets` | Список пресетов профилей |
| `get_notification_preferences` | Настройки уведомлений |
| `set_notification_preferences` | Обновить уведомления |
| `export_settings` | Экспорт в JSON файл |
| `import_settings` | Импорт из JSON файла |
| `list_settings_backups` | Список rolling бэкапов |
| `restore_settings_backup` | Восстановить из бэкапа |
| `create_manual_settings_backup` | Ручной бэкап настроек |

### `get_settings`
*(settings_service.py)*  
Возвращает все текущие настройки (из кэша с TTL 5 с).  
Нет params.  
Returns: `{settings: {...}}` — полный словарь настроек

### `set_settings`
*(settings_service.py)*  
Обновляет одно или несколько полей настроек. Принимает любое подмножество.  
Params: `{key: value, ...}`  
Returns: `{ok, updated_keys: [...]}`

### `apply_profile_preset`
*(settings_service.py)*  
Применяет пресет настроек профиля, сохраняет и сбрасывает кэш.  
Params: `{preset_name}` — `"default"`, `"meeting"`, `"translation"`, `"call_recording"`  
Returns: `{ok, preset_name, applied_settings: {...}}`

### `list_profile_presets`
*(settings_service.py)*  
Возвращает список доступных пресетов профилей с описаниями и значениями.  
Нет params.  
Returns: `{presets: [{name, description, settings}, ...]}`

### `get_notification_preferences`
*(settings_service.py)*  
Возвращает текущие настройки уведомлений из хранилища настроек.  
Нет params.  
Returns: `{notify_confidence_warn, notify_errors, notify_restart, ...}`

### `set_notification_preferences`
*(settings_service.py)*  
Обновляет настройки уведомлений. Принимает любое подмножество полей.  
Params: `{notify_confidence_warn?, notify_errors?, ...}`  
Returns: `{ok}`

### `export_settings`
*(settings_service.py)*  
Экспортирует текущие настройки в JSON-файл, исключая чувствительные поля (API ключи).  
Params: `{path}` (str, absolute path)  
Returns: `{ok, path, excluded_fields: [...]}`

### `import_settings`
*(settings_service.py)*  
Импортирует настройки из JSON-файла.  
Params: `{path}` (str)  
Returns: `{ok, imported_keys: [...]}`

### `list_settings_backups`
*(settings_service.py)*  
Возвращает список бэкапов настроек, от новых к старым.  
Нет params.  
Returns: `{backups: [{name, ts, reason}, ...]}`

### `restore_settings_backup`
*(settings_service.py)*  
Восстанавливает настройки из указанного бэкапа и сохраняет их.  
Params: `{backup_name}` (str)  
Returns: `{ok, backup_name}`

### `create_manual_settings_backup`
*(settings_service.py)*  
Создаёт ручной бэкап текущих настроек с произвольной причиной.  
Params: `{reason?}` (str)  
Returns: `{ok, backup_name}`

---

## Translation

| Метод | Описание |
|---|---|
| `translate_text` | Перевод текста |
| `translate_selection` | Перевод выделенного текста из любого приложения |

### `translate_text`
*(translation_service.py)*  
Отдельная IPC-команда перевода текста для UI и будущих workflow.  
Params: `{text, mode?}` — mode: `"ru_to_es"`, `"es_to_ru"`, `"en_to_ru"`, `"auto"`, `"bilingual_ru_es"`  
Returns: `{translated, source_lang, target_lang, mode}`

### `translate_selection`
*(translation_service.py)*  
Переводит выделенный текст из любого приложения (Phase 2A workflow — Cmd+Shift+T).  
Params: `{text, mode?}`  
Returns: `{translated, source_lang, target_lang}`

---

## Glossary & Vocabulary

| Метод | Описание |
|---|---|
| `set_translation_glossary_item` | Добавить/обновить пару глоссария |
| `remove_translation_glossary_item` | Удалить пару из глоссария |
| `get_glossary_suggestions` | Предложения для глоссария из истории |
| `suggest_medical_glossary_terms` | Мед. термины auto-learn |
| `apply_glossary_suggestions` | Применить выбранные предложения |
| `export_glossary_csv` | Экспорт глоссария в CSV |
| `import_glossary_csv` | Импорт CSV в глоссарий |
| `get_vocabulary_suggestions` | Предложения для STT vocabulary |
| `get_smart_vocabulary_suggestions` | Умные предложения vocabulary из паттернов |
| `add_stt_hotword` | Добавить STT hotword |
| `remove_stt_hotword` | Удалить STT hotword |
| `list_stt_hotwords` | Список STT hotwords |
| `add_hotword` | Добавить триггерное слово |
| `remove_hotword` | Удалить триггерное слово |
| `get_hotwords` | Список триггерных слов |
| `check_hotwords` | Проверить текст на триггерные слова |

### `set_translation_glossary_item`
*(translation_service.py)*  
Добавляет/обновляет одну пару глоссария перевода (`source→target`).  
Params: `{source, target, lang_pair?}`  
Returns: `{ok, glossary_size}`

### `remove_translation_glossary_item`
*(translation_service.py)*  
Удаляет одну пару из глоссария перевода.  
Params: `{source}` (str)  
Returns: `{ok, removed}`

### `get_glossary_suggestions`
*(translation_service.py)*  
Анализирует историю переводов и предлагает пары `source→target` для глоссария.  
Params: `{limit?}`  
Returns: `{suggestions: [{source, target, confidence}, ...]}`

### `suggest_medical_glossary_terms`
*(glossary_auto_learn.py)*  
Мед. домен auto-learn: предлагает пары ES↔RU из истории переводов.  
Params: `{limit?}`  
Returns: `{suggestions: [{source, target, domain}, ...]}`

### `apply_glossary_suggestions`
*(glossary_auto_learn.py)*  
Применяет выбранные мед. термины в `translation_glossary`.  
Params: `{suggestions: [{source, target}]}`  
Returns: `{ok, applied}`

### `export_glossary_csv`
*(service.py)*  
Экспортирует `translation_glossary` в CSV-строку.  
Нет params.  
Returns: `{csv}` (str)

### `import_glossary_csv`
*(service.py)*  
Импортирует CSV в `translation_glossary` (merge или replace).  
Params: `{csv, mode?}` — mode: `"merge"` | `"replace"`, default `"merge"`  
Returns: `{ok, imported, skipped}`

### `get_vocabulary_suggestions`
*(translation_service.py)*  
Анализирует историю транскрибаций и предлагает слова для STT vocabulary.  
Params: `{limit?}`  
Returns: `{suggestions: [{word, frequency}, ...]}`

### `get_smart_vocabulary_suggestions`
*(service.py)*  
Предложения для словаря STT на основе паттернов использования.  
Params: `{limit?}`  
Returns: `{suggestions: [{word, score, reason}, ...]}`

### `add_stt_hotword`
*(stt_management_service.py)*  
Добавляет термин в список STT hotwords (используются в initial_prompt Whisper).  
Params: `{word}` (str)  
Returns: `{ok, word, total}`

### `remove_stt_hotword`
*(stt_management_service.py)*  
Удаляет термин из списка STT hotwords.  
Params: `{word}` (str)  
Returns: `{ok, removed}`

### `list_stt_hotwords`
*(stt_management_service.py)*  
Возвращает текущий список STT hotwords.  
Нет params.  
Returns: `{hotwords: [...], total}`

### `add_hotword`
*(hotword_detector.py)*  
Добавляет горячее слово для отслеживания в транскрипциях.  
Params: `{word}` (str)  
Returns: `{ok}`

### `remove_hotword`
*(hotword_detector.py)*  
Удаляет горячее слово.  
Params: `{word}` (str)  
Returns: `{ok}`

### `get_hotwords`
*(hotword_detector.py)*  
Список горячих слов для детектора.  
Нет params.  
Returns: `{hotwords: [...]}`

### `check_hotwords`
*(hotword_detector.py)*  
Проверяет текст на наличие горячих слов.  
Params: `{text}` (str)  
Returns: `{found: [...], count}`

---

## STT Management

| Метод | Описание |
|---|---|
| `warmup_stt` | Ручной STT warmup |
| `warmup_rewriter` | Ручной LLM warmup probe |
| `select_model` | Умный выбор STT модели |
| `get_stt_routing_decision` | Debug: результат scored adapter selection |
| `list_normalization_profiles` | Список профилей нормализации текста |

### `warmup_stt`
*(stt_management_service.py)*  
Ручной запуск STT warmup — полезен после смены профиля или модели.  
Нет params.  
Returns: `{ok, model, duration_ms}`

### `warmup_rewriter`
*(service.py)*  
Ручной запуск LLM rewriter warmup probe (для кнопки "Load Model" в GUI).  
Нет params.  
Returns: `{ok, status, latency_ms}`

### `select_model`
*(stt_management_service.py)*  
Умный выбор STT-модели на основе условий записи (длительность, нагрузка, язык).  
Params: `{duration_hint_sec?, lang_hint?}`  
Returns: `{model, reason}`

### `get_stt_routing_decision`
*(stt_management_service.py)*  
Возвращает результат scored STT adapter selection для отладки.  
Params: `{lang?, duration_sec?}`  
Returns: `{adapters: [{name, score, reason}, ...], selected}`

### `list_normalization_profiles`
*(service.py)*  
Возвращает список всех профилей нормализации текста.  
Нет params.  
Returns: `{profiles: [{name, description}, ...]}`

---

## Audio — Devices & Analysis

| Метод | Описание |
|---|---|
| `list_audio_inputs` | Список аудиовходов для GUI пикера |
| `get_audio_devices` | Список аудиоустройств с деталями |
| `test_microphone` | Тест микрофона: RMS/peak |
| `check_mic_noise` | Pre-flight: RMS/peak + профиль фонового шума |
| `analyze_audio_quality` | Pre-flight анализ качества аудиофайла |
| `analyze_silence` | Обнаружение тишины в аудиофайле |
| `get_audio_info` | Метаданные аудиофайла |
| `get_waveform` | Waveform данные для GUI |
| `check_audio_duplicate` | Аудио-фингерпринтинг для обнаружения дублей |
| `profile_noise` | Профилирование фонового шума |
| `analyze_word_timing` | Анализ ритма речи по таймстемпам |

### `list_audio_inputs`
*(service.py → recording_core_service.py)*  
Список доступных аудиовходов для GUI пикера.  
Нет params.  
Returns: `{inputs: [{index, name, channels, sample_rate}, ...]}`

### `get_audio_devices`
*(service.py → recording_core_service.py)*  
Список аудиоустройств с деталями (все входы sounddevice).  
Нет params.  
Returns: `{devices: [{index, name, channels, sample_rate, default}, ...]}`

### `test_microphone`
*(service.py)*  
Записывает короткий фрагмент аудио и возвращает RMS/peak уровни.  
Params: `{duration_sec?}` (default 2.0)  
Returns: `{rms, peak, ok}`  
Throttle: heavy (≤5/min — синхронная запись на IPC-треде).

### `check_mic_noise`
*(service.py → core.NoiseProfiler)*  
Pre-flight проверка микрофона: записывает короткий фрагмент и профилирует фоновый шум (надмножество `test_microphone`; `NoiseProfiler.profile()` по in-memory массиву, без временного файла).  
Params: `{duration_sec?}` (default 2.0, clamp ≤5.0)  
Returns: `{ok, rms, peak, noise: {noise_type, noise_level_db, snr_db, frequency_profile, recommendations, suitable_for_stt}, devices}`  
Throttle: heavy (≤5/min — запись + FFT на IPC-треде).

### `analyze_audio_quality`
*(audio_analytics_service.py)*  
Pre-flight анализ качества аудиофайла перед транскрипцией (RMS, SNR, clipping, silence ratio).  
Params: `{path}` (str, absolute path)  
Returns: `{rms, peak, snr_db, clipping_ratio, silence_ratio, recommendation}`

### `analyze_silence`
*(audio_analytics_service.py)*  
Обнаруживает участки тишины в аудиофайле.  
Params: `{path, threshold?}`  
Returns: `{silence_regions: [{start_sec, end_sec}], speech_ratio}`

### `get_audio_info`
*(audio_analytics_service.py)*  
Возвращает метаданные аудиофайла (длительность, sample_rate, channels, codec).  
Params: `{path}` (str)  
Returns: `{duration_sec, sample_rate, channels, codec, size_bytes}`

### `get_waveform`
*(audio_analytics_service.py)*  
Генерирует waveform-данные из аудиофайла для GUI-визуализации.  
Params: `{path, points?}` (int, default 200)  
Returns: `{waveform: [...], duration_sec}`

### `check_audio_duplicate`
*(audio_analytics_service.py)*  
Проверяет, являются ли два аудио-сигнала дубликатами по фингерпринту.  
Params: `{path1, path2, threshold?}`  
Returns: `{is_duplicate, similarity}`

### `profile_noise`
*(audio_analytics_service.py)*  
Профилирует фоновый шум в аудиофайле: тип, уровень, SNR, рекомендации.  
Params: `{path}` (str)  
Returns: `{noise_type, level_db, snr_db, recommendations: [...]}`

### `analyze_word_timing`
*(audio_analytics_service.py)*  
Анализирует ритм речи по пословным таймстемпам Whisper.  
Params: `{id}` (str, history_id) или `{segments: [...]}`  
Returns: `{wpm, hesitations: [...], pace_category}`

---

## Audio Import & Transcription Queue

| Метод | Описание |
|---|---|
| `transcribe_paths` | Синхронная транскрипция файлов |
| `transcribe_paths_async` | Асинхронная транскрипция (job) |
| `get_transcribe_progress` | Прогресс job'а |
| `cancel_transcribe_job` | Отмена job'а |
| `preview_transcribe_paths` | Preview без сохранения |
| `enqueue_transcription` | Добавить в очередь транскрипции |
| `cancel_transcription` | Отменить задание по job_id |
| `get_queue_status` | Статус задания по job_id |
| `list_transcription_queue` | Список всех заданий очереди |

### `transcribe_paths`
*(service.py → recording_core_service.py)*  
Синхронная транскрипция одного или нескольких аудиофайлов.  
Params: `{paths: [...], quality_profile?, lang_hint?, translation_mode?}`  
Returns: `{results: [{path, text, history_id, ...}]}`

### `transcribe_paths_async`
*(service.py → recording_core_service.py)*  
Асинхронная транскрипция файлов в фоновом job с прогрессом.  
Params: `{paths: [...], quality_profile?, lang_hint?}`  
Returns: `{job_id}` — используйте `get_transcribe_progress` для опроса

### `get_transcribe_progress`
*(service.py → recording_core_service.py)*  
Опрос прогресса асинхронного job'а транскрипции.  
Params: `{job_id}` (str)  
Returns: `{job_id, status, progress_pct, results?, error?}`

### `cancel_transcribe_job`
*(service.py → recording_core_service.py)*  
Запрос отмены асинхронного job'а.  
Params: `{job_id}` (str)  
Returns: `{ok, job_id, status}`

### `preview_transcribe_paths`
*(service.py → recording_core_service.py)*  
Preview транскрипции файлов без сохранения в историю.  
Params: `{paths: [...], quality_profile?}`  
Returns: `{results: [{path, text, duration_sec}]}`

### `enqueue_transcription`
*(transcription_queue.py)*  
Добавляет аудиофайл в очередь транскрипции с приоритетом.  
Params: `{path, priority?}` (int 0–10, default 5)  
Returns: `{job_id, position}`

### `cancel_transcription`
*(transcription_queue.py)*  
Отменяет задание транскрипции по job_id.  
Params: `{job_id}` (str)  
Returns: `{ok, job_id}`

### `get_queue_status`
*(transcription_queue.py)*  
Статус задания транскрипции по job_id.  
Params: `{job_id}` (str)  
Returns: `{job_id, status, progress_pct, result?, error?}`

### `list_transcription_queue`
*(transcription_queue.py)*  
Список всех заданий очереди транскрипции.  
Нет params.  
Returns: `{jobs: [{job_id, path, status, priority, created_at}, ...]}`

---

## LLM / Rewriter

| Метод | Описание |
|---|---|
| `list_llm_models` | Список моделей из LM Studio |
| `probe_llm_http` | Ping LM Studio HTTP endpoint |
| `get_last_llm_diff` | Последний word-level diff от rewriter'а |
| `extract_action_items` | Извлечение задач из транскрипта |
| `batch_extract_action_items` | Пакетное извлечение |
| `get_pending_action_items` | Items без action_items |
| `summarize_text` | Lightweight summary текста |
| `summarize_item` | LLM summary элемента истории |

### `list_llm_models`
*(service.py)*  
Возвращает список моделей доступных в LM Studio через `/api/v1/models`.  
Нет params.  
Returns: `{models: [{id, name, ...}]}`

### `probe_llm_http`
*(service.py → health_check_service.py)*  
Однократный ping LM Studio HTTP endpoint. Возвращает `reachable`, `latency_ms`, `model`.  
Нет params.  
Returns: `{reachable, latency_ms, model?, error?}`

### `get_last_llm_diff`
*(service.py)*  
Возвращает последний word-level diff от LLM rewriter'а (для debug панели).  
Нет params.  
Returns: `{diff: [{op, text}, ...], before, after}`

### `extract_action_items`
*(service.py)*  
Извлекает задачи/решения/вопросы из транскрипта по item_id через LLM.  
Params: `{id}` (str)  
Returns: `{tasks: [...], decisions: [...], questions: [...], priority_tags: [...]}`

### `batch_extract_action_items`
*(service.py)*  
Пакетное извлечение задач/решений/вопросов для нескольких item_id.  
Params: `{ids: [...]}`  
Returns: `{results: [{id, tasks, decisions, questions}], failed: [...]}`

### `get_pending_action_items`
*(service.py)*  
Возвращает все items у которых `action_items=None` (ещё не анализировались).  
Нет params.  
Returns: `{items: [...]}`

### `summarize_text`
*(text_processing_service.py)*  
Локальный lightweight-summary для длинных транскриптов.  
Params: `{text, max_sentences?}`  
Returns: `{summary}`

### `summarize_item`
*(text_processing_service.py)*  
LLM-summary для элемента истории по ID.  
Params: `{id}` (str)  
Returns: `{summary, id}`

---

## Call Assist

| Метод | Описание |
|---|---|
| `start_call_assist` | Запустить сессию ассистента звонка |
| `stop_call_assist` | Остановить сессию |
| `get_call_assist_state` | Текущее состояние |
| `call_assist_diagnostics` | Диагностика и explain-пакет |
| `call_assist_summary` | Summary текущей сессии |
| `call_assist_quick_phrase` | Отправить быструю фразу |
| `list_call_assist_quick_phrases` | Библиотека быстрых фраз |
| `call_assist_cost_estimate` | Оценка telephony+AI стоимости |
| `call_assist_timeline` | Timeline текущей сессии |
| `call_assist_timeline_stats` | Статистика timeline |
| `call_assist_timeline_summary` | Summary timeline |
| `call_assist_timeline_export` | Экспорт timeline |
| `call_assist_timeline_clear` | Очистить timeline |
| `call_assist_timeline_to_history` | Сохранить timeline в историю |
| `call_assist_list_templates` | Список шаблонов быстрых реплик |
| `call_assist_add_template` | Добавить шаблон |
| `call_assist_remove_template` | Удалить шаблон |
| `call_assist_template` | Отправить шаблонную реплику |
| `call_assist_cost_report` | Detailed cost report |

### `start_call_assist`
*(call_assist_service.py)*  
Запускает сессию ассистента звонка с интеграцией Voice Gateway.  
Params: `{session_id?, lang?}`  
Returns: `{ok, session_id, state}`

### `stop_call_assist`
*(call_assist_service.py)*  
Останавливает текущую сессию ассистента звонка.  
Нет params.  
Returns: `{ok, session_id, duration_sec}`

### `get_call_assist_state`
*(call_assist_service.py)*  
Возвращает текущее состояние сессии call assist.  
Нет params.  
Returns: `{active, session_id?, state?}`

### `call_assist_diagnostics`
*(call_assist_service.py)*  
Возвращает diagnostics и explain-пакет почему перевод не появился.  
Нет params.  
Returns: `{state, gateway_connected, last_error?, explain}`

### `call_assist_summary`
*(call_assist_service.py)*  
Запрашивает summary текущей звонковой сессии.  
Нет params.  
Returns: `{summary, duration_sec, phrase_count}`

### `call_assist_quick_phrase`
*(call_assist_service.py)*  
Отправляет быструю фразу на перевод/озвучку в Voice Gateway.  
Params: `{phrase, lang?}`  
Returns: `{ok, translated?}`

### `list_call_assist_quick_phrases`
*(call_assist_service.py)*  
Возвращает библиотеку быстрых фраз из Voice Gateway.  
Нет params.  
Returns: `{phrases: [{id, text, category}, ...]}`

### `call_assist_cost_estimate`
*(call_assist_service.py)*  
Считает оценку telephony+AI стоимости через Voice Gateway.  
Params: `{country?, duration_min?}`  
Returns: `{estimated_cost_usd, breakdown}`

### `call_assist_timeline`
*(call_assist_service.py)*  
Возвращает timeline текущей звонковой сессии.  
Нет params.  
Returns: `{timeline: [{ts, speaker, text, translated?}, ...]}`

### `call_assist_timeline_stats`
*(call_assist_service.py)*  
Статистика timeline (счётчики реплик, спикеры).  
_(documented in call_assist_service.py)_

### `call_assist_timeline_summary`
*(call_assist_service.py)*  
Summary timeline текущей сессии.  
_(documented in call_assist_service.py)_

### `call_assist_timeline_export`
*(call_assist_service.py)*  
Экспорт timeline в текстовый формат.  
_(documented in call_assist_service.py)_

### `call_assist_timeline_clear`
*(call_assist_service.py)*  
Очищает timeline текущей сессии.  
_(documented in call_assist_service.py)_

### `call_assist_timeline_to_history`
*(call_assist_service.py)*  
Сохраняет экспорт timeline в историю Krab Ear.  
Нет params.  
Returns: `{ok, history_id}`

### `call_assist_list_templates`
*(call_assist_service.py)*  
Возвращает локальные шаблоны быстрых реплик.  
Нет params.  
Returns: `{templates: [{name, text}, ...]}`

### `call_assist_add_template`
*(call_assist_service.py)*  
Сохраняет пользовательский шаблон фразы.  
Params: `{name, text}`  
Returns: `{ok, name}`

### `call_assist_remove_template`
*(call_assist_service.py)*  
Удаляет шаблон по имени.  
Params: `{name}` (str)  
Returns: `{ok, removed}`

### `call_assist_template`
*(call_assist_service.py)*  
Отправляет быстрый шаблон в сессию через Gateway.  
Params: `{name}` (str)  
Returns: `{ok, text, translated?}`

### `call_assist_cost_report`
*(call_assist_service.py)*  
Считает usage-показатели и вызывает Gateway cost estimate для текущей сессии.  
Нет params.  
Returns: `{cost_usd, duration_sec, phrases, breakdown}`

---

## Call Automation (Phase 3)

| Метод | Описание |
|---|---|
| `call_session_create` | Создать звонковую сессию |
| `call_session_get` | Получить сессию по id |
| `call_session_list` | Список сессий |
| `call_session_update_status` | Переход статуса сессии |
| `call_session_add_transcript` | Добавить реплику в транскрипт |
| `call_session_end` | Завершить сессию |
| `call_estimate_cost` | Оценить стоимость звонка |
| `call_check_auto_end` | Проверить правила авто-завершения |

### `call_session_create`
*(call_session_service.py)*  
Создаёт новую звонковую сессию.  
Params: `{provider?, phone_number?, lang?, direction?}`  
Returns: `{session_id, status, created_at}`

### `call_session_get`
*(call_session_service.py)*  
Возвращает полную запись сессии по id.  
Params: `{session_id}` (str)  
Returns: CallSession object

### `call_session_list`
*(call_session_service.py)*  
Возвращает список звонковых сессий.  
Params: `{status?, limit?}`  
Returns: `{sessions: [...]}`

### `call_session_update_status`
*(call_session_service.py)*  
Применяет переход статуса звонковой сессии.  
State machine: `idle→dialing→connected→talking→ending→completed/failed`  
Params: `{session_id, status}`  
Returns: `{ok, session_id, old_status, new_status}`

### `call_session_add_transcript`
*(call_session_service.py)*  
Добавляет реплику в транскрипт сессии.  
Params: `{session_id, speaker, text, ts?}`  
Returns: `{ok, entry_index}`

### `call_session_end`
*(call_session_service.py)*  
Завершает звонковую сессию: переводит в COMPLETED, вычисляет duration/cost.  
Params: `{session_id}` (str)  
Returns: `{ok, session_id, duration_sec, total_cost_usd}`

### `call_estimate_cost`
*(call_cost_estimator.py)*  
Оценить стоимость звонка по провайдеру и стране до набора.  
Params: `{provider?, country_code?, duration_min?}`  
Returns: `{estimated_cost_usd, per_minute_rate, currency}`

### `call_check_auto_end`
*(call_auto_end.py)*  
Проверить правила автоматического завершения для текущей сессии.  
Params: `{session_id}`  
Returns: `{should_end, reason?, elapsed_sec}`

---

## Live Subtitles (Phase 2)

| Метод | Описание |
|---|---|
| `live_subs_ingest` | Потоковая STT+translate (частый вызов) |
| `live_subs_stop` | Flush и сброс буфера |

### `live_subs_ingest`
*(live_subs_service.py)*  
Принимает base64 PCM 16 kHz chunk от ScreenCaptureKit. Аккумулирует ≥3 с, затем Whisper STT → translate → emits `live_subs.result` via EventBus.  
Params: `{audio_b64, is_final?}` — audio_b64: base64 PCM 16 kHz mono  
Returns: `{ok, buffered_ms}`

### `live_subs_stop`
*(live_subs_service.py)*  
Flush накопленного буфера и сброс состояния live subtitles.  
Нет params.  
Returns: `{ok, flushed_ms}`

---

## Apple Integration

| Метод | Описание |
|---|---|
| `send_to_telegram` | Отправить в Telegram через Krab userbot |
| `list_telegram_chats` | Список доступных чатов Telegram |
| `create_apple_note` | Создать заметку в Apple Notes |
| `create_apple_reminder` | Создать напоминание в Reminders |
| `create_calendar_event` | Создать событие в Calendar |
| `send_imessage` | Отправить iMessage/SMS |

### `send_to_telegram`
*(apple_integration_service.py)*  
Отправляет текст в Telegram через main Krab userbot (`POST /api/notify`).  
Params: `{text, chat_id?, history_id?}`  
Returns: `{ok, message_id?}`

### `list_telegram_chats`
*(apple_integration_service.py)*  
Возвращает список доступных чатов через main Krab userbot.  
Нет params.  
Returns: `{chats: [{id, name, type}, ...]}`

### `create_apple_note`
*(apple_integration_service.py)*  
Создаёт заметку в Apple Notes через osascript.  
Params: `{title, text, folder?}`  
Returns: `{ok, note_id?}`

### `create_apple_reminder`
*(apple_integration_service.py)*  
Создаёт напоминание в Apple Reminders через osascript.  
Params: `{text, due_date?, list?}`  
Returns: `{ok}`

### `create_calendar_event`
*(apple_integration_service.py)*  
Создаёт событие в Apple Calendar через osascript.  
Params: `{title, start_date, end_date?, notes?, calendar?}`  
Returns: `{ok}`

### `send_imessage`
*(apple_integration_service.py)*  
Отправляет сообщение через iMessage/SMS через Messages.app (osascript).  
Params: `{recipient, text}` — recipient: phone number или email  
Returns: `{ok}`

---

## Sentry / Observability / Error Bus

| Метод | Описание |
|---|---|
| `list_recent_errors` | Ring-буфер KrabError: последние N |
| `clear_recent_errors` | Очистить ring-буфер |
| `handle_error_action` | Выполнить actionable действие |
| `send_diagnostics_to_sentry` | Отправить ошибки в Sentry |
| `get_error_report` | Последние ошибки из ErrorReporter |
| `get_error_stats` | Счётчики ошибок по компоненту/типу |
| `get_privacy_audit_log` | Privacy audit log |
| `clear_privacy_audit_log` | Удалить файл privacy audit log |

### `list_recent_errors`
*(service.py)*  
Возвращает до `limit` последних KrabError из ring-буфера ErrorBus.  
Params: `{limit?}` (default 200), `since_seq?` (int)  
Returns: `{errors: [{code, message_user, message_debug, component, timestamp, severity, ...}, ...], latest_seq: int}`

**Поллинг-контракт (2026-07-05)**: SSE между IPC-бэкендом и REST-сервером не
работает (два раздельных `EventBus`, см. `ErrorBus.push()` в `error_bus.py`) —
native-агент вместо SSE опрашивает этот метод (`ErrorBusPoller.swift`, интервал
2s, тот же паттерн что `wake_word_status`/`WakeWordPoller`). Передавая
`since_seq` (последний увиденный `latest_seq`), вызывающая сторона получает
только НОВЫЕ ошибки (`seq > since_seq`), не полный backlog. Без `since_seq`
поведение прежнее (полный ring-буфер), `latest_seq` добавлен в ответ всегда
(harmless addition, обратная совместимость с `DiagnosticsTabView.swift` /
`main+StatusMenu.swift`, которые не передают `since_seq`).

### `clear_recent_errors`
*(service.py)*  
Очищает ring-буфер и dedupe-состояние ErrorBus.  
Нет params.  
Returns: `{cleared}` (int — количество удалённых)

### `handle_error_action`
*(service.py)*  
Выполняет actionable-действие по `action_id` из toast/diagnostics кнопки.  
Params: `{action_id}` — например `"open_privacy_settings"`, `"disable_rewriter"`  
Returns: `{ok, action_id, result?}`

### `send_diagnostics_to_sentry`
*(service.py)*  
Отправляет последние N ошибок в Sentry — последние 20 как breadcrumbs, остальные в extras.  
Params: `{limit?}` (default 50)  
Returns: `{ok, sent}`

### `get_error_report`
*(error_reporter.py)*  
Последние N ошибок из ring-буфера ErrorReporter.  
Params: `{limit?}`  
Returns: `{errors: [...]}`

### `get_error_stats`
*(error_reporter.py)*  
Счётчики ошибок по компоненту, типу и временным окнам.  
Нет params.  
Returns: `{by_component: {...}, by_type: {...}, last_1min, last_5min, last_1h}`

### `get_privacy_audit_log`
*(service.py)*  
Возвращает последние записи privacy audit log (NDJSON, без контента транскрипций).  
Params: `{limit?}`  
Returns: `{entries: [{event, ts, mode}, ...]}`

### `clear_privacy_audit_log`
*(service.py)*  
Удаляет файл privacy audit log. Идемпотентен.  
Нет params.  
Returns: `{ok, deleted}`

---

## Health, Diagnostics & Metrics

| Метод | Описание |
|---|---|
| `health_check` | Агрегированный health check |
| `get_diagnostics` | Комплексная диагностика |
| `get_startup_diagnostics` | Результаты startup проверок |
| `get_metrics_dashboard` | Снимок метрик реального времени |
| `get_memory_stats` | RSS/VSZ по процессам |
| `get_system_info` | CPU, RAM, диск, GPU |
| `get_usage_stats` | Ежедневная статистика использования |
| `get_recording_stats` | Кумулятивная статистика записей |
| `get_shutdown_status` | Статус последнего graceful shutdown |
| `get_throttle_stats` | Статистика IPC throttle |
| `estimate_recording_cost` | Оценка вычислительной стоимости записи |
| `get_daily_cost_summary` | Сводка вычислительных расходов |

### `health_check`
*(health_check_service.py)*  
Агрегированный health check всех ключевых подсистем бэкенда.  
Нет params.  
Returns: `{ok, components: {disk, ipc, stt_model, history}, overall}`

### `get_diagnostics`
*(health_check_service.py)*  
Возвращает комплексную диагностику: системная информация, STT, LLM, история и кэш настроек.  
Нет params.  
Returns: `{system: {...}, stt: {...}, llm: {...}, history: {...}, settings_cache: {...}}`

### `get_startup_diagnostics`
*(health_check_service.py)*  
Возвращает результаты диагностики при старте бэкенда (все readiness checks).  
Нет params.  
Returns: `{checks: [{name, ok, message?, duration_ms}], overall_ok}`

### `get_metrics_dashboard`
*(service.py)*  
Снимок метрик реального времени: сессия, LLM, call_assist, конфиг.  
Нет params.  
Returns: `{session: {...}, llm: {...}, call_assist: {...}, config: {...}, uptime_sec}`

### `get_memory_stats`
*(service.py)*  
Возвращает RSS/VSZ для backend, agent и worker процессов через psutil.  
Нет params.  
Returns: `{backend: {rss_mb, vsz_mb, pid}, agent: {...}?, workers: [...]}`

### `get_system_info`
*(service.py)*  
Возвращает информацию о системных ресурсах: CPU, RAM, диск, GPU.  
Нет params.  
Returns: `{cpu_pct, ram_mb, ram_total_mb, disk_free_gb, gpu_available}`

### `get_usage_stats`
*(service.py)*  
Возвращает ежедневную статистику использования: записи, длительность, слова.  
Params: `{days?}` (default 7)  
Returns: `{days: [{date, recordings, duration_sec, words}], totals}`

### `get_recording_stats`
*(service.py)*  
Возвращает кумулятивную статистику записей: длительность, языки, LLM, диаризация.  
Нет params.  
Returns: `{total_recordings, total_duration_sec, languages: {...}, llm_used_pct, diarization_used_pct}`

### `get_shutdown_status`
*(service.py)*  
Возвращает статус последнего graceful shutdown.  
Нет params.  
Returns: `{clean, last_shutdown_time?, reason?}`

### `get_throttle_stats`
*(service.py)*  
Возвращает статистику IPC throttle: вызовы, отклонения по методу.  
Нет params.  
Returns: `{methods: {method: {calls, rejected, rate}}, total_rejected}`

### `estimate_recording_cost`
*(service.py)*  
Оценка вычислительной стоимости обработки записи (CPU time, memory, disk).  
Params: `{duration_sec, quality_profile?, with_llm?}`  
Returns: `{cpu_sec, memory_mb_peak, disk_mb, breakdown}`

### `get_daily_cost_summary`
*(service.py)*  
Сводка вычислительных расходов за сегодня.  
Нет params.  
Returns: `{date, total_recordings, total_cpu_sec, total_disk_mb}`

---

## Analytics & Trends

| Метод | Описание |
|---|---|
| `get_analytics_dashboard` | Комплексный дашборд аналитики |
| `get_sentiment_trends` | Тренды тональности за N дней |
| `analyze_quality_trends` | Тренды качества распознавания |
| `get_topic_timeline` | Таймлайн смен тем разговора |
| `get_activity_calendar` | GitHub-style activity calendar |
| `get_recording_insights` | Эвристические инсайты по записям |
| `compare_periods` | Сравнение двух периодов |
| `get_keyword_cloud` | Данные облака ключевых слов |
| `generate_daily_digest` | Ежедневный дайджест транскрипций |
| `generate_stats_report` | Полный Markdown отчёт статистики |
| `generate_mini_stats_report` | Краткий 5-строчный отчёт |
| `get_timeline_view` | Группировка истории по временным блокам |
| `get_learning_stats` | Статистика изучения языков |

### `get_analytics_dashboard`
*(analytics_service.py)*  
Комплексный дашборд всех метрик аналитики за один вызов.  
Params: `{days?}` (default 30)  
Returns: `{sentiment, quality, keywords, activity, usage, recording_stats}`

### `get_sentiment_trends`
*(analytics_service.py)*  
Анализирует тренды тональности транскрипций за последние N дней.  
Params: `{days?}` (default 30)  
Returns: `{trend: "improving"|"stable"|"declining", daily: [...], avg_sentiment}`

### `analyze_quality_trends`
*(audio_analytics_service.py)*  
Анализирует тренды качества распознавания за последние N дней.  
Params: `{days?}` (default 30)  
Returns: `{trend, daily: [{date, avg_confidence, count}]}`

### `get_topic_timeline`
*(service.py)*  
Таймлайн смен тем разговора из истории транскрибаций.  
Params: `{limit?}`  
Returns: `{timeline: [{topic, ts, keywords: [...]}]}`

### `get_activity_calendar`
*(analytics_service.py)*  
GitHub-style activity calendar данные за последние N месяцев.  
Params: `{months?}` (default 3)  
Returns: `{days: [{date, count, duration_sec}], max_count}`

### `get_recording_insights`
*(service.py)*  
Генерирует эвристические инсайты по записям за последние N дней.  
Params: `{days?}` (default 7)  
Returns: `{insights: [{title, description, type}]}`

### `compare_periods`
*(analytics_service.py)*  
Сравнивает статистику двух временных периодов.  
Params: `{period1_start, period1_end, period2_start, period2_end}` (плоские ISO-даты, все required)  
Returns: `{period1: {recordings, duration_sec, words, avg_confidence}, period2: {...}, recordings_change_pct, ...}`

### `get_keyword_cloud`
*(analytics_service.py)*  
Генерирует данные облака ключевых слов из истории транскрипций.  
Params: `{limit?, days?}`  
Returns: `{words: [{word, count, weight, font_size}]}`

### `generate_daily_digest`
*(service.py)*  
Генерирует ежедневный дайджест транскрипций за указанную дату.  
Params: `{date?}` (ISO 8601, default today)  
Returns: `{digest}` (str, Markdown)

### `generate_stats_report`
*(service.py)*  
Генерирует полный Markdown-отчёт статистики использования за период.  
Params: `{days?}` (default 30)  
Returns: `{report}` (str, Markdown)

### `generate_mini_stats_report`
*(service.py)*  
Генерирует краткий 5-строчный Markdown-отчёт состояния.  
Нет params.  
Returns: `{report}` (str)

### `get_timeline_view`
*(analytics_service.py)*  
Группирует историю транскрипций по временным блокам (timeline).  
Params: `{granularity?}` — `"hour"` | `"day"` | `"week"`, default `"day"`  
Returns: `{timeline: [{period, items: [...], count}]}`

### `get_learning_stats`
*(service.py)*  
Статистика прогресса изучения языков (двуязычные пары, flashcard stats).  
Нет params.  
Returns: `{languages: {...}, flashcards: {...}, vocab_size}`

---

## Collections & Chains

| Метод | Описание |
|---|---|
| `create_collection` | Создать коллекцию/папку |
| `delete_collection` | Удалить коллекцию |
| `list_collections` | Список всех коллекций |
| `add_to_collection` | Добавить запись в коллекцию |
| `remove_from_collection` | Удалить запись из коллекции |
| `get_collection_items` | Записи из коллекции |
| `start_chain` | Начать цепочку связанных записей |
| `add_to_chain` | Добавить запись в цепочку |
| `end_chain` | Завершить цепочку |
| `get_chain` | Получить цепочку с деталями |
| `list_chains` | Список цепочек |
| `merge_chain_text` | Объединённый текст цепочки |
| `unlink_recording_from_chain` | Убрать запись из цепочки |

### `create_collection`
*(collection_manager.py)*  
Создать коллекцию/папку для организации истории.  
Params: `{name, description?}`  
Returns: `{collection_id, name}`

### `delete_collection`
*(collection_manager.py)*  
Удалить коллекцию (записи истории не удаляются).  
Params: `{collection_id}` (str)  
Returns: `{ok, collection_id}`

### `list_collections`
*(collection_manager.py)*  
Список всех коллекций.  
Нет params.  
Returns: `{collections: [{collection_id, name, count}, ...]}`

### `add_to_collection`
*(collection_manager.py)*  
Добавить запись истории в коллекцию.  
Params: `{collection_id, item_id}`  
Returns: `{ok}`

### `remove_from_collection`
*(collection_manager.py)*  
Удалить запись из коллекции.  
Params: `{collection_id, item_id}`  
Returns: `{ok}`

### `get_collection_items`
*(collection_manager.py)*  
Получить записи истории из коллекции.  
Params: `{collection_id, page?, page_size?}`  
Returns: `{items: [...], total}`

### `start_chain`
*(recording_chain.py)*  
Начать цепочку связанных записей (напр. длинное совещание по частям).  
Params: `{name?, description?}`  
Returns: `{chain_id}`

### `add_to_chain`
*(recording_chain.py)*  
Добавить запись истории в цепочку.  
Params: `{chain_id, item_id}`  
Returns: `{ok}`

### `end_chain`
*(recording_chain.py)*  
Завершить цепочку.  
Params: `{chain_id}` (str)  
Returns: `{ok, chain_id, item_count}`

### `get_chain`
*(recording_chain.py)*  
Получить цепочку с деталями.  
Params: `{chain_id}` (str)  
Returns: `{chain_id, name, items: [...], created_at, ended_at?}`

### `list_chains`
*(recording_chain.py)*  
Список цепочек.  
Нет params.  
Returns: `{chains: [{chain_id, name, item_count, status}]}`

### `merge_chain_text`
*(recording_chain.py)*  
Объединённый текст всех записей цепочки.  
Params: `{chain_id}` (str)  
Returns: `{text, item_count, total_duration_sec}`

### `unlink_recording_from_chain`
*(recording_chain.py)*  
Убирает запись из цепочки без удаления записи.  
Params: `{chain_id, item_id}`  
Returns: `{ok}`

---

## Bookmarks

| Метод | Описание |
|---|---|
| `add_bookmark` | Создать закладку |
| `list_bookmarks` | Закладки для item_id |
| `list_all_bookmarks` | Все активные закладки |
| `delete_bookmark` | Удалить закладку |
| `jump_to_bookmark` | Перейти к закладке |

### `add_bookmark`
*(bookmarks.py)*  
Создать закладку для текущей записи.  
Params: `{item_id, position_sec, label?}`  
Returns: `{bookmark_id, item_id, position_sec}`

### `list_bookmarks`
*(bookmarks.py)*  
Список закладок для конкретного item_id.  
Params: `{item_id}` (str)  
Returns: `{bookmarks: [{bookmark_id, position_sec, label, ts}]}`

### `list_all_bookmarks`
*(bookmarks.py)*  
Все активные закладки.  
Нет params.  
Returns: `{bookmarks: [...]}`

### `delete_bookmark`
*(bookmarks.py)*  
Удалить закладку (tombstone).  
Params: `{bookmark_id}` (str)  
Returns: `{ok}`

### `jump_to_bookmark`
*(bookmarks.py)*  
Перейти к закладке — получить данные для навигации плеера. Эмитит `playback.seek`.  
Params: `{bookmark_id}` (str)  
Returns: `{item_id, position_sec, label?}`

---

## Sharing & Webhooks

| Метод | Описание |
|---|---|
| `prepare_share` | Подготовить пакет для шаринга |
| `list_shared` | Список пакетов шаринга |
| `get_shared` | Получить пакет по share_id |
| `revoke_share_link` | Отозвать пакет шаринга |
| `register_webhook` | Зарегистрировать webhook |
| `unregister_webhook` | Отменить webhook |
| `list_webhooks` | Список webhook-ов |

### `prepare_share`
*(sharing_manager.py)*  
Подготовить пакет для шаринга транскрипций (создаёт share_id + token).  
Params: `{ids: [...], include_metadata?}`  
Returns: `{share_id, token, expires_at?}`

### `list_shared`
*(sharing_manager.py)*  
Список сохранённых пакетов шаринга.  
Нет params.  
Returns: `{shares: [{share_id, created_at, item_count}]}`

### `get_shared`
*(sharing_manager.py)*  
Получить пакет шаринга по share_id.  
Params: `{share_id}` (str)  
Returns: `{share_id, items: [...], created_at}`

### `revoke_share_link`
*(sharing_manager.py)*  
Отозвать пакет шаринга по токену (share_id). Пакет становится недоступным.  
Params: `{share_id}` (str)  
Returns: `{ok, share_id}`

### `register_webhook`
*(webhook_manager.py)*  
Зарегистрировать webhook URL для событий IPC.  
Params: `{url, events: [...], secret?}`  
Returns: `{webhook_id, url, events}`

### `unregister_webhook`
*(webhook_manager.py)*  
Отменить регистрацию webhook.  
Params: `{webhook_id}` (str)  
Returns: `{ok, webhook_id}`

### `list_webhooks`
*(webhook_manager.py)*  
Список зарегистрированных webhook-ов.  
Нет params.  
Returns: `{webhooks: [{webhook_id, url, events, active}]}`

---

## Transcript Versioning

| Метод | Описание |
|---|---|
| `save_transcript_version` | Сохранить новую версию текста |
| `get_transcript_versions` | Все версии транскрипции |
| `revert_transcript_version` | Откат к указанной версии |

### `save_transcript_version`
*(transcript_versioning.py)*  
Сохранить новую версию текста транскрипции (append-only).  
Params: `{item_id, text, reason?}`  
Returns: `{version_id, item_id, version_number}`

### `get_transcript_versions`
*(transcript_versioning.py)*  
Получить все версии транскрипции по item_id.  
Params: `{item_id}` (str)  
Returns: `{versions: [{version_id, version_number, ts, reason?, preview}]}`

### `revert_transcript_version`
*(transcript_versioning.py)*  
Откат транскрипции к указанной версии.  
Params: `{item_id, version_id}`  
Returns: `{ok, item_id, reverted_to_version}`

---

## Paste Formatter & App Memory

| Метод | Описание |
|---|---|
| `format_for_paste` | Форматировать текст под целевое приложение |
| `list_paste_formatters` | Список доступных форматтеров |
| `get_paste_profile_for_app` | Профиль вставки для bundle_id |
| `record_paste_app_profile` | Сохранить профиль для приложения |
| `list_app_profiles` | Список сохранённых профилей |
| `delete_app_profile` | Удалить профиль приложения |
| `cleanup_stale_app_profiles` | Удалить устаревшие профили |

### `format_for_paste`
*(core/paste_formatter.py)*  
Форматирует текст под целевое приложение.  
Params: `{text, app_name?, bundle_id?, formatter?}`  
Returns: `{formatted_text, formatter_used}`

### `list_paste_formatters`
*(core/paste_formatter.py)*  
Список всех доступных форматтеров вставки.  
Нет params.  
Returns: `{formatters: [{name, description, apps: [...]}, ...], total}`

### `get_paste_profile_for_app`
*(paste_app_memory.py)*  
Возвращает сохранённый профиль вставки для bundle_id.  
Params: `{bundle_id}` (str)  
Returns: `{profile?, bundle_id}`

### `record_paste_app_profile`
*(paste_app_memory.py)*  
Сохраняет ассоциацию bundle_id → paste profile.  
Params: `{bundle_id, profile}` (str, str)  
Returns: `{ok, bundle_id, profile}`

### `list_app_profiles`
*(paste_app_memory.py)*  
Список всех сохранённых ассоциаций приложение → профиль.  
Нет params.  
Returns: `{profiles: [{bundle_id, profile, last_used}]}`

### `delete_app_profile`
*(paste_app_memory.py)*  
Удалить профиль приложения.  
Params: `{bundle_id}` (str)  
Returns: `{ok, deleted}`

### `cleanup_stale_app_profiles`
*(paste_app_memory.py)*  
Удаляет устаревшие записи профилей приложений.  
Нет params.  
Returns: `{ok, deleted_count}`

---

## Templates & Quick Phrases

| Метод | Описание |
|---|---|
| `get_templates` | Список шаблонов быстрой вставки |
| `add_template` | Добавить шаблон |
| `remove_template` | Удалить шаблон |
| `apply_template` | Применить шаблон с переменными |

### `get_templates`
*(template_manager.py)*  
Список всех шаблонов быстрой вставки текста.  
Нет params.  
Returns: `{templates: [{name, text, variables: [...]}]}`

### `add_template`
*(template_manager.py)*  
Добавляет или обновляет шаблон текста.  
Params: `{name, text}` — text может содержать `{{variable}}` плейсхолдеры  
Returns: `{ok, name}`

### `remove_template`
*(template_manager.py)*  
Удаляет шаблон по имени.  
Params: `{name}` (str)  
Returns: `{ok, removed}`

### `apply_template`
*(template_manager.py)*  
Применяет шаблон с подстановкой переменных.  
Params: `{name, variables?: {var: value}}`  
Returns: `{text}` — текст с подставленными переменными

---

## Plugins & Feature Flags

| Метод | Описание |
|---|---|
| `list_plugins` | Список обнаруженных плагинов |
| `get_plugin_info` | Информация о плагине |
| `unload_plugin` | Полная выгрузка плагина |
| `get_feature_flags` | Все feature-флаги |
| `set_feature_flag` | Установить значение флага |

### `list_plugins`
*(plugin_system.py)*  
Список обнаруженных плагинов с метаданными.  
Нет params.  
Returns: `{plugins: [{name, version, loaded, path}, ...]}`

### `get_plugin_info`
*(plugin_system.py)*  
Информация о конкретном плагине.  
Params: `{name}` (str)  
Returns: `{name, version, description, loaded, capabilities}`

### `unload_plugin`
*(plugin_system.py)*  
Полная выгрузка плагина из памяти.  
Params: `{name}` (str)  
Returns: `{ok, name}`

### `get_feature_flags`
*(feature_flags.py)*  
Получить все feature-флаги с описаниями.  
Нет params.  
Returns: `{flags: [{name, enabled, description}, ...]}`

### `set_feature_flag`
*(feature_flags.py)*  
Установить значение feature-флага.  
Params: `{flag_name, enabled}` (str, bool)  
Returns: `{ok, flag_name, enabled}`

---

## Speaker Manager

| Метод | Описание |
|---|---|
| `set_speaker_alias` | Назначить псевдоним спикеру |
| `get_speaker_aliases` | Список псевдонимов |
| `remove_speaker_alias` | Удалить псевдоним |

### `set_speaker_alias`
*(speaker_manager.py)*  
Назначить псевдоним для спикера диаризации.  
Params: `{speaker_id, alias}` (str, str)  
Returns: `{ok, speaker_id, alias}`

### `get_speaker_aliases`
*(speaker_manager.py)*  
Список псевдонимов спикеров.  
Нет params.  
Returns: `{aliases: [{speaker_id, alias}, ...]}`

### `remove_speaker_alias`
*(speaker_manager.py)*  
Удалить псевдоним спикера.  
Params: `{speaker_id}` (str)  
Returns: `{ok, speaker_id}`

---

## Semantic Search

| Метод | Описание |
|---|---|
| `semantic_search` | Семантический поиск через embeddings |
| `semantic_search_status` | Статус семантического поиска |
| `semantic_search_reindex` | Переиндексировать всю историю |

### `semantic_search`
*(service.py)*  
Семантический поиск по истории транскрипций через embeddings (`multilingual-e5-base`).  
Params: `{query, limit?}` (str, int default 10)  
Returns: `{results: [{id, score, text_preview}, ...]}`

### `semantic_search_status`
*(service.py)*  
Возвращает статус семантического поиска: модель, индекс.  
Нет params.  
Returns: `{model, indexed_count, index_size_mb, status}`

### `semantic_search_reindex`
*(service.py)*  
Переиндексирует всю историю транскрипций.  
Нет params.  
Returns: `{ok, indexed}`

---

## Scheduled Recordings

| Метод | Описание |
|---|---|
| `schedule_recording` | Запланировать запись |
| `cancel_scheduled_recording` | Отменить запланированную запись |
| `list_scheduled_recordings` | Список запланированных записей |

### `schedule_recording`
*(recording_scheduler.py)*  
Запланировать запись на определённое время.  
Params: `{start_at, duration_sec?, quality_profile?, note?}` — start_at: ISO 8601  
Returns: `{schedule_id, start_at, duration_sec}`

### `cancel_scheduled_recording`
*(recording_scheduler.py)*  
Отменить запланированную запись.  
Params: `{schedule_id}` (str)  
Returns: `{ok, schedule_id}`

### `list_scheduled_recordings`
*(recording_scheduler.py)*  
Список запланированных записей.  
Нет params.  
Returns: `{schedules: [{schedule_id, start_at, duration_sec, status}]}`

---

## Obsidian Sync

| Метод | Описание |
|---|---|
| `configure_obsidian_sync` | Настроить Obsidian vault |
| `run_obsidian_sync` | Синхронизировать с vault |
| `get_obsidian_sync_status` | Статус синхронизации |

### `configure_obsidian_sync`
*(obsidian_sync.py)*  
Настроить Obsidian vault для синхронизации транскрипций.  
Params: `{vault_path, folder?, incremental?}`  
Returns: `{ok, vault_path}`

### `run_obsidian_sync`
*(obsidian_sync.py)*  
Синхронизировать записи истории с Obsidian vault как .md файлы с YAML frontmatter.  
Params: `{force?}` (bool, default false — incremental)  
Returns: `{ok, synced, skipped, errors}`

### `get_obsidian_sync_status`
*(obsidian_sync.py)*  
Статус синхронизации с Obsidian vault.  
Нет params.  
Returns: `{configured, vault_path?, last_sync_ts?, synced_count}`

---

## Playback Tracker

| Метод | Описание |
|---|---|
| `record_playback` | Зарегистрировать событие воспроизведения |
| `get_playback_stats` | Статистика воспроизведения записи |
| `get_most_replayed` | Топ N воспроизводимых записей |

### `record_playback`
*(playback_tracker.py)*  
Зарегистрировать событие воспроизведения (play count, total listened).  
Params: `{item_id, duration_listened_sec?}`  
Returns: `{ok, play_count}`

### `get_playback_stats`
*(playback_tracker.py)*  
Статистика воспроизведения записи.  
Params: `{item_id}` (str)  
Returns: `{item_id, play_count, total_listened_sec, last_played_ts}`

### `get_most_replayed`
*(playback_tracker.py)*  
Топ N наиболее часто воспроизводимых записей.  
Params: `{limit?}` (default 10)  
Returns: `{items: [{item_id, play_count, total_listened_sec}, ...]}`

---

## Search History

| Метод | Описание |
|---|---|
| `get_recent_searches` | Последние поисковые запросы |
| `get_popular_searches` | Наиболее частые запросы |
| `clear_search_history` | Очистить историю запросов |

### `get_recent_searches`
*(search_history.py)*  
Последние поисковые запросы пользователя (для autocomplete).  
Params: `{limit?}` (default 10)  
Returns: `{searches: [{query, ts}, ...]}`

### `get_popular_searches`
*(search_history.py)*  
Наиболее частые поисковые запросы.  
Params: `{limit?}` (default 10)  
Returns: `{searches: [{query, count}, ...]}`

### `clear_search_history`
*(search_history.py)*  
Очищает всю историю поисковых запросов.  
Нет params.  
Returns: `{ok, deleted}`

---

## Archive Manager

| Метод | Описание |
|---|---|
| `archive_items` | Переместить записи в архив |
| `unarchive_items` | Восстановить из архива |
| `list_archived` | Список архивированных записей |
| `get_archive_stats` | Статистика архива |

### `archive_items`
*(archive_manager.py)*  
Переместить записи истории в отдельный `archive.ndjson` (lean main store).  
Params: `{ids: [...]}` или `{before_date}`  
Returns: `{ok, archived_count}`

### `unarchive_items`
*(archive_manager.py)*  
Восстановить записи из архива в основную историю.  
Params: `{ids: [...]}`  
Returns: `{ok, restored_count}`

### `list_archived`
*(archive_manager.py)*  
Список архивированных записей.  
Params: `{page?, page_size?}`  
Returns: `{items: [...], total}`

### `get_archive_stats`
*(archive_manager.py)*  
Статистика архива: количество, размер, oldest/newest.  
Нет params.  
Returns: `{count, size_bytes, oldest_ts?, newest_ts?}`

---

## Text Processing

| Метод | Описание |
|---|---|
| `compare_texts` | Сравнение двух текстов/транскрипций |
| `score_readability` | Оценка читабельности текста |
| `score_transcription` | Оценка качества транскрипции 0–100 |
| `detect_emotion` | Определение эмоции в тексте |
| `expand_abbreviations` | Раскрытие аббревиатур |
| `remove_abbreviation` | Удалить аббревиатуру |
| `list_abbreviations` | Список аббревиатур для языка |
| `post_process_text` | Прогнать текст через пост-обработку |
| `list_post_process_steps` | Список шагов пост-обработки |
| `compare_recordings` | Сравнение нескольких записей side-by-side |
| `extract_terms` | Извлечение ключевых терминов |
| `replace_word_in_last_transcript` | Замена слова в транскрипте |

### `compare_texts`
*(text_processing_service.py)*  
Сравнивает два текста или две записи истории по ID (word-level diff + similarity score).  
Params: `{text_a?, text_b?, id_a?, id_b?}` — пары text или id  
Returns: `{similarity, diff: [{op, text}], word_count_a, word_count_b}`

### `score_readability`
*(text_processing_service.py)*  
Оценивает читабельность текста (Flesch score, sentence complexity, vocabulary).  
Params: `{text?, id?}`  
Returns: `{score, grade, avg_sentence_length, vocabulary_richness}`

### `score_transcription`
*(text_processing_service.py)*  
Оценивает качество транскрибации, балл 0–100 (A–F): confidence, длительность, диаризация, LLM флаги.  
Params: `{id}`  
Returns: `{score, grade, breakdown: {confidence, duration, diarization, llm}}`

### `detect_emotion`
*(text_processing_service.py)*  
Эвристическое определение эмоции в тексте транскрипции.  
Params: `{text?, id?}`  
Returns: `{emotion: "neutral"|"positive"|"negative"|..., confidence}`

### `expand_abbreviations`
*(text_processing_service.py)*  
Раскрывает аббревиатуры в тексте транскрипции.  
Params: `{text, lang?}`  
Returns: `{text}` — текст с раскрытыми аббревиатурами

### `remove_abbreviation`
*(text_processing_service.py)*  
Удалить аббревиатуру из словаря.  
Params: `{abbreviation, lang?}`  
Returns: `{ok, removed}`

### `list_abbreviations`
*(text_processing_service.py)*  
Список аббревиатур для языка.  
Params: `{lang?}` (default all)  
Returns: `{abbreviations: [{abbr, expansion, lang}]}`

### `post_process_text`
*(text_processing_service.py)*  
Прогнать текст через конвейер пост-обработки (whitespace, punctuation, entities, abbreviations, anonymization).  
Params: `{text, steps?: [...]}` — steps: список step names (все если не указано)  
Returns: `{text, applied_steps: [...]}`

### `list_post_process_steps`
*(text_processing_service.py)*  
Список доступных шагов пост-обработки текста.  
Нет params.  
Returns: `{steps: [{name, description}, ...]}`

### `compare_recordings`
*(service.py)*  
Сравнение нескольких записей side-by-side: матрица сходства, статистика, общие/уникальные слова.  
Params: `{ids: [...]}` (2–10 элементов)  
Returns: `{similarity_matrix, shared_words, unique_words, stats: [...]}`

### `extract_terms`
*(service.py)*  
Извлекает ключевые термины из текста.  
Params: `{text?, id?}`  
Returns: `{terms: [{term, frequency, weight}]}`

### `replace_word_in_last_transcript`
*(service.py)*  
Заменяет слово в последней (или указанной) записи истории без перезаписи всего текста.  
Params: `{old_word, new_word, id?}`  
Returns: `{ok, id, replacements}`

---

## Event Replay

| Метод | Описание |
|---|---|
| `get_event_log` | Лог событий с фильтрацией |
| `get_event_stats` | Статистика событий |
| `replay_events` | Воспроизведение событий |

### `get_event_log`
*(event_replay.py)*  
Лог событий для отладки (фильтрация по типу/времени).  
Params: `{event_type?, from_ts?, to_ts?, limit?}`  
Returns: `{events: [{type, ts, data}]}`

### `get_event_stats`
*(event_replay.py)*  
Статистика событий: счётчики, скорость/мин.  
Нет params.  
Returns: `{by_type: {...}, total, events_per_min}`

### `replay_events`
*(event_replay.py)*  
Воспроизведение событий в диапазоне времени.  
Params: `{from_ts?, to_ts?, event_type?}`  
Returns: `{ok, replayed}`

---

## Config Presets Library

| Метод | Описание |
|---|---|
| `list_config_presets` | Список конфигурационных пресетов |
| `apply_config_preset` | Применить пресет |
| `create_config_preset` | Создать кастомный пресет |

### `list_config_presets`
*(config_presets_library.py)*  
Список всех конфигурационных пресетов (встроенных и кастомных).  
Нет params.  
Returns: `{presets: [{name, description, builtin, keys: [...]}]}`

### `apply_config_preset`
*(config_presets_library.py)*  
Применить конфигурационный пресет — вернуть `settings_patch` для применения через `set_settings`.  
Params: `{name}` (str)  
Returns: `{name, settings_patch: {...}}`

### `create_config_preset`
*(config_presets_library.py)*  
Создать кастомный конфигурационный пресет из текущих настроек или явных значений.  
Params: `{name, description?, settings_patch?}`  
Returns: `{ok, name}`

---

## Data Migrator

| Метод | Описание |
|---|---|
| `check_migration` | Проверить необходимость миграции |
| `run_migration` | Выполнить миграцию |

### `check_migration`
*(data_migrator.py)*  
Проверяет необходимость миграции данных между версиями схемы.  
Нет params.  
Returns: `{needs_migration, current_version, target_version}`

### `run_migration`
*(data_migrator.py)*  
Выполняет миграцию данных между версиями.  
Params: `{dry_run?}` (bool)  
Returns: `{ok, migrated_records, from_version, to_version}`

---

## Model Cache Manager

| Метод | Описание |
|---|---|
| `list_cached_models` | Список кэшированных ML-моделей |
| `get_model_cache_info` | Информация о кэше модели |

### `list_cached_models`
*(model_cache_manager.py)*  
Список кэшированных ML-моделей (HuggingFace cache).  
Нет params.  
Returns: `{models: [{name, size_gb, path, last_used}]}`

### `get_model_cache_info`
*(model_cache_manager.py)*  
Информация о кэше конкретной модели.  
Params: `{model_name}` (str)  
Returns: `{name, size_gb, path, cached, last_used?}`

---

## Wake Word

| Метод | Описание |
|---|---|
| `wake_word_list_models` | Список builtin+custom моделей |
| `wake_word_start` | Запустить прослушивание |
| `wake_word_stop` | Остановить прослушивание |
| `wake_word_status` | Статус адаптера |

### `wake_word_list_models`
*(openwakeword_adapter.py)*  
Список доступных wake word моделей (openWakeWord builtin + custom "Краб").  
Нет params.  
Returns: `{models: [{name, path, type, active}]}`

### `wake_word_start`
*(openwakeword_adapter.py)*  
Запустить прослушивание wake word.  
Params: `{model_name?, sensitivity?}` (float 0–1)  
Returns: `{ok, model, status}`

### `wake_word_stop`
*(openwakeword_adapter.py)*  
Остановить прослушивание wake word.  
Нет params.  
Returns: `{ok}`

### `wake_word_status`
*(openwakeword_adapter.py)*  
Статус адаптера + последняя детекция. Агент поллит этот метод (~0.75s) и
триггерит «Разговор с AI» по росту `last_detection.ts` (spec 2026-07-05
wake-word-openwakeword; SSE не подходит — раздельные EventBus двух процессов).  
Нет params.  
Returns: `{ok, running, active_model, engine_available, last_detection}`  
— `last_detection` (object|null): `{model: str, score: float, ts: float}`;
`ts` — МОНОТОННЫЙ (`time.monotonic`) таймстамп процесса backend, агент
дебаунсит по росту (nil re-arm'ит baseline). Сбрасывается в null при
`wake_word_start`/`wake_word_stop`. Поле добавлено 2026-07-05; до этого
описание в доке дрейфовало (несуществующие `active`/`detections_today`).

---

## TTS

| Метод | Описание |
|---|---|
| `synthesize_speech` | Синтез речи (text→audio) |

### `synthesize_speech`
*(tts_service.py)*  
Синтез речи. Dual-engine: Silero RU primary, Kokoro EN fallback, macOS `say` last resort. Автоопределение языка.  
Params: `{text, language?, voice?}` — language: `"ru"`, `"en"`, `"auto"`  
Returns: `{audio_b64?, file_path?, engine_used, duration_sec?}`

---

## Launch Readiness (2026-06-27)

Фичи запуска: in-app загрузка STT-модели, автокалибровка под железо, privacy-дашборд, миграция шифрования истории.

| Метод | Описание |
|---|---|
| `download_stt_model` | Запустить фоновую загрузку STT-модели из HuggingFace |
| `get_stt_model_status` | Статус кэша/загрузки STT-модели |
| `cancel_stt_model_download` | Отменить текущую фоновую загрузку STT-модели |
| `get_hardware_profile` | Аппаратный профиль Mac (chip/RAM/cores/tier) |
| `get_calibration_recommendation` | Рекомендация STT-модели/движка по tier + микрофону |
| `get_privacy_dashboard` | Агрегированный privacy/security дашборд (только счётчики/флаги) |
| `migrate_history_encryption` | Зашифровать существующие plaintext-записи истории (at-rest) |
| `get_history_encryption_status` | Статистика шифрования истории (total/encrypted/plaintext/pct) |

### `download_stt_model`
*(service.py → model_downloader.py)*  
Запускает фоновую загрузку STT-модели из HuggingFace (фреш-инстолл анблок). Эмитит событие `model_download.progress` через EventBus.  
Params: `{model_id?}` — HuggingFace repo_id; дефолт `settings.MODEL_BALANCED` (`mlx-community/whisper-large-v3-turbo`). Пустой/не-строка `model_id` → `ValueError`.  
Returns: `{ok, status, model_id}` — status: `"started"` | `"already_cached"` | `"in_progress"`

### `get_stt_model_status`
*(service.py → model_downloader.py)*  
Статус кэша и текущей загрузки STT-модели.  
Params: `{model_id?}` — дефолт `settings.MODEL_BALANCED`. Пустой/не-строка `model_id` → `ValueError`.  
Returns: `{ok, model_id, cached, downloading, status, pct, downloaded, total, error_msg, path}` — status: `"idle"` | `"downloading"` | `"done"` | `"error"`; `pct` 0..100; `downloaded`/`total` в байтах. Абсолютный путь кэша НЕ раскрывается (#1814).

### `cancel_stt_model_download`
*(service.py → model_downloader.py)*  
Отменяет текущую фоновую загрузку STT-модели (F1-hardening #1814). Идемпотентно.  
Params: `{model_id?}` — HuggingFace repo_id; дефолт `settings.MODEL_BALANCED`. Пустой/не-строка/слишком длинный `model_id` → `ValueError`.  
Returns: `{ok, cancelled, model_id}` — `cancelled=True` — загрузка активно шла и сигнал отмены отправлен; `cancelled=False` — загрузка не шла (отменять было нечего).

> **Cloud-rewriter (опциональная облачная полировка транскрипта)** — НЕ имеет отдельного IPC-метода: управляется настройками (`cloud_rewriter_enabled` default `False`, `cloud_rewriter_provider` = `openai`|`anthropic`|`custom`, `cloud_rewriter_base_url`/`cloud_rewriter_custom_model`/`cloud_rewriter_api_key` для `custom`). Fallback в `engine.py`, когда локальный rewriter вернул `ok=False`. Privacy: `privacy_mode_enabled=True` ВСЕГДА блокирует (см. `backend/cloud_rewriter.py`, sibling `cloud_stt.py`).

### `get_hardware_profile`
*(service.py → core/hardware_profile.py)*  
Аппаратный профиль Mac для автокалибровки STT. Читает только железо, не данные пользователя — нет privacy gate.  
Нет params.  
Returns: `{ok, chip, ram_gb, cores, is_apple_silicon, tier}` — tier: `"low"` | `"mid"` | `"high"`

### `get_calibration_recommendation`
*(service.py → core/hardware_profile.py)*  
Рекомендует STT-модель и движок на основе hardware tier + кэшированного профиля микрофона (не делает новую запись). Нет privacy gate.  
Нет params.  
Returns: `{ok, recommended_model, recommended_engine, tier, mic, rationale}` — recommended_model: `"balanced"` | `"max"`; `mic`: `{snr_db, suitable_for_stt}` | `null`; `rationale` — текстовое обоснование

### `get_privacy_dashboard`
*(service.py)*  
Агрегированный privacy/security дашборд одним вызовом. Возвращает только счётчики/флаги/размеры — ни одного транскрипта/словаря/псевдонима спикера (читает privacy-метаданные, не пользовательский контент → gate не нужен).  
Нет params.  
Returns: `{ok, privacy_mode, encryption_enabled, storage, retention, audit, purge_available}`  
- `storage`: `{item_count, history_bytes, history_file_size_mb, transcripts_count, transcripts_size_mb, total_bytes, total_data_mb}`  
- `retention`: `{auto_cleanup_enabled, auto_cleanup_after_days, auto_purge_enabled, auto_purge_retention_days}`  
- `audit`: `{total_events, last_event_ts, by_type}`  
- `purge_available`: всегда `True`

### `migrate_history_encryption`
*(service.py → state_store.py)*  
Шифрует существующие plaintext-записи `history.ndjson` (at-rest миграция) в фоновом потоке: пишет `.bak`, атомарная замена. Прогресс через событие `history_encryption.migrate.progress`. Идемпотентно.  
Нет params.  
Returns: `{ok, status}` — status: `"started"` | `"already_running"` | (`ok:false`) `"encryption_unavailable"`

### `get_history_encryption_status`
*(service.py → state_store.py)*  
Статистика шифрования `history.ndjson` (считает `ENC1:`-сигнатуры vs plaintext, не контент — gate не нужен).  
Нет params.  
Returns: `{ok, enabled, total, encrypted, plaintext, pct, migrating}`

---

## A1 — Рекомендованная настройка в один тап (2026-07-07)

Один IPC-метод: превью (dry_run) и применение безопасного набора настроек, подобранного
под железо и доступность LM Studio/HF-кэша. План:
`docs/superpowers/plans/2026-07-07-recommended-setup.md`. Спека:
`docs/superpowers/specs/2026-07-07-recommended-setup-design.md`.

| Метод | Описание |
|---|---|
| `apply_recommended_setup` | Превью (dry_run) или применение рекомендованного пресета настроек |

### `apply_recommended_setup`
*(service.py → settings_service.py, probe-колбэки инжектируются из service.py)*  
Применяет (или показывает превью) рекомендованный безопасный набор настроек:
**10 безусловных** (`smart_silence_skip_enabled`, `realtime_silence_filter_enabled`,
`auto_dedup_enabled`, `auto_save_transcripts`, `phonetic_vocab_enabled`,
`text_snippets_enabled`, `auto_learn_corrections_enabled`, `quick_edit_enabled`,
`paste_undo_enabled`, `calendar_link_enabled`) + **3 условных** через probe-гейт
(`llm_rewrite_enabled`/`action_items_auto_extract` — требуют `probe_llm_http.reachable`;
`stt_sensevoice_enabled` — требует `ModelDownloader.get_status("FunAudioLLM/SenseVoiceSmall")["cached"]`).
`auto_dedup_enabled`/`auto_learn_corrections_enabled`/`action_items_auto_extract`
уходят в `skipped` при `privacy_mode_enabled=True` (транскрипт-читающие ключи).  
**GigaAM-пара (`stt_gigaam_enabled`, `stt_language_routing_enabled`) ВСЕГДА `skipped`**
с фиксированной причиной `"настройте GigaAM вручную в Настройках"` — без какой-либо
probe-логики, независимо от состояния venv на диске (решение 9.7 финальной спеки).  
**Wake word НЕ входит в этот метод** — отдельный consent-экран онбординга вызывает
`set_settings {wake_word_engine: "openwakeword"}` напрямую (решение 9.4).  
Params: `{dry_run?: bool = true, keys?: list[str] | null}` — `keys` фильтрует, какие из
13 кандидатов рассматривать (v1 UI его не использует, но API поддерживает).  
Returns: `{ok, dry_run, tier, applied: [{key, old_value, new_value, restart_required}], skipped: [{key, reason}], rationale, snapshot_id, restart_required}` —
`tier`: `"low"` | `"mid"` | `"high"` (см. `get_hardware_profile`). `dry_run=true` НЕ пишет
диск и `snapshot_id=null`. `dry_run=false` создаёт бэкап через существующий
`SettingsBackup.create_backup(reason="before_recommended_setup")` ПЕРЕД записью —
`snapshot_id` == `backup_id`, который принимает уже существующий
`restore_settings_backup {backup_id}` (**параметр называется `backup_id`, не
`snapshot_id`** — значение то же самое; никакого нового кода отката не написано).

---

## Recording Management & Integrations (2026-07-02/03)

Запланированные записи, пресеты конфигурации, экспорт таймлайна, webhook-интеграции, цепочки записей, профили резюмирования.

| Метод | Описание |
|---|---|
| `schedule_recording` | Планирует новую запись на будущее время |
| `cancel_scheduled_recording` | Отменяет запланированное задание |
| `list_scheduled_recordings` | Список всех запланированных заданий |
| `list_config_presets` | Список конфигурационных пресетов |
| `apply_config_preset` | Применяет пресет: merge settings_patch и сохранение |
| `create_config_preset` | Создаёт кастомный пресет |
| `delete_config_preset` | Удаляет кастомный пресет |
| `export_config_preset` | Экспортирует пресет в JSON |
| `import_config_preset` | Импортирует пресет из JSON |
| `export_timeline_svg` | Экспортирует таймлайн в SVG |
| `export_timeline_json` | Экспортирует таймлайн в JSON |
| `export_timeline_ical` | Экспортирует таймлайн в iCalendar (.ics) |
| `register_webhook` | Регистрирует webhook-получателя событий |
| `unregister_webhook` | Удаляет webhook по ID |
| `list_webhooks` | Список зарегистрированных webhook-ов |
| `start_chain` | Создаёт новую цепочку записей |
| `add_to_chain` | Добавляет запись в цепочку (идемпотентно) |
| `end_chain` | Завершает цепочку |
| `get_chain` | Полные детали цепочки |
| `list_chains` | Краткий список цепочек |
| `merge_chain_text` | Конкатенирует тексты цепочки |
| `unlink_recording_from_chain` | Убирает запись из цепочки |
| `list_summary_profiles` | Список профилей стиля резюмирования |
| `add_summary_profile` | Создаёт/заменяет кастомный профиль |

> **RecordingScheduler** — запланировать запись на определённое время (фоновый триггер-поток каждые 30с).

### `schedule_recording`
*(service.py → recording_scheduler.py)*  
Планирует новую запись на будущее время.  
Params: `{start_time, duration_sec, label?}` — `start_time` (ОБЯЗАТЕЛЕН) ISO 8601 строка с таймзоной; не в прошлом; не дальше 30 дней вперёд. `duration_sec` (ОБЯЗАТЕЛЕН) диапазон 1..7200 (макс 2ч). `label` (опционально) — текстовая метка.  
Returns: `{schedule: {id, start_time, duration_sec, label, status, created_at}}` — status: `"pending"`. Невалидные данные (диапазон/лимит 50 pending/лимит 500 всего) → `ValueError` (top-level `ok:false`/`error`).

### `cancel_scheduled_recording`
*(service.py → recording_scheduler.py)*  
Отменяет запланированное задание.  
Params: `{schedule_id}` (или алиас `id`) — обязателен.  
Returns: `{cancelled: bool}` — `false` если id не найден или уже не pending.

### `list_scheduled_recordings`
*(service.py → recording_scheduler.py)*  
Список всех запланированных заданий (все статусы).  
Нет params.  
Returns: `{schedules: [{id, start_time, duration_sec, label, status, created_at}], count}` — status: `"pending"|"completed"|"cancelled"`.

> **ConfigPresetsLibrary** — именованные пресеты настроек (5 встроенных: interview/meeting/... + кастомные пользовательские).

### `list_config_presets`
*(service.py → config_presets_library.py)*  
Список всех пресетов (встроенные + кастомные).  
Нет params.  
Returns: `{presets: [{name, description, builtin, settings_patch}]}`

### `apply_config_preset`
*(service.py → config_presets_library.py)*  
Атомарно применяет пресет: merge settings_patch в текущие настройки + сохранение.  
Params: `{name}` — обязателен.  
Returns: `{name, settings_patch, applied: bool, saved}` — пресет не найден → `KeyError` (internal_error).

### `create_config_preset`
*(service.py → config_presets_library.py)*  
Создаёт кастомный пресет.  
Params: `{name, description, settings_patch}` — все три обязательны; `settings_patch` должен быть непустым dict.  
Returns: `{preset: {name, description, builtin: false, settings_patch}}`

### `delete_config_preset`
*(service.py → config_presets_library.py)*  
Удаляет кастомный пресет по имени.  
Params: `{name}` — обязателен.  
Returns: `{name, deleted: bool}` — `false` для встроенных пресетов (нельзя удалить) или если не найден.

### `export_config_preset`
*(service.py → config_presets_library.py)*  
Экспортирует пресет в JSON-строку.  
Params: `{name}` — обязателен.  
Returns: `{name, json: string}`

### `import_config_preset`
*(service.py → config_presets_library.py)*  
Импортирует пресет из JSON-строки (от export_config_preset).  
Params: `{json: string}` — обязателен.  
Returns: `{preset: {name, description, builtin: false, settings_patch}}`

> **TimelineExporter** — экспорт таймлайна записей истории (группировка по часу/дню/неделе) в файл.

### `export_timeline_svg`
*(service.py)*  
Экспортирует таймлайн в SVG-файл.  
Params: `{output_dir?, group_by="day", limit=500, width=1200, height=400}` — group_by: `"hour"|"day"|"week"`; limit макс 5000.  
Returns: `{path, blocks}` (успех) или `{error: {code: "privacy_mode"|"invalid_path", message}}` (бэкенд не бросает исключение — ошибка ВНУТРИ result, ok:true на верхнем уровне).

### `export_timeline_json`
*(service.py)*  
То же для JSON (без width/height).  
Params: `{output_dir?, group_by="day", limit=500}`  
Returns: `{path, blocks}` или `{error: {code, message}}`

### `export_timeline_ical`
*(service.py)*  
То же для iCalendar (.ics), blocks = число VEVENT.  
Params: `{output_dir?, group_by="day", limit=500}`  
Returns: `{path, blocks}` или `{error: {code, message}}`

> **WebhookManager** — внешние webhook-интеграции (HMAC-подпись, SSRF-защита, retry с backoff).

### `register_webhook`
*(service.py → webhook_manager.py)*  
Регистрирует webhook-получателя событий.  
Params: `{url, events: [string] (пусто=все), secret? (мин 16 симв. если непустой)}` — url должен начинаться с http(s)://, проходит SSRF-проверку.  
Returns: `{webhook_id}` (успех) или `{ok: false, reason: "webhook_limit_reached"}` (лимит 500, НЕ исключение) или top-level ValueError (пустой url/SSRF-отклонение/короткий secret).

### `unregister_webhook`
*(service.py → webhook_manager.py)*  
Удаляет webhook по ID.  
Params: `{webhook_id}` — обязателен.  
Returns: `{removed: bool}`

### `list_webhooks`
*(service.py → webhook_manager.py)*  
Список зарегистрированных webhook-ов (без секретов).  
Нет params.  
Returns: `{webhooks: [{webhook_id, url, events, has_secret: bool, enabled, created_at, deliveries, failures, last_status}]}`

> **RecordingChainManager** — связывание нескольких записей истории в упорядоченную «цепочку» (напр. совещание, записанное по частям).

### `start_chain`
*(service.py → recording_chain.py)*  
Создаёт новую цепочку.  
Params: `{name}` — обязателен, не пустой, макс 200 символов.  
Returns: `{chain_id}` (успех) или `{ok: false, error: string, reason: "limit_exceeded"}` (лимит 500 цепочек) или `{ok: false, error: string}` (ошибка записи на диск).

### `add_to_chain`
*(service.py → recording_chain.py)*  
Добавляет запись истории в цепочку. Идемпотентно (повторное добавление того же item_id — no-op).  
Params: `{chain_id, item_id}` — оба обязательны.  
Returns: `{ok: true}` или `{ok: false, error: string, reason: "chain_ended"|"limit_exceeded"}` (лимит 1000 записей в цепочке) или `{ok: false, error: string}`.

### `end_chain`
*(service.py → recording_chain.py)*  
Завершает цепочку (помечает ended_at).  
Params: `{chain_id}` — обязателен.  
Returns: `{ok: true}` или `{ok: false, error: string}`

### `get_chain`
*(service.py → recording_chain.py)*  
Полные детали цепочки: элементы по порядку, суммарные duration/word_count.  
Params: `{chain_id}` — обязателен.  
Returns: `{chain_id, name, created_at, ended_at, item_ids: [string], items: [dict], total_duration_sec, total_word_count}`. В privacy mode — `items: []` + доп. поле `privacy_mode: true`. Цепочка не найдена → `KeyError` (internal_error).

### `list_chains`
*(service.py → recording_chain.py)*  
Краткий список цепочек (без items/total_duration — для деталей нужен get_chain).  
Params: `{limit=20}` — макс 1000.  
Returns: `{chains: [{chain_id, name, created_at, ended_at, item_count}]}`

### `merge_chain_text`
*(service.py → recording_chain.py)*  
Конкатенирует тексты всех записей цепочки по порядку добавления.  
Params: `{chain_id}` — обязателен.  
Returns: `{text: string}`

### `unlink_recording_from_chain`
*(service.py → recording_chain.py)*  
Убирает запись из цепочки (не удаляет саму запись истории).  
Params: `{chain_id, item_id}` — оба обязательны.  
Returns: `{ok: true, removed: bool}` (removed=false если записи не было в цепочке — идемпотентно) или `{ok: false, error: string}`

> **SummaryProfileManager** — профили стиля резюмирования (5 встроенных: brief/detailed/bullet_points/meeting_notes/telegram + кастомные).

### `list_summary_profiles`
*(service.py → history_service.py → summary_profiles.py)*  
Список всех профилей.  
Нет params.  
Returns: `{profiles: [{name, system_prompt, max_tokens, format_instructions, builtin}]}`

### `add_summary_profile`
*(service.py → history_service.py → summary_profiles.py)*  
Создаёт ИЛИ заменяет кастомный профиль (upsert среди кастомных; нет отдельного delete-метода). Имя не может совпадать со встроенным.  
Params: `{name, prompt, max_tokens=300, format_instructions?}` — `name`/`prompt` обязательны, prompt попадает в поле ответа `system_prompt`. Макс 100/2000/500 символов соответственно.  
Returns: `{profile: {name, system_prompt, max_tokens, format_instructions, builtin: false}}` — совпадение с встроенным именем → `ValueError`.

---

## Event-мост IPC→REST (2026-07-07)

**НЕ IPC-метод** (не в dispatch table `service.py`) — REST-only внутренний
эндпоинт. Закрывает класс багов «событие эмитится в IPC-процессе (`service.py`),
подписчик слушает REST-процесс (`rest_server.py` :5005)» — жертвы: wake word /
`krab_error` (чинились IPC-поллингом), `rewriter_recovered` flash-green,
`live_subs.result` агентским путём.

`backend/event_bridge.py::EventBridge` подписывается на локальную шину
IPC-процесса (`event_bus.add_listener`), батчами (≤20 конвертов) POST-ит на
`POST /internal/event` (REST-процесс, loopback-only + bridge-токен
`<data_dir>/event_bridge_token`, права 0600, ВСЕГДА требуется независимо от
`REST_API_AUTH_ENABLED`/`REST_API_KEY`) → `EventBus.emit_envelope()` на
REST-стороне доставляет конверт КАК ЕСТЬ существующим SSE/WS подписчикам без
повторного вызова push-листенеров (структурный no-echo guard — вебхуки не
фаерятся дважды на одно событие).

Настройка `event_bridge_enabled` (default `True`, `KRAB_EAR_EVENT_BRIDGE_ENABLED`)
— killswitch, читается один раз при старте (как `disk_monitor_enabled`, НЕ
live-toggle через `set_settings`).

Наблюдаемость: `get_diagnostics.event_bridge` →
`{enabled, state, queue_depth, sent, dropped, dropped_stale, failed}`
(`state`: `"unknown"`|`"up"`|`"down"`|`"disabled"`).

REST недоступен → экспоненциальный backoff 1→30с, WARN только по смене состояния
(не на каждое событие), эмиттеры никогда не блокируются, deque(256) drop-oldest
при переполнении. Stale-TTL: конверты старше `MAX_EVENT_AGE_SEC=30.0` при отправке
отбрасываются (не доставляются задним числом после долгого даунтайма REST) —
счётчик `dropped_stale`. Однонаправлено (IPC→REST) — REST-originated события
вебхуками не форвардятся (известный пре-существующий гэп, вне скоупа этой волны).

Живой двухпроцессный e2e (`scripts/run_e2e_bridge_smoke.command`) доказывает:
нормальную доставку (мс-класс latency), хаос-кейс (REST убит → IPC не
блокируется), восстановление (событие доходит после рестарта REST), и отдельно
`realtime.partial_transcript` (5-я жертва гэпа — `StreamingPasteController.
swift:121`, streaming paste) проходит через мост так же, как `krab_error`.
`streaming_paste_enabled` теперь разблокирован мостом (кандидат обратно в
A1-пресет, см. `2026-07-07-recommended-setup-DRAFT.md`).

---

## Misc

| Метод | Описание |
|---|---|
| `batch` | Пакетное выполнение нескольких методов |
| `get_context_memory` | Контекстная память STT |

### `batch`
*(service.py)*  
Пакетное выполнение нескольких IPC-методов за один вызов (макс. 50).  
Params: `{requests: [{id, method, params}, ...]}`  
Returns: `{results: [{id, ok, result}]}`

**Пример:**
```json
Request:  {"id":"b1","method":"batch","params":{"requests":[{"id":"r1","method":"ping","params":{}},{"id":"r2","method":"get_recording_state","params":{}}]}}
Response: {"id":"b1","ok":true,"result":{"results":[{"id":"r1","ok":true,"result":{...}},{"id":"r2","ok":true,"result":{...}}]}}
```

### `get_context_memory`
*(service.py)*  
Возвращает текущее состояние контекстной памяти STT (слова и темы из последних транскрибаций).  
Params: `{max_words?, last_n?}`  
Returns: `{context_words: [...], recent_topics: [...], size}`

---

*Документ сгенерирован из `service.py` (строки 905–1233) + делегированных сервисных модулей. Живое количество хэндлеров: `grep -cE '"[a-z_]+":\s*self\._' KrabEar/backend/service.py`.*
