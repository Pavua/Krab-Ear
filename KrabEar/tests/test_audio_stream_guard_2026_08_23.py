"""RED-тесты guarded read (спека 2026-08-23-portaudio-unkillable-read-design, T1).

Корень класса: `stream.read()` = `Pa_ReadStream` = usleep-цикл БЕЗ таймаута.
Если CoreAudio не запустил IO-поток, чтение висит вечно и `stop_event`
(проверяемый только МЕЖДУ чтениями) становится недостижим.

Фейки — честные duck-type объекты с `read_available` (спека §7: fail-open
на не-int запрещён, MagicMock тут не годится: `MagicMock() < int` = TypeError).
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _FakeStream:
    """Duck-type sd.InputStream: управляемая доступность кадров."""

    def __init__(self, *, available: int = 0, raises: BaseException | None = None):
        self._available = available
        self._raises = raises
        self.read_calls = 0
        self.time = 1000.0  # тикает всегда — доказано живым замером, см. §4.2

    @property
    def read_available(self) -> int:
        self.time += 0.01  # 🔴 растёт даже на мёртвом стриме
        if self._raises is not None:
            raise self._raises
        return self._available

    def set_available(self, value: int) -> None:
        self._available = value

    def read(self, frames: int):
        self.read_calls += 1
        return (b"\x00" * frames, False)


def _guard():
    import importlib
    return importlib.import_module("core.audio_stream_guard")


class WaitForFramesTest(unittest.TestCase):
    def test_returns_true_when_frames_already_available(self):
        g = _guard()
        st = _FakeStream(available=2048)
        stop = threading.Event()
        self.assertTrue(g.wait_for_frames(st, 1280, stop_event=stop))

    def test_does_not_call_read_while_buffer_is_short(self):
        """Инвариант §4.1: read() не зовём без данных — иначе он заблокируется."""
        g = _guard()
        st = _FakeStream(available=0)
        stop = threading.Event()
        threading.Timer(0.15, stop.set).start()
        g.wait_for_frames(st, 1280, stop_event=stop, poll_sec=0.01)
        self.assertEqual(st.read_calls, 0)

    def test_stop_event_during_starvation_exits_fast(self):
        """Цель §2.1: выход из read-цикла за <= 2x poll_sec."""
        g = _guard()
        st = _FakeStream(available=0)
        stop = threading.Event()
        threading.Timer(0.05, stop.set).start()
        t0 = time.monotonic()
        result = g.wait_for_frames(st, 1280, stop_event=stop, poll_sec=0.02)
        elapsed = time.monotonic() - t0
        self.assertFalse(result)
        self.assertLess(elapsed, 1.0, "цикл обязан выйти сразу по stop_event")

    def test_starved_stream_raises_after_threshold(self):
        """§4.2: read_available == 0 дольше starve_sec → StreamStarved."""
        g = _guard()
        st = _FakeStream(available=0)
        stop = threading.Event()
        with self.assertRaises(g.StreamStarved):
            g.wait_for_frames(st, 1280, stop_event=stop, poll_sec=0.01, starve_sec=0.2)

    def test_recording_gate_suppresses_starvation(self):
        """🔴 §4.2: meeting-запись НЕ снимает слушатель — голодание легитимно.

        Без этого гейта детектор открыл бы второй тап под записью (инцидент F6).
        """
        g = _guard()
        st = _FakeStream(available=0)
        stop = threading.Event()
        threading.Timer(0.4, stop.set).start()
        result = g.wait_for_frames(
            st, 1280, stop_event=stop, poll_sec=0.01, starve_sec=0.1,
            is_recording=lambda: True,
        )
        self.assertFalse(result, "выход по stop_event, а не по голоданию")

    def test_recording_gate_fail_safe_on_callback_error(self):
        """is_recording() упал → считаем, что запись идёт (fail-safe §4.2)."""
        g = _guard()
        st = _FakeStream(available=0)
        stop = threading.Event()
        threading.Timer(0.4, stop.set).start()

        def _boom():
            raise RuntimeError("датчик записи упал")

        result = g.wait_for_frames(
            st, 1280, stop_event=stop, poll_sec=0.01, starve_sec=0.1,
            is_recording=_boom,
        )
        self.assertFalse(result)

    def test_read_available_exception_counts_as_starvation(self):
        """§4.4: PortAudioError из read_available — голодание, НЕ fail-open."""
        g = _guard()
        st = _FakeStream(raises=OSError("PortAudioError: -9986"))
        stop = threading.Event()
        with self.assertRaises(g.StreamStarved):
            g.wait_for_frames(st, 1280, stop_event=stop, poll_sec=0.01, starve_sec=0.15)

    def test_fresh_frames_reset_starvation_timer(self):
        """§4.2: легитимная тишина (буфер наполняется) детектор не будит."""
        g = _guard()
        st = _FakeStream(available=0)
        stop = threading.Event()

        def _feed():
            time.sleep(0.12)
            st.set_available(4096)

        threading.Thread(target=_feed, daemon=True).start()
        self.assertTrue(
            g.wait_for_frames(st, 1280, stop_event=stop, poll_sec=0.01, starve_sec=0.5)
        )

    def test_module_imports_without_sounddevice(self):
        """T1: ubuntu-CI без PortAudio обязан импортировать модуль."""
        import subprocess
        code = (
            "import sys; sys.modules['sounddevice'] = None;"
            "import core.audio_stream_guard as g; print(bool(g.wait_for_frames))"
        )
        res = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            env={"PYTHONPATH": str(PROJECT_ROOT), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("True", res.stdout)


if __name__ == "__main__":
    unittest.main()
