<!--
Технический source of truth для Krab Ear Native.
-->

# Архитектура: Krab Ear Native

## 1. Высокоуровневая схема

- **Swift Agent (`native/KrabEarAgent`)**:
  - глобальный hotkey `Right Option`;
  - режимы `headless|menubar`;
  - панель истории;
  - автовставка `Cmd+V` + fallback.
- **Python Backend (`KrabEar/backend`)**:
  - запись аудио;
  - локальная транскрибация;
  - хранение настроек и истории;
  - IPC по Unix socket.

## 2. Компоненты backend

- `KrabEar/backend/service.py`: роутинг IPC-методов.
- `KrabEar/backend/recorder.py`: захват микрофона.
- `KrabEar/backend/transcriber.py`: вызов `AudioEngine`.
- `KrabEar/backend/state_store.py`: `settings.json` + NDJSON-журналы.
- `KrabEar/backend/state_store.py`: `settings.json` + NDJSON-журналы + ускоренный индекс поиска по последним N записям.
- `KrabEar/core/engine.py`: локальный STT (balanced/max), offline-first поведение.
- `KrabEar/core/engine.py`: локальный STT (balanced/max) + постобработка хвоста (`soft|strict`).

### Формат истории

- `history.ndjson` — append-only записи;
- `history_tombstones.ndjson` — логические удаления;
- `history_status.ndjson` — обновления `paste_status`;
- компактация по триггерам (старт/размер/явная команда).

## 3. Компоненты Swift агента

- `main.swift`: AppDelegate, lifecycle, состояние записи.
- `BackendSupervisor.swift`: старт/контроль backend процесса.
- `IPCClient.swift`: JSON RPC-запросы в Unix socket.
- `HotkeyManager.swift`: глобальный toggle записи.
- `PasteService.swift`: вставка текста в активное приложение.
- `HistoryPanelController.swift`: поиск/пагинация/копирование/удаление/компактация.
- `HistoryPanelController.swift`: поиск/пагинация/копирование/удаление/компактация + очередь batch-импорта (`pause/resume/cancel`).
- `LaunchAgentManager.swift`: launchd автозапуск.
- `PermissionWizard.swift`: onboarding по правам macOS.

## 4. Ключевой runtime-поток

1. Hotkey `Right Option` -> `start_recording`.
2. Повторный hotkey -> `stop_recording`.
3. Backend возвращает транскрипт + `history_id`.
4. Агент пытается вставить текст в активное поле.
5. `set_paste_status(ok|failed)` фиксирует итог вставки.
6. При `failed` агент копирует текст в буфер, переключает режим в `menubar`, открывает панель.

## 6. IPC контракт (актуальный)

- `start_recording`
- `stop_recording` (параметры: `quality_profile`, опционально `cleanup_profile`, `translation_mode`, `translate_and_paste`)
- `get_recording_state` (для realtime preview)
- `start_call_assist` / `stop_call_assist` / `get_call_assist_state`
- `call_assist_summary` / `call_assist_diagnostics` / `call_assist_quick_phrase` / `list_call_assist_quick_phrases`
- `list_audio_inputs`
- `get_history_page` и `search_history` (опц. фильтры: `paste_status`, `translation_mode`, `translation_status`, `from_ts`, `to_ts`)
- `delete_history_item`
- `set_paste_status`
- `get_settings`
- `set_settings`
- `compact_history`
- `get_history_stats`
- `import_history_ndjson`
- `preview_transcribe_paths`
- `add_history_item`
- `translate_text`
- `get_capabilities`
- `set_translation_glossary_item`
- `remove_translation_glossary_item`

Ключевые поля в `settings.json`:
- `quality_profile`: `balanced|max`
- `cleanup_profile`: `soft|strict`
- `translation_mode`: `off|ru_to_es|es_to_ru|en_to_ru|auto|auto_to_ru|bilingual_ru_es`
- `translate_and_paste`: `true|false`
- `translation_style`: `neutral|chat|formal`
- `translation_glossary`: `{ "source": "target", ... }`
- `clipboard_mode`: `always_copy|copy_on_fail|never_copy`
- `hotkey_profile`: `default|meeting|translation`
- `audio_ducking_enabled`: `true|false`
- `audio_ducking_percent`: `0..100`
- `overlay_opacity_percent`: `15..90`
- `voice_gateway_url`: URL локального/внешнего `Krab Voice Gateway`
- `voice_gateway_api_key`: опциональный bearer token
- `call_notify_default`: `true|false`
- `call_auto_summary`: `true|false`
- `capture_source_mode`: `mic|system_audio|mic_plus_system`
- `ui_last_tab`: `dictation|live_translation|history`

Операционные скрипты (automation):
- `scripts/run_release_checklist.command` — fail-fast релизный чеклист;
- `scripts/run_daily_driver_validation.command` — daily-driver валидация;
- `scripts/run_autonomous_hour.command` — checkpoints и stop-condition;
- `scripts/run_sprint_prioritizer.command` — скоринг очереди спринтов;
- `scripts/run_roadmap_self_update.command` — self-update отчёт roadmap;
- `scripts/run_regression_radar.command` — радар повторяющихся ошибок.

Для batch-импорта:
- `preview_transcribe_paths` даёт быстрый подсчёт аудиофайлов до запуска очереди;
- `preview_transcribe_paths` возвращает `audio_count`, `sample`, `by_ext`, `total_bytes`;
- UI поддерживает drag-and-drop, очередь задач и отмену "после текущей".
- `realtime_preview_enabled`: `true|false`
- `history_policy`: `unlimited`

## 5. Границы ответственности

- Swift не содержит STT-логики.
- Python не содержит macOS UI/меню-логики.
- История и настройки являются единственным источником состояния между перезапусками.
