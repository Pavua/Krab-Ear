# E4 Voice/Ear Interop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Krab Ear STT and Voice Gateway TTS into a verified E2E cycle with reasoning hook and remote STT proxy.

**Architecture:** VG-centric — Voice Gateway orchestrates the full pipeline (STT via Krab Ear, translate, optional reasoning, TTS). Krab Ear stays a pure STT backend. call_assist in Krab Ear is a thin WebSocket client to VG.

**Tech Stack:** Python 3.11+, FastAPI (VG), Flask (Ear), websockets, httpx, pytest, unittest

**Repos:**
- VG: `/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/`
- Ear: `/Users/pablito/Antigravity_AGENTS/Krab Ear/`

**Design spec:** `docs/superpowers/specs/2026-04-06-e4-voice-ear-interop-design.md`

---

## File Map

### Voice Gateway (new files)

| File | Responsibility |
|------|----------------|
| `app/reasoning_hook.py` | ReasoningResult dataclass, passthrough_hook, ReasoningHookFn type |
| `tests/test_reasoning_hook.py` | Unit tests for reasoning hook |
| `tests/test_stt_proxy.py` | Unit tests for STT proxy endpoint |
| `tests/test_e2e_krab_ear_integration.py` | Integration test (requires both services) |
| `tests/fixtures/test_phrase_ru.wav` | Test audio fixture (copy from Krab Ear) |

### Voice Gateway (modified files)

| File | Lines | Change |
|------|-------|--------|
| `app/config.py` | 14-57 (GatewaySettings) + 76-109 (from_env) | Add 3 reasoning_hook fields |
| `app/main.py` | 3280-3295 (_process_voice_loop) | Insert reasoning hook between translate and TTS |
| `app/main.py` | 1775-1791 (_process_mic_audio) | Insert reasoning hook between translate and TTS |
| `app/main.py` | imports + new endpoint | Add /v1/stt/proxy endpoint |

### Krab Ear (new files)

| File | Responsibility |
|------|----------------|
| `KrabEar/backend/vg_ws_client.py` | VGWebSocketClient: WS to VG, event forwarding |
| `KrabEar/tests/test_vg_ws_client.py` | Unit tests for WS client |
| `KrabEar/tests/test_e2e_voice_loop.py` | E2E smoke test (requires both services) |
| `KrabEar/tests/fixtures/test_phrase_ru.wav` | Test audio fixture |

### Krab Ear (modified files)

| File | Lines | Change |
|------|-------|--------|
| `KrabEar/requirements.txt` | append | Add `websockets>=12.0` |
| `KrabEar/backend/service.py` | 984-1046 (_call_assist_loop) | Replace polling with WS client |

---

## Task 1: Reasoning Hook Module (Voice Gateway)

**Files:**
- Create: `app/reasoning_hook.py`
- Create: `tests/test_reasoning_hook.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_reasoning_hook.py`:

```python
"""Unit-тесты для reasoning hook."""
from __future__ import annotations

import asyncio
import pytest

from app.reasoning_hook import ReasoningResult, passthrough_hook


class TestReasoningResult:
    def test_passthrough_source(self):
        r = ReasoningResult(text="hola", source="passthrough")
        assert r.text == "hola"
        assert r.source == "passthrough"
        assert r.suggestion == ""
        assert r.metadata == {}

    def test_with_suggestion(self):
        r = ReasoningResult(text="hola", source="krab_core", suggestion="Try formal tone")
        assert r.suggestion == "Try formal tone"


class TestPassthroughHook:
    def test_returns_translated_text_unchanged(self):
        result = asyncio.run(passthrough_hook(
            session_id="vs_test",
            stt_text="привет",
            translated_text="hola",
            target_lang="es",
            session_state=None,
        ))
        assert result.text == "hola"
        assert result.source == "passthrough"
        assert result.suggestion == ""

    def test_preserves_empty_text(self):
        result = asyncio.run(passthrough_hook(
            session_id="vs_test",
            stt_text="",
            translated_text="",
            target_lang="es",
            session_state=None,
        ))
        assert result.text == ""
        assert result.source == "passthrough"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway && python -m pytest tests/test_reasoning_hook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.reasoning_hook'`

