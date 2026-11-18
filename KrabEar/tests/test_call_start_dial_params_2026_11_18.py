"""`call_start` передаёт шлюзу лимит длительности, голос и «говорить первым» из настроек (решение владельца 2026-11-18).

Было: панель звонков не посылала шлюзу `max_duration_sec`, `voice_mode`, `speak_first`, и звонок шёл
с умолчаниями шлюза без возможности их настроить. Умолчания настроек совпадают с умолчаниями шлюза
(300 с, голос-клон владельца, ждать собеседника), поэтому поведение не меняется, пока владелец
ничего не трогал. Голос-клон остаётся выбором по умолчанию: системный голос выдаёт робота.

Контракт:
 * `call_max_duration_sec` (30..3600), `call_voice_mode` (clone|system|gemini), `call_speak_first` (bool);
 * явные параметры `call_start` сильнее настроек;
 * мусор в настройках не уходит шлюзу (его `Literal`/int-валидация ответила бы 422 и сорвала звонок).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_session_service import CallSessionService  # noqa: E402
from backend.call_session_store import CallSessionStore  # noqa: E402
from backend.gateway_call_provider import GatewayCallProvider  # noqa: E402
from backend.settings_validator import SettingsValidator  # noqa: E402
from core.config import DEFAULT_SETTINGS  # noqa: E402

_GW = "vs_params_session"
_KEYS = ("max_duration_sec", "voice_mode", "speak_first")


class _Client:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def post(self, base, key, path, payload=None, timeout=None):
        self.payloads.append(dict(payload or {}))
        return {"ok": True, "payload": {"ok": True, "session_id": _GW, "call_sid": "CA1", "status": "dialing"}}

    def get(self, base, key, path, timeout=None):
        return {"ok": False, "error": "unused"}


def _dial(tmp_path, settings: dict, **params):
    client = _Client()
    provider = GatewayCallProvider("http://127.0.0.1:8090", "k", http=client)
    service = CallSessionService(
        CallSessionStore(tmp_path),
        settings_get=lambda key, default: settings.get(key, default),
        get_provider_fn=lambda _s: provider,
    )
    result = service.handle_call_start(
        {"phone": "+10000000000", "goal_text": "тест", "prompt": "тест", **params})
    assert result["ok"] is True, result
    return client.payloads[-1]


def test_settings_reach_the_gateway_payload(tmp_path):
    payload = _dial(tmp_path, {
        "call_max_duration_sec": 600, "call_voice_mode": "system", "call_speak_first": True})
    assert payload["max_duration_sec"] == 600
    assert payload["voice_mode"] == "system"
    assert payload["speak_first"] is True


def test_untouched_settings_add_nothing_so_gateway_defaults_stay(tmp_path):
    payload = _dial(tmp_path, {})
    assert not any(k in payload for k in _KEYS), payload


def test_defaults_equal_gateway_defaults_and_keep_the_clone_voice():
    assert DEFAULT_SETTINGS["call_max_duration_sec"] == 300
    assert DEFAULT_SETTINGS["call_voice_mode"] == "clone"
    assert DEFAULT_SETTINGS["call_speak_first"] is False


@pytest.mark.parametrize("raw,expected", [(99999999, 3600), (5, 30), (600.0, 600)])
def test_duration_is_clamped_to_a_safe_range(tmp_path, raw, expected):
    assert _dial(tmp_path, {"call_max_duration_sec": raw})["max_duration_sec"] == expected


@pytest.mark.parametrize("junk", ["abc", None, True, float("nan"), float("inf"), [], {}])
def test_unusable_duration_is_dropped_not_forwarded(tmp_path, junk):
    assert "max_duration_sec" not in _dial(tmp_path, {"call_max_duration_sec": junk})


@pytest.mark.parametrize("junk", ["loopbak", "", None, 3, "CLONE "])
def test_unknown_voice_mode_is_dropped(tmp_path, junk):
    payload = _dial(tmp_path, {"call_voice_mode": junk})
    assert "voice_mode" not in payload


@pytest.mark.parametrize("junk", ["true", "yes", 1, None, "false"])
def test_speak_first_requires_a_real_bool(tmp_path, junk):
    assert "speak_first" not in _dial(tmp_path, {"call_speak_first": junk})


def test_explicit_params_beat_settings(tmp_path):
    payload = _dial(
        tmp_path,
        {"call_max_duration_sec": 600, "call_voice_mode": "system", "call_speak_first": True},
        max_duration_sec=120, voice_mode="clone", speak_first=False,
    )
    assert payload["max_duration_sec"] == 120
    assert payload["voice_mode"] == "clone"
    assert payload["speak_first"] is False


def test_explicit_junk_param_is_dropped_and_falls_back_to_setting(tmp_path):
    payload = _dial(tmp_path, {"call_voice_mode": "system"}, voice_mode="loopbak")
    assert payload["voice_mode"] == "system"


def test_validator_bounds_and_enum_for_new_settings():
    validator = SettingsValidator()
    fixed = validator.validate({
        "call_max_duration_sec": 99999999, "call_voice_mode": "robot", "call_speak_first": "false"}).fixed
    assert fixed["call_max_duration_sec"] == 3600
    assert fixed["call_voice_mode"] == "clone"
    assert fixed["call_speak_first"] is False
