<!--
Краткая точка входа по проекту Krab Ear Native.
-->

# Krab Ear Native (Standalone)

Krab Ear — локальный голосовой ассистент-транскрибатор для macOS:
- глобальный hotkey `Right Option` (toggle: старт/стоп записи);
- офлайн STT через локальный Python backend (`mlx-whisper`);
- diarization спикеров для импортированных аудиофайлов через `pyannote.audio` при наличии `HF_TOKEN`;
- профиль `Balanced` использует `mlx-community/whisper-large-v3-turbo`;
- профиль `Max` использует `mlx-community/whisper-large-v3-mlx` (fallback: `whisper-large-v3-turbo`);
- realtime preview во время записи (опционально);
- профиль очистки хвоста: `Soft` / `Strict`;
- пакетная транскрибация аудиофайлов/папок из UI (`Импорт аудио`);
- drag-and-drop зона импорта с очередью задач, прогрессом и отменой;
- предпросмотр импорта (подсчёт найденных аудиофайлов перед запуском);
- перевод RU<->ES / EN->RU (offline-first), включая перевод в импорте аудио;
- режим `Auto -> RU` для звонкового сценария и смешанного входного языка;
- режим `Bilingual RU<->ES` (двуязычная строка `RU: ...` + `ES: ...`);
- стиль перевода `Neutral/Chat/Formal` и пользовательский глоссарий;
- отдельные вкладки UI: `Диктовка` / `Live перевод` / `История`;
- `Call Assist` в `Live перевод`: старт/стоп сессии, выбор источника (mic/system/mix), notify ON/OFF, `Авто-summary звонка` с сохранением в историю;
- quick-phrase workflow в `Live перевод`: библиотека фраз RU/ES, отправка реплики, summary и explain-диагностика;
- фильтры истории по статусу вставки, режиму/статусу перевода и дате;
- быстрые пресеты истории: `Сегодня`, `7 дней`, `Ошибки перевода`, `Сброс дат`;
- обзорные метрики истории в панели (сегодня/24ч, вставка и перевод);
- очередь импорта с `pause/resume/cancel` для больших batch-задач;
- импорт/экспорт истории в `NDJSON` (без дублей при импорте);
- детальная оптимизация истории с отчётом по освобождённому объёму;
- режим буфера обмена `Always copy / Copy on fail / Never copy`;
- профиль поведения hotkey: `Default / Meeting / Translation`;
- шаблоны quick-actions (`RU/ES follow-up`) поверх последнего результата;
- локальный summary выбранной записи истории (`Summary выбранного`);
- режим `Фокус истории` (ON/OFF) для быстрого раскрытия таблицы транскрибаций;
- режим плотности истории `Normal/Compact` для больших списков;
- настраиваемое приглушение системного звука во время записи (`0..100%`);
- автовставка в активное поле + fallback в буфер обмена;
- безлимитная история транскрибаций в `history.ndjson` с пагинацией по 50.
- настраиваемый размер страницы истории (`25/50/100/200`) + кнопка `Загрузить всё`.

## Быстрый запуск

1. Двойной клик: `/Users/pablito/Antigravity_AGENTS/Krab Ear/Start Krab Ear.command`
2. Первый запуск:
- создаст/обновит `.venv_krab_ear`;
- установит зависимости Python;
- соберёт нативный Swift-агент.
3. Для открытия GUI-панели:
- двойной клик `/Users/pablito/Antigravity_AGENTS/Krab Ear/Open Krab Ear Panel.command`

Важно:
- `Start Krab Ear.command` больше не форс-обновляет нативный агент при каждом изменении исходников.
- Для установки новой версии используйте: `/Users/pablito/Antigravity_AGENTS/Krab Ear/Update Krab Ear Agent.command`
- Для diarization импортированных звонков можно экспортировать `HF_TOKEN` перед запуском backend или batch-скрипта.