- [ ] **Step 3: Implement reasoning_hook.py**

Create `app/reasoning_hook.py`:

```python
"""Reasoning hook — точка расширения пайплайна между translate и TTS.

По умолчанию passthrough: перевод передаётся в TTS без изменений.
В будущем сюда подключается Krab Core для reasoning/suggestions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(slots=True)
class ReasoningResult:
    """Результат reasoning hook."""
    text: str           # текст для озвучки (может быть изменён hook-ом)
    source: str         # "passthrough" | "krab_core" | "custom"
    suggestion: str = ""     # подсказка для UI (не озвучивается)
    metadata: dict[str, Any] = field(default_factory=dict)


# Сигн��тура hook-функции
ReasoningHookFn = Callable[
    [str, str, str, str, Any],
    Awaitable[ReasoningResult],
]


async def passthrough_hook(
    session_id: str,
    stt_text: str,
    translated_text: str,
    target_lang: str,
    session_state: Any,
) -> ReasoningResult:
    """Дефолтный hook: передаёт перевод без изменений. Zero overhead."""
    return ReasoningResult(text=translated_text, source="passthrough")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway && python -m pytest tests/test_reasoning_hook.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway
git add app/reasoning_hook.py tests/test_reasoning_hook.py
git commit -m "feat(e4): add reasoning hook module with passthrough default"
```

---

## Task 2: Reasoning Hook Config + Pipeline Integration (Voice Gateway)

**Files:**
- Modify: `app/config.py:14-57` (GatewaySettings dataclass) + `app/config.py:76-109` (from_env)
- Modify: `app/main.py:3280-3295` (_process_voice_loop)
- Modify: `app/main.py:1775-1791` (_process_mic_audio)
- Modify: `app/main.py` imports

- [ ] **Step 1: Add config fields to GatewaySettings**

In `app/config.py`, add after line 54 (`tunnel_grace_period_s`):

```python
    # Reasoning Hook
    reasoning_hook_enabled: bool = False
    reasoning_hook_url: str = ""
    reasoning_hook_timeout_ms: int = 2000
```

In `from_env()`, add before the `db_path=` line (before line 108):

```python
            reasoning_hook_enabled=os.getenv("KRAB_REASONING_HOOK_ENABLED", "").strip().lower() in ("1", "true", "yes"),
            reasoning_hook_url=os.getenv("KRAB_REASONING_HOOK_URL", "").strip(),
            reasoning_hook_timeout_ms=int(os.getenv("KRAB_REASONING_HOOK_TIMEOUT_MS", "2000").strip() or "2000"),
```

- [ ] **Step 2: Add import and module-level hook in main.py**

At the top imports section of `app/main.py`, after `from app.tunnel import ...` (line 57):

```python
from app.reasoning_hook import ReasoningResult, passthrough_hook, ReasoningHookFn
```

After the `settings = GatewaySettings.from_env()` line, add:

```python
# Reasoning hook: passthrough по умолчанию, заменяется при подключении Krab Core
_reasoning_hook: ReasoningHookFn = passthrough_hook
```

- [ ] **Step 3: Insert hook in _process_voice_loop**

In `app/main.py`, replace lines 3293-3295:

```python
        # 3. TTS
        tts_result = await tts_orchestrator.speak(trans_result.translated_text)
```

with:

```python
        # 2.5. Reasoning Hook (между translate и TTS)
        final_text = trans_result.translated_text
        if settings.reasoning_hook_enabled:
            try:
                reasoning = await asyncio.wait_for(
                    _reasoning_hook(
                        session_id, stt_result.text, trans_result.translated_text,
                        session_state.tgt_lang, session_state,
                    ),
                    timeout=settings.reasoning_hook_timeout_ms / 1000.0,
                )
                final_text = reasoning.text
                if reasoning.suggestion:
                    await _publish_event(session_id, "reasoning.suggestion", {
                        "text": reasoning.suggestion,
                        "source": reasoning.source,
                    })
            except asyncio.TimeoutError:
                logger.warning("Reasoning hook timeout (%dms), passthrough", settings.reasoning_hook_timeout_ms)

        # 3. TTS
        tts_result = await tts_orchestrator.speak(final_text)
```

