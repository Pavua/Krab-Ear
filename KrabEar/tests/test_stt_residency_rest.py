"""Actual REST auth/readiness + owner snapshot, без живого сервера/моделей."""
import importlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core.engine import AudioEngine
from core.stt_router import STTRouter


@pytest.fixture
def rest(monkeypatch, tmp_path):
    pytest.importorskip("flask_smorest")
    store = Mock(data_dir=tmp_path)
    store.load_settings.return_value = {}
    store.load_vocabulary.return_value = []
    # Патчим только конструкторы import-time owners и secret/settings OS leaf.
    # Под HTTP deps передаётся actual AudioEngine без __init__/warmup.
    with patch("core.engine.AudioEngine", return_value=Mock(quality_profile="balanced")), \
         patch("backend.state_store.StateStore", return_value=store), \
         patch("backend.transcriber.Transcriber", return_value=Mock()), \
         patch("backend.translator.Translator", return_value=Mock()), \
         patch("backend.tts_service.TTSService", return_value=Mock()), \
         patch("core.config._SETTINGS_JSON_FILE", tmp_path / "absent.json"):
        rs = importlib.import_module("backend.rest_server")
    engine = AudioEngine.__new__(AudioEngine)
    engine.quality_profile = "balanced"
    engine.current_model = "mlx-community/whisper-large-v3-turbo"
    engine._router = STTRouter(SimpleNamespace(STT_GIGAAM_ENABLED=False))
    engine._router.get_gigaam_adapter = Mock(side_effect=AssertionError("lazy GigaAM lookup"))
    import core.mlx_whisper_session as whisper
    monkeypatch.setattr(whisper, "_session", None)
    monkeypatch.setattr(whisper, "get_mlx_whisper_session", Mock(side_effect=AssertionError("session creation")))
    monkeypatch.setenv("KRAB_EAR_MLX_WHISPER_WORKER", "1")
    monkeypatch.setattr(rs.settings, "REST_API_AUTH_ENABLED", False)
    monkeypatch.setattr(rs.settings, "REST_API_KEY", "test-residency-key")
    monkeypatch.setattr(rs, "_log_unauthorized", lambda _: None)
    monkeypatch.setattr(rs.BackendService, "_build_readiness_report_static", staticmethod(lambda: {"overall_ready": True, "components": {"stt": True}}))
    deps = rs.StaticDeps(engine, store, Mock(), Mock(), Mock(), Mock(), Mock(), Mock())
    app = rs.create_app(deps, {"TESTING": True, "RATELIMIT_ENABLED": False})
    return app.test_client(), engine


def test_rest_readiness_preserves_auth_before_any_owner_metadata(rest, monkeypatch):
    client, engine = rest
    monkeypatch.setattr(engine, "stt_residency", Mock(side_effect=AssertionError("metadata before auth")), raising=False)
    response = client.get("/v1/readiness")
    assert response.status_code == 401
    assert "stt_residency" not in response.get_json()


def test_authenticated_readiness_observes_rest_owner_without_loading(rest):
    client, engine = rest
    response = client.get("/v1/readiness", headers={"Authorization": "Bearer test-residency-key"})
    assert response.status_code == 200
    result = response.get_json()
    assert result["overall_ready"] is True
    assert result["components"] == {"stt": True}
    assert "stt_residency" in result, "Actual auth-gated route не возвращает owner-local snapshot"
    state = result["stt_residency"]
    assert state["owner"] == "rest"
    assert state["configured"]["model"] == "mlx-community/whisper-large-v3-turbo"
    assert state["whisper"]["state"] == "unloaded"
    assert engine._router._gigaam_adapter is None
