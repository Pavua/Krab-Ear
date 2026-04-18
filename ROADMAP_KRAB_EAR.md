# ROADMAP_KRAB_EAR.md

Обновлено: 2026-02-17  
Статус: В реализации (Фазы EAR-1, EAR-2 завершены)

## Цель сервиса

`Krab Ear` — независимый сервис распознавания речи и аудио-предобработки.
Сервис не содержит бизнес-логики Telegram и не зависит от `Krab` рантайма напрямую.

## Почему раздельно

- Упрощается масштабирование STT отдельно от чата.
- Падение STT-контура не валит `Krab Core`.
- Можно переиспользовать `Krab Ear` в других каналах (не только Telegram).

## Контракт интеграции (минимум)

### REST

- `POST /v1/stt/transcribe`
  - вход: `audio_url|audio_file`, `lang_hint`, `chat_id`, `message_id`, `trace_id`
  - выход: `text`, `confidence`, `duration_ms`, `engine`, `segments[]`
- `GET /health`
- `GET /metrics`

### События (опционально)

- `stt.completed`
- `stt.failed`

## Фазы

### EAR-1 (P0): Надёжность STT

- [x] Унифицированный pipeline загрузки аудио (voice/audio/ogg/mp3/m4a).
- [x] Нормализация громкости/шумоподавление.
- [x] Таймауты и retry-политика.
- [x] Идемпотентность по `(chat_id,message_id)`.

### EAR-2 (P0): Качество и наблюдаемость

- [x] WER-бенчмарк на тестовом наборе.
- [x] Логи: latency, confidence, failure reason.
- [x] Метрики p50/p95/p99 и error rate.

Статус: Реализовано (EAR-1 - EAR-4 завершены)

... (остальное без изменений) ...

### EAR-3 (P1): Контекстная транскрибация

- [x] Поддержка domain hints (финансы, код, разговорный).
- [x] User dictionary (имена/термины владельца).
- [x] Пост-обработка пунктуации и нормализации.

### EAR-4 (P1): Offline/Remote режим

- [x] Режим «локально на Mac» и «через API-шлюз».
- [x] Ограничение по размеру/длительности аудио.
- [x] Безопасное хранение временных файлов и автоочистка.

### EAR-5 (P1): STT Adapters (Advanced Speech Recognition)

- [x] SenseVoice (RU + emotion detection) — Krab-Ear #23
- [x] Parakeet-TDT-1.1B (EN OpenASR leader) — Krab-Ear #26
- [x] WhisperX (word-level timestamps + diarization) — Krab-Ear #30
- [x] Voxtral Mini 4B Realtime (STT + reasoning, Apache 2.0) — Krab-Ear #37

**Статус:** Завершено 2026-04-18. Все 4 адаптера интегрированы и протестированы.

## KPI

- STT success rate >= 98% на валидных аудио.
- P95 latency <= 8s для voice <= 60s.
- Дубли обработки одного сообщения = 0.

## Definition of Done

- [x] Есть независимый smoke-тест `Krab Ear` (`tests/test_rest_smoke.py`, `scripts/run_smoke_release.command`).
- [x] Есть контрактные тесты совместимости с `Krab` (`tests/test_contract_compatibility.py`).
- [x] Есть отдельный релизный чеклист (`RELEASE_CHECKLIST.md`).
