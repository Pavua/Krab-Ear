"""pause()/resume() у RealtimePartialTranscriber (C2a, спека §2.2).

Во время паузы воркер пропускает итерации цикла (snapshot_audio не
вызывается, новые emit'ы не публикуются) — Metal-констрейнт: на время
LLM/диар-вызова партиалы молчат. Примечание: pause() не является
синхронным барьером — единственная уже стартовавшая до pause() итерация
может дойти до своего emit уже после того, как pause() вернул управление
(см. докстринг RealtimePartialTranscriber.pause()).
"""
import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.realtime_partial import RealtimePartialTranscriber  # noqa: E402


class _SpyRecorder:
    def __init__(self) -> None:
        self.snapshot_calls = 0

    def snapshot_audio(self, max_duration_sec: float = 8.0):
        self.snapshot_calls += 1
        return np.ones(16000, dtype=np.float32), 1.0


class _SpyTranscriber:
    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return {"text": "чанк"}


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def emit(self, event_type: str, payload: dict) -> None:
        with self.lock:
            self.events.append((event_type, payload))


class PartialPauseTestCase(unittest.TestCase):
    def _make(self) -> tuple[RealtimePartialTranscriber, _SpyRecorder, _SpyBus]:
        rec, bus = _SpyRecorder(), _SpyBus()
        rt = RealtimePartialTranscriber(
            transcriber=_SpyTranscriber(), recorder=rec, event_bus=bus,
            interval_sec=0.05, buffer_sec=1.0, privacy_getter=lambda: False,
        )
        return rt, rec, bus

    def test_pause_stops_snapshots_resume_restarts(self) -> None:
        rt, rec, _ = self._make()
        rt.start(session_id="s1", sample_rate=16000)
        try:
            time.sleep(0.3)
            self.assertGreater(rec.snapshot_calls, 0, "до паузы воркер должен работать")

            rt.pause()
            time.sleep(0.15)  # дать текущей итерации дожить
            calls_at_pause = rec.snapshot_calls
            time.sleep(0.3)
            self.assertEqual(
                rec.snapshot_calls, calls_at_pause,
                "во время паузы snapshot_audio не должен вызываться",
            )

            rt.resume()
            time.sleep(0.3)
            self.assertGreater(rec.snapshot_calls, calls_at_pause,
                               "после resume воркер должен продолжить")
        finally:
            rt.stop(timeout_sec=5.0)

    def test_pause_is_idempotent_and_stop_works_while_paused(self) -> None:
        rt, _, _ = self._make()
        rt.start(session_id="s2", sample_rate=16000)
        rt.pause()
        rt.pause()  # повторный вызов — no-op
        rt.stop(timeout_sec=5.0)  # stop из паузы не должен зависнуть
        self.assertTrue(True)

    def test_unresumed_pause_does_not_leak_into_next_session(self) -> None:
        """Регрессия: pause() без resume() перед stop()/следующим start() не
        должен «застревать» и молчать всю следующую сессию записи.

        Сценарий из ревью: caller делает pause() перед LLM/диар-вызовом (как
        описано в докстринге pause()), но resume() не срабатывает на каком-то
        пути (исключение до resume() или забытый вызов). Тот же долгоживущий
        RealtimePartialTranscriber затем stop()/start()'ится для СЛЕДУЮЩЕЙ
        сессии — новая сессия обязана работать с чистого листа.
        """
        rt, rec, _ = self._make()
        rt.start(session_id="s1", sample_rate=16000)
        time.sleep(0.15)
        rt.pause()  # намеренно НЕ resume()
        rt.stop(timeout_sec=5.0)

        rec.snapshot_calls = 0  # изолируем счётчик от первой сессии
        rt.start(session_id="s2", sample_rate=16000)
        try:
            time.sleep(0.3)
            self.assertGreater(
                rec.snapshot_calls, 0,
                "новая сессия не должна наследовать паузу от предыдущей",
            )
        finally:
            rt.stop(timeout_sec=5.0)


if __name__ == "__main__":
    unittest.main()
