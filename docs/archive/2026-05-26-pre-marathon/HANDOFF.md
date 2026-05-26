<!--
Docstring: Сводка передачи контекста Krab Voice v2 между агентами — архитектура, текущее состояние, открытые задачи, тесты.
--> 

# Krab Voice v2 — handoff summary

## Архитектура
- `Krab Ear` (macOS): клиент с трёхвкладочным интерфейсом (`Диктовка`, `Live перевод`, `История`), взаимодействует с `backend/service.py` через IPC, управляет history, realtime-превью, call-assist и настройками (включая `voice_gateway_url/api_key`, `capture_source_mode`, `ui_last_tab`).
- `Krab Voice Gateway` (FastAPI + WebSocket): централизует /v1/sessions API, Twilio вебхуки/медиа, telephony cost estimator и telemetry для STT/translation/TTS pipeline; снабжён launch-скриптами и тестами (`tests/test_sessions_api.py`, `tests/test_telephony_cost_estimator.py`).
- `OpenClaw Krab` (Telegram-бот): содержит `voice_gateway_client`, команды `!callstart/stop/status/notify/calllang/callcost/...`, thin-client orchestration и web-интерфейс, использует клиента для управления звонковыми сессиями без дублирования логики.
- `Krab Voice Gateway iOS` (SwiftUI skeleton): готовые `ContentView`, `GatewayClient`, WS `GatewayStreamClient`, `CallManager`, `PushRegistryManager` и модели для Phase 3/4). Скелет уже поддерживает live subtitles и cost view, но нуждается в переносе в Xcode и CallKit/PushKit логике.

## Что уже сделано
1. **GUI Krab Ear**: добавлены табы, синхронизация `ui_last_tab`, history-панель с фильтрами, call-assist controls, быстрые фразы, timeline, diagnostics и history focus/density. Сторадж настроен через `AgentSettings`/`DEFAULT_SETTINGS`. Таблица истории показывает значки перевода, позволяет итерировать страницы.
2. **Backend Krab Ear**: `service.py` реализует весь call-assist/workflow включая start/stop, summary, quick phrases, diagnostics, timeline export, list audio inputs, translations, history manipulation и прямой вызов Voice Gateway (`_request_voice_gateway_*`).
3. **Krab Voice Gateway**: FastAPI сервис поддерживает `/v1/sessions`, `/v1/quick-phrases`, runtime tuning, summaries, diagnostics, telephony cost endpoint, Twilio webhooks/stream, mobile device registration, `SessionStore`, `telephony_cost.py`, launch scripts (`start/stop_gateway.command`, `smoke_gateway_api.command`, cost estimators). Есть tests covering sessions API and cost estimator.
4. **OpenClaw Krab**: `voice_gateway_client` с HTTP+WS, нормализацией событий; handler `tools.py` содержит команды управления звонками, quick phrases, diagnostics, cost and runtime tweaks; `commands.py` и `ops.py` используют клиента для телеметрии и health checks.
5. **Krab Voice iOS**: SwiftUI skeleton с gateway клиентом, WebSocket listener, CallKit/PushKit менеджерами. README описывает шаги для развёртывания. Phase 3/4 план уже отражён в файлах.

## Что осталось сделать (next steps)
1. **Phase 3—4 completion**: перенести iOS-скелет в Xcode проект, доработать CallKit/PushKit flows, обеспечить регистрацию устройства (`/v1/mobile/devices/register`) после получения токена, интегрировать Twilio Voice SDK и bg-mode, покрыть TestFlight rollout.
2. **PSTN <-> Twilio**: подключить операторскую переадресацию на виртуальный номер (Twilio SIP) и убедиться, что `Krab Voice Gateway` получает `session_id` через `customParameters` (webhook/WS). Тестировать входящие/исходящие звонки, latency, `notify_mode`, `translation_mode`, `tts_mode`. Добавить сценарии `Telegram + WhatsApp Desktop` (VoIP) через gateway media stream.
3. **OpenClaw orchestration**: удостовериться, что `!callstart/stop/status` покрывает все edge-case (очередь, повторный вызов), добавить команды `!calllang`, `!notify`, `!calldiag`, `!callsummary` в tools handler и убедиться, что voice_gateway_client отражает новые endpoints. Подключить `voice_gateway_client` к OpenClaw scheduler и telemetry; протестировать `voice_gateway_client.get_stream_event`. Обновить документацию (ROADMAP и AGENTS) если что-то изменится.
4. **UI/UX polish**: убедиться, что `Krab Ear` показывает историю по умолчанию (вкладка `История` открывается сама), добавить опциональные `Tab` подсказки, управлять focus mode, сделать энтузиазм `Realtime preview`/`call text` (двустрочные субтитры) понятными.
5. **Cost/ops automation**: запланировать запуск `scripts/estimate_telephony_cost.command` перед крупной telephony-активностью, держать `gateway.log` в актуальном состоянии; добавить ручные smoke тесты для Twilio endpoint (`scripts/smoke_gateway_api.command`).

## Тесты и проверки
- `./scripts/start_gateway.command` / `stop_gateway.command` — локальный запуск FastAPI + логирование `gateway.log`.
- `python -m pytest tests/test_sessions_api.py` — покрытие контрактов `/v1/sessions`.
- `python -m pytest tests/test_telephony_cost_estimator.py` — проверка оценщика затрат.
- `python -m pytest tests/test_validation_challenge.py` — общая валидация FastAPI (включает WebSocket). `Run Agent Boundary Check.command codex` после changes.
- `yarn test` или `python tests/smoke_test.py` для `OpenClaw Krab` при наличии окружения (см. AGENTS). Не забыть `./start_krab.command` для smoke.
- `Krab Ear` — открыть `Open Krab Ear Panel.command`, проверить Realtime preview + history + call assist UI, запуск `Run Autonomous Cycle.command` по расписанию.

## Контексты и зависимости
1. `Krab Ear` dependencies: `sounddevice`, `PyObjC`, `openai` (transcription/translation). Проверить `requirements.txt` и launchers в `/scripts` (напр. `start_krab_agent.command`).
2. `Krab Voice Gateway` требует `.venv_krab_voice_gateway`, `uvicorn`, `FastAPI`, `httpx`, `pydantic`, `twilio` (если Twilio SDK добавится). Скрипты уже создают виртуалку и кешируют `requirements.sha256`.
3. `OpenClaw Krab` зависит от `pyrogram`, `openclaw_client`, `voice_gateway_client`. Key env vars: `OPENCLAW_BASE_URL`, `OPENCLAW_API_KEY`, `TWILIO_*` (при необходимости). Убедитесь, что `config_manager` сохраняет `runtime.last_session_id`.
4. `Krab Voice Gateway iOS` пока в каталоге `ios/KrabVoiceiOS`; для разработки нужно Xcode + provisioning + `KRAB_VOICE` переменные (gateway URL/API key).

## Рекомендации для следующего агента
1. Начинать с запуска `Krab Voice Gateway` (через `.command`), потом `Krab Ear` и `OpenClaw Krab` (по отдельности). Отследить логи `gateway.log` и `krab.log`.
2. Покрыть охват: `Krab Ear` history (loadInitial, filters), call assist (start/stop), `OpenClaw Krab` `!call*` команды, iOS + Twilio flows, Twilio media stream mapping. Протестировать `voice_gateway_client.get_stream_event` для WebSocket.
3. Обновить эту handoff-сводку по мере прогресса (файл docs/HANDOFF.md) и уведомить Antigravity (см. collaboration split). Если доступно, документируй новые API в docs/API.md и README.