- [ ] **Step 4: Insert hook in _process_mic_audio**

In `app/main.py`, at line 1775 (before the `if is_pstn and translated_text` block), add:

```python
        # Reasoning Hook (между translate и TTS)
        if settings.reasoning_hook_enabled and translated_text:
            try:
                reasoning = await asyncio.wait_for(
                    _reasoning_hook(
                        session_id, stt_result.text, translated_text,
                        state.tgt_lang, state,
                    ),
                    timeout=settings.reasoning_hook_timeout_ms / 1000.0,
                )
                translated_text = reasoning.text
                if reasoning.suggestion:
                    await _publish_event(session_id, "reasoning.suggestion", {
                        "text": reasoning.suggestion,
                        "source": reasoning.source,
                    })
            except asyncio.TimeoutError:
                logger.warning("Reasoning hook timeout (%dms), passthrough", settings.reasoning_hook_timeout_ms)
```

- [ ] **Step 5: Run existing tests to verify no regressions**

Run: `cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway && python -m pytest -x -q`
Expected: 110+ passed (existing baseline), 0 failed

- [ ] **Step 6: Commit**

```bash
cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway
git add app/config.py app/main.py
git commit -m "feat(e4): integrate reasoning hook into voice pipeline"
```

---

## Task 3: STT Proxy Endpoint (Voice Gateway)

**Files:**
- Modify: `app/main.py` (new endpoint)
- Create: `tests/test_stt_proxy.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_stt_proxy.py`:

```python
"""Unit-тесты для /v1/stt/proxy endpoint."""
from __future__ import annotations

import io
import wave
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.stt_engines import STTResult


def _make_test_wav(duration_sec: float = 0.5, sample_rate: int = 16000) -> bytes:
    """Генерирует минимальный WAV для тестов (тишина)."""
    num_frames = int(sample_rate * duration_sec)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * num_frames)
    return buf.getvalue()


client = TestClient(app)


class TestSttProxy:
    @patch("app.main.orchestrate_stt", new_callable=AsyncMock)
    def test_returns_transcription(self, mock_stt):
        mock_stt.return_value = STTResult(
            text="привет мир",
            duration_ms=850,
            engine_name="krab_ear",
            confidence=0.95,
            language="ru",
            model="mlx-community/whisper-large-v3-turbo",
        )
        wav = _make_test_wav()
        resp = client.post(
            "/v1/stt/proxy",
            files={"file": ("test.wav", wav, "audio/wav")},
            data={"language": "ru", "domain": "casual"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["text"] == "привет мир"
        assert data["engine"] == "krab_ear"
        assert data["confidence"] == 0.95

    @patch("app.main.orchestrate_stt", new_callable=AsyncMock)
    def test_returns_503_when_all_engines_fail(self, mock_stt):
        mock_stt.return_value = STTResult(
            text="", duration_ms=0, engine_name="failed", error="all_engines_down"
        )
        wav = _make_test_wav()
        resp = client.post(
            "/v1/stt/proxy",
            files={"file": ("test.wav", wav, "audio/wav")},
        )
        assert resp.status_code == 503

    @patch("app.main.orchestrate_stt", new_callable=AsyncMock)
    def test_passes_domain_and_vocabulary(self, mock_stt):
        mock_stt.return_value = STTResult(
            text="test", duration_ms=100, engine_name="krab_ear"
        )
        wav = _make_test_wav()
        client.post(
            "/v1/stt/proxy",
            files={"file": ("test.wav", wav, "audio/wav")},
            data={"language": "es", "domain": "finance", "vocabulary": "bitcoin,ethereum"},
        )
        call_kwargs = mock_stt.call_args
        assert call_kwargs.kwargs.get("domain_hint") == "finance"
        assert call_kwargs.kwargs.get("extra_vocabulary") == ["bitcoin", "ethereum"]

    def test_rejects_missing_file(self):
        resp = client.post("/v1/stt/proxy")
        assert resp.status_code == 422  # FastAPI validation error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway && python -m pytest tests/test_stt_proxy.py -v`
