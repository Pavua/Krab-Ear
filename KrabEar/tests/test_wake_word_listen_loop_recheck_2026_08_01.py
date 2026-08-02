"""Re-check активной записи ПЕРЕД открытием микрофонного тапа в `_listen_loop`.

Находка adversarial-ревью (Fable, 2026-08-01), подтверждённая по коду и по
логу живого инцидента.

Гейт F6 (`handle_wake_word_start`) проверяет `is_recording` в НАЧАЛЕ старта, а
реальный `sd.InputStream(...)` открывается позже — уже в фоновом
`_listen_loop`, после синхронной загрузки модели. Между этими точками проходит
не микросекунда, а вся загрузка `OWWModel` (кэша нет, каждый `start()` строит
модель заново). Лог инцидента даёт точный размер окна:

    02:35:21 OpenWakeWordAdapter: уже запущен, сначала stop()   <- гейт пройден
    02:35:37 OpenWakeWordAdapter: запущен                        <- тап открыт

Шестнадцать секунд. Владелец диктовал подряд («терял диктовки одну за
другой»), и запись, начатая ВНУТРИ окна, встречала уже разрешённый старт:
адаптер открывал второй тап на то же устройство → worker AudioRecorder висел
насмерть → recorder_timeout → диктовка потеряна.

Штатная пауза поллера от этого не спасает: `wake_word_stop` ждёт на
`adapter._lock`, который держит загрузка модели, а `_listen_loop` сначала
открывал стрим и только потом проверял `_stop_event` (первая проверка стояла
внутри цикла, ПОСЛЕ `with sd.InputStream(...)`). Даже кратковременный
open/close второго тапа воспроизводит конфликт.

Лекарство — mid-flight re-check непосредственно перед открытием, ровно тот
паттерн, что уже применён в `audio_reinit.py` перед `Pa_Terminate`
(«is_recording re-check танца»). Направление отказа — FAIL-CLOSED, как у F6:
не знаем состояние → тап не открываем, поллер ретрайнет своим циклом.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.openwakeword_adapter import OpenWakeWordAdapter  # noqa: E402


def _make_stream_cm() -> MagicMock:
    """Фейковый `sd.InputStream` — фиксирует сам факт открытия."""
    stream = MagicMock()
    stream.read.return_value = (MagicMock(), False)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=stream)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _run_listen_loop(adapter: OpenWakeWordAdapter, timeout: float = 3.0) -> None:
    thread = threading.Thread(
        target=adapter._listen_loop,
        kwargs={
            "threshold": 0.5,
            "chunk_size": 1280,
            "sample_rate": 16000,
            "generation": adapter._generation,
        },
        daemon=True,
    )
    thread.start()
    thread.join(timeout=timeout)
    adapter._stop_event.set()


class TestListenLoopRechecksRecording(unittest.TestCase):
    """Запись, начавшаяся ПОСЛЕ гейта, обязана отменить открытие тапа."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def _adapter(self, is_recording) -> OpenWakeWordAdapter:
        adapter = OpenWakeWordAdapter(
            data_dir=self.tmp,
            settings_get=lambda k, d: {"wake_word_enabled": True}.get(k, d),
            is_recording=is_recording,
        )
        adapter._oww_available = True
        adapter._oww = MagicMock(predict=MagicMock(return_value={}))
        return adapter

    def test_stream_not_opened_when_recording_started_during_model_load(self) -> None:
        """Классический сценарий инцидента: запись стартовала внутри окна загрузки."""
        adapter = self._adapter(is_recording=lambda: True)

        with patch("sounddevice.InputStream") as mock_stream:
            mock_stream.return_value = _make_stream_cm()
            _run_listen_loop(adapter)

        mock_stream.assert_not_called()

    def test_stream_opens_normally_when_idle(self) -> None:
        """Без записи поведение прежнее — тап открывается."""
        adapter = self._adapter(is_recording=lambda: False)

        with patch("sounddevice.InputStream") as mock_stream:
            mock_stream.return_value = _make_stream_cm()
            adapter._stop_event.clear()
            thread = threading.Thread(
                target=adapter._listen_loop,
                kwargs={
                    "threshold": 0.5,
                    "chunk_size": 1280,
                    "sample_rate": 16000,
                    "generation": adapter._generation,
                },
                daemon=True,
            )
            thread.start()
            # Дать циклу дойти до открытия, затем остановить.
            for _ in range(100):
                if mock_stream.called:
                    break
                threading.Event().wait(0.02)
            adapter._stop_event.set()
            thread.join(timeout=3.0)

        mock_stream.assert_called()

    def test_recheck_fails_closed_when_callback_raises(self) -> None:
        """Сбой колбэка = «идёт запись»: тап не открываем (симметрично F6)."""

        def _boom() -> bool:
            raise RuntimeError("recorder недоступен")

        adapter = self._adapter(is_recording=_boom)

        with patch("sounddevice.InputStream") as mock_stream:
            mock_stream.return_value = _make_stream_cm()
            _run_listen_loop(adapter)

        mock_stream.assert_not_called()

    def test_no_callback_keeps_legacy_behaviour(self) -> None:
        """Без колбэка (старый вызывающий код) re-check не мешает работе."""
        adapter = self._adapter(is_recording=None)

        with patch("sounddevice.InputStream") as mock_stream:
            mock_stream.return_value = _make_stream_cm()
            adapter._stop_event.clear()
            thread = threading.Thread(
                target=adapter._listen_loop,
                kwargs={
                    "threshold": 0.5,
                    "chunk_size": 1280,
                    "sample_rate": 16000,
                    "generation": adapter._generation,
                },
                daemon=True,
            )
            thread.start()
            for _ in range(100):
                if mock_stream.called:
                    break
                threading.Event().wait(0.02)
            adapter._stop_event.set()
            thread.join(timeout=3.0)

        mock_stream.assert_called()

    def test_stop_requested_before_open_skips_stream(self) -> None:
        """Уже взведённый _stop_event тоже обязан отменить открытие тапа."""
        adapter = self._adapter(is_recording=lambda: False)
        adapter._stop_event.set()

        with patch("sounddevice.InputStream") as mock_stream:
            mock_stream.return_value = _make_stream_cm()
            thread = threading.Thread(
                target=adapter._listen_loop,
                kwargs={
                    "threshold": 0.5,
                    "chunk_size": 1280,
                    "sample_rate": 16000,
                    "generation": adapter._generation,
                },
                daemon=True,
            )
            thread.start()
            thread.join(timeout=3.0)

        mock_stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
