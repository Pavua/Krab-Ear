# E4 Voice/Ear Interop — E2E + API-Bridge + Reasoning Hook

> Дата: 2026-04-06
> Статус: На ревью
> Владелец: По
> Фаза: E4 (ROADMAP_ECOSYSTEM.md)

---

## Обзор

Замыкаем E4 Voice/Ear Interop — последнюю незакрытую фазу экосистемного роадмапа. Три задачи:

1. **E2E сценарий** — верификация полного цикла `voice input → STT → translate → TTS → voice output` между Krab Ear и Voice Gateway
2. **API-bridge** — удалённый доступ к Krab Ear STT через Voice Gateway tunnel (без отдельного tunnel для Krab Ear)
3. **Reasoning hook** — точка расширения в пайплайне VG для будущей интеграции с Krab Core (интерфейс сейчас, реализация позже)

### Подход: VG-centric

Voice Gateway — единственный оркестратор. Krab Ear — чистый STT-бэкенд. Это соответствует мастер-плану (Фаза 4): «VG = real control-plane», «Krab Ear = deterministic STT pipeline».

### Предпосылки

- VG уже вызывает Krab Ear через `KrabEarSTTEngine` → `POST /v1/stt/transcribe`
- VG `_process_voice_loop` уже делает STT→Translate→TTS для Twilio
- VG `_process_mic_audio` делает то же для iPhone-микрофона
- Krab Ear `call_assist_*` создаёт VG-сессию и шлёт `stt.partial` события
- Event контракты (E4.1-E4.2) уже унифицированы: `{type, ts, data}`
- VG имеет Cloudflare Tunnel для удалённого доступа

---

## Архитектура

```
+------------------------------------------------------------------+
|                    Voice Gateway (:8090)                          |
|                                                                  |
|  +--------------+    +--------------+    +--------------+        |
|  | _process_    |    | translator   |    | tts_         |        |
|  | voice_loop   |--->| .translate() |--->| orchestrator |        |
|  | _process_    |    +--------------+    | .speak()     |        |
|  | mic_audio    |           |            +--------------+        |
|  +------+-------+    +------+-------+                            |
|         |            | reasoning    |  <-- НОВОЕ                 |
|         |            | hook         |    (интерфейс сейчас,      |
|         |            +--------------+    реализация потом)        |
|         |                                                        |
|  +------+-------+    +--------------+                            |
|  | orchestrate_ |    | /v1/stt/     |  <-- НОВОЕ                 |
|  | stt()        |    | proxy        |    (STT proxy endpoint)    |
|  +------+-------+    +------+-------+                            |
|         |                   |                                    |
|         +-------+-----------+                                    |
|                 | HTTP                                            |
|  +--------------+--+                                             |
|  | Cloudflare      |  <-- API-bridge (уже есть)                  |
|  | Tunnel          |                                             |
|  +-----------------+                                             |
+---------------------+--------------------------------------------+
                       | POST /v1/stt/transcribe
                       v
+------------------------------------------------------------------+
|                    Krab Ear (:5005)                               |
|                                                                  |
|  +--------------+    +--------------+    +--------------+        |
|  | REST API     |    | AudioEngine  |    | TextUtils    |        |
|  | /v1/stt/     |--->| (mlx-whisper)|--->| (cleanup)    |        |
|  | transcribe   |    +--------------+    +--------------+        |
|  +--------------+                                                |
|                                                                  |
|  +--------------+                                                |
|  | IPC: call_   |---> VG WebSocket /v1/sessions/{id}/stream      |
|  | assist_*     |    (рефакторинг: WS вместо polling)            |
|  +--------------+                                                |
+------------------------------------------------------------------+
```

---

## Компонент 1: Reasoning Hook (Voice Gateway)

### Назначение

Точка расширения в пайплайне VG между translate и TTS. Позволяет в будущем подключить Krab Core для «умных» ответов, подсказок, суммаризации в реальном времени.

### Интерфейс

Новый файл `app/reasoning_hook.py`:

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass(slots=True)
class ReasoningResult:
    text: str              # текст для озвучки (может быть изменён)
    source: str            # "passthrough" | "krab_core" | "custom"
    suggestion: str = ""   # подсказка для UI (не озвучи��ается)
    metadata: dict = field(default_factory=dict)

# Сигнатура hook-функции
ReasoningHookFn = Callable[
    [str, str, str, str, Any],        # session_id, stt_text, translated_text, target_lang, session_state
    Awaitable[ReasoningResult]
]

