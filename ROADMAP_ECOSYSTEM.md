# ROADMAP_ECOSYSTEM.md

Обновлено: 2026-02-16
Статус: In Progress

## Цель
Синхронизировать развитие `Krab`, `OpenClaw`, `Krab Ear`, `Krab Voice Gateway` без жёсткой связки рантаймов и без дублей функционала.

## Принцип интеграции
- `Krab` + `OpenClaw`: единый логический контур UX + reasoning/tool runtime.
- `Krab Ear`: отдельный сервис, интеграция только по API.
- `Krab Voice Gateway`: отдельный сервис, интеграция только по API.

## Отдельные дорожные карты сервисов
- `ROADMAP_KRAB_EAR.md` — полный трек STT/аудио-предобработки.
- `ROADMAP_KRAB_VOICE_GATEWAY.md` — полный трек TTS/звонков/перевода.

## Фазы

### E1. Core Contracts (P0)
- [x] Единые health/check endpoints.
- [x] Единая модель route/context snapshot для Telegram.
- [x] Fallback local -> cloud при stream-failure.

### E2. Queue/Context Reliability (P0)
- [x] Per-chat FIFO очередь для burst-сообщений.
- [x] Явный forward/reply/author контекст.
- [x] Runtime policy-команды (`!policy`, `!ctx`).

### E3. Feedback/Mood Layer (P0)
- [x] Реакции Telegram как weak-signal (reaction store).
- [x] Chat mood profile (rolling).
- [x] Авто-реакции с kill-switch и rate-limit.

### E4. Voice/Ear Interop (P1)
- [ ] Контракт событий STT/TTS и унифицированные payload schema.
- [ ] E2E сценарий `voice input -> chat reasoning -> voice output`.
- [ ] API-bridge для удалённого режима (вне локальной сети).

### E5. iOS Companion Track (P1)
- [ ] Отдельный roadmap для PSTN-перевода на iOS.
- [ ] Определить target-архитектуру: on-device / relay / hybrid.
- [ ] Прототип remote call translation workflow.

## KPI
- Не теряются сообщения в burst-сценариях.
- Нет циклических «простыней» в финальных ответах.
- Ошибки local stream не ломают UX благодаря cloud fallback.
- Реакции и mood улучшают тональность, не ломая базовую policy.

## План на 30 дней (без дублей, с приоритетами)

### Неделя 1: Stability & Attribution (P0)
- [x] Дожать FIFO/forward/reply/author контекст.
- [x] Усилить anti-loop для stream + post-sanitize.
- [x] Вынести контекст в `GET /api/ctx` для наблюдаемости.
- [ ] Добавить e2e-тест «burst forwards + group reply attribution».

Критерий готовности:
- 0 потерь в burst из 10 сообщений в одном чате.
- 0 случаев «подмены автора» в групповом сценарии теста.

### Неделя 2: Agentic Reliability (P1)
- [ ] Ввести профили задач для роев: `dev`, `research`, `ops`, `trade`.
- [ ] Добавить quality-gate для agent cycle: `plan -> execute -> verify -> self-critique`.
- [ ] Включить обязательный verify-step для code/tasks (тесты/линт/health).
- [ ] Добавить runbook автовосстановления swarm-задач.

Критерий готовности:
- Не менее 80% swarm-задач завершаются без ручного вмешательства.
- Для code-задач всегда есть отчёт о верификации.

### Неделя 3: Cost & Model Governance (P1)
- [ ] Ролевая политика моделей:
  - группы/мусорные чаты -> Flash Lite;
  - важные приватные с владельцем -> Gemini Pro;
  - кодинг/архитектура -> Gemini Pro по policy.
- [ ] Пер-чату закрепляемая policy с TTL.
- [ ] Ежедневный отчёт затрат и тренда по моделям.
- [ ] Алерты: drift качества/рост cost per successful task.
- [ ] Image policy governance:
  - дефолты local/cloud image-моделей,
  - health-check local ComfyUI + cloud image provider,
  - закрепление per-chat/per-owner image policy.

Критерий готовности:
- Прозрачный отчёт «стоимость на задачу» по профилям.
- Переключение policy не требует рестарта.

### Неделя 4: Ecosystem Interop (P1/P2)
- [ ] Контрактные API между Krab <-> Ear <-> Voice Gateway (versioned schemas).
- [ ] Единый интеграционный smoke-пайплайн по трём сервисам.
- [ ] Подготовка трека iOS companion (PSTN translation) без слияния рантаймов.
- [ ] Архивировать устаревшие roadmap-ветки и дубли.
- [ ] Automation layer decision:
  - определить, где нужен n8n (интеграции/cron/webhook),
  - где достаточно встроенного scheduler/commands в Krab,
  - зафиксировать «не дублировать» карту ответственности.

Критерий готовности:
- Единый e2e smoke проходит с нуля за один запуск.
- Для каждого сервиса есть независимый релизный чеклист.

## Backlog (следующие фазы, приоритезация)

### B1. Long-context Intelligence
- Иерархическая память по чатам/темам.
- Автосуммаризация с quality-control и rehydrate.

### B2. Tooling Expansion
- Расширенный browser/tool orchestration через OpenClaw.
- Многошаговые web-задачи с checkpoints.

### B3. Advanced Feedback Learning
- Мультисигнальное обучение (reaction + explicit feedback + completion success).
- Анти-шум фильтрация слабых сигналов.

### B4. Trading Swarm (только после safety gate)
- Режим paper-trading как обязательный этап.
- KPI: risk-adjusted, max drawdown, discipline score.