По умолчанию агент может работать в `headless`-режиме (без видимого окна/иконки), поэтому hotkey работает, а интерфейс не появляется сам, пока его не открыть командой выше.
В панели доступны переключатели `Автозапуск`, `Иконка в Dock`, `Автовставка`, `Звук старта`, `Realtime превью`, `Приглушать звук`, слайдер `Громкость при записи`, слайдер `Прозрачность оверлея`, профиль `Balanced/Max`, профиль очистки `Soft/Strict`, режимы перевода (`RU->ES`, `ES->RU`, `EN->RU`, `Auto`, `Auto -> RU`, `Bilingual RU<->ES`), блок `Call Assist` (источник звонка + notify + `Авто-summary звонка` + `Старт звонка`/`Стоп звонка`), поля `Gateway URL` / `API key` и кнопка проверки `/health`, блок быстрых реплик (`Загрузить фразы`, `Сказать фразу`, `Summary звонка`, `Диагностика`), кнопки `Добавить термин`/`Удалить термин` для глоссария, пресет `Live Translation`, кнопка `Swap RU<->ES`, быстрые фильтры истории (`Сегодня`, `7 дней`, `Ошибки перевода`, `С переводом`, `Без перевода`, `Ошибки вставки`, `Сброс дат`), режим `Фокус истории` для увеличения рабочей области таблицы, режим плотности `Normal/Compact`, действия `Экспорт NDJSON`/`Импорт NDJSON`, кнопки `Показать ещё`/`К последней`/`Загрузить всё`, выбор страницы `25/50/100/200`, drag-and-drop зона импорта с очередью/`Пауза импорта`/`Отменить импорт`/отчётом, профиль hotkey (`Default/Meeting/Translation`), кнопки управления агентом (`Старт/Стоп`, `Перезапуск`, `Остановить`) и статус истории с объёмом.

## Полезные скрипты

- Запуск агента: `/Users/pablito/Antigravity_AGENTS/Krab Ear/scripts/start_agent.command`
- Открыть панель истории: `/Users/pablito/Antigravity_AGENTS/Krab Ear/scripts/open_control_panel.command`
- Включить автозапуск launchd: `/Users/pablito/Antigravity_AGENTS/Krab Ear/scripts/install_agent.command`
- Удалить автозапуск: `/Users/pablito/Antigravity_AGENTS/Krab Ear/scripts/remove_agent.command`
- Запустить Voice Gateway: `/Users/pablito/Antigravity_AGENTS/Krab Ear/scripts/start_voice_gateway.command`
- Остановить Voice Gateway: `/Users/pablito/Antigravity_AGENTS/Krab Ear/scripts/stop_voice_gateway.command`
- Health-отчёт истории: `/Users/pablito/Antigravity_AGENTS/Krab Ear/scripts/run_history_health.command`
- Проверка границ зон агентов: `/Users/pablito/Antigravity_AGENTS/Krab Ear/scripts/run_agent_boundary_check.command`

## Root .command ярлыки

- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Open Krab Ear Panel.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Enable Krab Ear Autostart.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Disable Krab Ear Autostart.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Create Stable Backup.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Repair Krab Ear Permissions.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Update Krab Ear Agent.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Update and Open Krab Ear Panel.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Backend Soak Test.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Release Smoke.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Release Checklist.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Daily Driver Validation.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Autonomous Cycle.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Autonomous Hour.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Sprint Prioritizer.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Roadmap Self Update.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Regression Radar.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run UX Telemetry.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Performance Budget.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run History Health.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Run Agent Boundary Check.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Validate Latest Backup.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Preview Restore Backup.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Open Reports.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Start Krab Voice Gateway.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Stop Krab Voice Gateway.command`

## Права macOS

Нужны системные разрешения:
- Microphone
- Accessibility
- Input Monitoring

Если hotkey работает, но автовставка не вставляет текст:
1. Откройте `/Users/pablito/Library/Application Support/KrabEar/agent.log`
2. Проверьте последнюю строку `reason=...`
3. Для `reason=accessibility_not_granted` включите доступ в:
`Системные настройки -> Конфиденциальность и безопасность -> Accessibility`
4. Добавьте и включите именно runtime-бинарь агента:
`/Users/pablito/Antigravity_AGENTS/Krab Ear/native/runtime/KrabEarAgent`

## Каноничные документы

- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/PRD.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/ROADMAP.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/ARCHITECTURE.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/API.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/COLLABORATION_SPLIT.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/RUNBOOK.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/AI_WORKFLOW.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/MASTER_PLAN.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/AUTONOMOUS_EXECUTION_PLAN.md`
