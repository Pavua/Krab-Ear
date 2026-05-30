# Аудит: VGWebSocketClient — Wave W1675

**Файл:** `KrabEar/backend/vg_ws_client.py`  
**Дата:** 2026-05-30  
**Статус:** 6 находок (1 HIGH, 2 MEDIUM, 3 LOW)

---

## Контекст

`VGWebSocketClient` — асинхронный WebSocket-клиент к Voice Gateway `/v1/sessions/{id}/stream`.
Проброс входящих событий в `EventBus` (→ SSE → Swift). W1209 добавил TLS-проверку
(`CERT_REQUIRED`) и regex-валидацию `session_id` для защиты от path-traversal. Настоящий
аудит — первый целостный осмотр после W1209.

**Ключевые факты:**
- 138 LOC, нет зависимых backend-модулей (только `event_bus`, `contracts.registry`).
- Реконнект: exponential backoff 1 → 2 → 4 → 8 → 10 s (cap `_RECONNECT_MAX_SEC = 10`).
- TLS: `ssl.CERT_REQUIRED` для `wss://`, `None` для `ws://`.
- `max_size = 2 MiB` (W1209 F4).
- `open_timeout = 30 s` (W1209 F1).
- Класс **не импортируется ни одним production-модулем** (только тесты) — см. F-1 ниже.
- Тестовое покрытие: `test_vg_ws_client.py` (15 тестов) + `test_vg_ws_client_security_W1209.py` (security-тесты).

---

## Находки

### F-1 · HIGH · Класс не используется в production-коде (dead module)

**Описание.** `grep -rn "VGWebSocketClient\|from.*vg_ws_client"` по `KrabEar/backend/`
(исключая тесты и `error_codes.py`) возвращает 0 результатов. `VGWebSocketClient`
существует только в тестах. `CallAssistService` использует синхронный `VoiceGatewayClient`
(HTTP REST, `call_assist_service.py`), но **не запускает VGWebSocketClient ни в одной точке**.
Метод `CallAssistService.handle_start` не инстанциирует и не хранит ни одного `VGWebSocketClient`.

**Последствие.** Весь WS-стриминг событий от VG (`stt.final`, `tts.audio`) не доставляется
в EventBus при реальном call-assist сеансе. Если фича задумана как рабочая —
это незамеченный регрессион. Если фича ещё не активирована — модуль необходимо пометить
`# STATUS: dormant` и убрать из регулярных аудитов.

**Исправление.** Либо добавить вызов в `CallAssistService.handle_start` (создать задачу
`asyncio.create_task` в asyncio-loop, передав инстанс клиента), либо добавить комментарий
`# STATUS: dormant — not yet wired to CallAssistService` в начало файла.

---

### F-2 · MEDIUM · Heartbeat/ping-pong не настроен — silent dead-connection

**Описание.** `websockets.connect()` вызывается без явных `ping_interval` и `ping_timeout`.
По умолчанию в библиотеке `websockets` `ping_interval=20` и `ping_timeout=20` секунд — это
может работать корректно, однако **не задокументировано явно в коде**. При некоторых сетевых
топологиях (NAT timeout < 20 s, VPN keepalive) соединение может «зависнуть» без ошибки:
`async for raw in ws` блокируется бесконечно, `_stop` не проверяется внутри итерации.

**Воспроизведение.** Сценарий: NAT-правило дропает пакеты через 15 s простоя. WS-итерация
зависает → реконнект не происходит → события не доставляются.

**Исправление.** Сделать поведение явным:
```python
async with websockets.connect(
    self.ws_url,
    ...
    ping_interval=20,
    ping_timeout=20,
) as ws:
```
Добавить константы `_VG_WS_PING_INTERVAL = 20` и `_VG_WS_PING_TIMEOUT = 20` с комментарием.

---

### F-3 · MEDIUM · session_id test W1209 имеет contradictory expectation (пустой ID)

