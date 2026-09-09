"""Обычный REST STT/TTS privacy-гейт обязан быть fail-CLOSED (2026-09).

ЧТО НАЙДЕНО
-----------
`_privacy_gate` для обычного ``/v1/stt/transcribe`` и ``/v1/tts/synthesize``
(не ``request_profile=voice_gateway_call``) читает флаг через

    _load_settings_field("privacy_mode_enabled", False)
        try: return store.load_settings().get(key, default)
        except Exception: return default   # ← для privacy это fail-OPEN

Call-profile ветка уже fail-closed (``_call_profile_privacy_enabled``:
``call_privacy_mode() is not False``, except → True) — её не трогаем.

Persist обычного STT повторно зовёт тот же fail-open getter: при сбое
чтения после прошедшего гейта транскрипт ещё и пишется в history.

WS ``/v1/stream`` читает ``load_settings().get(..., False)`` без fail-closed
обёртки — тот же helper.

Эталон: ``RecordingCoreService._privacy_mode_enabled`` и call-profile REST.
"""
from __future__ import annotations

import io
import json
import wave
from unittest.mock import MagicMock

import pytest

from test_rest_vg_contract_M1 import _deps_with
import backend.rest_server as rs


def _raises_oserror(*_a, **_k):
    # Именно OSError: StateStore._lock() документирует ENOSPC/EMFILE/EACCES
    # в фазе захвата как реалистичные; generic except глотает их в default=False.
    raise OSError(24, "Too many open files")


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x10\0" * 800)
    return output.getvalue()


@pytest.fixture
def rest(monkeypatch, tmp_path):
    deps = _deps_with()
    deps.store.is_idempotent.return_value = False
    deps.store.add_history_item.return_value = MagicMock(id="hist-rest-privacy")
    deps.transcriber.transcribe.return_value = {
        "text": "секретный транскрипт",
        "raw_text": "секретный транскрипт",
        "confidence": 0.9,
        "duration_ms": 120,
        "engine": "mlx-whisper",
        "model": "whisper-small",
        "language": "ru",
        "segments": [],
        "diarization": {},
    }
    monkeypatch.setattr(rs, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(rs.settings, "REST_API_AUTH_ENABLED", False)
    monkeypatch.setattr(rs.settings, "REST_API_KEY", "")
    app = rs.create_app(deps)
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    return app, deps


def _post_stt(client, **extra):
    data = {
        "file": (io.BytesIO(_wav_bytes()), "test.wav"),
        **extra,
    }
    return client.post("/v1/stt/transcribe", data=data)


def _privacy_403(resp):
    assert resp.status_code == 403, f"ожидали 403 privacy, получили {resp.status_code}: {resp.get_json()}"
    body = resp.get_json() or {}
    assert body.get("skipped") == "privacy_mode"
    assert not body.get("ok", True)
    assert not body.get("text")


def test_ordinary_stt_fails_closed_when_settings_unreadable(rest):
    """IO/lock ошибка чтения settings → 403, без текста, без persist, без STT."""
    app, deps = rest
    deps.store.load_settings.side_effect = _raises_oserror
    client = app.test_client()

    resp = _post_stt(client)

    _privacy_403(resp)
    deps.transcriber.transcribe.assert_not_called()
    deps.store.add_history_item.assert_not_called()


def test_ordinary_tts_fails_closed_when_settings_unreadable(rest):
    """Тот же гейт на /v1/tts/synthesize: сбой чтения → 403, синтез не зовётся."""
    app, deps = rest
    deps.store.load_settings.side_effect = _raises_oserror
    client = app.test_client()

    resp = client.post("/v1/tts/synthesize", json={"text": "привет"})

    _privacy_403(resp)
    deps.tts_service.handle_synthesize_speech.assert_not_called()


def test_ordinary_stt_missing_key_is_privacy_off(rest):
    """Отсутствие ключа после успешного чтения ≠ сбой: privacy OFF, не вечный 403."""
    app, deps = rest
    deps.store.load_settings.return_value = {}
    client = app.test_client()

    resp = _post_stt(client)

    assert resp.status_code != 403, f"missing key не должен давать 403, получили {resp.get_json()}"
    deps.transcriber.transcribe.assert_called()


def test_ordinary_tts_missing_key_is_privacy_off(rest):
    app, deps = rest
    deps.store.load_settings.return_value = {}
    client = app.test_client()

    resp = client.post("/v1/tts/synthesize", json={"text": "привет"})

    assert resp.status_code == 200
    deps.tts_service.handle_synthesize_speech.assert_called_once()


def test_ordinary_stt_does_not_persist_when_later_privacy_read_fails(rest):
    """Persist обычного пути тоже fail-closed: сбой после гейта не пишет history."""
    app, deps = rest
    reads = {"n": 0}

    def load_settings(*_a, **_k):
        reads["n"] += 1
        if reads["n"] == 1:
            return {"privacy_mode_enabled": False}
        raise OSError(24, "Too many open files")

    deps.store.load_settings.side_effect = load_settings
    client = app.test_client()

    resp = _post_stt(client, persist_history="true")

    assert resp.status_code != 403 or not (resp.get_json() or {}).get("text")
    deps.store.add_history_item.assert_not_called()


def test_ws_stream_fails_closed_when_settings_unreadable(rest):
    """WS /v1/stream: тот же helper, IO-сбой → privacy error, без транскрипта."""
    app, deps = rest
    deps.store.load_settings.side_effect = _raises_oserror

    class MockWS:
        def __init__(self):
            self.sends = []
            self.closed = False

        def receive(self):
            return json.dumps({"type": "config", "mode": "transcribe"})

        def send(self, msg):
            self.sends.append(msg)

        def close(self, message=None):
            self.closed = True

    ws = MockWS()
    with app.test_request_context("/v1/stream"):
        rs._ws_stream_handler(ws)

    assert ws.sends, "при сбое чтения settings WS обязан закрыть privacy-ошибкой"
    payload = json.loads(ws.sends[0])
    assert payload.get("type") == "error"
    assert payload.get("code") == "privacy_mode_active"
    assert all("секрет" not in str(msg) for msg in ws.sends)
