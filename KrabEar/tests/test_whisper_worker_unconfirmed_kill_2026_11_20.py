"""Whisper worker: SIGKILL без подтверждённого выхода не вешает запрос навсегда."""

import subprocess
from unittest.mock import Mock

import pytest

from core import mlx_whisper_session as whisper
from core.mlx_lock import MLXLockTimeoutError
from core.mlx_subprocess import MLXTimeoutError


def _unkillable_child():
    child = Mock()
    child.stdin.closed = False
    child.stdout.readline.return_value = ""
    child.poll.return_value = None
    child.pid = 4242
    child.wait.side_effect = subprocess.TimeoutExpired(cmd="worker", timeout=1.0)
    return child


def _session_with(child, monkeypatch):
    session = whisper.MLXWhisperSession()
    session._proc = child
    monkeypatch.setattr(whisper.threading, "Timer", lambda _delay, kill: Mock(start=kill))
    return session


def _send(session):
    return session._send({"op": "transcribe"}, timeout_sec=1.0, model_name="synthetic-whisper")


def test_unconfirmed_exit_is_waited_with_a_bound_and_reported_as_timeout(monkeypatch):
    child = _unkillable_child()
    session = _session_with(child, monkeypatch)
    with pytest.raises(MLXTimeoutError):
        _send(session)
    child.kill.assert_called_once()
    (args, kwargs) = child.wait.call_args
    bound = kwargs.get("timeout", args[0] if args else None)
    assert bound == whisper._KILL_CONFIRM_TIMEOUT_SEC


def test_next_request_is_refused_without_spawn_while_child_may_be_alive(monkeypatch):
    child = _unkillable_child()
    session = _session_with(child, monkeypatch)
    with pytest.raises(MLXTimeoutError):
        _send(session)
    popen = Mock(side_effect=AssertionError("второй worker нельзя запускать"))
    monkeypatch.setattr(whisper.subprocess, "Popen", popen)
    with pytest.raises(MLXLockTimeoutError):
        session.transcribe("synthetic.wav", {}, 5.0, "synthetic-whisper")
    popen.assert_not_called()
    assert session.inflight == 0


def test_spawn_resumes_after_the_unconfirmed_child_finally_exits(monkeypatch):
    child = _unkillable_child()
    session = _session_with(child, monkeypatch)
    with pytest.raises(MLXTimeoutError):
        _send(session)
    child.poll.return_value = -9
    spawned = []
    monkeypatch.setattr(session, "_spawn_unlocked", lambda: spawned.append(True))
    session.start()
    assert spawned == [True]
    assert session._unconfirmed_exit is None


def test_confirmed_exit_keeps_the_previous_behaviour(monkeypatch):
    child = _unkillable_child()
    child.wait.side_effect = None
    child.wait.return_value = -9
    child.poll.return_value = -9
    session = _session_with(child, monkeypatch)
    with pytest.raises(MLXTimeoutError):
        _send(session)
    assert session._proc is None
    assert session._unconfirmed_exit is None


def _orphaned_session(monkeypatch):
    child = _unkillable_child()
    session = _session_with(child, monkeypatch)
    with pytest.raises(MLXTimeoutError):
        _send(session)
    return session, child


def test_refusal_is_a_distinct_lock_timeout_subclass(monkeypatch):
    session, _child = _orphaned_session(monkeypatch)
    with pytest.raises(whisper.MLXWorkerUnconfirmedExit) as info:
        session.start()
    assert isinstance(info.value, MLXLockTimeoutError)
    assert "рестарт" in str(info.value)


def test_reset_keeps_a_session_that_remembers_a_live_orphan(monkeypatch):
    session, child = _orphaned_session(monkeypatch)
    monkeypatch.setattr(whisper, "_session", session)
    whisper.reset_mlx_whisper_session()
    assert whisper._session is session
    assert whisper.get_mlx_whisper_session() is session
    child.poll.return_value = -9
    whisper.reset_mlx_whisper_session()
    assert whisper._session is None


def test_close_retries_the_kill_of_an_unconfirmed_child(monkeypatch):
    session, child = _orphaned_session(monkeypatch)
    child.kill.reset_mock()
    session.close()
    child.kill.assert_called_once()


def test_hard_kill_before_exit_also_kills_the_unconfirmed_child(monkeypatch):
    session, child = _orphaned_session(monkeypatch)
    child.kill.reset_mock()
    session.kill()
    child.kill.assert_called_once()


def test_residency_does_not_report_unloaded_while_an_orphan_may_hold_memory(monkeypatch):
    session, child = _orphaned_session(monkeypatch)
    monkeypatch.setattr(whisper, "_child_rss_mb", lambda pid: 1500 if pid == child.pid else None)
    snapshot = session.residency_snapshot()
    assert snapshot["pid"] == child.pid
    assert snapshot["resident_mb"] == 1500
    assert snapshot["state"] != "unloaded"


def test_blocked_pipe_read_after_sigkill_does_not_hang_the_request(monkeypatch):
    """Ревью ChatGPT A1: убитый child в непрерываемом состоянии не закрывает stdout."""
    import threading
    import time

    release = threading.Event()
    child = _unkillable_child()
    child.stdout.readline.side_effect = lambda: (release.wait(10), "")[1]
    monkeypatch.setattr(whisper, "_KILL_CONFIRM_TIMEOUT_SEC", 0.2)
    session = _session_with(child, monkeypatch)
    started = time.monotonic()
    try:
        with pytest.raises(MLXTimeoutError):
            session._send({"op": "transcribe"}, timeout_sec=0.05, model_name="synthetic-whisper")
        assert time.monotonic() - started < 3
        assert session._unconfirmed_exit is child
        assert session._proc is None
        child.stdout.close.assert_not_called()  # закрытие блокировалось бы на локе читателя
        with pytest.raises(whisper.MLXWorkerUnconfirmedExit):
            session.start()
    finally:
        release.set()