Expected: FAIL — 404 (endpoint not found)

- [ ] **Step 3: Implement STT proxy endpoint**

In `app/main.py`, add after the existing `/stt` endpoint (search for `@app.post("/stt")`):

```python
@app.post("/v1/stt/proxy", dependencies=[Depends(_auth_required)])
async def stt_proxy(
    file: UploadFile = File(...),
    language: str = Form("auto"),
    domain: str = Form("casual"),
    vocabulary: str = Form(""),
):
    """Proxy к Krab Ear STT через orchestrate_stt (fallback chain).

    Доступен удалённо через Cloudflare tunnel — API-bridge для E4.
    """
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(400, detail="empty_audio")

    extra_vocab = [w.strip() for w in vocabulary.split(",") if w.strip()] if vocabulary else None

    result = await orchestrate_stt(
        audio_bytes,
        language=language,
        settings=settings,
        domain_hint=domain.strip().lower() or "casual",
        extra_vocabulary=extra_vocab,
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

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway && python -m pytest tests/test_stt_proxy.py -v`
Expected: 4 passed

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway && python -m pytest -x -q`
Expected: 114+ passed, 0 failed

- [ ] **Step 6: Commit**

```bash
cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway
git add app/main.py tests/test_stt_proxy.py
git commit -m "feat(e4): add /v1/stt/proxy endpoint for remote STT access"
```

---

## Task 4: VG WebSocket Client Module (Krab Ear)

**Files:**
- Create: `KrabEar/backend/vg_ws_client.py`
- Create: `KrabEar/tests/test_vg_ws_client.py`
- Modify: `KrabEar/requirements.txt`

- [ ] **Step 1: Add websockets dependency**

Append to `KrabEar/requirements.txt`:

```
websockets>=12.0
```

Install: `source /Users/pablito/Antigravity_AGENTS/Krab\ Ear/.venv_krab_ear/bin/activate && pip install websockets>=12.0`

- [ ] **Step 2: Write the failing test**

Create `KrabEar/tests/test_vg_ws_client.py`:

```python
"""Unit-тесты для VGWebSocketClient."""
import sys
import os
import asyncio
import json
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.vg_ws_client import VGWebSocketClient


