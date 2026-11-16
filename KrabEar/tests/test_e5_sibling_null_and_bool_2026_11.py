"""Тот же класс дефектов, что в E5 п.1/п.4, но в сиблингах (находка agy-ревью 2026-11-16, воспроизведена).

- `"false"` в булевых настройках set_settings читалось как True (`bool("false")` ДО валидатора);
- `null` в voice_gateway_api_key / smtp_host превращался в строку "None";
- `null` в api-ключах облачных провайдеров ронял `.get(k, "").strip()` вне try/except.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_service import SettingsService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


@pytest.fixture()
def svc(tmp_path):
    service = SettingsService(StateStore(tmp_path / "data"))
    service.handle_set_settings({"privacy_mode_enabled": False, "history_encryption_enabled": False})
    return service


def _saved(svc, payload):
    svc.handle_set_settings(payload)
    svc.invalidate_cache()
    return svc.cached_settings()


@pytest.mark.parametrize("key", [
    "auto_start_enabled", "show_dock_icon", "auto_paste", "play_start_sound", "realtime_preview_enabled",
    "translate_and_paste", "onboarding_completed", "audio_ducking_enabled",
])
def test_string_false_is_false_for_pre_validated_booleans(svc, key):
    assert _saved(svc, {key: "false"})[key] is False
    assert _saved(svc, {key: "true"})[key] is True


def test_non_finite_float_is_not_true_for_pre_validated_booleans(svc):
    assert _saved(svc, {"translate_and_paste": float("nan")})["translate_and_paste"] is False


@pytest.mark.parametrize("key", ["voice_gateway_api_key", "smtp_host"])
def test_null_clears_string_field_instead_of_becoming_none_literal(svc, key):
    assert _saved(svc, {key: None})[key] == ""


def test_cloud_stt_providers_survive_null_api_keys(monkeypatch):
    from backend import cloud_stt

    monkeypatch.setattr(cloud_stt, "_load_settings", lambda: {
        "openai_api_key": None, "deepgram_api_key": None, "assemblyai_api_key": None})
    for provider in (cloud_stt.OpenAISTTProvider(), cloud_stt.DeepgramSTTProvider(), cloud_stt.AssemblyAISTTProvider()):
        result = provider.transcribe(b"\x00\x00", 16000, "ru")
        assert result.get("error") == "no_api_key", type(provider).__name__


def test_cloud_rewriter_settings_survive_null_values(monkeypatch):
    from backend import cloud_rewriter

    source = Path(cloud_rewriter.__file__).read_text(encoding="utf-8")
    assert '.get("openai_api_key", "").strip()' not in source
    assert '.get("anthropic_api_key", "").strip()' not in source
    assert '.get("cloud_rewriter_custom_model", "").strip()' not in source
    assert '.get("cloud_rewriter_api_key", "").strip()' not in source
