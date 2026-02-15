<!--
Как запускать soak-тесты Krab Ear backend и читать отчёты.
-->

# Soak Testing

## Быстрый запуск

- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Backend Soak Test.command`
- c кастомным числом циклов: `scripts/run_soak_backend.command 5000`

Скрипт сохраняет:
- JSON: `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/reports/soak_backend_*.json`
- Markdown: `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/reports/soak_backend_*.md`
- latest-срез:
: `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/reports/soak_backend_latest.json`
: `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/reports/soak_backend_latest.md`
- индекс прогонов:
: `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/reports/SOAK_BACKEND_INDEX.md`

## Какие метрики выдаются

- latency: `add`, `set_paste_status`, `page`, `search`, `delete`, `compact` (`p50/p95/avg`)
- `paste_success_rate` (эмулируемый показатель в рамках soak-потока)
- `crash_count`

## Интерпретация

- `status=ok`, `crash_count=0` — базовый критерий стабильности пройден.
- рост `p95` в нескольких прогонах подряд — сигнал к профилированию/оптимизации.
- падение `paste_success_rate` в soak может указывать на регрессию IPC-контракта статусов.
