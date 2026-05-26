<!--
Технический source of truth для Krab Ear Native.
Последний аудит: 2026-05-26 (Wave 764).
Метрики: service.py 3873 LOC, 304 handler-а, 11 wired-сервисов (15 *_service.py файлов).
-->

# Архитектура: Krab Ear Native

## 1. Высокоуровневая схема

- **Swift Agent (`native/KrabEarAgent`)**:
  - глобальный hotkey `Right Option`;
  - режимы `headless|menubar`;
  - панель истории (3 вкладки: Запись / История / Настройки);
  - автовставка `Cmd+V` + fallback.
- **Python Backend (`KrabEar/backend`)**:
  - запись аудио;
  - локальная транскрибация (mlx-whisper, GigaAM, SenseVoice, Parakeet);
  - хранение настроек и истории;
  - IPC по Unix socket (JSON-RPC).

```
┌─────────────────────────┐   Unix socket (JSON-RPC)   ┌──────────────────────────┐
│  Swift Agent (macOS)    │ ◄────────────────────────►  │  Python Backend           │
│  - HotkeyManager        │                             │  - IPCServer              │
│  - PasteService         │                             │  - BackendService (hub)   │
│  - HistoryPanel+12 ext  │   Krab Ear.app/             │    → SettingsService      │
│  - BackendSupervisor    │   (bundle wraps agent       │    → HistoryService       │
│  - HealthMonitor        │    + Python venv)           │    → CallAssistService    │
│  - KrabEarTheme         │                             │    → TranslationService   │
│  - RealtimeOverlay      │                             │    → RecordingCoreService │
│  - NotificationService  │                             │    → TextProcessingService│
│  - LaunchAgentManager   │                             │    → AudioAnalyticsSvc    │
│  - SystemAudioDucking   │                             │    → CallSessionService   │
│  - ErrorToastPresenter  │                             │    → LiveSubsService      │
│  - WakeWordListener     │                             │    → GlossaryAutoLearn    │
│  - SingleInstanceGuard  │                             │    → TTSService           │
│                         │                             │  - AudioRecorder          │
│                         │                             │  - Transcriber            │
│                         │                             │  - Translator             │
│                         │                             │  - LLMRewriter            │
│                         │                             │  - StateStore (NDJSON)    │
│                         │                             │  - MetricsCollector       │
│                         │                             │  - VGWSClient             │
└─────────────────────────┘                             └──────────────────────────┘
```

## 2. Компоненты backend

### 2.1 Монолит и извлечённые сервисы

`KrabEar/backend/service.py` — центральный хаб IPC. Содержит `BackendService` (бизнес-логика) + `IPCServer` (Unix socket).

**Метрики на 2026-05-26:**
- **3873 LOC** (было 5821 до марафона экстракций; −33%)
- **304 активных handler-а** (live: `grep -cE '"[a-z_]+":\s*self\._' KrabEar/backend/service.py`)
- **11 wired-сервисов** — инстанцируются и делегируют в `service.py`

| # | Класс | Файл | Delegated handlers |
|---|-------|------|-------------------|
| 1 | `SettingsService` | `settings_service.py` | settings CRUD, profile presets, 5s TTL cache |
| 2 | `HistoryService` | `history_service.py` | history CRUD, SRT export, clipboard hist |
| 3 | `CallAssistService` | `call_assist_service.py` | call assist, VG WS client |
| 4 | `TranslationService` | `translation_service.py` | translate, glossary mgmt |
| 5 | `RecordingCoreService` | `recording_core_service.py` | start/stop_recording, transcribe_paths |
| 6 | `TextProcessingService` | `text_processing_service.py` | readability/transcription scoring, post-process |
| 7 | `AudioAnalyticsService` | `audio_analytics_service.py` | audio quality, waveform, silence, trends |
| 8 | `CallSessionService` | `call_session_service.py` | call session CRUD + status lifecycle |
| 9 | `LiveSubsService` | `live_subs_service.py` | system-audio streaming STT for live subtitles |
| 10 | `GlossaryAutoLearnService` | `glossary_auto_learn.py` | auto-extract domain terms, glossary proposals |
| 11 | `TTSService` | `tts_service.py` | dual-engine TTS (Silero / Kokoro / say) |