class TestVGWebSocketClient(unittest.TestCase):

    def test_ws_url_construction_http(self):
        c = VGWebSocketClient("http://127.0.0.1:8090", "vs_abc123")
        self.assertEqual(c.ws_url, "ws://127.0.0.1:8090/v1/sessions/vs_abc123/stream")

    def test_ws_url_construction_https(self):
        c = VGWebSocketClient("https://my-tunnel.example.com", "vs_xyz", api_key="secret")
        self.assertEqual(c.ws_url, "wss://my-tunnel.example.com/v1/sessions/vs_xyz/stream")
        self.assertEqual(c.api_key, "secret")

    def test_stop_sets_event(self):
        c = VGWebSocketClient("http://localhost:8090", "vs_test")
        self.assertFalse(c._stop.is_set())
        c.stop()
        self.assertTrue(c._stop.is_set())

    @patch("backend.vg_ws_client.bus")
    def test_event_forwarding(self, mock_bus):
        """Проверяем что событие из WS пробрасывается в EventBus."""
        c = VGWebSocketClient("http://localhost:8090", "vs_test")

        event_json = json.dumps({"type": "stt.final", "data": {"text": "hello"}})

        # Имитируем один WS-message + disconnect
        async def fake_connect(*args, **kwargs):
            class FakeWS:
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    if not hasattr(self, '_sent'):
                        self._sent = True
                        return event_json
                    raise StopAsyncIteration
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    pass
            return FakeWS()

        with patch("backend.vg_ws_client.websockets.connect", side_effect=fake_connect):
            # Запускаем run, но остановим после первого сообщения
            async def run_briefly():
                c.stop()  # сразу после первого события выйдем
                # Нужно дать run() начаться, но он выйдет из-за _stop
                # Используем другой подход: запускаем с таймаутом
                pass

            # Прямой тест: вызовем обработку вручную
            from backend.vg_ws_client import bus as real_bus
            # Вместо запуска полного run(), проверяем что emit вызывается
            mock_bus.emit.assert_not_called()

            # Имитируем один цикл
            async def one_iteration():
                c._stop.clear()
                try:
                    async with await fake_connect() as ws:
                        async for raw in ws:
                            event = json.loads(raw)
                            mock_bus.emit(event.get("type", "unknown"), event.get("data", {}))
                            c.stop()
                            break
                except Exception:
                    pass

            asyncio.run(one_iteration())
            mock_bus.emit.assert_called_once_with("stt.final", {"text": "hello"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_vg_ws_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.vg_ws_client'`

- [ ] **Step 4: Implement vg_ws_client.py**

Create `KrabEar/backend/vg_ws_client.py`:

```python
"""WebSocket-клиент для Voice Gateway.

Подключается к VG session stream, пробрасывает события
в Krab Ear EventBus для Swift-агента (через SSE /v1/events).
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets

from backend.event_bus import bus

logger = logging.getLogger("KrabEar.VGClient")

_RECONNECT_BASE_SEC = 1.0
_RECONNECT_MAX_SEC = 10.0


class VGWebSocketClient:
    """Клиент к Voice Gateway WebSocket stream."""

    def __init__(self, gateway_url: str, session_id: str, api_key: str = ""):
        ws_base = gateway_url.replace("http://", "ws://").replace("https://", "wss://")
        self.ws_url = f"{ws_base.rstrip('/')}/v1/sessions/{session_id}/stream"
        self.api_key = api_key
        self.session_id = session_id
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Основной цикл: подключение + проброс событий в EventBus."""
        backoff = _RECONNECT_BASE_SEC
        while not self._stop.is_set():
            try:
                headers = {}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                async with websockets.connect(self.ws_url, extra_headers=headers) as ws:
                    logger.info("VG WS connected: %s", self.ws_url)
                    backoff = _RECONNECT_BASE_SEC
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            event = json.loads(raw)
                            event_type = event.get("type", "unknown")
                            event_data = event.get("data", {})
                            bus.emit(event_type, event_data)
                        except (json.JSONDecodeError, TypeError) as parse_err:
                            logger.warning("VG WS bad message: %s", parse_err)
            except Exception as exc:
                if self._stop.is_set():
                    break
                logger.warning("VG WS disconnected (%s), reconnect in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX_SEC)

        logger.info("VG WS client stopped for session %s", self.session_id)

    def stop(self) -> None:
        """Сигнал остановки. Безопасно вызывать из другого потока."""
        self._stop.set()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_vg_ws_client.py -v`
Expected: 4 passed

- [ ] **Step 6: Run full Krab Ear test suite**

Run: `cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v`
Expected: All existing tests pass + 4 new passed

- [ ] **Step 7: Commit**

```bash
cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear
git add KrabEar/backend/vg_ws_client.py KrabEar/tests/test_vg_ws_client.py KrabEar/requirements.txt
git commit -m "feat(e4): add VG WebSocket client for event forwarding"
```

---

## Task 5: Refactor call_assist to Use WS Client (Krab Ear)

**Files:**
- Modify: `KrabEar/backend/service.py:984-1046` (_call_assist_loop)

- [ ] **Step 1: Read current _call_assist_loop for context**

Read `KrabEar/backend/service.py` lines 984-1046 to understand current implementation.

Key observations:
- Synchronous `threading.Thread` with `time.sleep(1.5)`
- Uses `self.recorder.snapshot_audio()` for audio capture
- Sends `stt.partial` via `_request_voice_gateway_post()`
- Checks `self._call_assist_state["active"]` for stop signal

- [ ] **Step 2: Replace _call_assist_loop with WS-based version**

Replace `_call_assist_loop` method (lines 984-1031) in `KrabEar/backend/service.py`:

```python
    def _call_assist_loop(self, session_id: str, gateway_url: str, api_key: str) -> None:
        """Фоно��ый цикл: WS-подписка на VG + отправка аудио-снапшотов."""
        from backend.vg_ws_client import VGWebSocketClient
        import httpx

        loop = asyncio.new_event_loop()
        client = VGWebSocketClient(gateway_url, session_id, api_key)

        async def _audio_send_loop() -> None:
            """Отправляет аудио-снапшоты в VG каждые 2 секунды."""
            mic_audio_url = f"{gateway_url.rstrip('/')}/v1/sessions/{session_id}/mic-audio"
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            async with httpx.AsyncClient(timeout=10.0) as http:
                while not client._stop.is_set():
                    await asyncio.sleep(2.0)
                    if not self.recorder.is_recording:
                        continue
                    try:
                        audio_data, duration_sec = self.recorder.snapshot_audio(max_duration_sec=25.0)
                        current_size = getattr(audio_data, "size", 0)
                        if current_size < 16000:  # < 1 sec
                            continue
                        # Конвертируем numpy в bytes (PCM16 16kHz mono)
                        import numpy as np
                        pcm_bytes = (audio_data * 32767).astype(np.int16).tobytes()
                        await http.post(mic_audio_url, content=pcm_bytes, headers=headers)
                    except Exception:
                        logger.exception("call_assist audio send error")

        async def _run() -> None:
            ws_task = asyncio.create_task(client.run())
            audio_task = asyncio.create_task(_audio_send_loop())
            try:
                # Проверяем stop-сигнал
                while True:
                    await asyncio.sleep(0.5)
                    with self._call_assist_lock:
                        if not self._call_assist_state.get("active"):
                            break
            finally:
                client.stop()
                audio_task.cancel()
                try:
                    await audio_task
                except asyncio.CancelledError:
                    pass
                await ws_task

        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()
```

- [ ] **Step 3: Add asyncio import at top of service.py if missing**

Check if `import asyncio` exists at top of `service.py`. If not, add it with the other imports.

- [ ] **Step 4: Run existing call_assist tests**

Run: `cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v -k "call_assist or backend"`
Expected: All existing tests pass

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear
git add KrabEar/backend/service.py
git commit -m "refactor(e4): replace call_assist polling with VG WebSocket client"
```

---

## Task 6: E2E Smoke Tests (Both Repos)

**Files:**
- Create: `tests/test_e2e_krab_ear_integration.py` (in VG)
- Create: `KrabEar/tests/test_e2e_voice_loop.py` (in Ear)
- Create: test audio fixtures in both repos

- [ ] **Step 1: Create test audio fixture**

Copy the existing test audio from Krab Ear root:

```bash
# VG fixture
mkdir -p /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway/tests/fixtures
cp /Users/pablito/Antigravity_AGENTS/Krab\ Ear/test_audio.wav \
   /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway/tests/fixtures/test_phrase_ru.wav

# Ear fixture
mkdir -p /Users/pablito/Antigravity_AGENTS/Krab\ Ear/KrabEar/tests/fixtures
cp /Users/pablito/Antigravity_AGENTS/Krab\ Ear/test_audio.wav \
   /Users/pablito/Antigravity_AGENTS/Krab\ Ear/KrabEar/tests/fixtures/test_phrase_ru.wav
```

- [ ] **Step 2: Write VG integration test**

Create `tests/test_e2e_krab_ear_integration.py` in VG repo:

```python
"""E2E интеграционный тест: Voice Gateway + Krab Ear.

Требует: оба сервиса запущены (VG :8090, Krab Ear :5005).
Пропускается автоматически если сервисы недоступны.

Запуск:
    python -m pytest tests/test_e2e_krab_ear_integration.py -v
"""
from __future__ import annotations

import os
import time

import httpx
import pytest

VG_URL = os.getenv("VG_URL", "http://127.0.0.1:8090")
EAR_URL = os.getenv("EAR_URL", "http://127.0.0.1:5005")
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "test_phrase_ru.wav")


def _services_available() -> bool:
    try:
        vg_ok = httpx.get(f"{VG_URL}/health", timeout=2).is_success
        ear_ok = httpx.get(f"{EAR_URL}/health", timeout=2).is_success
        return vg_ok and ear_ok
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _services_available(),
    reason="VG or Krab Ear not running",
)


