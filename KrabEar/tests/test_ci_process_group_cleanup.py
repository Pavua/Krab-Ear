"""Регрессия A1: cleanup CI не может завершать чужие ML-воркеры.

До A1 workflow делал `pkill -9 -f gigaam_worker`: любой production worker с
таким именем попадал под cleanup тестового job. Нужный контракт — завершать
только process group, созданную для конкретного pytest-запуска.
"""
from __future__ import annotations

import importlib.util
import signal
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "run_isolated_pytest.py"


def _load_runner_module():
    spec = importlib.util.spec_from_file_location("a1_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wait_for(path: Path, *, timeout_sec: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def test_cleanup_kills_only_its_process_group(tmp_path: Path) -> None:
    """A global name-match would kill foreign `gigaam_worker`; group cleanup cannot."""
    own_marker = tmp_path / "own-marker"
    foreign_marker = tmp_path / "foreign-marker"
    foreign_code = (
        "import pathlib, time; time.sleep(0.35); "
        f"pathlib.Path({str(foreign_marker)!r}).write_text('foreign')"
    )
    owned_child_code = (
        "import pathlib, time; time.sleep(0.35); "
        f"pathlib.Path({str(own_marker)!r}).write_text('own')"
    )
    own_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {owned_child_code!r}]); "
        "time.sleep(0.05)"
    )
    foreign = subprocess.Popen(
        [sys.executable, "-c", foreign_code + " # gigaam_worker foreign"],
        start_new_session=True,
    )
    try:
        result = subprocess.run(
            [sys.executable, str(RUNNER), sys.executable, "-c", own_code],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert _wait_for(foreign_marker), "foreign worker was terminated by CI cleanup"
        assert not own_marker.exists(), "orphan from the test group survived cleanup"
    finally:
        foreign.terminate()
        try:
            foreign.wait(timeout=2)
        except subprocess.TimeoutExpired:
            foreign.kill()
            foreign.wait(timeout=2)


def test_sigterm_reaps_its_process_group(tmp_path: Path) -> None:
    """A1: отмена job не оставляет потомка отдельной test process group."""
    ready_marker = tmp_path / "ready-marker"
    own_marker = tmp_path / "own-marker"
    owned_child_code = (
        "import pathlib, time; "
        f"pathlib.Path({str(ready_marker)!r}).write_text('ready'); "
        "time.sleep(0.35); "
        f"pathlib.Path({str(own_marker)!r}).write_text('own')"
    )
    own_code = (
        "import pathlib, subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {owned_child_code!r}]); "
        "time.sleep(0.6)"
    )
    runner = subprocess.Popen(
        [sys.executable, str(RUNNER), sys.executable, "-c", own_code],
    )
    try:
        assert _wait_for(ready_marker), "isolated command did not start"
        runner.terminate()
        assert runner.wait(timeout=3) == 143
        time.sleep(0.45)
        assert not own_marker.exists(), "SIGTERM left a test-group child alive"
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=2)


def test_signal_exit_is_reported_as_conventional_shell_status() -> None:
    """A1: SIGTERM child is reported as 128+15, not Python's wrapped -15."""
    signal_code = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    result = subprocess.run(
        [sys.executable, str(RUNNER), sys.executable, "-c", signal_code],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 143, result.stderr


def test_termination_handler_defers_cleanup_until_process_group_is_known() -> None:
    """A1: signal inside Popen marks cancellation instead of skipping finally."""
    runner = _load_runner_module()
    runner._termination_signal = None
    assert runner._mark_termination(signal.SIGTERM, None) is None
    assert runner._termination_signal == signal.SIGTERM
