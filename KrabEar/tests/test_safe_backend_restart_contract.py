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
