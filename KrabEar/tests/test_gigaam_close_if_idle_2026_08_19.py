"""Тесты для GigaAMAdapter.close_if_idle (Memory Conductor T2b, C-INFLIGHT).

Спека: docs/superpowers/specs/2026-08-19-memory-conductor-design.md §3 C-INFLIGHT —
gigaam evict идёт под собственным _spawn_lock с in-flight-проверкой (тот же лок,
что уже сериализует spawn subprocess-воркера, W1216 F2).

Три обязательных сценария:
    (a) close_if_idle() → False пока идёт transcribe (inflight != 0), даже если
        last_used_ts формально «древний» — in-flight всегда выигрывает.
    (b) close_if_idle() → True + реально зовёт close(), когда адаптер загружен
        и простаивает ≥ порога.
    (c) close_if_idle() → False без побочных эффектов, если адаптер не загружен.

Плюс race-тест: reaper не может выселить адаптер в узком окне между входом в
transcribe() и стартом самой транскрипции (round-2 finding спеки, C-INFLIGHT).

Все тесты мокируют subprocess-сессию — реальный GigaAM worker не запускается.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_gigaam_close_if_idle_2026_08_19.py -v
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock

import numpy as np

# Настройка PYTHONPATH для standalone-запуска
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_adapter(transport: str = "subprocess"):
    """Return a GigaAMAdapter configured for subprocess transport (no real spawn)."""
    from core.pipeline.stt_gigaam import GigaAMAdapter
    return GigaAMAdapter(device="cpu", mode="rnnt", transport=transport)


def _make_mock_session(loaded: bool = True) -> MagicMock:
    """Return a MagicMock that mimics _GigaAMSubprocessSession."""
    session = MagicMock()
    session.is_loaded.return_value = loaded
    session.transcribe.return_value = {"ok": True, "text": "тест", "engine": "gigaam-rnnt"}
    session.oom_callback = None

    def _close():
        session.is_loaded.return_value = False

    session.close.side_effect = _close
    return session


class CloseIfIdleBasicsTestCase(unittest.TestCase):
    """Три обязательных сценария из брифа T2b."""

    def test_false_while_transcribe_inflight(self):
        """(a) inflight != 0 блокирует close_if_idle, даже при «древнем» last_used_ts."""
        adapter = _make_adapter()
        adapter._subprocess = _make_mock_session(loaded=True)
        adapter.inflight = 1
        adapter.last_used_ts = time.monotonic() - 10_000.0  # давно неактивен

        result = adapter.close_if_idle(idle_sec=0.0)

        self.assertFalse(result)
        adapter._subprocess.close.assert_not_called()
        self.assertIsNotNone(adapter._subprocess)

    def test_true_and_closes_when_loaded_and_idle(self):
        """(b) loaded + inflight==0 + простой >= порога → True, close() реально вызван."""
        adapter = _make_adapter()
        mock_session = _make_mock_session(loaded=True)
        adapter._subprocess = mock_session
        adapter.inflight = 0
        adapter.last_used_ts = time.monotonic() - 1000.0

        result = adapter.close_if_idle(idle_sec=1.0)

        self.assertTrue(result)
        mock_session.close.assert_called_once()
        self.assertIsNone(adapter._subprocess)

    def test_false_when_not_loaded_no_side_effects(self):
        """(c) не загружен → False, без исключений и без side-эффектов."""
        adapter = _make_adapter()
        self.assertFalse(adapter.is_loaded())

        result = adapter.close_if_idle(idle_sec=0.0)

        self.assertFalse(result)
        self.assertIsNone(adapter._subprocess)
        self.assertIsNone(adapter._model)

    def test_false_when_idle_below_threshold(self):
        """Загружен, ничего не в работе, но простой ЕЩЁ не достиг порога → False."""
        adapter = _make_adapter()
        mock_session = _make_mock_session(loaded=True)
        adapter._subprocess = mock_session
        adapter.inflight = 0
        adapter.last_used_ts = time.monotonic()  # только что использовался

        result = adapter.close_if_idle(idle_sec=600.0)

        self.assertFalse(result)
        mock_session.close.assert_not_called()


class TranscribeInflightBookkeepingTestCase(unittest.TestCase):
    """transcribe() обязан инкрементировать/декрементировать inflight и
    обновлять last_used_ts под _spawn_lock на входе/выходе (C-INFLIGHT)."""

    def _make_audio(self, seconds: float = 0.05) -> np.ndarray:
        t = np.linspace(0, seconds, int(seconds * 16000), dtype=np.float32)
        return np.sin(2 * np.pi * 440 * t).astype(np.float32)

    def test_inflight_returns_to_zero_after_transcribe(self):
        adapter = _make_adapter()
        adapter._transcribe_subprocess = MagicMock(return_value=("текст", "gigaam-rnnt"))

        before_ts = adapter.last_used_ts
        result = adapter.transcribe(self._make_audio(), sample_rate=16000)

        self.assertEqual(result["text"], "текст")
        self.assertEqual(adapter.inflight, 0)
        self.assertGreaterEqual(adapter.last_used_ts, before_ts)

    def test_reaper_cannot_evict_during_inflight_window(self):
        """Race: close_if_idle(), вызванный ПОКА transcribe() ещё внутри
        (после инкремента inflight, до завершения работы), обязан вернуть False.

        Округляет узкое окно из спеки: "reaper firing in that gap kills a
        just-started request" — bump last_used_ts/inflight на ВХОДЕ, не после.
        """
        adapter = _make_adapter()
        started = threading.Event()
        release = threading.Event()

        def _slow_transcribe(*_args, **_kwargs):
            started.set()
            release.wait(timeout=5.0)
            return ("текст", "gigaam-rnnt")

        adapter._transcribe_subprocess = MagicMock(side_effect=_slow_transcribe)
        adapter.last_used_ts = time.monotonic() - 10_000.0  # формально «давно простаивал»

        result_holder: dict = {}

        def _run_transcribe():
            result_holder["result"] = adapter.transcribe(self._make_audio(), sample_rate=16000)

        worker = threading.Thread(target=_run_transcribe, daemon=True)
        worker.start()
        self.assertTrue(started.wait(timeout=5.0), "transcribe() не стартовал вовремя")

        # На этом этапе inflight должен быть уже забампан (C-INFLIGHT round-2 fix).
        evicted = adapter.close_if_idle(idle_sec=0.0)
        self.assertFalse(evicted, "reaper не должен выселять адаптер во время in-flight работы")

        release.set()
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(adapter.inflight, 0)


if __name__ == "__main__":
    unittest.main()