**Описание.** `test_handles_invalid_session_id` в `test_vg_ws_client.py` (строка 270–276)
передаёт пустой `session_id=""` и **ожидает успешную конструкцию**:
```python
client = VGWebSocketClient("http://localhost:8090", "")
self.assertIn("/v1/sessions//stream", client.ws_url)
```
Но W1209 (F2) добавил regex-валидацию `^[A-Za-z0-9_-]{1,128}$` — пустая строка **не
проходит** regex, и `__init__` должен поднимать `ValueError`. Тест либо устарел (не
адаптирован после W1209), либо отражает иной сценарий. При запуске тест **упадёт**
с `ValueError`, что свидетельствует о регрессии тестового покрытия.

**Исправление.** Обновить тест: ожидать `ValueError` на пустой `session_id`.
```python
def test_handles_invalid_session_id(self):
    with self.assertRaises(ValueError):
        VGWebSocketClient("http://localhost:8090", "")
```

---

### F-4 · LOW · URL-схема: произвольный `gateway_url` без `http(s)://` даёт неверный WS URL

**Описание.** `__init__` строит `ws_url` через `.replace("http://", "ws://")` и
`.replace("https://", "wss://")`. Если `gateway_url` пришёл уже как `ws://` или `wss://`,
`replace` ничего не меняет и результат корректен. Но если придёт нестандартный URL
(например `tcp://...` или опечатка `httpsmy-gateway.local`) — замены не произойдут и
`websockets.connect` получит невалидный URL, что даст неочевидную ошибку.

**Исправление.** Добавить fallback-guard:
```python
ws_base = gateway_url.replace("http://", "ws://").replace("https://", "wss://")
if not ws_base.startswith(("ws://", "wss://")):
    raise ValueError(f"Unsupported gateway_url scheme: {gateway_url!r}")
```

---

### F-5 · LOW · `ws_url` содержит `session_id` в DEBUG-логе — избыточная утечка в plain-text логах

**Описание.** Строка 70:
```python
logger.info("VG WS connected: %s", self.ws_url)
```
`ws_url` включает `session_id` (embed в path) и при `LOG_FORMAT=json` попадает в
лог-запись целиком. `session_id` формата `vs_<hex>` не является секретом, однако
в сочетании с `ws_url` (хост + порт VG) это информация, полезная для SSRF-анализа.

**Исправление.** Логировать только хост и `session_id` отдельно:
```python
logger.info(
    "VG WS connected",
    extra={"host": self.ws_url.split("/")[2], "session_id": self.session_id},
)
```

---

### F-6 · LOW · Нет `close_timeout` — graceful shutdown может заблокироваться

**Описание.** `stop()` устанавливает `asyncio.Event`, что прерывает `await
asyncio.wait_for(self._stop.wait(), ...)`. Однако при вызове `stop()` во время активного
`async for raw in ws:` итерация прерывается только при получении следующего сообщения или
закрытии WS со стороны сервера. Если VG не отправляет ничего в течение нескольких секунд
после `stop()` — `run()` не завершится до следующего сообщения. `websockets.connect` не
получает `close_timeout` → закрытие handshake может занять системный дефолт (обычно 10 s).

**Исправление.** Добавить `close_timeout`:
```python
async with websockets.connect(
    self.ws_url,
    ...
    close_timeout=5,
) as ws:
```

---

## Итог

| # | Severity | Проблема |
|---|----------|---------|
| F-1 | HIGH | Класс не используется в production — VGWebSocketClient dead module |
| F-2 | MEDIUM | Heartbeat не задокументирован явно — риск silent dead-connection |
| F-3 | MEDIUM | Тест `test_handles_invalid_session_id` противоречит W1209 regex-guard |
| F-4 | LOW | Нестандартная схема URL не отклоняется с понятной ошибкой |
| F-5 | LOW | `ws_url` (с session_id и хостом VG) попадает целиком в INFO-лог |
| F-6 | LOW | Нет `close_timeout` — graceful shutdown может занять 10+ секунд |

**Следующий шаг (HIGH):** Подтвердить статус F-1 у владельца фичи. Если VGWebSocketClient
должен работать — вставить его вызов в `CallAssistService.handle_start`. Если дормантный —
пометить явно. Тест F-3 требует немедленного исправления (сломан).
