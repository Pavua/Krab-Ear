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
    assert bound is not None and 0 < bound <= 30


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