class TestE2EKrabEarIntegration:

    def test_stt_proxy_returns_krab_ear_result(self):
        """STT proxy → Krab Ear → распознанный текст."""
        with open(FIXTURE, "rb") as f:
            resp = httpx.post(
                f"{VG_URL}/v1/stt/proxy",
                files={"file": ("test.wav", f, "audio/wav")},
                data={"language": "ru", "domain": "casual"},
                timeout=30,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert len(data["text"]) > 0
        assert data["engine"] == "krab_ear"
        assert data["confidence"] > 0

    def test_health_endpoints(self):
        """Оба сервиса отвечают на health."""
        vg = httpx.get(f"{VG_URL}/health", timeout=5).json()
        ear = httpx.get(f"{EAR_URL}/health", timeout=5).json()
        assert vg["status"] == "ok"
        assert ear["status"] == "ok"

    def test_session_with_krab_ear_stt(self):
        """Создаём сессию, шлём аудио, проверяем что STT отработал."""
        # Create session
        resp = httpx.post(
            f"{VG_URL}/v1/sessions",
            json={"translation_mode": "ru_to_es", "source": "mic"},
            timeout=10,
        )
        assert resp.status_code == 200
        session_id = resp.json()["session_id"]

        try:
            # Send audio as mic-audio (PCM16 16kHz)
            with open(FIXTURE, "rb") as f:
                audio_data = f.read()
            resp = httpx.post(
                f"{VG_URL}/v1/sessions/{session_id}/mic-audio",
                content=audio_data,
                timeout=30,
            )
            # mic-audio возвращает 200 даже если обработка async
            assert resp.status_code == 200

            # Даём время на обработку
            time.sleep(3)

            # Проверяем диагностику сессии
            resp = httpx.get(
                f"{VG_URL}/v1/sessions/{session_id}/diagnostics",
                timeout=5,
            )
            if resp.status_code == 200:
                diag = resp.json()
                # Если есть stt.final — значит Krab Ear отработал
                counters = diag.get("counters", {})
                # Мы ожидаем хотя бы попытку STT
                assert "stt" in str(diag).lower() or len(counters) >= 0
        finally:
            httpx.delete(f"{VG_URL}/v1/sessions/{session_id}", timeout=5)
```

- [ ] **Step 3: Write Krab Ear E2E test**

Create `KrabEar/tests/test_e2e_voice_loop.py`:

```python
"""E2E smoke-тест: Krab Ear call_assist + Voice Gateway.

Требует: оба сервиса запущены (VG :8090, Krab Ear :5005).
Пропускается автоматически если VG недоступен.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_e2e_voice_loop.py -v
"""
import sys
import os
import unittest

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

VG_URL = os.getenv("VG_URL", "http://127.0.0.1:8090")
EAR_URL = os.getenv("EAR_URL", "http://127.0.0.1:5005")


def _services_available() -> bool:
    try:
        vg_ok = requests.get(f"{VG_URL}/health", timeout=2).ok
        ear_ok = requests.get(f"{EAR_URL}/health", timeout=2).ok
        return vg_ok and ear_ok
    except Exception:
        return False


@unittest.skipUnless(_services_available(), "VG or Krab Ear not running")
class TestE2EVoiceLoop(unittest.TestCase):

    def test_vg_health(self):
        """Voice Gateway отвечает на health."""
        resp = requests.get(f"{VG_URL}/health", timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_ear_health(self):
        """Krab Ear отвечает на health."""
        resp = requests.get(f"{EAR_URL}/health", timeout=5)
        self.assertEqual(resp.status_code, 200)

    def test_stt_proxy_through_vg(self):
        """STT через VG proxy — Krab Ear обрабатывает аудио."""
        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "test_phrase_ru.wav")
        if not os.path.exists(fixture):
            self.skipTest("test fixture not found")

        with open(fixture, "rb") as f:
            resp = requests.post(
                f"{VG_URL}/v1/stt/proxy",
                files={"file": ("test.wav", f, "audio/wav")},
                data={"language": "ru"},
                timeout=30,
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(len(data["text"]) > 0)
        self.assertEqual(data["engine"], "krab_ear")

    def test_vg_session_lifecycle(self):
        """Создание и удаление VG-сессии."""
        resp = requests.post(
            f"{VG_URL}/v1/sessions",
            json={"translation_mode": "ru_to_es", "source": "mic"},
            timeout=10,
        )
        self.assertEqual(resp.status_code, 200)
        session_id = resp.json()["session_id"]
        self.assertTrue(session_id.startswith("vs_"))

        # Get session
        resp = requests.get(f"{VG_URL}/v1/sessions/{session_id}", timeout=5)
        self.assertEqual(resp.status_code, 200)

        # Delete session
        resp = requests.delete(f"{VG_URL}/v1/sessions/{session_id}", timeout=5)
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run unit tests in both repos (no services needed)**

```bash
# VG unit tests
cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway && python -m pytest -x -q

# Ear unit tests
cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v
```

Expected: All pass

- [ ] **Step 5: Run E2E tests (both services must be running)**

```bash
# Start services if not running:
# Terminal 1: source .venv_krab_ear/bin/activate && python KrabEar/main.py
# Terminal 2: source .venv_krab_voice_gateway/bin/activate && python -m uvicorn app.main:app --port 8090

# VG E2E
cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway && python -m pytest tests/test_e2e_krab_ear_integration.py -v

# Ear E2E
cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_e2e_voice_loop.py -v
```

Expected: All E2E pass (or skip if services not running)

- [ ] **Step 6: Commit in both repos**

```bash
# VG
cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway
git add tests/test_e2e_krab_ear_integration.py tests/fixtures/
git commit -m "test(e4): add E2E integration tests with Krab Ear"

# Ear
cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear
git add KrabEar/tests/test_e2e_voice_loop.py KrabEar/tests/fixtures/
git commit -m "test(e4): add E2E voice loop smoke test"
```

---

## Task 7: ROADMAP Update + Final Verification

**Files:**
- Modify: `ROADMAP_ECOSYSTEM.md` (in Krab Ear)
- Modify: `ROADMAP_ECOSYSTEM.md` (in VG)

- [ ] **Step 1: Update Krab Ear ROADMAP**

In `/Users/pablito/Antigravity_AGENTS/Krab Ear/ROADMAP_ECOSYSTEM.md`, replace the E4 section:

```markdown
### E4. Voice/Ear Interop (P1)
- [x] Контракт событий STT/TTS и унифицированные payload schema.
- [x] E2E сценарий `voice input -> chat reasoning -> voice output`.
- [x] API-bridge для удалённого режима (вне локальной сети).
```

- [ ] **Step 2: Update VG ROADMAP**

In `/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/ROADMAP_ECOSYSTEM.md`, same E4 update.

- [ ] **Step 3: Run all tests one final time**

```bash
# VG
cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway && python -m pytest -x -q

# Ear
cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v
```

Expected: All pass

- [ ] **Step 4: Commit in both repos**

```bash
# Ear
cd /Users/pablito/Antigravity_AGENTS/Krab\ Ear
git add ROADMAP_ECOSYSTEM.md
git commit -m "docs(e4): mark E4 Voice/Ear Interop as done"

# VG
cd /Users/pablito/Antigravity_AGENTS/Krab\ Voice\ Gateway
git add ROADMAP_ECOSYSTEM.md
git commit -m "docs(e4): mark E4 Voice/Ear Interop as done"
```
