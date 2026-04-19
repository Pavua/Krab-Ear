# ARCHITECTURE.md

Обновлено: 2026-02-16

## 1. Компоненты

### Krab Core (Telegram UX + orchestration)
- Обрабатывает сообщения/команды Telegram.
- Управляет очередью per-chat (`ChatWorkQueue`).
- Формирует контекст (author/reply/forward/mood).
- Выполняет policy/ops команды (`!policy`, `!ctx`, `!reactions`, `!mood`).

### OpenClaw (Reasoning + Tools Runtime)
- Основной AI gateway для cloud/tool workflow.
- Используется как cloud fallback при local stream failure.

### Local LLM (LM Studio)
- Fast/local канал с guardrails и loop-protection.
- Поток `route_stream`: local-first при `force_mode=auto`.

### Krab Ear
- Отдельный STT-проект.
- Интеграция по API, без shared runtime state.

### Krab Voice Gateway
- Отдельный TTS/call pipeline.
- Интеграция по API, независимый релизный цикл.

## 2. Ключевые runtime-контуры

### Auto Reply Queue
- Входящие сообщения не отбрасываются.
- Один worker на чат, FIFO.
- При переполнении — явный сигнал пользователю.

### Stream Guardrails
- `reasoning_limit`, `reasoning_loop`, `content_loop`, `stream_timeout`, `connection_error`.
- Tail-loop детектор для повторов абзацев, независимый от chunk boundaries.

### Feedback & Mood
- Реакции на ответы Краба сохраняются в `artifacts/reaction_feedback.json`.
- Weak-signal отправляется в `ModelRouter.submit_feedback(...)`.
- Mood чата добавляется как контекстный сигнал в prompt.

## 3. Границы ответственности
- Krab не реализует локальный web scraping как primary path.
- OpenClaw остаётся источником tool/reasoning логики.
- Ear/Voice не встраиваются в monolith, только API-контракты.

## 4. Failover
- Local stream failure -> cloud fallback.
- При invalid cloud model mapping — явная диагностическая ошибка в чат и ops.
- Каноничный restart path через `restart_core_hard.command`.