async def passthrough_hook(
    session_id: str,
    stt_text: str,
    translated_text: str,
    target_lang: str,
    session_state: Any,
) -> ReasoningResult:
    """Дефолтный hook: передаёт перевод без изменений."""
    return ReasoningResult(text=translated_text, source="passthrough")
```

### Конфигурация

В `app/config.py` (`GatewaySettings`):

```python
reasoning_hook_enabled: bool = False      # выключен по умолчанию
reasoning_hook_url: str = ""              # URL Krab Core (будущее)
reasoning_hook_timeout_ms: int = 2000     # жёсткий лимит на латентность
```

### Интеграция в пайплайн

В `_process_voice_loop` и `_process_mic_audio` — между translate и TTS:

```python
# После translate, перед TTS:
if settings.reasoning_hook_enabled and _reasoning_hook is not None:
    try:
        reasoning = await asyncio.wait_for(
            _reasoning_hook(session_id, stt_text, translated_text, tgt_lang, state),
            timeout=settings.reasoning_hook_timeout_ms / 1000.0,
        )
        final_text = reasoning.text
        if reasoning.suggestion:
            await _publish_event(session_id, "reasoning.suggestion", {
                "text": reasoning.suggestion,
                "source": reasoning.source,
            })
    except asyncio.TimeoutError:
        logger.warning("Reasoning hook timeout (%dms), using passthrough", settings.reasoning_hook_timeout_ms)
        final_text = translated_text
else:
    final_text = translated_text

# TTS получает final_text вместо translated_text
tts_result = await tts_orchestrator.speak(final_text)
```

### Критерии

- При `reasoning_hook_enabled=False` — **zero overhead** (ветка не исполняется)
- Таймаут гарантирует: hook не может сломать latency звонка
- `reasoning.suggestion` публикуется как событие для UI, но не озвучивается
- Событие `reasoning.suggestion` — проброс, без Pydantic-модели в Krab Ear contracts

---

## Компонент 2: STT Proxy Endpoint (Voice Gateway)

### ��азначение

Удалённый доступ к Krab Ear STT через VG tunnel. Для клиентов вне локальной сети (iPhone, будущие интеграции).

### Endpoint

```
POST /v1/stt/proxy
Content-Type: multipart/form-data
Authorization: Bearer {api_key}

Параметры формы:
  file:       audio файл (wav/ogg/mp3, обязательно)
  language:   "auto" | "ru" | "es" | "en" (default: "auto")
  domain:     "casual" | "finance" | "code" | "meeting" (default: "casual")
  vocabulary: "слово1,слово2" (опционально)

Response 200:
{
  "status": "ok",
  "text": "распознанный текст",
  "confidence": 0.92,
  "duration_ms": 1250,
  "engine": "krab_ear",
  "model": "mlx-community/whisper-large-v3-turbo",
  "language": "ru",
  "segments": [...]
}

Response 503 (все движки недоступны):
{
  "status": "error",
  "error": "stt_unavailable"
}
```

### Реализация

```python
@app.post("/v1/stt/proxy")
@_auth_required
async def stt_proxy(file: UploadFile = File(...), ...):
    audio_bytes = await file.read()

    # Пробуем через orchestrate_stt (Krab Ear → faster-whisper → cloud)
    result = await orchestrate_stt(
        audio_bytes,
        language=language,
        settings=settings,
        domain_hint=domain,
        extra_vocabulary=vocabulary.split(",") if vocabulary else None,
    )

    if result.error and not result.text:
        raise HTTPException(503, detail="stt_unavailable")

    return {
        "status": "ok",
        "text": result.text,
        "confidence": result.confidence,
        "duration_ms": result.duration_ms,
        "engine": result.engine_name,
        "model": result.model,
        "language": result.language,
    }
```

### Формат ответа

Совместим с Krab Ear `/v1/stt/transcribe` — удалённый клиент получает тот же формат. Поле `segments` опционально (зависит от движка).

---

## Компонент 3: Call Assist WebSocket Client (Krab Ear)

### Назначение

Рефакторинг `_call_assist_loop` в `service.py` — замена polling на WebSocket для реального времени.

### Текущее состояние

- Синхронный `threading.Thread` с `urllib`
- Polling каждые 1.5с
- Шлёт только `stt.partial` в VG
- Не получает обратных событий

### Новое поведение

1. **WebSocket подписка** к `WS /v1/sessions/{id}/stream`
2. **Event forwarding** — события о�� VG → Krab Ear EventBus → SSE `/v1/events`
3. **Отправка аудио** — снапшоты через `POST /v1/sessions/{id}/mic-audio`
4. **Reconnect** — при обрыве WS с exponential backoff (1→2→4с, max 10с)
5. **Graceful stop** — при `stop_call_assist` закрываем WS и останавливаем loop

### Реализация

Новый внутренний модуль `KrabEar/backend/vg_ws_client.py`:

```python
"""WebSocket клиент для Voice Gateway.

Подключается к VG session stream, пробрасывает события
в Krab Ear EventBus для Swift-агента.
"""

