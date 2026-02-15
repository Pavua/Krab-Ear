# Sprint 2 Plan For Antigravity

Цель: закрыть крупный UI/client/orchestration пласт параллельно с Codex backend работой.

## Оценка объёма (ориентир)

1. Общий объём спринта: 24-36 часов.
2. Доля Antigravity в этом спринте: ~70%.
3. Доля Codex параллельно: ~30%.

## Блок A (обязательно): Krab Ear Native UI

1. History-first polish:
- сделать поведение `История` как основной рабочей вкладки;
- адаптировать низкие ширины окна (<1100, <900).

2. Filter UX:
- явный badge активных фильтров;
- горячие клавиши для 3-5 пресетов.

3. Accessibility/UI clarity:
- повысить читаемость overview/status;
- убрать визуальный шум в нижней панели.

Готовность блока A:
- сборка `swift build -c release --package-path /Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent`;
- ручной smoke открытия панели и переключения пресетов.

## Блок B (обязательно): iOS клиент

1. Gateway config screen:
- URL/API key;
- кнопка health-check (`/health`);
- сохранение настроек.

2. Session screen:
- start/stop session;
- статус и индикатор соединения;
- mock/real partial subtitles placeholder.

3. CallKit/PushKit wiring (каркас):
- интеграционный слой без продовой телефонии.

Готовность блока B:
- проект собирается без crash;
- ручной сценарий запуска/остановки сессии.

## Блок C (опционально, если есть время): OpenClaw

1. Команды:
- `!callstart`, `!callstop`, `!callstatus`, `!notify on|off`, `!calllang`.

2. UX-ответы:
- дружелюбные и информативные статусы в чате.

Готовность блока C:
- smoke сценарий команд локально.

## Ограничения

1. Не править backend зоны Codex.
2. Если нужен новый API field — только через запись в API.md (proposal).

## Сдача

1. Boundary-check:
`/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Agent Boundary Check.command antigravity`
2. Acceptance:
`/Users/pablito/Antigravity_AGENTS/Krab Ear/ANTIGRAVITY_HANDOFF/Run Antigravity Acceptance.command`
3. Отчёт:
`/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/reports/antigravity_handoff_report_YYYYMMDD_HHMM.md`
