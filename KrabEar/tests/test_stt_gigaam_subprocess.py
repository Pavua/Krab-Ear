"""Тесты для subprocess transport в GigaAMAdapter (B-3, 2026-04-26).

Покрывает:
- _GigaAMSubprocessSession lifecycle (start/transcribe/close) с mock Popen
- GigaAMAdapter с transport="subprocess" / "auto" routing
- Resolve transport: auto → in_process если gigaam импортируется, иначе subprocess
- Защита от отсутствия venv_python_path
- is_subprocess_venv_available() utility

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_stt_gigaam_subprocess.py -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Хелперы для mock subprocess.Popen
# ---------------------------------------------------------------------------

class _FakePopen:
    """Минимальный аналог subprocess.Popen для тестов: scripted stdout responses."""

    def __init__(self, stdout_responses: list[str], poll_value=None) -> None:
        # stdout_responses — список строк (по одной на каждый readline()).
        self._responses = list(stdout_responses)
        self._poll_value = poll_value
        self.pid = 12345
        self.stdin = io.StringIO()
        self.stdin.close = self._close_stdin  # noqa: метод stub
        self._stdin_closed = False
        self.stdout = MagicMock()
        self.stdout.readline.side_effect = lambda: self._responses.pop(0) if self._responses else ""
        # Пустой конечный поток важен для реалистичной модели Popen: безнастроенный
        # MagicMock.readline() всегда truthy и превращает stderr-drain в бесконечный цикл.
        self.stderr = io.StringIO()
        self.terminate_called = False
        self.kill_called = False
        self.wait_called = False

    def _close_stdin(self) -> None:
        self._stdin_closed = True

    def poll(self):  # noqa: D401 — match Popen API
        return self._poll_value

    def wait(self, timeout=None):
        self.wait_called = True
        self._poll_value = 0
        return 0

    def terminate(self):
        self.terminate_called = True
        self._poll_value = 0

    def kill(self):
        self.kill_called = True
        self._poll_value = -9


def _ok_load_response() -> str:
    return json.dumps({"ok": True, "mode": "rnnt", "device": "cpu"}) + "\n"


def _ok_transcribe_response(text: str = "Привет мир") -> str:
    return json.dumps({"ok": True, "text": text, "engine": "gigaam-rnnt"}) + "\n"


def _err_response(error: str) -> str:
    return json.dumps({"ok": False, "error": error}) + "\n"


# ---------------------------------------------------------------------------
# Тест 1: _GigaAMSubprocessSession lifecycle
# ---------------------------------------------------------------------------

class TestSubprocessSessionLifecycle(unittest.TestCase):

    def test_is_loaded_false_before_start(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        sess = _GigaAMSubprocessSession(
            venv_python="/fake/python",
            worker_path="/fake/worker.py",
            mode="rnnt",
            device="cpu",
        )
        self.assertFalse(sess.is_loaded())

    def test_transcribe_raises_if_not_started(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
        with self.assertRaises(RuntimeError) as ctx:
            sess.transcribe("/tmp/test.wav")
        self.assertIn("not started", str(ctx.exception))

    def test_start_load_ok(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        fake_popen = _FakePopen([_ok_load_response()])
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
            self.addCleanup(sess.close)
            sess.start()
        self.assertTrue(sess.is_loaded())
        drain_thread = sess._stderr_drain_thread
        self.assertIsNotNone(drain_thread)
        drain_thread.join(timeout=1.0)
        self.assertFalse(
            drain_thread.is_alive(),
            "stderr-drain обязан завершиться после конца тестового stderr",
        )
        # Stdin должен содержать команду load.
        sent = fake_popen.stdin.getvalue()
        self.assertIn('"op": "load"', sent)
        self.assertIn('"mode": "rnnt"', sent)
        self.assertIn('"device": "cpu"', sent)

    def test_start_load_failure_raises(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        fake_popen = _FakePopen([_err_response("gigaam_not_installed: ...")])
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
            self.addCleanup(sess.close)
            with self.assertRaises(RuntimeError) as ctx:
                sess.start()
        self.assertIn("load failed", str(ctx.exception))
        self.assertFalse(sess.is_loaded())

    def test_start_idempotent(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        fake_popen = _FakePopen([_ok_load_response()])
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen) as p:
            sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
            self.addCleanup(sess.close)
            sess.start()
            sess.start()  # второй вызов не должен спавнить ещё один процесс
        self.assertEqual(p.call_count, 1)

    def test_start_popen_failure_wraps_in_runtime_error(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        with patch(
            "core.pipeline.stt_gigaam.subprocess.Popen",
            side_effect=FileNotFoundError("no such venv"),
        ):
            sess = _GigaAMSubprocessSession("/missing/py", "/fake/w.py", "rnnt", "cpu")
            with self.assertRaises(RuntimeError) as ctx:
                sess.start()
        self.assertIn("worker", str(ctx.exception))

    def test_start_send_exception_still_terminates_process(self):
        """Regression: start() must clean up the spawned Popen when the load
        handshake RAISES (worker crashed / empty read), not just when it
        returns an explicit {ok: False} response.

        _send() raises "empty response (worker exited or timed out)" when
        readline() returns "" — this happens BEFORE start() ever inspects a
        load_response dict, so the existing `if not load_response.get("ok")`
        cleanup branch is never reached and the already-spawned subprocess
        was leaked as an orphan (observed live: gigaam_worker.py surviving
        with PPID=1 after BackendServiceLLMInitializationTestCase, which
        builds a real Transcriber/AudioEngine and hits this exact path when
        the dev machine's real settings.json has stt_gigaam_enabled=true).
        """
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        fake_popen = _FakePopen([])  # readline() -> "" immediately
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
            with self.assertRaises(RuntimeError) as ctx:
                sess.start()
        self.assertIn("empty response", str(ctx.exception))
        self.assertFalse(sess.is_loaded())
        self.assertIsNone(
            sess._proc,
            "start() must clear _proc via close() on failure, not leak the Popen handle",
        )
        self.assertTrue(
            fake_popen._stdin_closed,
            "close() must run its shutdown sequence (stdin write+close) even when "
            "the load handshake raised instead of returning ok=False",
        )


# ---------------------------------------------------------------------------
# Тест 2: transcribe + close
# ---------------------------------------------------------------------------

class TestSubprocessTranscribe(unittest.TestCase):

    def test_transcribe_sends_correct_json_returns_text(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        fake_popen = _FakePopen([
            _ok_load_response(),
            _ok_transcribe_response("Тестовый текст"),
        ])
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
            self.addCleanup(sess.close)
            sess.start()
            result = sess.transcribe("/tmp/audio.wav")
        self.assertEqual(result["text"], "Тестовый текст")
        self.assertEqual(result["engine"], "gigaam-rnnt")
        # Stdin должен содержать transcribe-команду с правильным path.
        sent = fake_popen.stdin.getvalue()
        self.assertIn('"op": "transcribe"', sent)
        self.assertIn('/tmp/audio.wav', sent)

    def test_transcribe_error_raises(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        fake_popen = _FakePopen([
            _ok_load_response(),
            _err_response("transcribe_failed: bad audio"),
        ])
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
            self.addCleanup(sess.close)
            sess.start()
            with self.assertRaises(RuntimeError) as ctx:
                sess.transcribe("/tmp/bad.wav")
        self.assertIn("transcribe_failed", str(ctx.exception))

    def test_transcribe_invalid_json_raises(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        fake_popen = _FakePopen([
            _ok_load_response(),
            "not json at all\n",
        ])
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
            self.addCleanup(sess.close)
            sess.start()
            with self.assertRaises(RuntimeError) as ctx:
                sess.transcribe("/tmp/x.wav")
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_close_sends_shutdown_and_waits(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        fake_popen = _FakePopen([_ok_load_response()])
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
            self.addCleanup(sess.close)
            sess.start()
            sess.close()
        sent = fake_popen.stdin.getvalue()
        self.assertIn('"op": "shutdown"', sent)
        self.assertTrue(fake_popen.wait_called)
        self.assertFalse(sess.is_loaded())

    def test_close_idempotent(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
        sess.close()  # без start() — не должен падать
        sess.close()


# ---------------------------------------------------------------------------
# Тест 3: GigaAMAdapter — transport параметр
# ---------------------------------------------------------------------------

class TestAdapterTransport(unittest.TestCase):

    def test_invalid_transport_raises(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        with self.assertRaises(ValueError) as ctx:
            GigaAMAdapter(device="cpu", mode="rnnt", transport="invalid")
        self.assertIn("transport", str(ctx.exception))

    def test_default_transport_is_auto(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        a = GigaAMAdapter(device="cpu", mode="rnnt")
        self.assertEqual(a._transport_pref, "auto")

    def test_explicit_subprocess_stored(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        a = GigaAMAdapter(device="cpu", mode="rnnt", transport="subprocess")
        self.assertEqual(a._transport_pref, "subprocess")

    def test_venv_python_path_default(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter, _DEFAULT_VENV_PYTHON
        a = GigaAMAdapter(device="cpu", mode="rnnt", transport="subprocess")
        self.assertEqual(a._venv_python_path, _DEFAULT_VENV_PYTHON)

    def test_venv_python_path_override(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        a = GigaAMAdapter(
            device="cpu",
            mode="rnnt",
            transport="subprocess",
            venv_python_path="/custom/python",
        )
        self.assertEqual(a._venv_python_path, "/custom/python")


# ---------------------------------------------------------------------------
# Тест 4: _resolve_transport
# ---------------------------------------------------------------------------

class TestAdapterResolveTransport(unittest.TestCase):

    def test_explicit_in_process_kept(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        a = GigaAMAdapter(device="cpu", mode="rnnt", transport="in_process")
        self.assertEqual(a._resolve_transport(), "in_process")

    def test_explicit_subprocess_kept(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        a = GigaAMAdapter(device="cpu", mode="rnnt", transport="subprocess")
        self.assertEqual(a._resolve_transport(), "subprocess")

    def test_auto_with_gigaam_available_returns_in_process(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        # Делаем gigaam "доступным" через подмену в sys.modules.
        import types
        fake_gigaam = types.ModuleType("gigaam")
        with patch.dict(sys.modules, {"gigaam": fake_gigaam}):
            a = GigaAMAdapter(device="cpu", mode="rnnt", transport="auto")
            self.assertEqual(a._resolve_transport(), "in_process")

    def test_auto_with_gigaam_missing_returns_subprocess(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        # Эмулируем ImportError — кладём None в sys.modules.
        original = sys.modules.pop("gigaam", None)
        sys.modules["gigaam"] = None  # type: ignore[assignment]
        try:
            a = GigaAMAdapter(device="cpu", mode="rnnt", transport="auto")
            self.assertEqual(a._resolve_transport(), "subprocess")
        finally:
            if original is not None:
                sys.modules["gigaam"] = original
            else:
                sys.modules.pop("gigaam", None)

    def test_resolve_cached(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        a = GigaAMAdapter(device="cpu", mode="rnnt", transport="subprocess")
        first = a._resolve_transport()
        second = a._resolve_transport()
        self.assertEqual(first, second)
        self.assertEqual(a._active_transport, first)


# ---------------------------------------------------------------------------
# Тест 5: _get_subprocess_session — защита от отсутствия venv
# ---------------------------------------------------------------------------

class TestSubprocessSessionGuards(unittest.TestCase):

    def test_missing_venv_python_raises(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        a = GigaAMAdapter(
            device="cpu",
            mode="rnnt",
            transport="subprocess",
            venv_python_path="/definitely/not/here/python",
        )
        with self.assertRaises(RuntimeError) as ctx:
            a._get_subprocess_session()
        msg = str(ctx.exception)
        self.assertIn("venv Python не найден", msg)
        self.assertIn("install_gigaam_venv", msg)

    def test_session_is_lazy(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        a = GigaAMAdapter(
            device="cpu",
            mode="rnnt",
            transport="subprocess",
            venv_python_path="/fake/python",
        )
        # До первого вызова _get_subprocess_session — session не создан
        self.assertIsNone(a._subprocess)


# ---------------------------------------------------------------------------
# Тест 6: is_subprocess_venv_available
# ---------------------------------------------------------------------------

class TestVenvAvailability(unittest.TestCase):

    def test_returns_false_for_missing_path(self):
        from core.pipeline.stt_gigaam import is_subprocess_venv_available
        self.assertFalse(is_subprocess_venv_available("/definitely/not/here/python"))

    def test_returns_true_for_existing_file(self):
        from core.pipeline.stt_gigaam import is_subprocess_venv_available
        # /usr/bin/python3 существует на macOS — используем как проксу.
        if os.path.exists("/usr/bin/python3"):
            self.assertTrue(is_subprocess_venv_available("/usr/bin/python3"))
        else:
            self.skipTest("/usr/bin/python3 не существует на этой системе")


# ---------------------------------------------------------------------------
# Тест 7: end-to-end через GigaAMAdapter.transcribe (mock Popen)
# ---------------------------------------------------------------------------

class TestAdapterTranscribeViaSubprocess(unittest.TestCase):

    def test_transcribe_calls_subprocess_session(self):
        from core.pipeline.stt_gigaam import GigaAMAdapter
        import numpy as np

        fake_popen = _FakePopen([
            _ok_load_response(),
            _ok_transcribe_response("Распознанный текст"),
        ])

        a = GigaAMAdapter(
            device="cpu",
            mode="rnnt",
            transport="subprocess",
            venv_python_path="/usr/bin/python3",  # путь существует — guard пропустит
        )
        self.addCleanup(a.close)

        # Моки: Popen + проверка существования worker_path.
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            with patch("core.pipeline.stt_gigaam.os.path.exists", return_value=True):
                # Простой синус 1 сек.
                t = np.linspace(0, 1.0, 16000, dtype=np.float32)
                audio = np.sin(2 * np.pi * 440 * t)
                result = a.transcribe(audio, sample_rate=16000)

        self.assertEqual(result["text"], "Распознанный текст")
        self.assertEqual(result["language"], "ru")
        self.assertEqual(result["engine"], "gigaam-rnnt")
        self.assertIsInstance(result["confidence"], float)


# ---------------------------------------------------------------------------
# Smoke (real subprocess) — пропускается если venv_gigaam не установлен
# ---------------------------------------------------------------------------

class TestRealSubprocessSmoke(unittest.TestCase):
    """Реальный subprocess smoke: запускает worker из venv_gigaam, проверяет ping/shutdown.

    Не требует загрузки реальной модели — только проверка что worker стартует
    и отвечает на ping. Skip если venv_gigaam не установлен.
    """

    def setUp(self):
        from core.pipeline.stt_gigaam import _DEFAULT_VENV_PYTHON, is_subprocess_venv_available
        if not is_subprocess_venv_available(_DEFAULT_VENV_PYTHON):
            self.skipTest("venv_gigaam не установлен (запусти scripts/install_gigaam_venv.command)")
        self._venv_python = _DEFAULT_VENV_PYTHON

    def test_worker_responds_to_ping(self):
        import subprocess as sp
        worker_path = os.path.normpath(
            os.path.join(PROJECT_ROOT, "core", "workers", "gigaam_worker.py")
        )
        proc = sp.Popen(
            [self._venv_python, "-u", worker_path],
            stdin=sp.PIPE,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            assert proc.stdin and proc.stdout
            proc.stdin.write(json.dumps({"op": "ping"}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            response = json.loads(line)
            self.assertTrue(response.get("ok"))
            self.assertTrue(response.get("pong"))
            self.assertFalse(response.get("model_loaded"))  # модель не загружена
        finally:
            try:
                proc.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                proc.stdin.flush()
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=3.0)
            except sp.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=1.0)


# ---------------------------------------------------------------------------
# W1688 — Test _error_bus propagation (W1686 F4 fix)
# ---------------------------------------------------------------------------

class TestGigaAMSessionReceivesErrorBus(unittest.TestCase):
    """W1688: GigaAMAdapter must propagate _error_bus to each spawned session.

    Before the fix, _GigaAMSubprocessSession._error_bus was always None because
    _get_subprocess_session() never forwarded adapter._error_bus.  Worker
    timeout/crash errors silently disappeared without reaching the Loud Errors toast.
    """

    def test_gigaam_session_receives_error_bus(self):
        """After adapter._error_bus is set, a newly spawned session inherits it."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        fake_popen = _FakePopen([_ok_load_response()])
        fake_error_bus = MagicMock()

        adapter = GigaAMAdapter(
            device="cpu",
            mode="rnnt",
            transport="subprocess",
            venv_python_path="/usr/bin/python3",
        )
        self.addCleanup(adapter.close)
        adapter._error_bus = fake_error_bus  # late-inject, as service.py does

        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            with patch("core.pipeline.stt_gigaam.os.path.exists", return_value=True):
                session = adapter._get_subprocess_session()

        self.assertIs(session._error_bus, fake_error_bus,
                      "_GigaAMSubprocessSession._error_bus must equal adapter._error_bus")

    def test_error_bus_none_if_adapter_has_no_bus(self):
        """If adapter._error_bus is None (not yet wired), session also gets None (default)."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        fake_popen = _FakePopen([_ok_load_response()])

        adapter = GigaAMAdapter(
            device="cpu",
            mode="rnnt",
            transport="subprocess",
            venv_python_path="/usr/bin/python3",
        )
        self.addCleanup(adapter.close)
        # adapter._error_bus == None by default

        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            with patch("core.pipeline.stt_gigaam.os.path.exists", return_value=True):
                session = adapter._get_subprocess_session()

        self.assertIsNone(session._error_bus,
                          "Session _error_bus should be None when adapter has no bus")


class TestGigaAMWorkerTimeoutPushesError(unittest.TestCase):
    """W1688: worker timeout must call error_bus.push with stt.gigaam_worker_timeout."""

    def test_gigaam_worker_timeout_pushes_error(self):
        """_timeout_kill pushes stt.gigaam_worker_timeout to a wired error_bus."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession

        fake_popen = _FakePopen([_ok_load_response()])
        fake_error_bus = MagicMock()

        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
            self.addCleanup(sess.close)
            sess.start()

        # Late-inject error_bus (as GigaAMAdapter does after W1688 fix)
        sess._error_bus = fake_error_bus

        # Stub out the error_bus imports so push receives a real KrabError-like call
        # without needing the full backend installed.
        class _FakeKrabError:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        with patch("core.pipeline.stt_gigaam.threading.Timer"):
            with patch.dict("sys.modules", {
                "backend.error_bus": MagicMock(KrabError=_FakeKrabError),
                "backend.error_codes": MagicMock(ERROR_REGISTRY={
                    "stt.gigaam_worker_timeout": {
                        "severity": "error",
                        "user_msg_ru": "GigaAM timeout",
                        "actionable": False,
                        "action_id": None,
                    },
                }),
            }):
                # Manually invoke _timeout_kill — simulates a worker timeout.
                sess._timeout_kill()

        # error_bus.push must have been called once with a timeout error.
        self.assertTrue(fake_error_bus.push.called,
                        "error_bus.push must be called on worker timeout")
        pushed_obj = fake_error_bus.push.call_args[0][0]
        # The pushed error must be for stt.gigaam_worker_timeout.
        self.assertIn("gigaam_worker_timeout", getattr(pushed_obj, "code", ""),
                      f"Unexpected error code: {getattr(pushed_obj, 'code', None)}")


# ---------------------------------------------------------------------------
# Live-инцидент 2026-08-03 — _timeout_kill не эскалировал terminate()→kill()
#
# Sibling-gate asymmetry: close() (строки 761-771) уже делает terminate→wait→
# kill escalation при graceful shutdown; _timeout_kill() слал ТОЛЬКО terminate()
# и возвращался, не проверяя, реально ли процесс умер. Под нагрузкой (D-state,
# застрявший в native-коде GigaAM-инференс, задавленный планировщик ОС) SIGTERM
# может не убить процесс вовсе — тогда stdout-fd никогда не закрывается, и
# заблокированный на readline() поток _send() виснет НАВСЕГДА (наблюдаемый
# симптом: воркер `<defunct>`, backend ждёт ответа вечно).
# ---------------------------------------------------------------------------

class TestGigaAMTimeoutKillEscalation(unittest.TestCase):
    """_timeout_kill эскалирует до kill(), если terminate() не убил процесс."""

    def test_escalates_to_kill_when_terminate_does_not_die(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        import subprocess as sp

        session = _GigaAMSubprocessSession.__new__(_GigaAMSubprocessSession)
        session.oom_callback = None
        session._error_bus = None

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # процесс всё ещё жив на входе
        mock_proc.wait.side_effect = sp.TimeoutExpired(cmd="worker", timeout=1.0)
        session._proc = mock_proc

        session._timeout_kill()

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once()
        mock_proc.kill.assert_called_once()

    def test_no_escalation_when_terminate_succeeds(self):
        """wait() не бросает TimeoutExpired — SIGTERM сработал, kill() лишний."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession

        session = _GigaAMSubprocessSession.__new__(_GigaAMSubprocessSession)
        session.oom_callback = None
        session._error_bus = None

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0  # процесс вышел сам, wait() не таймаутит
        session._proc = mock_proc

        session._timeout_kill()

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once()
        mock_proc.kill.assert_not_called()

    def test_never_raises_when_wait_or_kill_error(self):
        """Timer-тред: любое исключение здесь молча теряется, никогда не всплывает."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        import subprocess as sp

        session = _GigaAMSubprocessSession.__new__(_GigaAMSubprocessSession)
        session.oom_callback = None
        session._error_bus = None

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = sp.TimeoutExpired(cmd="worker", timeout=1.0)
        mock_proc.kill.side_effect = ProcessLookupError("already gone")
        session._proc = mock_proc

        session._timeout_kill()  # не должен бросить


# ---------------------------------------------------------------------------
# Wave 1742 — GigaAM worker subprocess PATH must include ffmpeg directory
# Regression guard for launchd minimal-PATH bug:
#   REST server starts with PATH=/usr/bin:/bin:/usr/sbin:/sbin
#   → gigaam internal bare-ffmpeg call → FileNotFoundError
#   → STTRouter.warmup_gigaam ошибка warmup (103×) → все движки вышли из строя (48×)
# ---------------------------------------------------------------------------

class TestGigaAMSubprocessFfmpegPath(unittest.TestCase):
    """W1742: GigaAM worker subprocess must receive a PATH that includes ffmpeg dir."""

    def _capture_popen_env(self) -> dict:
        """Start a session under minimal launchd PATH, capture the env passed to Popen."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession

        captured_env: dict = {}

        def fake_popen_factory(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return _FakePopen([_ok_load_response()])

        # Simulate launchd minimal PATH — no /opt/homebrew/bin, no ffmpeg in PATH.
        minimal_path = "/usr/bin:/bin:/usr/sbin:/sbin"

        with patch.dict(os.environ, {"PATH": minimal_path}, clear=False):
            with patch("core.pipeline.stt_gigaam.subprocess.Popen",
                       side_effect=fake_popen_factory):
                sess = _GigaAMSubprocessSession(
                    "/fake/py", "/fake/w.py", "rnnt", "cpu"
                )
                self.addCleanup(sess.close)
                sess.start()

        return captured_env

    def test_ffmpeg_dir_injected_when_only_homebrew_path_exists(self):
        """If /opt/homebrew/bin/ffmpeg exists, its dir must appear in worker PATH."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession

        captured_env: dict = {}

        def fake_popen_factory(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return _FakePopen([_ok_load_response()])

        def mock_resolve_ffmpeg_dir():
            return "/opt/homebrew/bin"

        minimal_path = "/usr/bin:/bin:/usr/sbin:/sbin"

        with patch("core.pipeline.stt_gigaam._resolve_ffmpeg_dir",
                   side_effect=mock_resolve_ffmpeg_dir):
            with patch.dict(os.environ, {"PATH": minimal_path}, clear=False):
                with patch("core.pipeline.stt_gigaam.subprocess.Popen",
                           side_effect=fake_popen_factory):
                    sess = _GigaAMSubprocessSession(
                        "/fake/py", "/fake/w.py", "rnnt", "cpu"
                    )
                    self.addCleanup(sess.close)
                    sess.start()

        worker_path_dirs = captured_env.get("PATH", "").split(os.pathsep)
        self.assertIn(
            "/opt/homebrew/bin",
            worker_path_dirs,
            f"Worker PATH must contain /opt/homebrew/bin; got: {captured_env.get('PATH')!r}",
        )

    def test_ffmpeg_dir_prepended_before_minimal_path(self):
        """ffmpeg dir must appear BEFORE the existing minimal PATH entries (prepend, not append)."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession

        captured_env: dict = {}

        def fake_popen_factory(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return _FakePopen([_ok_load_response()])

        def mock_resolve():
            return "/opt/homebrew/bin"

        minimal_path = "/usr/bin:/bin"

        with patch("core.pipeline.stt_gigaam._resolve_ffmpeg_dir", side_effect=mock_resolve):
            with patch.dict(os.environ, {"PATH": minimal_path}, clear=False):
                with patch("core.pipeline.stt_gigaam.subprocess.Popen",
                           side_effect=fake_popen_factory):
                    sess = _GigaAMSubprocessSession(
                        "/fake/py", "/fake/w.py", "rnnt", "cpu"
                    )
                    self.addCleanup(sess.close)
                    sess.start()

        worker_path = captured_env.get("PATH", "")
        # /opt/homebrew/bin must come BEFORE /usr/bin
        idx_homebrew = worker_path.find("/opt/homebrew/bin")
        idx_usr_bin = worker_path.find("/usr/bin")
        self.assertGreater(idx_usr_bin, -1, "PATH must contain /usr/bin")
        self.assertGreater(idx_homebrew, -1, "PATH must contain /opt/homebrew/bin")
        self.assertLess(
            idx_homebrew, idx_usr_bin,
            f"ffmpeg dir must precede /usr/bin in PATH; got: {worker_path!r}",
        )

    def test_no_duplication_when_ffmpeg_already_in_path(self):
        """If ffmpeg dir is already in PATH, it must NOT be duplicated."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession

        captured_env: dict = {}

        def fake_popen_factory(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return _FakePopen([_ok_load_response()])

        def mock_resolve():
            return "/opt/homebrew/bin"

        # PATH already includes /opt/homebrew/bin.
        existing_path = "/opt/homebrew/bin:/usr/bin:/bin"

        with patch("core.pipeline.stt_gigaam._resolve_ffmpeg_dir", side_effect=mock_resolve):
            with patch.dict(os.environ, {"PATH": existing_path}, clear=False):
                with patch("core.pipeline.stt_gigaam.subprocess.Popen",
                           side_effect=fake_popen_factory):
                    sess = _GigaAMSubprocessSession(
                        "/fake/py", "/fake/w.py", "rnnt", "cpu"
                    )
                    self.addCleanup(sess.close)
                    sess.start()

        worker_path = captured_env.get("PATH", "")
        count = worker_path.split(os.pathsep).count("/opt/homebrew/bin")
        self.assertEqual(
            count, 1,
            f"/opt/homebrew/bin should appear exactly once in PATH; got: {worker_path!r}",
        )

    def test_path_not_clobbered_when_ffmpeg_not_found(self):
        """If ffmpeg cannot be resolved, the existing PATH must be passed through unchanged."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession

        captured_env: dict = {}

        def fake_popen_factory(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return _FakePopen([_ok_load_response()])

        def mock_resolve_none():
            return None

        original_path = "/usr/bin:/bin"

        with patch("core.pipeline.stt_gigaam._resolve_ffmpeg_dir",
                   side_effect=mock_resolve_none):
            with patch.dict(os.environ, {"PATH": original_path}, clear=False):
                with patch("core.pipeline.stt_gigaam.subprocess.Popen",
                           side_effect=fake_popen_factory):
                    sess = _GigaAMSubprocessSession(
                        "/fake/py", "/fake/w.py", "rnnt", "cpu"
                    )
                    self.addCleanup(sess.close)
                    sess.start()

        # PATH should still be set (from os.environ.copy()) but not modified.
        self.assertEqual(
            captured_env.get("PATH"), original_path,
            "When ffmpeg dir is None, PATH must be preserved as-is",
        )


class TestResolveFfmpegDir(unittest.TestCase):
    """Unit tests for the _resolve_ffmpeg_dir() helper."""

    def test_returns_directory_when_shutil_which_finds_ffmpeg(self):
        """If shutil.which finds ffmpeg, returns its directory."""
        from core.pipeline.stt_gigaam import _resolve_ffmpeg_dir
        with patch("core.pipeline.stt_gigaam.shutil.which",
                   return_value="/opt/homebrew/bin/ffmpeg"):
            result = _resolve_ffmpeg_dir()
        self.assertEqual(result, "/opt/homebrew/bin")

    def test_falls_back_to_homebrew_when_which_returns_none(self):
        """Falls back to /opt/homebrew/bin when shutil.which returns None."""
        from core.pipeline.stt_gigaam import _resolve_ffmpeg_dir
        with patch("core.pipeline.stt_gigaam.shutil.which", return_value=None):
            with patch("core.pipeline.stt_gigaam.os.path.isfile", return_value=True):
                with patch("core.pipeline.stt_gigaam.os.access", return_value=True):
                    result = _resolve_ffmpeg_dir()
        self.assertEqual(result, "/opt/homebrew/bin")

    def test_returns_none_when_ffmpeg_not_found_anywhere(self):
        """Returns None when ffmpeg is not in PATH or any candidate path."""
        from core.pipeline.stt_gigaam import _resolve_ffmpeg_dir
        with patch("core.pipeline.stt_gigaam.shutil.which", return_value=None):
            with patch("core.pipeline.stt_gigaam.os.path.isfile", return_value=False):
                result = _resolve_ffmpeg_dir()
        self.assertIsNone(result)

    def test_never_raises(self):
        """_resolve_ffmpeg_dir() must never raise, even if shutil.which blows up."""
        from core.pipeline.stt_gigaam import _resolve_ffmpeg_dir
        with patch("core.pipeline.stt_gigaam.shutil.which",
                   side_effect=RuntimeError("unexpected")):
            result = _resolve_ffmpeg_dir()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Живой инцидент 2026-08-13 — тихая смерть воркера в простое между диктовками
#
# _check_proc_oom_on_exit() (OOM-диагностика: exit code + stderr ring →
# oom_callback → ErrorBus/Sentry) вызывается ТОЛЬКО из _send() при пустом
# ответе — то есть только когда запрос был В ПОЛЁТЕ. Если воркер умирает
# ПРОСТАИВАЯ (между диктовками, никакой _send() не выполняется), респавн-путь
# _get_subprocess_session() (W1216 F1) просто видит is_loaded()==False и зовёт
# close() — который НИКОГДА не читает exit code/stderr ring. Диагностика молча
# теряется: ни oom_callback, ни ErrorBus, ни Sentry. Живой пример: воркер исчез
# за ~1ч44м простоя без единой строки WARNING/ERROR в логе; следующая диктовка
# просто тихо пересоздала воркера (holodный старт 10.95с → 1 переполнение
# буфера), причина смерти осталась неизвестной навсегда.
# ---------------------------------------------------------------------------

class TestIdleDeathOomDiagnosis(unittest.TestCase):
    """Респавн после тихой смерти в простое обязан прогнать OOM-диагностику
    ДО того, как close() отбросит exit code и stderr ring."""

    def test_respawn_after_idle_death_runs_oom_diagnosis(self):
        """W1216 F1 respawn path must diagnose the dead session before discarding it."""
        from core.pipeline.stt_gigaam import GigaAMAdapter, _GigaAMSubprocessSession

        # Воркер уже мёртв ДО первого вызова _get_subprocess_session в этом тесте —
        # смоделированная idle-смерть: poll() возвращает -9 (SIGKILL),
        # но НИКАКОЙ _send() не выполнялся (никто не заметил в момент смерти).
        dead_session = _GigaAMSubprocessSession.__new__(_GigaAMSubprocessSession)
        dead_session._loaded = True
        dead_proc = MagicMock()
        dead_proc.poll.return_value = -9  # SIGKILL — kernel OOM killer signature
        dead_proc.stdin = MagicMock()
        dead_proc.stdin.closed = False
        dead_session._proc = dead_proc
        dead_session._stderr_ring = []
        dead_session._error_bus = None
        oom_calls = []
        dead_session.oom_callback = lambda name, rc, stderr: oom_calls.append((name, rc, stderr))

        adapter = GigaAMAdapter(
            device="cpu",
            mode="rnnt",
            transport="subprocess",
            venv_python_path="/usr/bin/python3",
        )
        self.addCleanup(adapter.close)
        adapter._subprocess = dead_session

        fake_popen = _FakePopen([_ok_load_response()])
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            with patch("core.pipeline.stt_gigaam.os.path.exists", return_value=True):
                adapter._get_subprocess_session()

        self.assertEqual(
            len(oom_calls), 1,
            "idle-death respawn must run OOM diagnosis on the just-discovered-dead "
            "session BEFORE close() throws away its exit code and stderr ring",
        )
        self.assertEqual(oom_calls[0][:2], ("gigaam_worker", -9))


if __name__ == "__main__":
    unittest.main()