import asyncio
import json
import logging
import websockets

from backend.event_bus import bus

logger = logging.getLogger("KrabEar.VGClient")

class VGWebSocketClient:
    def __init__(self, gateway_url: str, session_id: str, api_key: str = ""):
        ws_base = gateway_url.replace("http://", "ws://").replace("https://", "wss://")
        self.ws_url = f"{ws_base}/v1/sessions/{session_id}/stream"
        self.api_key = api_key
        self.session_id = session_id
        self._stop = asyncio.Event()

    async def run(self):
        """Основной цикл: подключение + проброс событий."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                headers = {}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                async with websockets.connect(self.ws_url, extra_headers=headers) as ws:
                    backoff = 1.0  # reset on success
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        event = json.loads(raw)
                        # Пробрасываем в Krab Ear EventBus
                        bus.emit(event.get("type", "unknown"), event.get("data", {}))
            except Exception as e:
                if self._stop.is_set():
                    break
                logger.warning("VG WS disconnected (%s), reconnect in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def stop(self):
        self._stop.set()
```

### Интеграция в service.py

`_call_assist_loop` заменяется на:

```python
def _call_assist_loop(self, session_id, gateway_url, api_key):
    """Запускает VG WS client + audio sender в asyncio loop."""
    loop = asyncio.new_event_loop()
    client = VGWebSocketClient(gateway_url, session_id, api_key)

    async def _run():
        ws_task = asyncio.create_task(client.run())
        audio_task = asyncio.create_task(
            self._audio_send_loop(session_id, gateway_url, api_key)
        )
        # Ждём stop-сигнала
        while self._call_assist_state.get("active"):
            await asyncio.sleep(0.5)
        client.stop()
        audio_task.cancel()
        await ws_task

    loop.run_until_complete(_run())
    loop.close()
```

`_audio_send_loop` — async-версия текущего audio polling (snapshot каждые 2с, POST в VG mic-audio).

---

## Компонент 4: E2E Smoke Test

### В Voice Gateway: `tests/test_e2e_krab_ear_integration.py`

```python
"""Интеграционный тест: VG + Krab Ear E2E.

