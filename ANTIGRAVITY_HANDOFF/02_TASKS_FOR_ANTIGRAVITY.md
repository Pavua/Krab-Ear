# Tasks For Antigravity (50-70% остатка)

Ниже задачи, которые можно делать параллельно и почти не пересекаются с Codex.

## A. Krab Ear Native UI (приоритет высокий)

1. Финальный UI-рефактор вкладок:
- сделать `История` дефолтной при первом открытии панели;
- добавить компактные responsive-layout правила для width < 1100;
- улучшить читаемость нижней панели (overview/status) на узких экранах.

Путь:
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift`

2. UX-ускорители в истории:
- keyboard shortcuts для быстрых пресетов (`Сегодня`, `С переводом`, `Ошибки вставки`);
- визуальные индикаторы активных фильтров (badge/label).

Путь:
- тот же `HistoryPanelController.swift`

3. Онбординг UI (без backend изменений):
- экран «быстрый старт» с проверкой разрешений и CTA на открытие panel.

Путь:
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/main.swift`

## B. iOS клиент (Krab Voice Gateway/ios) (приоритет высокий)

1. Скелет call-flow в SwiftUI + CallKit/PushKit:
- экран статуса сессии;
- кнопки start/stop;
- отображение partial subtitles.

2. Конфиг экрана Gateway:
- URL/API key;
- проверка `/health`;
- сохранение настроек локально.

Путь:
- `/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/ios`

## C. OpenClaw orchestration (приоритет средний)

1. Команды управления звонком:
- `!callstart`, `!callstop`, `!callstatus`, `!notify on|off`, `!calllang`.

2. Привязка к Gateway API (уже есть контур, доделать UX/ответы).

Путь:
- `/Users/pablito/Antigravity_AGENTS/Краб/src`

## D. Документация (shared)

1. Обновить:
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/API.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/RUNBOOK.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/README.md`

Только для реальных изменений, без «бумажных» апдейтов.
