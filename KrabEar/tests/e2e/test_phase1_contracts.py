"""Phase 1 E2E smoke tests: three-tier contract compatibility.

Tests verify WebSocket handshake, audio chunk roundtrip, event schema,
and error handling without loading real ML models.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

logger = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, WebSocket
    from starlette.testclient import TestClient
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


@pytest.fixture
def mock_voice_gateway_app() -> FastAPI:
    """Minimal FastAPI app simulating Voice Gateway /v1/sessions/{id}/stream endpoint."""
    app = FastAPI()
    ACTIVE_SESSIONS = {}

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "Phase1-E2E"}

    @app.post("/v1/sessions")
    async def create_session(payload=None):
        """Mock session creation."""
        session_id = "test-session-" + str(int(time.time() * 1000))
        ACTIVE_SESSIONS[session_id] = {
            "state": "init",
            "events": [],
            "created_at": time.time(),
        }
        return {"session_id": session_id, "status": "created"}

    @app.websocket("/v1/sessions/{session_id}/stream")
    async def stream_handler(websocket: WebSocket, session_id: str):
        """Mock WebSocket conversation endpoint."""
        await websocket.accept()

        try:
            elapsed = time.time()
            engine_event = {
                "type": "engine.loaded",
                "engine": "moshi-mlx-mock",
                "elapsed_sec": 0.1,
                "timestamp": elapsed,
            }
            await websocket.send_json(engine_event)

            message_count = 0
            while True:
                data = await websocket.receive()

                if "bytes" in data:
                    message_count += 1

                    # Send final on 3rd chunk, partial before that
                    if message_count >= 3:
                        final_event = {
                            "type": "stt.final",
                            "text": "final mock transcription result",
                            "lang": "auto",
                            "confidence": 0.92,
                            "is_final": True,
                            "timestamp": time.time(),
                        }
                        await websocket.send_json(final_event)
                        break
                    else:
                        stt_event = {
                            "type": "stt.partial",
                            "text": f"mock transcription chunk {message_count}",
                            "lang": "auto",
                            "confidence": 0.85,
                            "is_final": False,
                            "timestamp": time.time(),
                        }
                        await websocket.send_json(stt_event)

                elif "text" in data:
                    msg_json = json.loads(data["text"])
                    if msg_json.get("type") == "control":
                        if msg_json.get("action") == "interrupt":
                            control_ack = {
                                "type": "control.ack",
                                "action": "interrupt",
                                "status": "acknowledged",
                            }
                            await websocket.send_json(control_ack)

        except Exception as e:
            logger.error(f"[mock-gateway] WS error: {e}")
        finally:
            await websocket.close()

    return app


@pytest.fixture
def mock_voice_gateway_client(mock_voice_gateway_app: FastAPI):
    """FastAPI TestClient for mock Voice Gateway."""
    return TestClient(mock_voice_gateway_app, raise_server_exceptions=False)


@pytest.fixture
def mock_brain_client():
    """Mock Krab-openclaw voice_routes client."""
    client = MagicMock()

    async def mock_send_message_stream(session_id: str, prompt: str):
        """Mock async generator for SSE token stream."""
        async def token_gen():
            tokens = [
                "Привет", " мир", "! Это", " тестовый",
                " ответ", " от", " мозга", "."
            ]
            for tok in tokens:
                await asyncio.sleep(0.01)
                yield {"type": "text", "token": tok, "session_id": session_id}
            yield {"type": "done", "session_id": session_id}

        return token_gen()

    client.send_message_stream = AsyncMock(side_effect=mock_send_message_stream)
    return client


@pytest.mark.skipif(not FASTAPI_AVAILABLE, reason="fastapi/starlette not installed")
class TestPhase1Contracts:
    """Phase 1 three-tier contract smoke tests."""

    def test_1_ws_connection_handshake(self, mock_voice_gateway_client: TestClient):
        """1. WS connection opens, engine.loaded event received."""
        resp = mock_voice_gateway_client.post("/v1/sessions")
        assert resp.status_code == 200
        session_data = resp.json()
        session_id = session_data["session_id"]

        with mock_voice_gateway_client.websocket_connect(
            f"/v1/sessions/{session_id}/stream"
        ) as ws:
            data = ws.receive_json()
            assert data["type"] == "engine.loaded"
            assert "elapsed_sec" in data

    def test_2_audio_chunk_roundtrip(self, mock_voice_gateway_client: TestClient):
        """2. Send 80ms PCM chunk, receive stt.partial event."""
        resp = mock_voice_gateway_client.post("/v1/sessions")
        session_id = resp.json()["session_id"]

        with mock_voice_gateway_client.websocket_connect(
            f"/v1/sessions/{session_id}/stream"
        ) as ws:
            ws.receive_json()
            mock_pcm = b"\x00" * 2560
            ws.send_bytes(mock_pcm)
            response = ws.receive_json()
            assert response["type"] == "stt.partial"
            assert "text" in response

    def test_3_stt_partial_event_schema(self, mock_voice_gateway_client: TestClient):
        """3. STT partial event contains required fields."""
        resp = mock_voice_gateway_client.post("/v1/sessions")
        session_id = resp.json()["session_id"]

        with mock_voice_gateway_client.websocket_connect(
            f"/v1/sessions/{session_id}/stream"
        ) as ws:
            ws.receive_json()
            ws.send_bytes(b"\x00" * 2560)
            event = ws.receive_json()

            required_fields = ["type", "text", "lang", "confidence", "is_final"]
            for field in required_fields:
                assert field in event
            assert 0 <= event["confidence"] <= 1

    def test_4_interrupt_control(self, mock_voice_gateway_client: TestClient):
        """4. Send control.interrupt, receive control.ack."""
        resp = mock_voice_gateway_client.post("/v1/sessions")
        session_id = resp.json()["session_id"]

        with mock_voice_gateway_client.websocket_connect(
            f"/v1/sessions/{session_id}/stream"
        ) as ws:
            ws.receive_json()
            control_msg = {"type": "control", "action": "interrupt"}
            ws.send_json(control_msg)
            ack = ws.receive_json()
            assert ack["type"] == "control.ack"
            assert ack["action"] == "interrupt"

    def test_5_stt_final_event_transition(self, mock_voice_gateway_client: TestClient):
        """5. After 3 audio chunks, stt.final event sent and connection closes."""
        resp = mock_voice_gateway_client.post("/v1/sessions")
        session_id = resp.json()["session_id"]

        with mock_voice_gateway_client.websocket_connect(
            f"/v1/sessions/{session_id}/stream"
        ) as ws:
            ws.receive_json()  # engine.loaded

            # Send 2 audio chunks, expecting partial responses
            for i in range(2):
                ws.send_bytes(b"\x00" * 2560)
                partial = ws.receive_json()
                assert partial["type"] == "stt.partial"
                assert partial["is_final"] is False

            # Send 3rd chunk - should get final event
            ws.send_bytes(b"\x00" * 2560)
            final = ws.receive_json()
            assert final["type"] == "stt.final"
            assert final["is_final"] is True

    def test_6_http_health_endpoint(self, mock_voice_gateway_client: TestClient):
        """6. GET /health returns OK status."""
        resp = mock_voice_gateway_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_7_session_creation(self, mock_voice_gateway_client: TestClient):
        """7. Create session, verify session_id in response."""
        resp = mock_voice_gateway_client.post("/v1/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert len(data["session_id"]) > 0

    def test_8_multiple_concurrent_sessions(self, mock_voice_gateway_client: TestClient):
        """8. Multiple WebSocket sessions isolated."""
        sessions = []
        for i in range(2):
            resp = mock_voice_gateway_client.post("/v1/sessions")
            sessions.append(resp.json()["session_id"])

        with mock_voice_gateway_client.websocket_connect(
            f"/v1/sessions/{sessions[0]}/stream"
        ) as ws0:
            with mock_voice_gateway_client.websocket_connect(
                f"/v1/sessions/{sessions[1]}/stream"
            ) as ws1:
                ev0 = ws0.receive_json()
                ev1 = ws1.receive_json()
                assert ev0["type"] == "engine.loaded"
                assert ev1["type"] == "engine.loaded"

    def test_9_binary_and_json_coexistence(self, mock_voice_gateway_client: TestClient):
        """9. WebSocket handles both binary (audio) and JSON (control) frames."""
        resp = mock_voice_gateway_client.post("/v1/sessions")
        session_id = resp.json()["session_id"]

        with mock_voice_gateway_client.websocket_connect(
            f"/v1/sessions/{session_id}/stream"
        ) as ws:
            ws.receive_json()
            ws.send_bytes(b"\x00" * 2560)
            stt_event = ws.receive_json()
            assert stt_event["type"] == "stt.partial"

            ws.send_json({"type": "control", "action": "interrupt"})
            ack = ws.receive_json()
            assert ack["type"] == "control.ack"

    @pytest.mark.asyncio
    async def test_10_brain_token_stream(self, mock_brain_client):
        """10. Mock brain send_message_stream yields tokens."""
        token_stream = await mock_brain_client.send_message_stream(
            session_id="test-session",
            prompt="Test prompt",
        )

        tokens = []
        async for msg in token_stream:
            tokens.append(msg)

        assert len(tokens) > 0
        text_tokens = [t for t in tokens if t.get("type") == "text"]
        done_tokens = [t for t in tokens if t.get("type") == "done"]
        assert len(text_tokens) > 0
        assert len(done_tokens) == 1
