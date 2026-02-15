# Tasks For Antigravity — Batch B (без пересечений с Codex)

Цель: закрыть оставшиеся пробелы и снять риски дублирования.

## 1) Krab Ear UI: индикатор активных фильтров (обязательно)

Что сделать:
1. Добавить label/badge в блок истории с текстом вида `Фильтры: 0` / `Фильтры: 3`.
2. Считать активными:
- поисковую строку;
- paste status != `Все`;
- translation mode != `Все`;
- translation status != `Все`;
- from/to даты непустые.
3. Обновлять badge при каждом изменении фильтров и после `Сбросить фильтры`.

Путь:
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift`

## 2) Krab Ear UI: Quick Start / Onboarding (обязательно)

Что сделать:
1. Добавить простой first-run поток в `main.swift`:
- чек доступности микрофона/автовставки (минимум диагностика состояния);
- кнопка открытия панели;
- кнопка перехода к справке.
2. Онбординг должен быть опциональным и отключаемым (`не показывать снова`).

Путь:
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/Sources/KrabEarAgent/main.swift`

## 3) OpenClaw: дедупликация voice-команд (обязательно)

Что сделать:
1. Оставить единый источник регистрации voice-команд (`tools.py` ИЛИ `commands.py`).
2. Удалить/отключить дубли, чтобы на одно сообщение приходил ровно один ответ.
3. Сохранить текущую функциональность команд:
- `!callstart`
- `!callstop`
- `!callstatus`
- `!notify on|off`
- `!calllang`

Пути:
- `/Users/pablito/Antigravity_AGENTS/Краб/src/handlers/tools.py`
- `/Users/pablito/Antigravity_AGENTS/Краб/src/handlers/commands.py`

## 4) iOS: WS partial subtitles (желательно)

Что сделать:
1. Добавить реальное подключение к `GET /v1/sessions/{id}/stream`.
2. Обновлять subtitles из `stt.partial` и `translation.partial`.
3. При stop корректно закрывать WS.

Путь:
- `/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/ios/KrabVoiceiOS/ContentView.swift`
- `/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/ios/KrabVoiceiOS/GatewayClient.swift`

## Definition of Done (Batch B)

1. Boundary-check antigravity: без нарушений.
2. Krab Ear build + release checklist: зелёные.
3. OpenClaw smoke:
- на каждую voice-команду только один ответ.
4. Короткий отчёт о выполнении:
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/reports/antigravity_batch_b_report_YYYYMMDD_HHMM.md`
