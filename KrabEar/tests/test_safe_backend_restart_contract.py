"""Полный safe-restart script: частный IPC и fake launchctl, без live lifecycle."""
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
