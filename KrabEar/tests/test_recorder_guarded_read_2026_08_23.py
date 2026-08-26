"""Guarded read в рекордере диктовки (T4 спеки PortAudio).

🔴 Тот же корень, что у wake-word: `recorder.py` читал микрофон блокирующим
`stream.read()` (= `Pa_ReadStream` = usleep-цикл без таймаута). Если CoreAudio
не запустил IO-поток, воркер висел вечно, `stop()` таймаутил, и владелец видел
`recorder_timeout` — 81 раз за 30 суток, вплоть до потери диктовки.

Политика при голодании ПОСРЕДИ записи (спека §4.5): НЕ переоткрывать
(дыра в аудио + рассинхрон spill), а завершить запись по образцу
max-duration: собрать `_pending_result`, закрыть spill и ГРОМКО сообщить —
иначе владелец продолжит диктовать в умерший захват.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recorder import AudioRecorder  # noqa: E402


class _StarvedStream:
    """Открылся, но кадров не отдаёт (мёртвый с рождения)."""

    def __init__(self):
        self.read_calls = 0
        self._blocked = threading.Event()

    @property
    def read_available(self) -> int:
        return 0

    def read(self, frames):
        self.read_calls += 1
        self._blocked.wait(30.0)          # вечная блокировка, как в проде
        return (np.zeros((frames, 1), dtype=np.float32), False)


class _LiveStream(_StarvedStream):
    @property
    def read_available(self) -> int:
        return 8192

    def read(self, frames):
        self.read_calls += 1
        return (np.full((frames, 1), 0.1, dtype=np.float32), False)


def _cm(stream):
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=stream)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _recorder(**kw) -> AudioRecorder:
    rec = AudioRecorder(
        sample_rate=16000,
        settings_get=lambda k, d=None: {
            "audio_stream_starve_sec": 0.3,
            "audio_read_poll_sec": 0.01,
        }.get(k, d),
        **kw,
    )
    rec._error_bus = MagicMock()
    return rec


class RecorderStarvationTest(unittest.TestCase):
    def test_starved_stream_does_not_hang_worker(self):
        """Корень: воркер обязан выйти сам, а не висеть до stop()-таймаута."""
        rec = _recorder()
        stream = _StarvedStream()
        with patch("sounddevice.InputStream", return_value=_cm(stream)):
            rec.start()
            time.sleep(1.2)                     # заведомо дольше starve_sec
            worker_alive = rec.is_worker_thread_alive
        self.assertFalse(worker_alive, "воркер обязан выйти по голоданию")
        self.assertEqual(stream.read_calls, 0, "read() не звали без кадров")

    def test_starvation_pushes_loud_error(self):
        """Владелец обязан узнать, что захват умер, а не диктовать в пустоту."""
        rec = _recorder()
        with patch("sounddevice.InputStream", return_value=_cm(_StarvedStream())):
            rec.start()
            time.sleep(1.2)
        codes = [
            c.args[0].code for c in rec._error_bus.push.call_args_list
            if c.args and hasattr(c.args[0], "code")
        ]
        self.assertIn("audio.capture_starved", codes)

    def test_recorded_audio_survives_starvation(self):
        """Накопленное до смерти стрима аудио не теряется (как в max-duration)."""
        rec = _recorder()
        live = _LiveStream()

        class _FlipStream(_LiveStream):
            def __init__(self):
                super().__init__()
                self.started = time.monotonic()

            @property
            def read_available(self) -> int:
                # первые 0.3с кадры идут, потом стрим «умирает»
                return 8192 if time.monotonic() - self.started < 0.3 else 0

        with patch("sounddevice.InputStream", return_value=_cm(_FlipStream())):
            rec.start()
            time.sleep(1.2)
            audio, duration = rec.stop() or (None, 0.0)
        self.assertIsNotNone(audio, "аудио до голодания обязано вернуться")
        self.assertGreater(len(audio), 0)
        del live

    def test_live_stream_records_normally(self):
        """Анти-регресс: здоровый стрим пишет как раньше."""
        rec = _recorder()
        stream = _LiveStream()
        with patch("sounddevice.InputStream", return_value=_cm(stream)):
            rec.start()
            time.sleep(0.3)
            result = rec.stop()
        self.assertIsNotNone(result)
        self.assertGreater(stream.read_calls, 0)


if __name__ == "__main__":
    unittest.main()