**Извлечённые, но ещё не встроенные в `service.py` (standalone files):**
- `analytics_service.py` — `AnalyticsService` (dashboard, sentiment, period compare, keyword cloud)
- `health_check_service.py` — `HealthCheckService` (ping, diagnostics, integrity check)
- `stt_management_service.py` — `STTManagementService` (hotwords CRUD, warmup, routing)
- `apple_integration_service.py` — `AppleIntegrationService` (Telegram bridge, Notes, Reminders, Calendar, iMessage)
- `text_scoring_service.py` — `TextScoringService` (warmup_rewriter, extract_terms, auto_title)

Итого 15 файлов `*_service.py`; wired в service.py — 11.

### 2.2 Прочие модули backend

- `recorder.py` — `AudioRecorder`: захват микрофона через `sounddevice`
- `state_store.py` — `StateStore`: append-only NDJSON + tombstone deletes + file-lock + compaction
- `transcriber.py` — тонкая обёртка над `AudioEngine` для profile/vocabulary management
- `translator.py` — offline-first переводчик (RU↔ES, EN→RU, Auto, Bilingual) с in-memory кэшем
- `llm_rewriter.py` — LLM post-processing (LM Studio qwen3-4b). CircuitBreaker + chatbot guard + length ratio guard
- `rest_server.py` — Flask REST API (порт 5005), отдельный от IPC
- `event_bus.py` — in-process pub/sub EventBus с SSE streaming
- `metrics_collector.py` — sliding-window метрики (latency percentiles, confidence)
- `error_bus.py` / `error_codes.py` — `KrabError` + `ErrorBus` ring buffer + Sentry tier routing; **57 error кодов** (Phase B)
- `observability.py` — Sentry/GlitchTip init; no-op без DSN
- `openwakeword_adapter.py` — wake-word detection (openWakeWord, Apache-2.0)
- `startup_diagnostics.py` — `StartupDiagnostics`: readiness checks при старте

### 2.3 Формат истории

- `history.ndjson` — append-only записи
- `history_tombstones.ndjson` — логические удаления
- `history_status.ndjson` — обновления `paste_status`
- компактация по триггерам (старт / размер / явная команда)

## 3. Компоненты Swift агента

- `main.swift`: AppDelegate, lifecycle, состояние записи
- `BackendSupervisor.swift`: двухкольцевой supervisor; exp backoff 0/2/5/15s + circuit breaker (5 fails/60s → 5 min cooldown)
- `HealthMonitor.swift`: actor с 3s ping; 2 fails → SIGTERM → wait → SIGKILL → respawn
- `IPCClient.swift`: JSON-RPC запросы в Unix socket
- `HotkeyManager.swift`: глобальный toggle записи (`Right Option`)
- `HotkeyDoubleTapDetector.swift`: double-tap (300ms) → Voice Assistant
- `PasteService.swift`: вставка текста в активное приложение
- `HistoryPanelController.swift` + 12 extension-файлов (`+CallAssist`, `+CallAutomation`, `+Diagnostics`, `+GlossarySuggestions`, `+History`, `+HistoryEnhancements`, `+Import`, `+LiveSubsSettings`, `+LiveTranslation`, `+Management`, `+SelectionTranslator`, `+Settings`)
- `RealtimeOverlayController.swift`: floating overlay для live feedback
- `LaunchAgentManager.swift`: launchd автозапуск
- `PermissionWizard.swift`: onboarding по правам macOS
- `WakeWordListener.swift`: openWakeWord bridge (Swift↔Python)
- `SingleInstanceGuard.swift`: kill duplicate `KrabEarAgent` при запуске
- `BackendToast.swift` / `ErrorToastView.swift`: severity-aware toast (AGENT-M fix: `prewarmPanel()` против CoreText AppHang)
- `StatusIndicatorView.swift`: menu bar dot (SF Symbol `circle.fill`; AGENT-J fix)
- `SentryConfig.swift`: Sentry/GlitchTip init; читает DSN из IPC; no-op без DSN

#### Phase 2 Swift:
- `SelectionTranslator.swift`: Cmd+Shift+T → AX API → `translate_selection` IPC → write back
- `SystemAudioCapture.swift`: ScreenCaptureKit tap → base64 PCM → `live_subs_ingest`
- `LiveSubtitlesOverlay.swift`: floating HUD, 4s auto-fade

#### Phase 3 Swift:
- `CallAutomationController.swift`: outbound call lifecycle + cost ticker + silence probe

## 4. Ключевой runtime-поток

