"""Один бюджет worker включает PCM, очередь, spawn и запись запроса.

Реальные Lock/Popen/pipes; subprocess — stdlib echo, без ML и production state.
"""
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import mlx_whisper_session as worker
from core.mlx_lock import MLXLockTimeoutError


ECHO = '''import json, sys, time
from pathlib import Path
for line in sys.stdin:
    req = json.loads(line)
    if req.get("op") == "shutdown": break
    params = req["params"]
    Path(params["entered"]).touch()
    while not Path(params["release"]).exists(): time.sleep(0.005)
    print(json.dumps({"ok": True, "result": {"text": params["name"]}}), flush=True)
'''


@pytest.fixture
def echo_session(tmp_path, monkeypatch):
    script = tmp_path / "echo.py"
    script.write_text(ECHO)
    monkeypatch.setattr(worker, "_WORKER_SCRIPT", script)
    session = worker.MLXWhisperSession()
    session.start()
    proc = session._proc
    threads = []
    try:
        yield session, proc, threads
    finally:
        for name in ("first", "queued"):
            (tmp_path / (name + ".release")).touch()
        for thread in threads:
            thread.join(5)
        if any(thread.is_alive() for thread in threads):
            proc.kill()
        for thread in threads:
            thread.join(5)
        session.close(timeout=1)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe:
                pipe.close()
        if session._stderr_drain_thread:
            session._stderr_drain_thread.join(5)
        assert all(not thread.is_alive() for thread in threads)


def test_queued_timeout_returns_without_sending_or_killing_active_child(echo_session, tmp_path):
    session, proc, threads = echo_session
    results, errors = {}, {}
    entered = threading.Event()

    def call(name, timeout):
        params = {
            "name": name, "entered": str(tmp_path / (name + ".entered")),
            "release": str(tmp_path / (name + ".release")),
        }
        if name == "queued":
            entered.set()
        try:
            results[name] = session.transcribe("synthetic.wav", params, timeout, name)
        except BaseException as exc:
            errors[name] = exc

    first = threading.Thread(target=call, args=("first", 60), daemon=True)
    threads.append(first)
    first.start()
    ready_by = time.monotonic() + 10
    while not (tmp_path / "first.entered").exists() and time.monotonic() < ready_by:
        time.sleep(0.005)
    assert (tmp_path / "first.entered").exists(), errors
    queued = threading.Thread(target=call, args=("queued", 0.05), daemon=True)
    threads.append(queued)
    queued.start()
    assert entered.wait(5)
    queued.join(3)
    finished_before_release = not queued.is_alive()
    # Always drain both calls, including the deliberately broken RED implementation.
    (tmp_path / "first.release").touch()
    (tmp_path / "queued.release").touch()
    first.join(5)
    queued.join(5)
    assert finished_before_release, "Просроченный запрос остался за I/O-lock"
    assert isinstance(errors.get("queued"), MLXLockTimeoutError)
    assert "queued" not in results
    assert not (tmp_path / "queued.entered").exists(), "Поздний JSON дошёл до worker"
    assert results.get("first") == {"text": "first"}
    assert "first" not in errors
    assert session._proc is proc and proc.poll() is None
    assert session.inflight == 0


def test_expired_session_does_not_spawn(monkeypatch):
    spawns = []

    def forbidden_spawn(*args, **kwargs):
        spawns.append(True)
        raise AssertionError("Просроченный запрос запустил child")

    monkeypatch.setattr(worker.subprocess, "Popen", forbidden_spawn)
    session = worker.MLXWhisperSession()
    with pytest.raises(MLXLockTimeoutError):
        session.transcribe("synthetic.wav", {}, 0.0, "expired")
    assert spawns == []
    assert session.inflight == 0


def test_pcm_write_consumes_budget_and_removes_temporary_wav(tmp_path, monkeypatch):
    now = [100.0]
    paths, calls = [], []
    real_mkstemp = worker.tempfile.mkstemp

    def temporary(**kwargs):
        fd, path = real_mkstemp(dir=tmp_path, **kwargs)
        paths.append(Path(path))
        return fd, path

    def write(path, audio):
        Path(path).write_bytes(b"synthetic-pcm")
        now[0] += 2.0

    def transcribe(*args, **kwargs):
        calls.append(True)
        return {"text": "late"}

    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(worker.tempfile, "mkstemp", temporary)
    monkeypatch.setattr(worker, "_write_pcm_wav", write)
    monkeypatch.setattr(worker, "get_mlx_whisper_session", lambda: SimpleNamespace(transcribe=transcribe))
    with pytest.raises(MLXLockTimeoutError):
        worker.transcribe_via_mlx_worker(object(), {}, 1.0, "expired")
    assert calls == []
    assert len(paths) == 1 and not paths[0].exists()


def test_watchdog_covers_blocked_stdin_write(tmp_path, monkeypatch):
    script = tmp_path / "unread.py"
    # Worker stays alive without consuming stdin: a large JSON fills the pipe.
    script.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(worker, "_WORKER_SCRIPT", script)
    session = worker.MLXWhisperSession()
    session.start()
    proc = session._proc
    errors = []

    def call():
        try:
            session.transcribe("synthetic.wav", {"padding": "x" * 1_000_000}, 0.1, "blocked-write")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    try:
        thread.join(3)
        finished_without_rescue = not thread.is_alive()
    finally:
        if proc.poll() is None:
            proc.kill()
        thread.join(5)
        session.close(timeout=1)
        proc.wait(timeout=5)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe:
                pipe.close()
        if session._stderr_drain_thread:
            session._stderr_drain_thread.join(5)
    assert finished_without_rescue, "Watchdog не ограничил stdin.write"
    assert len(errors) == 1 and isinstance(errors[0], worker.MLXTimeoutError)
    assert session.inflight == 0


def test_expiry_between_start_and_send_never_writes_json(echo_session, tmp_path, monkeypatch):
    session, proc, _threads = echo_session
    now = [100.0]
    original_start = session.start

    def delayed_start(**kwargs):
        original_start(**kwargs)
        now[0] += 2.0

    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(session, "start", delayed_start)
    params = {
        "name": "queued", "entered": str(tmp_path / "queued.entered"),
        "release": str(tmp_path / "queued.release"),
    }
    with pytest.raises(MLXLockTimeoutError):
        session.transcribe("synthetic.wav", params, 1.0, "expired")
    assert session.inflight == 0
    assert not (tmp_path / "queued.entered").exists()
    assert session._proc is proc and proc.poll() is None
