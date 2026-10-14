"""Поздний watchdog не убивает следующий запрос того же MLX worker.

Только управляемые Timer/pipe и отдельный stdlib JSON echo-child из temp.
Ни ML, ни аудиоустройства, ни production-файлы здесь не используются.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import mlx_whisper_session as worker


class _ControlledTimer:
    def __init__(self, interval, function, args=None, kwargs=None):
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.cancelled = False
        self.entered = threading.Event()
        self.resume = threading.Event()

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function(*self.args, **self.kwargs)

    def enter_before_callback(self):
        # Timer.run уже прошёл проверку finished; cancel больше не поможет.
        self.entered.set()
        if not self.resume.wait(10):
            raise AssertionError("synthetic timer was not released")
        self.fire()


class _WhisperWatchdogFixture(unittest.TestCase):
    def setUp(self):
        self.timers = []

        def timer(*args, **kwargs):
            instance = _ControlledTimer(*args, **kwargs)
            self.timers.append(instance)
            return instance

        thread_api = SimpleNamespace(
            Lock=threading.Lock, Thread=threading.Thread, Timer=timer,
        )
        self.timer_patch = patch.object(worker, "threading", thread_api)
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)

    @staticmethod
    def _reply(text):
        return json.dumps({"ok": True, "result": {"text": text}}) + "\n"

    def _fake_session(self, readline):
        session = worker.MLXWhisperSession()
        proc = Mock()
        proc.poll.return_value = None
        proc.stdout.readline.side_effect = readline
        proc.stdin.closed = False
        session._proc = proc
        self.addCleanup(session.close)
        return session, proc

    def _call(self, session, name, results, errors, params=None):
        try:
            results.append(session.transcribe(
                "synthetic.wav", params or {}, 60.0, name,
            ))
        except BaseException as exc:
            errors.append(exc)


class WhisperWatchdogGenerationTest(_WhisperWatchdogFixture):
    def test_entered_old_timer_does_not_kill_same_process_next_request(self):
        reading_second = threading.Event()
        finish_second = threading.Event()
        old_callback = None
        reads = 0

        def read():
            nonlocal old_callback, reads
            reads += 1
            if reads == 1:
                old_callback = threading.Thread(
                    target=self.timers[0].enter_before_callback, daemon=True,
                )
                old_callback.start()
                self.assertTrue(self.timers[0].entered.wait(5))
                return self._reply("first")
            reading_second.set()
            self.assertTrue(finish_second.wait(5))
            return "" if proc.kill.called else self._reply("second")

        session, proc = self._fake_session(read)
        proc.kill.side_effect = finish_second.set
        first = session.transcribe("synthetic.wav", {}, 60.0, "first")
        self.assertEqual(first["text"], "first")
        self.assertTrue(self.timers[0].cancelled)
        results, errors = [], []
        second = threading.Thread(
            target=self._call, args=(session, "second", results, errors), daemon=True,
        )
        second.start()
        try:
            self.assertTrue(reading_second.wait(5))
            self.timers[0].resume.set()
            old_callback.join(5)
            self.assertFalse(old_callback.is_alive())
            self.assertEqual(proc.kill.call_count, 0)
        finally:
            self.timers[0].resume.set()
            finish_second.set()
            old_callback.join(5)
            second.join(5)
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [{"text": "second"}])

    def test_cancelled_timer_after_completed_request_is_harmless(self):
        session, proc = self._fake_session(lambda: self._reply("first"))
        self.assertEqual(session.transcribe("synthetic.wav", {}, 60.0, "first"), {
            "text": "first",
        })
        self.timers[0].fire()
        proc.kill.assert_not_called()

    def test_current_timer_kills_and_classifies_timeout(self):
        def read():
            self.timers[0].fire()
            return ""

        session, proc = self._fake_session(read)
        with self.assertRaises(worker.MLXTimeoutError) as caught:
            session.transcribe("synthetic.wav", {}, 60.0, "current")
        self.assertEqual(caught.exception.model_name, "current")
        self.assertEqual(proc.kill.call_count, 1)


_ECHO_WORKER = '''import json
import sys
import time
from pathlib import Path

for line in sys.stdin:
    request = json.loads(line)
    if request.get("op") == "shutdown":
        break
    params = request["params"]
    Path(params["entered"]).touch()
    while not Path(params["release"]).exists():
        time.sleep(0.005)
    print(json.dumps({"ok": True, "result": {"text": params["name"]}}), flush=True)
'''


class WhisperWatchdogPipeIntegrationTest(_WhisperWatchdogFixture):
    """Настоящие Popen/stdin/stdout/session с безопасным stdlib child."""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="whisper-pipe-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        script = self.directory / "lifecycle_echo_worker.py"
        script.write_text(_ECHO_WORKER, encoding="utf-8")
        self.script_patch = patch.object(worker, "_WORKER_SCRIPT", script)
        self.script_patch.start()
        self.addCleanup(self.script_patch.stop)
        self.session = worker.MLXWhisperSession()
        self.session.start()
        self.proc = self.session._proc
        self.threads = []
        self.releases = []
        self.addCleanup(self._drain_child)

    def _params(self, name):
        entered = self.directory / (name + ".entered")
        release = self.directory / (name + ".release")
        self.releases.append(release)
        return {"name": name, "entered": str(entered), "release": str(release)}

    def _start_call(self, name):
        params = self._params(name)
        results, errors = [], []
        thread = threading.Thread(
            target=self._call,
            args=(self.session, name, results, errors, params), daemon=True,
        )
        self.threads.append(thread)
        thread.start()
        deadline = time.monotonic() + 10
        while not Path(params["entered"]).exists() and time.monotonic() < deadline:
            if not thread.is_alive():
                break
            time.sleep(0.005)
        self.assertTrue(Path(params["entered"]).exists(), errors)
        return thread, results, errors, Path(params["release"])

    def _drain_child(self):
        for release in self.releases:
            release.touch()
        for timer in self.timers:
            timer.resume.set()
        for thread in self.threads:
            thread.join(5)
        self.session.close(timeout=1.0)
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=3)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if pipe is not None:
                pipe.close()
        drain = self.session._stderr_drain_thread
        if drain is not None:
            drain.join(3)
            self.assertFalse(drain.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in self.threads))

    def test_real_pipe_same_child_survives_old_callback_and_answers_next_request(self):
        first, first_results, first_errors, first_release = self._start_call("first")
        callback = threading.Thread(
            target=self.timers[0].enter_before_callback, daemon=True,
        )
        self.threads.append(callback)
        callback.start()
        self.assertTrue(self.timers[0].entered.wait(5))
        first_release.touch()
        first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(first_results, [{"text": "first"}])
        self.assertTrue(self.timers[0].cancelled)

        second, results, errors, release = self._start_call("second")
        self.timers[0].resume.set()
        callback.join(5)
        self.assertFalse(callback.is_alive())
        release.touch()
        second.join(5)
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [{"text": "second"}])
        self.assertIsNone(self.proc.poll())
        self.assertIs(self.session._proc, self.proc)

    def test_real_pipe_current_timeout_terminates_owned_child(self):
        thread, results, errors, _release = self._start_call("blocked")
        self.timers[0].fire()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], worker.MLXTimeoutError)
        self.assertEqual(errors[0].model_name, "blocked")
        self.assertIsNotNone(self.proc.poll())
