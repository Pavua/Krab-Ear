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
        self.stderr = MagicMock()
        self.terminate_called = False
        self.kill_called = False
        self.wait_called = False

    def _close_stdin(self) -> None:
        self._stdin_closed = True

    def poll(self):  # noqa: D401 — match Popen API
        return self._poll_value

    def wait(self, timeout=None):
        self.wait_called = True
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
            sess.start()
        self.assertTrue(sess.is_loaded())
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
            with self.assertRaises(RuntimeError) as ctx:
                sess.start()
        self.assertIn("load failed", str(ctx.exception))
        self.assertFalse(sess.is_loaded())

    def test_start_idempotent(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        fake_popen = _FakePopen([_ok_load_response()])
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen) as p:
            sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
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
            sess.start()
            with self.assertRaises(RuntimeError) as ctx:
                sess.transcribe("/tmp/x.wav")
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_close_sends_shutdown_and_waits(self):
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        fake_popen = _FakePopen([_ok_load_response()])
        with patch("core.pipeline.stt_gigaam.subprocess.Popen", return_value=fake_popen):
            sess = _GigaAMSubprocessSession("/fake/py", "/fake/w.py", "rnnt", "cpu")
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


if __name__ == "__main__":
    unittest.main()
