"""Явный auto: REST → настоящий Transcriber/AudioEngine → граница ML API.

Подменяем только тяжёлый инференс, нормализацию контейнера и хранилище.
Разрешение языка, маршрутизация, fallback и возврат результата — настоящий код.
"""

from __future__ import annotations

import importlib
import io
import wave
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(np.full(16000, 1000, dtype=np.int16).tobytes())
    return buffer.getvalue()


@pytest.fixture
def wiring(tmp_path):
    from core import config, engine as engine_module
    from backend.transcriber import Transcriber

    with ExitStack() as stack:
        # Никогда не читаем боевые settings/history и не создаём ML-процессы.
        values = {
            "DATA_DIR": tmp_path,
            "TRANSCRIBE_LANGUAGE": "ru",
            "PIPELINE_V2": False,
            "STT_STREAMING_ENABLED": False,
            "DIARIZED_TRANSCRIPTION_ENABLED": False,
            "STT_MULTIPASS_ENABLED": False,
            "STT_USE_RU_FINETUNE": True,
            "STT_GIGAAM_ENABLED": True,
            "PARAKEET_ENABLED": True,
            "SENSEVOICE_ENABLED": False,
            "WHISPERX_ENABLED": False,
            "WHISPERX_WORD_TIMESTAMPS": False,
            "WHISPERX_DIARIZATION": False,
            "VOXTRAL_ENABLED": False,
            "NUMBER_NORMALIZATION_ENABLED": False,
            "DATETIME_NORMALIZATION_ENABLED": False,
            "NETWORK_MODE": "offline_strict",
        }
        for name, value in values.items():
            stack.enter_context(patch.object(config.settings, name, value))
        stack.enter_context(patch.object(config, "_SETTINGS_JSON_FILE", tmp_path / "settings.json"))
        store = MagicMock()
        store.load_settings.return_value = {}
        store.load_vocabulary.return_value = []
        with (
            patch("core.engine.AudioEngine"),
            patch("backend.state_store.StateStore", return_value=store),
            patch("backend.translator.Translator"),
            patch("backend.tts_service.TTSService"),
        ):
            rest = importlib.import_module("backend.rest_server")

        engine = engine_module.AudioEngine(skip_gigaam_warmup=True)
        stack.enter_context(patch.object(engine, "normalize_audio", return_value=True))
        # Намеренно допускаем router в цепочку: auto сам обязан исключать RU.
        engine._skip_gigaam = False
        router = MagicMock()
        engine._router = router
        gigaam = stack.enter_context(patch.object(engine, "_transcribe_gigaam"))
        parakeet = stack.enter_context(patch.object(engine, "_transcribe_parakeet"))
        worker = stack.enter_context(patch(
            "core.mlx_whisper_session.transcribe_via_mlx_worker",
            return_value={"text": "Hola, necesito una cita.", "language": "es", "segments": []},
        ))
        stack.enter_context(patch(
            "core.mlx_whisper_session.mlx_whisper_worker_enabled", return_value=True,
        ))
        stack.enter_context(patch.object(engine_module, "mlx_inter_process_lock", return_value=nullcontext()))
        deps = SimpleNamespace(engine=engine, transcriber=Transcriber(engine=engine), store=store, metrics=MagicMock())
        stack.enter_context(patch.dict(rest.app.config, {"TESTING": True, "REST_DEPS": deps}))
        stack.enter_context(patch.object(rest, "TEMP_DIR", tmp_path / "uploads"))
        stack.enter_context(patch.object(rest, "_load_settings_field", side_effect=lambda _key, default=None: default))
        stack.enter_context(patch.object(rest.limiter, "enabled", False))
        yield SimpleNamespace(
            client=rest.app.test_client(), engine=engine, worker=worker, store=store,
            router=router, gigaam=gigaam, parakeet=parakeet,
        )


def _post(wiring, language):
    data = {"file": (io.BytesIO(_wav()), "call.wav"), "diarize": "false", "persist_history": "false", "cleanup_profile": "off"}
    if language is not None:
        data["language"] = language
    return wiring.client.post("/v1/stt/transcribe", data=data, content_type="multipart/form-data")


@pytest.mark.parametrize("language, expected", [("auto", None), (" AUTO ", None), (None, "ru"), ("en", "en")])
def test_rest_reaches_whisper_api_with_request_language(wiring, language, expected):
    # Для omitted RU проверяем Whisper, отключив RU-специалистов отдельно.
    from core.config import settings
    with patch.object(settings, "STT_GIGAAM_ENABLED", False), patch.object(settings, "STT_USE_RU_FINETUNE", False):
        response = _post(wiring, language)
    assert response.status_code == 200, response.get_json()
    assert wiring.worker.call_count == 1
    assert wiring.worker.call_args.args[1]["language"] == expected
    assert response.get_json()["language"] == "es"  # именно ответ модели
    assert response.get_json()["text"] == "Hola, necesito una cita."
    wiring.store.add_history_item.assert_not_called()


def test_auto_does_not_admit_ru_or_en_specific_models(wiring):
    response = _post(wiring, "auto")
    assert response.status_code == 200, response.get_json()
    assert wiring.worker.call_count == 1
    assert wiring.worker.call_args.args[1]["language"] is None
    assert wiring.worker.call_args.kwargs["model_name"] == wiring.engine.current_model
    wiring.router.get_gigaam_adapter.assert_not_called()
    wiring.gigaam.assert_not_called()
    wiring.parakeet.assert_not_called()


def test_auto_survives_worker_argument_compatibility_retry(wiring):
    wiring.worker.side_effect = [TypeError("unsupported option"), {"text": "Hola", "language": "es"}]
    response = _post(wiring, "auto")
    assert response.status_code == 200, response.get_json()
    assert wiring.worker.call_count == 2
    assert all(call.args[1]["language"] is None for call in wiring.worker.call_args_list)
    assert response.get_json()["language"] == "es"


@pytest.mark.parametrize("detected_language", ["es", None])
def test_auto_fallback_whisperx_gets_none_and_returns_detected_language(wiring, detected_language):
    from core.config import settings
    import core.engine as engine_module
    wiring.worker.side_effect = OSError("model unavailable")
    whisperx = MagicMock()
    whisperx.transcribe.return_value = {"text": "Hola desde la reserva", "language": detected_language, "segments": []}
    with (
        patch.object(settings, "WHISPERX_ENABLED", True),
        patch.object(wiring.engine, "_load_whisperx_model", return_value=whisperx),
        patch.object(engine_module, "_whisperx", SimpleNamespace(load_audio=lambda _path: np.ones(16000))),
    ):
        response = _post(wiring, "auto")
    assert response.status_code == 200, response.get_json()
    whisperx.transcribe.assert_called_once()
    assert whisperx.transcribe.call_args.kwargs["language"] is None
    assert response.get_json()["engine"] == "whisperx"
    assert response.get_json()["language"] == detected_language


def test_auto_fallback_sensevoice_keeps_its_native_auto(wiring):
    from core.config import settings
    wiring.worker.side_effect = OSError("model unavailable")
    sensevoice = MagicMock()
    sensevoice.generate.return_value = [{"text": "<|en|><|NEUTRAL|><|Speech|>Hello from fallback"}]
    with patch.object(settings, "SENSEVOICE_ENABLED", True), patch.object(wiring.engine, "_load_sensevoice_model", return_value=sensevoice):
        response = _post(wiring, "auto")
    assert response.status_code == 200, response.get_json()
    assert sensevoice.generate.call_args.kwargs["language"] == "auto"
    assert response.get_json()["engine"] == "sensevoice"
    assert response.get_json()["language"] == "en"
