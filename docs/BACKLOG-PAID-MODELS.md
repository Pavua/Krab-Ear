# Бэклог платных моделей (отложено до восстановления квоты)

Контекст 2026-09-14: основная сессия идёт на бесплатной
**Muse Spark 1.3 Contributor** (Zen contributor-tier — промпты и ответы
используются для обучения будущих моделей Meta). Всё ниже — только после
восстановления квоты. До тех пор: бесплатная сессия = только публичный код
репо + метаданные, без PII/секретов в контексте (см. §Privacy).

## Ф0-остаток (проводка opencode)

1. `opencode.json`: `small_model` → `opencode-go/glm-5.3-flash`.
2. Агент `executor` (subagent, `opencode/deepseek-v4-flash`, playbook-дисциплина
   в промпте; акция до 20.09; точный ID подтвердить — в каталоге Zen 14.09
   `v4.1-flash` отсутствует, fallback `deepseek-v4-flash-free`) + агент
   `gate-security` (subagent, `opencode/claude-fable-5-1`,
   `edit: deny`, adversarial two-stage review).
3. MCP Sentry: remote `https://mcp.sentry.dev/mcp`, токен через
   `{env:SENTRY_AUTH_TOKEN}` (значение — только из Main Krab `.env`,
   поточечно, никогда в файлы/чат).
4. Формы сверить со схемой `https://opencode.ai/config.json` перед записью.
5. **Рестарт opencode** (конфиг не hot-reload) + проверка: `/models`,
   живой запрос Sentry MCP.

## Очередь исполнителя (DeepSeek V4 Flash, ~$0.14/$0.28 за 1M, акция до 20.09)

- R2 `test_integration_1000_cycles` (30с при load ~30).
- R4 миграция Ear-smoke на launchd (карточка; исполнение — flash).
- C6 HealthMonitor `timeoutSec: 2`.
- Поддержка agy-брифа «Глоссарий» (тексты, не визуал).

## Точечные гейты (Fable, ~$10/$50, кэш-рид $0.25)

- **R1 encryption fail-closed — гейт диффа ОБЯЗАТЕЛЕН до мержа в колею**
  (privacy-touching diff).
- Любой другой privacy/security-дифф (см. recurring class «fail-open
  в except-ветке»).
- Пилот дешёвого визуала (GPT 5.4 Mini, ~$0.75/$4.50) — только после
  провала/дороговизны остальных; жирные визуал-брифы остаются за
  Gemini 3.1 Pro High (квота agy-сессии владельца, не Zen).

## Не использовать для PII-работы (обучаются/логируют)

- Muse Spark Contributor Free (эта сессия), Big Pickle, MiMo-V2.5 Free,
  Nemotron * Free (trial: сессии логируются для security+improvement).
- Ноль-риск альтернативы: локальные LM Studio-модели (данные не покидают
  машину) или платные Zen-модели (zero-retention, без обучения).

## Триггер

Квота восстановлена → сообщить владельцу → выполнить Ф0 → рестарт →
прогнать R1-дифф через `gate-security` до мержа.
