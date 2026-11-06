"""Полный safe-restart script: частный IPC и fake launchctl, без live lifecycle."""
import subprocess
from pathlib import Path

import pytest

from test_restart_unknown_activity_2026_09_07 import reply, run_gate


@pytest.mark.parametrize("args", [
    ["--wait"], ["--wait", "--check-only"], ["--wait", "-1"],
    ["--wait", "1.5"], ["--wait", "abc"], ["--wait", "99999999999999999999"],
])
def test_invalid_wait_returns_usage_without_hanging(args, tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "safe_backend_restart.command"
    isolated = tmp_path / "restart.sh"
    original = script.read_text()
    socket_expression = 'os.path.expanduser("~/Library/Application Support/KrabEar/krabear.sock")'
    assert original.count(socket_expression) == 1
    source = original.replace(socket_expression, repr(str(tmp_path / "nonexistent.sock")))
    isolated.write_text(source)
    try:
        result = subprocess.run(["/bin/bash", str(isolated), *args], capture_output=True, text=True, timeout=1)
    except subprocess.TimeoutExpired:
        pytest.fail("argument parser did not terminate")
    assert result.returncode == 2
    assert "--wait" in result.stderr


def test_wait_accepts_decimal_with_leading_zero(tmp_path):
    completed, calls = run_gate(tmp_path, "safe_backend_restart.command",
                                reply(is_recording=False), reply(active=False),
                                wait_value="08", check_only=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not calls


@pytest.mark.parametrize("with_rest", [False, True])
def test_idle_snapshot_without_admission_fence_refuses_restart(tmp_path, with_rest):
    completed, calls = run_gate(tmp_path, "safe_backend_restart.command",
                                reply(is_recording=False), reply(ok=True, active=False), with_rest=with_rest)
    assert completed.returncode == 1
    assert "admission" in completed.stderr
    assert not calls


def test_active_meeting_boolean_refuses_restart(tmp_path):
    completed, calls = run_gate(tmp_path, "safe_backend_restart.command",
                                reply(is_recording=False), reply(ok=True, active=True))
    assert completed.returncode == 1
    assert "активная сессия (meeting)" in completed.stderr
    assert not calls


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
