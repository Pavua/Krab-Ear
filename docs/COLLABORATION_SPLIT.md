<!--
Разделение обязанностей между Codex и Antigravity для параллельной работы без конфликтов.
-->

# Collaboration Split: Codex + Antigravity

## Цель

Сделать параллельную разработку предсказуемой:
1. агенты не редактируют одни и те же зоны;
2. интеграция идёт через согласованный контракт;
3. конфликтующие правки отлавливаются до release.

## Владение зонами

Источник истины: `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/agent_ownership.json`

### Codex (backend/platform)

1. `Krab Ear backend` (`/Users/pablito/Antigravity_AGENTS/Krab Ear/KrabEar/backend`)
2. `Krab Ear tests` (`/Users/pablito/Antigravity_AGENTS/Krab Ear/KrabEar/tests`)
3. `Krab Ear scripts` (`/Users/pablito/Antigravity_AGENTS/Krab Ear/scripts`)
4. `Krab Voice Gateway app/tests/scripts`

### Antigravity (UI/client/orchestration)

1. `Krab Ear native UI` (`/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent`)
2. `Krab Voice Gateway iOS` (`/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/ios`)
3. `OpenClaw integration` (`/Users/pablito/Antigravity_AGENTS/Краб/src`)

Примечание:
`OpenClaw integration` оставлен в матрице обязанностей, но исключён из авто-monitor boundary-check,
чтобы параллельная разработка отдельного проекта не давала ложные конфликты в текущем цикле Krab Voice.

### Shared

1. `docs/*` и `README.md`
2. root `.command` интеграционные ярлыки

## Правила, чтобы не мешать друг другу

1. Каждый агент работает только в своих зонах + shared.
2. Изменения cross-zone допускаются только через handoff:
   `описание -> контракт API/данных -> интеграционный smoke`.
3. Перед релизом запускается boundary-check.

## Процедура boundary-check

1. Codex: `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Agent Boundary Check.command codex`
2. Antigravity: `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Agent Boundary Check.command antigravity`
3. Отчёты: `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/reports/agent_boundary_*.md`
4. При смене матрицы ownership используйте пересоздание baseline:
`/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Agent Boundary Check.command codex --reset-baseline`

## Контракт интеграции между зонами

1. Изменения API фиксируются в `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/API.md`.
2. Если Codex меняет backend/Gateway контракт, Antigravity обновляет UI-клиент только после обновления API.md.
3. Если Antigravity меняет UI-поведение, Codex подтверждает совместимость через backend/release smoke.

## Практический workflow

1. В начале спринта: boundary-check (базовая линия/дельта).
2. Разработка в своей зоне.
3. Локальные тесты своей зоны.
4. Boundary-check повторно.
5. Интеграционные smoke.
