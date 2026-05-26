<!--
Операционный runbook Krab Ear.
-->

# Krab Ear Runbook

## Быстрые команды
1. Запуск агента: `/Users/pablito/Antigravity_AGENTS/Krab Ear/Start Krab Ear.command`
2. Обновление: `/Users/pablito/Antigravity_AGENTS/Krab Ear/Update Krab Ear Agent.command`
3. Smoke: `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Release Smoke.command`
4. Release checklist: `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Release Checklist.command`
5. Daily-driver: `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Daily Driver Validation.command`
6. Regression radar: `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Regression Radar.command`
7. Boundary-check (Codex/Antigravity): `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Agent Boundary Check.command codex`

## Если не работает вставка
1. Проверить `~/Library/Application Support/KrabEar/agent.log`
2. Найти `reason=...` в последних строках
3. Для `accessibility_not_granted` включить Accessibility для runtime бинаря:
`/Users/pablito/Antigravity_AGENTS/Krab Ear/native/runtime/KrabEarAgent`

## Если не открывается панель
1. Выполнить `/Users/pablito/Antigravity_AGENTS/Krab Ear/Open Krab Ear Panel.command`
2. Проверить, что процесс активен: `pgrep -fl KrabEarAgent`
3. Проверить backend socket: `~/Library/Application Support/KrabEar/krabear.sock`

## Если обновление не стартует
1. Проверить preflight-ошибку в выводе `Update Krab Ear Agent.command`
2. Проверить свободное место и права записи в `native/runtime`
3. Проверить venv: `.venv_krab_ear/bin/python`

## Еженедельный ритуал стабильности
1. `Run Release Checklist.command`
2. `Run Daily Driver Validation.command`
3. `Run Regression Radar.command`
4. Сохранить отчёты из `docs/reports`

## Ghost-recording recovery (v2026-02-15+)
1. Если hotkey дал двойной старт, backend теперь отвечает `already_recording` (идемпотентно).
2. Агент перед toggle синхронизирует `isRecording` через `get_recording_state`.
3. При десинхроне агент сначала выполняет stop для зависшей сессии и только потом допускает новый start.
4. Для быстрой проверки цикла выполнить 20 повторов:
`/Users/pablito/Antigravity_AGENTS/Krab Ear/scripts/run_soak_backend.command 20`
