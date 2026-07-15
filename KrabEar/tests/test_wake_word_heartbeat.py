"""Heartbeat/generation/stop()->bool в OpenWakeWordAdapter (спека 2026-07-15).

_listen_loop вызывается СИНХРОННО с фейковым sounddevice-модулем,
вставленным в sys.modules ОБРАТИМО (setUp/tearDown).
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
BACKEND_DIR = _PROJECT_ROOT / "KrabEar"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from backend.openwakeword_adapter import OpenWakeWordAdapter  # noqa: E402


class _FakeStream:
    """Контекст-менеджер, эмулирующий sd.InputStream: отдаёт заготовленные
    чанки; когда чанки кончились — взводит stop_event и отдаёт нули."""

    def __init__(self, chunks, stop_event):
        self._chunks = list(chunks)
        self._stop_event = stop_event
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        self.reads += 1
        if not self._chunks:
            self._stop_event.set()
            return np.zeros((n, 1), dtype=np.int16), False
        return self._chunks.pop(0), False


class _FakeOWW:
    def predict(self, arr):
        return {}


def _nonzero_chunk(n=4):
    a = np.zeros((n, 1), dtype=np.int16)
    a[0][0] = 7
    return a


def _zero_chunk(n=4):
    return np.zeros((n, 1), dtype=np.int16)


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        self._sd_was_present = "sounddevice" in sys.modules
        self._sd_saved = sys.modules.get("sounddevice")
        self.fake_sd = types.ModuleType("sounddevice")
        sys.modules["sounddevice"] = self.fake_sd

    def tearDown(self):
        if self._sd_was_present:
            sys.modules["sounddevice"] = self._sd_saved
        else:
            sys.modules.pop("sounddevice", None)

    def _run_loop(self, chunks, generation=None):
        """Синхронный прогон _listen_loop с фейковым стримом."""
        self.adapter._stop_event.clear()
        stream = _FakeStream(chunks, self.adapter._stop_event)
        self.fake_sd.InputStream = lambda **kw: stream
        self.adapter._oww = _FakeOWW()
        gen = generation if generation is not None else self.adapter._generation
        self.adapter._listen_loop(
            threshold=0.5, chunk_size=4, sample_rate=16000, generation=gen,
        )
        return stream

    def test_nonzero_chunk_stamps_heartbeat(self):
        self._run_loop([_nonzero_chunk()])
        hb = self.adapter.heartbeat()
        self.assertIsNotNone(hb["listen_started_ts"])
        self.assertIsNotNone(hb["last_chunk_ts"])

    def test_zero_chunks_do_not_stamp_heartbeat(self):
        self._run_loop([_zero_chunk(), _zero_chunk()])
        hb = self.adapter.heartbeat()
        self.assertIsNotNone(hb["listen_started_ts"])
        self.assertIsNone(hb["last_chunk_ts"])

    def test_stale_generation_exits_loop_early(self):
        # Поколение адаптера ушло вперёд — «зомби»-тред обязан выйти,
        # не дочитав все чанки (проверяем по счётчику reads: 1, не 3).
        self.adapter._generation = 5
        stream = self._run_loop(
            [_nonzero_chunk(), _nonzero_chunk(), _nonzero_chunk()],
            generation=4,
        )
        self.assertEqual(stream.reads, 1)

    def test_start_resets_heartbeat_and_wedged(self):
        self.adapter._last_chunk_ts = 123.0
        self.adapter._listen_started_ts = 120.0
        self.adapter.set_wedged(True)
        # Прямой вызов внутренностей start() невозможен без библиотеки —
        # проверяем контракт через _reset_session_state(), который start()
        # обязан вызывать под локом.
        self.adapter._reset_session_state()
        hb = self.adapter.heartbeat()
        self.assertIsNone(hb["last_chunk_ts"])
        self.assertIsNone(hb["listen_started_ts"])
        self.assertFalse(self.adapter.is_wedged())


class _FakeThreadCleanExit:
    """Duck-type (НЕ наследник threading.Thread — atexit-hang правило).
    Жив до join, после join — вышел."""

    def __init__(self):
        self._alive = True
        self.join_timeout = None

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.join_timeout = timeout
        self._alive = False


class _FakeThreadHung(_FakeThreadCleanExit):
    def join(self, timeout=None):
        self.join_timeout = timeout  # остаётся _alive=True


class StopReturnsBoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)

    def test_stop_returns_true_when_not_running(self):
        self.assertTrue(self.adapter.stop())

    def test_stop_returns_true_on_clean_exit(self):
        self.adapter._thread = _FakeThreadCleanExit()
        self.assertTrue(self.adapter.stop())

    def test_stop_returns_false_when_thread_hung(self):
        fake = _FakeThreadHung()
        self.adapter._thread = fake
        self.assertFalse(self.adapter.stop())
        self.assertEqual(fake.join_timeout, 3.0)


class StatusFieldsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)

    def test_status_contains_additive_fields(self):
        result = self.adapter.handle_wake_word_status({})
        self.assertTrue(result["ok"])
        self.assertIn("last_chunk_ts", result)
        self.assertIn("listen_started_ts", result)
        self.assertIn("wedged", result)
        self.assertFalse(result["wedged"])

    def test_set_wedged_roundtrip(self):
        self.adapter.set_wedged(True)
        self.assertTrue(self.adapter.is_wedged())
        self.assertTrue(self.adapter.handle_wake_word_status({})["wedged"])
        self.adapter.set_wedged(False)
        self.assertFalse(self.adapter.is_wedged())


if __name__ == "__main__":
    unittest.main()