Требует: оба сервиса запущены (VG :8090, Krab Ear :5005).
Запуск: pytest tests/test_e2e_krab_ear_integration.py -v
Пропускается автоматически если сервисы недоступны.
"""

import pytest
import httpx

VG_URL = "http://127.0.0.1:8090"
EAR_URL = "http://127.0.0.1:5005"

def _services_available():
    try:
        return (httpx.get(f"{VG_URL}/health", timeout=2).is_success
                and httpx.get(f"{EAR_URL}/health", timeout=2).is_success)
    except Exception:
        return False

@pytest.mark.skipif(not _services_available(), reason="VG or Krab Ear not running")
class TestE2EKrabEarIntegration:

    def test_stt_proxy_uses_krab_ear(self):
        """STT proxy → Krab Ear → текст."""
        with open("tests/fixtures/test_phrase_ru.wav", "rb") as f:
            resp = httpx.post(f"{VG_URL}/v1/stt/proxy",
                              files={"file": ("test.wav", f, "audio/wav")},
                              data={"language": "ru"},
                              timeout=30)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert len(data["text"]) > 0
        assert data["engine"] == "krab_ear"

    def test_session_voice_loop(self):
        """Session → mic-audio → stt.final + translation.final."""
        # Create session
        resp = httpx.post(f"{VG_URL}/v1/sessions",
                          json={"translation_mode": "ru_to_es", "source": "mic"})
        session_id = resp.json()["session_id"]

        try:
            # Send audio
            with open("tests/fixtures/test_phrase_ru.wav", "rb") as f:
                httpx.post(f"{VG_URL}/v1/sessions/{session_id}/mic-audio",
                           content=f.read(), timeout=30)

            # Check timeline has events
            resp = httpx.get(f"{VG_URL}/v1/sessions/{session_id}/timeline")
            timeline = resp.json()
            event_types = [e["type"] for e in timeline.get("events", [])]
            assert "stt.final" in event_types
        finally:
            httpx.delete(f"{VG_URL}/v1/sessions/{session_id}")
```

### �� Krab Ear: `KrabEar/tests/test_e2e_voice_loop.py`

Аналогичный тест со стороны Krab Ear — проверяет что `call_assist` корректно создаёт сессию и получает события через WS.

---

## Поток событий

### Сценарий: iPhone звонок с переводом

```
1. Swift Agent: start_call_assist(translation_mode="ru_to_es")
2. Krab Ear: POST VG /v1/sessions → session_id=vs_abc123
3. Krab Ear: WS connect VG /v1/sessions/vs_abc123/stream
4. Krab Ear: записывает микрофон → snapshot каждые 2с
5. Krab Ear: POST VG /v1/sessions/vs_abc123/mic-audio (PCM16)
6. VG: orchestrate_stt(pcm) → KrabEarSTTEngine → POST :5005/v1/stt/transcribe
7. VG: publish stt.final → WS → Krab Ear EventBus → SSE → Swift UI
8. VG: translator.translate(text, ru→es)
9. VG: publish translation.final → WS → Krab Ear → Swift UI
10. VG: [reasoning hook — passthrough]
11. VG: tts_orchestrator.speak(translated) → audio
12. VG: publish tts.ready → WS → Krab Ear → Swift UI
13. (если PSTN) VG: inject_audio_to_twilio(audio)
```

### Сценарий: Удалённый STT (API-bridge)

```
1. Remote client: POST VG_TUNNEL_URL/v1/stt/proxy (audio file)
2. VG: orchestrate_stt() → KrabEarSTTEngine → POST :5005/v1/stt/transcribe
3. VG: return {text, confidence, engine: "krab_ear", ...}
```

---

## Контракт совместимости

### Krab Ear владеет

- `stt.partial`, `stt.final`, `stt.failed` (Pydantic модели в `contracts/`)
- `translation.completed`, `translation.failed`

### Voice Gateway владеет

- `tts.ready`, `call.state`, `call.closed`
- `reasoning.suggestion` (новое, будущее)

### Проброс событий

VG-события пробрасываются через Krab Ear EventBus как raw dict (`bus.emit(type, data)`). **НЕ** через `bus.emit_typed()` — Krab Ear не владеет этими схемами.

---

## Зависимости

### Voice Gateway
- Нет новых pip-зависимостей (httpx, fastapi, websockets уже установлены)

### Krab Ear
- `websockets` — для WS клиента к VG. Добавить в `requirements.txt`
- Тестовый аудио-файл `tests/fixtures/test_phrase_ru.wav` — короткая русская фраза (3-5 сек), нужно создать или скопировать из `test_audio.wav` в корне

---

## Что НЕ входит

- Реализация reasoning через Krab Core (будущее, мастер-план Фаза 4)
- Отдельный Cloudflare tunnel для Krab Ear
- Изменения в Swift-агенте (только Python backend)
- Изменения в iOS-приложении
- Новые Pydantic-модели для VG-событий в `contracts/`
- Тесты с реальными Twilio-звонками

---

## Порядок реализации

```
Task 1: Reasoning hook (VG)          — интерфейс, passthrough, unit тест
Task 2: STT proxy endpoint (VG)      — endpoint, unit тест
Task 3: call_assist WS client (Ear)  — рефакторинг, unit тест
Task 4: Integration test (VG)        — E2E с Krab Ear
Task 5: E2E smoke test (оба)         — полный цикл
Task 6: ROADMAP update               — E4 done
```

## Тестовая стратегия

| Уровень | Что | Где | Зависимости |
|---------|-----|-----|-------------|
| Unit | Reasoning hook (passthrough) | VG `tests/test_reasoning_hook.py` | Нет |
| Unit | STT proxy endpoint | VG `tests/test_stt_proxy.py` | Mock Krab Ear |
| Unit | call_assist WS client | Ear `tests/test_call_assist_ws.py` | Mock WS |
| Integration | Krab Ear STT через VG | VG `tests/test_e2e_krab_ear_integration.py` | Оба с��рвиса |
| E2E smoke | Полный цикл call_assist | Ear `tests/test_e2e_voice_loop.py` | Оба сервиса |
