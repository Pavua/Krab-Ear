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

import pytest

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


@pytest.mark.parametrize("payload, expected", [
    (b"0\n", 0), (b"255\n", 255), (b"999\n", 125),
    (b"", 125), (b"0\nx", 125), (b"1", 125),
])
def test_cleanup_signals_before_reaping_its_group_leader(monkeypatch, payload, expected) -> None:
    """После wait PID/PGID может стать чужим; держим leader до group cleanup."""
    import os

    runner = _load_runner_module()
    runner._termination_signal = None
    events = []

    class Leader:
        pid = 999999
        returncode = None

        def wait(self, timeout=None):
            events.append("reap")
            self.returncode = 0
            return 0

    leader = Leader()

    def popen(*args, **kwargs):
        # Новый keeper protocol. Настоящих subprocess/signal в этом тесте нет.
        descriptors = kwargs.get("pass_fds", ())
        if descriptors:
            os.write(descriptors[0], payload)
        return leader

    def killpg(pid, signum):
        assert pid == leader.pid
        assert leader.returncode is None, "group ownership lost after reap"
        events.append("signal")

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.os, "killpg", killpg)
    assert runner.run_isolated(["synthetic-command"]) == expected
    assert events == ["signal", "reap"]


def test_auto_reaping_sigchld_fails_before_start(monkeypatch) -> None:
    """SIGCHLD=IGN лишает parent гарантии резервирования PID даже без wait."""
    import pytest

    runner = _load_runner_module()
    monkeypatch.setattr(runner.signal, "getsignal", lambda signum: signal.SIG_IGN)
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("unsafe auto-reaping parent started a child"))
    with pytest.raises(RuntimeError, match="SIGCHLD"):
        runner.run_isolated(["synthetic-command"])


def test_pipe_cannot_alias_standard_streams(monkeypatch) -> None:
    import pytest

    runner = _load_runner_module()
    endpoints = iter(((0, 1), (2, 3)))
    closed = []
    monkeypatch.setattr(runner.os, "pipe", lambda: next(endpoints))
    monkeypatch.setattr(runner.os, "close", closed.append)
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("stdio-aliasing pipe reached command launch"))
    with pytest.raises(RuntimeError, match="stdio"):
        runner.run_isolated(["synthetic-command"])
    assert set(closed) == {0, 1, 2, 3}


def test_internal_keeper_rejects_callers_process_group(monkeypatch) -> None:
    import pytest

    runner = _load_runner_module()
    monkeypatch.setattr(runner.os, "getpid", lambda: 100)
    monkeypatch.setattr(runner.os, "getpgrp", lambda: 99)
    monkeypatch.setattr(runner.os, "getsid", lambda pid: 99)
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("unowned keeper launched command"))
    monkeypatch.setattr(runner.os, "killpg",
                        lambda *a: pytest.fail("unowned keeper signalled caller group"))
    with pytest.raises(RuntimeError, match="session leader"):
        runner._run_group_leader(["synthetic-command"], 10, 11)


def test_parent_sigkill_still_cleans_running_group(tmp_path: Path) -> None:
    """Control EOF позволяет keeper завершить только свою группу после parent death."""
    ready = tmp_path / "keeper-ready"
    escaped = tmp_path / "unexpected-survivor"
    code = (
        "import pathlib,time; "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(0.5); "
        f"pathlib.Path({str(escaped)!r}).write_text('survived')"
    )
    runner = subprocess.Popen([sys.executable, str(RUNNER), sys.executable, "-c", code])
    try:
        assert _wait_for(ready), "owned command did not start"
        runner.kill()
        runner.wait(timeout=3)
        time.sleep(0.65)
        assert not escaped.exists(), "parent SIGKILL left the owned group running"
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=2)


def test_command_start_failure_returns_127() -> None:
    result = subprocess.run(
        [sys.executable, str(RUNNER), "/definitely-not-an-ear-test-command"],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 127, result.stderr


def test_failed_keeper_start_closes_own_pipe_descriptors(monkeypatch) -> None:
    import os
    import pytest

    runner = _load_runner_module()
    descriptors = []
    real_pipe = os.pipe

    def pipe():
        pair = real_pipe()
        descriptors.extend(pair)
        return pair

    def unavailable(*args, **kwargs):
        raise OSError("synthetic keeper start failure")

    monkeypatch.setattr(runner.os, "pipe", pipe)
    monkeypatch.setattr(runner.subprocess, "Popen", unavailable)
    with pytest.raises(OSError, match="synthetic keeper"):
        runner.run_isolated(["synthetic-command"])
    assert len(descriptors) == 4
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_keeper_status_write_failure_still_cleans_its_group(monkeypatch) -> None:
    """Parent death между control check и status write не оставляет orphan."""
    runner = _load_runner_module()
    leader_pid = 999999
    signals = []
    monkeypatch.setattr(runner.os, "getpid", lambda: leader_pid)
    monkeypatch.setattr(runner.os, "getpgrp", lambda: leader_pid)
    monkeypatch.setattr(runner.os, "getsid", lambda pid: leader_pid)
    monkeypatch.setattr(runner.select, "select", lambda *args: ([], [], []))

    class FinishedCommand:
        def poll(self):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: FinishedCommand())

    def broken_write(*args):
        raise BrokenPipeError("synthetic parent death at status write")

    monkeypatch.setattr(runner.os, "write", broken_write)
    monkeypatch.setattr(runner.os, "killpg", lambda *args: signals.append(args))
    with pytest.raises(BrokenPipeError, match="synthetic parent death"):
        runner._run_group_leader(["synthetic-command"], 10, 11)
    assert signals == [(leader_pid, signal.SIGKILL)]
