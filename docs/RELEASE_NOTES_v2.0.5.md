# Krab Ear v2.0.5 — Release Notes

**Дата выпуска:** 2026-05-26  
**Предыдущая версия:** v2.0.4 (2026-05-24)  
**Коммитов с v2.0.4:** 105

---

## 🔴 Critical fixes

- **Wave 525 (PR #619):** Постоянный singleton-lock для GigaAM-воркера — устраняет утечку ~1.5–3 ГБ RAM при повторных запусках REST-сервера (дублированный `gigaam_worker` больше не создаётся).
- **Wave 704 / W701 (этот релиз):** Тег релиза Sentry застрял на значении `"2.0.0"` — бэкенд читал хардкод из `__version__.py` вместо источника правды. Исправлено: теперь читается из цепочки `Info.plist → CFBundleVersion → VERSION file` (критично для корректного отслеживания регрессий по релизам в Sentry).
- **Wave 171 (PR #519):** Закрыт BACKEND-J — Metal GPU stream corruption (код `rewriter.gpu_stream_error` добавлен в ERROR_REGISTRY, маршрутизация в Sentry).
- **Wave 577–578 (PR #629):** Завершены ранее неполные исправления Wave 547 и 554.

## ✨ New features

- **Wave 734 (PR #663):** Новые модули `stt_management_service.py` + `apple_integration_service.py` — STT-менеджмент и интеграция с Calendar/Contacts/Notes вынесены в отдельные сервисы.
- **Wave 687 (PR #647):** Log rotation — замена `FileHandler` на `RotatingFileHandler` (Python) + проверка размера в Swift; предотвращает рост логов на длинных сессиях.
- **Wave 692–707 (PRs #649–655):** Sentry breadcrumbs для SettingsService, HistoryService, TranslationService, CallAssistService — полный покрытый путь от действия пользователя до ошибки в Sentry.
- **Wave 656 (PR #642):** `AgentRecoveryLogger` подключён к bootstrap `main.swift` — автоматическое логирование восстановлений агента.
- **Wave 686 (PR #646):** Скрипт аудита TCC-разрешений (`scripts/tcc_audit.command`).
- **Wave 490 (PR #615):** 3 новых HIGH-приоритетных error-кода Phase B Wave 82: `disk.critical`, `system.proc_cmdline_permission`, `startup.stt_model_cache_miss`.
- **Wave 156 (PR #506):** `SemanticSearcher.remove_item` API + верификация IPC `RecordingChain unlink`.
- **Wave 159 (PR #510):** `FeatureFlags` — защита от whitespace-значений; `TranscriptionQueue` — опциональный persist-режим.

## 🔧 Refactoring

- **Wave 392 (PR #602):** Извлечён `AnalyticsService` из `service.py` (~98 LOC, 6 хендлеров).
- **Wave 423 (PR #606):** Извлечён `HealthCheckService` из `service.py`.
- **Wave 172 (PR #524):** Извлечён `RecordingCoreService` — крупнейшая оставшаяся монолитная секция (`service.py` −833 LOC).
- **Wave 404 (PR #604):** Извлечён `TextScoringService`.
- Итого: **11 сервисов** извлечено из монолита (`service.py`), активных хендлеров: **318**.

## 🐛 Bug fixes

- **Wave 523 (PR #620):** Замена Unicode-буллетов в `GlobalStatusBar` на SF Symbols — сестра AGENT-J (предотвращает зависание CoreText при рендере в ColorSync callback).
- **Wave 547 (PR #624):** Замена Unicode-глифов в `CallAutomationController` на SF Symbols.
- **Wave 554 (PR #623):** Swift 6 strict concurrency warnings устранены — разблокированы все Swift PR.
- **Wave 546 (PR #625):** Defensive `float()` cast для `HISTORY_LARGE_MB` в `disk_monitor` (guard против TypeError при невалидном значении).
- **Wave 545 (PR #622):** Скоуп `test_no_stray_callers` ограничен production-кодом (разблокированы ~30 PR).
- **Wave 157 (PR #505):** `WebhookManager` SSRF-guard — блокировка localhost / RFC 1918 / link-local адресов.
- **Wave 732 (PR #661):** `pytest-xdist group` для `test_full_workflow.py` — устраняет race condition при параллельных CI-запусках.
- **model_cache_manager (PR #460):** Guard `evict()` против concurrent `FileNotFoundError` (явная race semantics).

## 🧪 Tests + coverage

Добавлены тесты для **35+ модулей** за этот цикл (Waves 71–164):
`rest_server`, `period_comparison`, `analytics_dashboard`, `error_reporter`, `event_replay`, `feature_flags`, `sharing_manager`, `smart_vocabulary`, `speaker_statistics`, `text_anonymizer`, `abbreviation_expander`, `silence_detector`, `vad`, `audio_chunker`, `audio_converter`, `audio_quality`, `paste_formatter`, `confidence_calibrator`, `transcription_scorer`, `retry_strategy`, `auto_title`, `duplicate_detector`, `datetime_normalizer`, `number_normalizer`, `auto_glossary`, `playback_tracker`, `recording_scheduler`, `recording_insights`, `semantic_search`, `bookmarks`, `recording_comparison`, `recording_merger`, `vg_ws_client`, `contracts`, `event_bus`, `TelegramBridge` (Wave 622), `ObsidianSync` error paths (Wave 659).

Dispatch invariant tests: +10 хендлеров (Waves 654, 693).  
Sequoia 26 integration tests: Wave 521.  
Итого тестов: **~11,000+ методов / 240+ файлов**.

## 📚 Docs

- `docs/USER_MANUAL.md` — секция "Что нового в v2.0.5" (Wave 690).
- `docs/audit/wave695-wake-word.md` — аудит wake-word pipeline.
- `docs/audit/wave715-sentry-release-stale-process.md` — root cause Sentry тега.
- `docs/NSALERT_HANG_INVESTIGATION.md` — документация NSAlert hang.
- `docs/USER_ACTION_CHECKLIST.md` — актуализирован (Wave 552–553).
- `RELEASE_CHECKLIST.md` — секция v2.0.5 добавлена (Wave 660).
- `IPC_API_REFERENCE.md` — drift-аудит (Wave 657, 58% drift зафиксирован).

## 🔒 Security

- **Wave 187 (PR #535):** REST legacy auth — timing attack fix (constant-time compare вместо `==`).
- **Wave 157 (PR #505):** WebhookManager SSRF guard (см. Bug fixes).

---

## Known issues

- **Duplicate gigaam_worker (P0, Wave 716):** Несмотря на singleton guard (Wave 525), при параллельном запуске многих sub-агентов могут возникать дублированные воркеры. Workaround: `scripts/kill_dup_gigaam.command`. Полный fix запланирован.
- **Daily macOS reboot (P0):** Система перезагружается в ~14:04 CEST (macOS auto-update). Disable через `System Settings → General → Software Update → Automatic Updates`.
- **IPC_API_REFERENCE.md устарел на ~58%** (Wave 657 audit) — используйте live grep по `service.py` как источник правды.
- **HF pyannote gated model** — требует ручного accept на HuggingFace для diarization longform pipeline.

---

## Migration notes

- **Ничего ломающего** — обратная совместимость сохранена для всех IPC-методов.
- **Рекомендуется:** после установки перезапустить бэкенд (`pkill -f "python.*main.py"` + рестарт) — это необходимо, чтобы Sentry получил корректный тег релиза `v2.0.5` (исправление W701).
- GigaAM singleton guard активен автоматически — дополнительных действий не требуется.
- Новые модули `stt_management_service.py` и `apple_integration_service.py` создаются при первом запуске.
