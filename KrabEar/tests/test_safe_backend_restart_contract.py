"""Полный safe-restart script: частный IPC и fake launchctl, без live lifecycle."""
import pytest

from test_restart_unknown_activity_2026_09_07 import reply, run_gate


def test_active_meeting_boolean_refuses_restart(tmp_path):
    completed, calls = run_gate(tmp_path, "safe_backend_restart.command",
                                reply(is_recording=False), reply(ok=True, active=True))
    assert completed.returncode == 1
    assert "активная сессия (meeting)" in completed.stderr
    assert not calls


def test_with_rest_kickstarts_rest_after_backend_ping(tmp_path):
    completed, calls = run_gate(tmp_path, "safe_backend_restart.command",
                                reply(is_recording=False), reply(ok=True, active=False), with_rest=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    lines = calls.splitlines()
    backend_at = next(i for i, line in enumerate(lines) if "kickstart" in line and "ai.krab.ear.backend" in line)
    ping_at = lines.index("IPC:ping")
    rest_at = next(i for i, line in enumerate(lines) if "kickstart" in line and "ai.krab.ear.rest" in line)
    assert backend_at < ping_at < rest_at


def test_check_only_confirms_idle_without_any_launchctl_or_ping(tmp_path):
    result, calls = run_gate(tmp_path, "safe_backend_restart.command",
                             reply(is_recording=False), reply(ok=True, active=False),
                             check_only=True, with_rest=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not calls


@pytest.mark.parametrize("rec,meet", [
    (reply(is_recording=True), reply(active=False)),
    (reply(is_recording=False), reply(active=True)),
    (reply(is_recording="false"), reply(active=False)),
    (reply(is_recording=False), "{}"),
])
def test_check_only_refuses_busy_or_unknown(tmp_path, rec, meet):
    result, calls = run_gate(tmp_path, "safe_backend_restart.command", rec, meet,
                             check_only=True)
    assert result.returncode == 1
    assert not calls


# --- KEG2-09: барьер maintenance_fence_close между проверкой покоя и kickstart ---

import json  # noqa: E402

from test_restart_unknown_activity_2026_09_07 import FENCE_TOKEN  # noqa: E402

_IDLE = (reply(is_recording=False), reply(ok=True, active=False))


def _fence(result=None, *, error=None):
    if error is not None:
        return json.dumps({"id": "1", "ok": False, "error": error})
    return json.dumps({"id": "1", "ok": True, "result": result})


def test_fence_closed_before_kickstart(tmp_path):
    result, calls = run_gate(tmp_path, "safe_backend_restart.command", *_IDLE)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = calls.splitlines()
    fence_at = lines.index("IPC:maintenance_fence_close")
    kick_at = next(i for i, line in enumerate(lines) if "kickstart" in line and "ai.krab.ear.backend" in line)
    assert fence_at < kick_at
    assert not any(line.startswith("IPC:maintenance_fence_open") for line in lines)


@pytest.mark.parametrize("fence", [
    _fence({"ok": False, "reason": "busy", "busy_reasons": ["starting_recording"]}),
    _fence({"ok": False, "reason": "already_closed", "busy_reasons": []}),
    _fence({"ok": True, "token": "../../evil"}),
    _fence({"ok": True}),
    _fence({"status": "weird"}),
    _fence(error={"code": "internal", "message": "x"}),
    "not-json",
])
def test_fence_refusal_or_garbage_blocks_kickstart(tmp_path, fence):
    """Снимок сказал idle, но атомарный барьер не подтвердил — рестарта нет."""
    result, calls = run_gate(tmp_path, "safe_backend_restart.command", *_IDLE, fence=fence)
    assert result.returncode == 1
    assert "REFUSED" in result.stderr
    assert "kickstart" not in calls


def test_fence_busy_reason_is_reported(tmp_path):
    fence = _fence({"ok": False, "reason": "busy", "busy_reasons": ["starting_call_assist"]})
    result, _ = run_gate(tmp_path, "safe_backend_restart.command", *_IDLE, fence=fence)
    assert "fence:starting_call_assist" in result.stderr


def test_legacy_backend_without_fence_falls_back_with_warning(tmp_path):
    """Переходный деплой: старый backend не знает метода — рестарт по снимку, громко."""
    fence = _fence(error={"code": "unknown_method", "message": "Неизвестный метод"})
    result, calls = run_gate(tmp_path, "safe_backend_restart.command", *_IDLE, fence=fence)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING" in result.stderr and "maintenance_fence" in result.stderr
    assert any("kickstart" in line for line in calls.splitlines())


def test_failed_kickstart_reopens_fence_with_token(tmp_path):
    result, calls = run_gate(tmp_path, "safe_backend_restart.command", *_IDLE, kickstart_rc=1)
    assert result.returncode == 1
    lines = calls.splitlines()
    assert f"IPC:maintenance_fence_open:{FENCE_TOKEN}" in lines
    assert "IPC:ping" not in lines


def test_check_only_never_closes_fence(tmp_path):
    result, calls = run_gate(tmp_path, "safe_backend_restart.command", *_IDLE, check_only=True)
    assert result.returncode == 0
    assert "maintenance_fence" not in calls