1. Hotkey `Right Option` → `start_recording`
2. Повторный hotkey → `stop_recording`
3. Backend возвращает транскрипт + `history_id`
4. Агент пытается вставить текст в активное поле
5. `set_paste_status(ok|failed)` фиксирует итог вставки
6. При `failed` — копирует текст в буфер, переключает в `menubar`, открывает панель

## 5. Границы ответственности

- Swift не содержит STT-логики
- Python не содержит macOS UI/меню-логики
- История и настройки — единственный источник состояния между перезапусками
- IPC socket path: production = `~/Library/Application Support/KrabEar/krabear.sock`; dev = `~/.krab_ear_data/backend.sock`

## 6. IPC контракт (ключевые методы)

Полный справочник: `docs/IPC_API_REFERENCE.md` (PR #243, ~4341 строк; drift ~58% по данным W657).
Live count: `grep -cE '"[a-z_]+":\s*self\._' KrabEar/backend/service.py` → **304** на 2026-05-26.

**Запись / транскрипция:**
- `start_recording`, `stop_recording` (params: `quality_profile`, `cleanup_profile`, `translation_mode`, `translate_and_paste`)
- `get_recording_state`, `preview_transcribe_paths`

**История:**
- `get_history_page`, `search_history` (фильтры: `paste_status`, `translation_mode`, `from_ts`, `to_ts`)
- `delete_history_item`, `set_paste_status`, `compact_history`, `get_history_stats`
- `import_history_ndjson`, `add_history_item`, `export_history_srt`

**Настройки:**
- `get_settings`, `set_settings`, `apply_profile_preset`, `list_profile_presets`

**Перевод:**
- `translate_text`, `translate_selection`
- `set_translation_glossary_item`, `remove_translation_glossary_item`, `get_glossary_suggestions`

**Call Assist (Phase 3):**
- `start_call_assist`, `stop_call_assist`, `get_call_assist_state`
- `call_assist_summary`, `call_assist_diagnostics`, `call_assist_quick_phrase`

**Диагностика / observability:**
- `ping`, `health_check`, `get_diagnostics`, `get_startup_diagnostics`
- `get_metrics_dashboard`, `probe_llm_http`, `list_llm_models`

**Phase B/C IPC (ошибки):**
- `list_recent_errors`, `clear_recent_errors`, `handle_error_action`
- `report_paste_failure`, `report_hotkey_conflict`, `report_reconnect`, `handshake`

**Ключевые поля `settings.json`:**
- `quality_profile`: `balanced|max`
- `cleanup_profile`: `soft|strict`
- `translation_mode`: `off|ru_to_es|es_to_ru|en_to_ru|auto|auto_to_ru|bilingual_ru_es`
- `translate_and_paste`: `true|false`
- `translation_style`: `neutral|chat|formal`
- `translation_glossary`: `{ "source": "target", ... }`
- `clipboard_mode`: `always_copy|copy_on_fail|never_copy`
- `audio_ducking_enabled`: `true|false`
- `audio_ducking_percent`: `0..100`
- `overlay_opacity_percent`: `15..90`
- `voice_gateway_url`: URL локального/внешнего `Krab Voice Gateway`
- `voice_gateway_api_key`: опциональный bearer token
- `call_provider`: `telnyx|twilio`
- `capture_source_mode`: `mic|system_audio|mic_plus_system`
- `sentry_dsn`: Sentry/GlitchTip DSN (пусто = no-op)
- `realtime_preview_enabled`: `true|false`
- `history_policy`: `unlimited`

## 7. Операционные скрипты

- `scripts/run_release_checklist.command` — fail-fast релизный чеклист
- `scripts/run_daily_driver_validation.command` — daily-driver валидация
- `scripts/audit_orphan_imports.py` — CI guard: ловит потерянные import сервисов (W750)
- `scripts/memory_baseline.py` — psutil RSS snapshot в CSV
- `scripts/repair_permissions.command` — TCC reset + re-grant (PR #234)
- `scripts/build_distribution_dmg.command` — distribution DMG (PR #229)
- `scripts/install_agent_launchagent.command` — launchd KeepAlive для Swift-агента
- `scripts/verify_claude_md.py` — CLAUDE.md drift checker (CI)

---

*Последний аудит: 2026-05-26 (Wave 764). Метрики сверены с `git grep` по `feature/arch-doc-audit-W764`.*
