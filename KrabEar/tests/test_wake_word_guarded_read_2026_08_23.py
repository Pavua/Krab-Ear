"""Guarded read в `_listen_loop`: тред обязан быть прерываемым (T2).

Спека: docs/superpowers/specs/2026-08-23-portaudio-unkillable-read-design.md.

Живая улика класса (sample 2026-08-22): тред 2360/2360 сэмплов внутри
`ReadStream → usleep`, ноль CoreAudio IO-тредов в процессе. `stop_event`
проверялся только МЕЖДУ чтениями, поэтому зависшее чтение делало тред
неубиваемым, `stop()` возвращал False, и единственным лекарством оставался
рестарт backend-процесса (61% случаев за 30 суток).
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.openwakeword_adapter import OpenWakeWordAdapter  # noqa: E402


class _DeadStream:
    """Стрим, открытый успешно, но НЕ поставляющий кадры (целевой класс).

    `read()` здесь имитирует `Pa_ReadStream`: блокируется навсегда. Тест,
    который его дождётся, повиснет — это и есть проверяемое поведение.
    """

    def __init__(self):
        self.read_calls = 0
        self.time = 5000.0
        self._blocked = threading.Event()

    @property
    def read_available(self) -> int:
        self.time += 0.01     # 🔴 тикает даже у мёртвого стрима (замер 08-23)
        return 0

    def read(self, frames):
        self.read_calls += 1
        self._blocked.wait(30.0)          # вечная блокировка, как в проде
        return (MagicMock(), False)


class _LiveStream(_DeadStream):
    @property
    def read_available(self) -> int:
        self.time += 0.01
        return 4096

    def read(self, frames):
        self.read_calls += 1
        import numpy as np
        return (np.full((frames, 1), 7, dtype="int16"), False)


def _cm(stream):
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=stream)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _make_adapter(**kw) -> OpenWakeWordAdapter:
    settings = {"audio_stream_starve_sec": 0.3, "audio_read_poll_sec": 0.01}
    settings.update(kw.pop("settings", {}))
    import tempfile
    adapter = OpenWakeWordAdapter(
        data_dir=Path(tempfile.mkdtemp(prefix="wwguard_")),
        settings_get=lambda k, d: settings.get(k, d),
        **kw,
    )
    adapter._oww = MagicMock()
    adapter._oww.predict.return_value = {}
    adapter._active_model = "krab_ru"
    return adapter


def _run_loop(adapter: OpenWakeWordAdapter, timeout: float = 5.0) -> threading.Thread:
    th = threading.Thread(
        target=adapter._listen_loop,
        kwargs={"threshold": 0.5, "chunk_size": 1280,
                "sample_rate": 16000, "generation": adapter._generation},
        daemon=True,
    )
    th.start()
    th.join(timeout=timeout)
    return th


class GuardedReadExitsTest(unittest.TestCase):
    def test_dead_stream_does_not_block_thread_forever(self):
        """Корень класса: мёртвый стрим → тред ВЫХОДИТ, а не висит вечно."""
        adapter = _make_adapter()
        stream = _DeadStream()
        with patch("sounddevice.InputStream", return_value=_cm(stream)):
            th = _run_loop(adapter, timeout=5.0)
        self.assertFalse(th.is_alive(), "тред обязан выйти по голоданию")
        self.assertEqual(stream.read_calls, 0, "read() не звали без кадров")

    def test_starvation_is_visible_in_heartbeat(self):
        """Watchdog обязан отличать голодание от «чистой паузы» (§4.3)."""
        adapter = _make_adapter()
        with patch("sounddevice.InputStream", return_value=_cm(_DeadStream())):
            _run_loop(adapter, timeout=5.0)
        hb = adapter.heartbeat()
        self.assertTrue(hb.get("starvation_active"))
        self.assertGreaterEqual(hb.get("consecutive_starve_exits", 0), 1)

    def test_starvation_state_survives_session_cleanup(self):
        """§4.3: cleanup сессии НЕ смеет стирать причину выхода."""
        adapter = _make_adapter()
        with patch("sounddevice.InputStream", return_value=_cm(_DeadStream())):
            _run_loop(adapter, timeout=5.0)
        self.assertIsNone(adapter.active_model(), "сессия очищена")
        self.assertTrue(adapter.heartbeat().get("starvation_active"),
                        "но признак голодания сохранён для watchdog")

    def test_public_stop_clears_starvation_state(self):
        """🔴 §4.3 (HIGH ре-ревью): выключенный владельцем слушатель не смеет
        дозревать до wedged по застывшему флагу."""
        adapter = _make_adapter()
        with patch("sounddevice.InputStream", return_value=_cm(_DeadStream())):
            _run_loop(adapter, timeout=5.0)
        self.assertTrue(adapter.heartbeat().get("starvation_active"))
        adapter.stop()
        hb = adapter.heartbeat()
        self.assertFalse(hb.get("starvation_active"))
        self.assertEqual(hb.get("consecutive_starve_exits", 0), 0)

    def test_live_chunk_resets_starve_counter(self):
        """§4.3 п.4: живой чанк обнуляет лестницу."""
        adapter = _make_adapter()
        adapter._consecutive_starve_exits = 2
        stream = _LiveStream()
        with patch("sounddevice.InputStream", return_value=_cm(stream)):
            th = threading.Thread(
                target=adapter._listen_loop,
                kwargs={"threshold": 0.5, "chunk_size": 1280,
                        "sample_rate": 16000, "generation": adapter._generation},
                daemon=True,
            )
            th.start()
            time.sleep(0.4)
            adapter._stop_event.set()
            th.join(timeout=5.0)
        self.assertEqual(adapter.heartbeat().get("consecutive_starve_exits", -1), 0)

    def test_starvation_suppressed_during_recording(self):
        """🔴 §4.2: meeting-запись не снимает слушатель — голодание легитимно."""
        recording = {"on": False}   # старт цикла требует is_recording()==False:
        # mid-flight re-check (волна 2026-08-01) иначе не откроет тап вообще.
        adapter = _make_adapter(is_recording=lambda: recording["on"])
        stream = _DeadStream()
        with patch("sounddevice.InputStream", return_value=_cm(stream)):
            th = threading.Thread(
                target=adapter._listen_loop,
                kwargs={"threshold": 0.5, "chunk_size": 1280,
                        "sample_rate": 16000, "generation": adapter._generation},
                daemon=True,
            )
            th.start()
            time.sleep(0.05)          # даём открыть тап
            recording["on"] = True    # владелец начал встречу/диктовку
            time.sleep(1.0)           # заведомо дольше starve_sec=0.3
            alive_during_recording = th.is_alive()
            adapter._stop_event.set()
            th.join(timeout=5.0)
        self.assertTrue(alive_during_recording,
                        "под записью голодание НЕ считается клином")
        self.assertFalse(adapter.heartbeat().get("starvation_active"))

    def test_killswitch_restores_blocking_read(self):
        """§9: рубильник возвращает прежнее поведение целиком."""
        adapter = _make_adapter(settings={"audio_guarded_read_enabled": False})
        stream = _LiveStream()
        with patch("sounddevice.InputStream", return_value=_cm(stream)):
            th = threading.Thread(
                target=adapter._listen_loop,
                kwargs={"threshold": 0.5, "chunk_size": 1280,
                        "sample_rate": 16000, "generation": adapter._generation},
                daemon=True,
            )
            th.start()
            time.sleep(0.3)
            adapter._stop_event.set()
            th.join(timeout=5.0)
        self.assertGreater(stream.read_calls, 0)


if __name__ == "__main__":
    unittest.main()
