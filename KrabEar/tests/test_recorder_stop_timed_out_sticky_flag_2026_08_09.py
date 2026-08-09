"""Sticky `is_stop_timed_out` — NEW-5 (MEDIUM), третий раунд адверсариального
ревью PortAudio-фикса #12 (2026-08-09).

## Разбор

`BackendService._reinit_is_worker_hung_gate()` выводил «worker-тред
рекордера заклинил» из комбинации ``not is_recording and
is_worker_thread_alive`` (см. ``test_recorder_worker_alive_safety_2026_08_09.py``,
NEW-1-фикс второго раунда). Но эту же комбинацию на короткое время даёт
КАЖДОЕ обычное завершение записи, не только настоящий заклин:

- ``AudioRecorder.stop()`` выставляет ``self._is_recording = False`` ДО
  попытки ``thread.join()`` (см. ``recorder.py::stop``, строка ~250) — всё
  окно ожидания ``thread.join(timeout_sec)`` (0-150мс на обычном stop(),
  секунды на max-duration авто-стопе с большим буфером, пока воркер
  дособирает ``np.concatenate`` вне лока) worker физически ещё жив, а
  ``is_recording`` уже упал.
- Если ЛЮБОЙ триггер reinit (``WakeWordWatchdog`` тик 5с) сэмплирует оба
  сигнала именно в этом окне — старый гейт классифицировал бы это как
  ``DEFERRED_WORKER_HUNG`` и потенциально эскалировал ``wedged:true`` →
  Swift-агент делает ``kickstart -k`` backend ПОСРЕДИ обычной финализации
  записи (особенно рискованно для meeting-записей, которые долго держат
  recorder занятым в финализации).

## Фикс

Sticky ``AudioRecorder.is_stop_timed_out`` — True ТОЛЬКО в ветке, где
``stop()`` реально кинул ``AudioRecorderStopTimeout`` после полного
ожидания таймаута (не выводимый из комбинации is_recording/
is_worker_thread_alive). Снимается следующим ``start()`` и успешным
``stop()``/``abort()``. ``_reinit_is_worker_hung_gate()`` переключается на
этот сигнал напрямую.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.recorder import AudioRecorder, AudioRecorderStopTimeout  # noqa: E402


class StopTimedOutFlagBasicsTest(unittest.TestCase):
    """Базовые состояния sticky-флага."""

    def test_false_initially(self) -> None:
        rec = AudioRecorder()
        self.assertFalse(rec.is_stop_timed_out)

    def test_true_after_stop_raises_timeout(self) -> None:
        rec = AudioRecorder()
        release = threading.Event()
        stuck_thread = threading.Thread(target=release.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._started_at = time.monotonic()
            rec._thread = stuck_thread
        stuck_thread.start()
        self.addCleanup(stuck_thread.join, 1.0)
        self.addCleanup(release.set)

        with self.assertRaises(AudioRecorderStopTimeout):
            rec.stop(timeout_sec=0.01)

        self.assertTrue(
            rec.is_stop_timed_out,
            "sticky-флаг обязан подняться РОВНО в ветке AudioRecorderStopTimeout",
        )


class StopTimedOutFlagNoFalsePositiveTest(unittest.TestCase):
    """🔴 Ядро NEW-5: обычное (не заклинившее) завершение не должно поднимать флаг."""

    def test_not_set_during_ordinary_stop_join_window(self) -> None:
        """Воспроизводим ровно то самое окно: is_recording уже False (stop()
        выставил его до join), worker физически ещё жив (join ещё не
        вернулся) — но это НЕ заклин, воркер просто чуть медленно выходит.
        Старый гейт (комбинация is_recording/is_worker_thread_alive) увидел
        бы здесь «заклинило»; sticky-флаг обязан остаться False."""
        rec = AudioRecorder()
        exit_gate = threading.Event()
        worker = threading.Thread(target=exit_gate.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._started_at = time.monotonic()
            rec._thread = worker
        worker.start()
        self.addCleanup(worker.join, 1.0)
        self.addCleanup(exit_gate.set)

        stop_outcome: dict = {}

        def _run_stop() -> None:
            stop_outcome["result"] = rec.stop(timeout_sec=2.0)

        stop_thread = threading.Thread(target=_run_stop, daemon=True)
        stop_thread.start()
        try:
            # Дождаться (poll, не фиксированный sleep — устойчивее к
            # перегруженному CI, см. Fable-ревью LOW), пока stop() выставит
            # is_recording=False и войдёт в join(), пока worker всё ещё жив
            # (обычное окно, не заклин).
            deadline = time.monotonic() + 2.0
            while rec.is_recording and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertFalse(
                rec.is_recording, "предпосылка: stop() уже сбросил is_recording"
            )
            with rec._lock:
                self.assertTrue(
                    rec._thread is not None and rec._thread.is_alive(),
                    "предпосылка: worker физически ещё жив — воспроизводим "
                    "именно то окно, где старый гейт ложно срабатывал",
                )
            self.assertFalse(
                rec.is_stop_timed_out,
                "NEW-5: обычное окно join() внутри здорового stop() не "
                "должно поднимать флаг — только реальный "
                "AudioRecorderStopTimeout вправе это делать",
            )
        finally:
            exit_gate.set()
            stop_thread.join(timeout=2.0)

        self.assertFalse(rec.is_stop_timed_out)
        self.assertIsNotNone(stop_outcome.get("result"))


class StopTimedOutFlagClearedTest(unittest.TestCase):
    """Снятие sticky-флага следующим start() и успешным stop()/abort()."""

    def test_cleared_by_next_start(self) -> None:
        rec = AudioRecorder()
        with rec._lock:
            rec._stop_timed_out = True
        self.assertTrue(rec.start())
        self.assertFalse(rec.is_stop_timed_out)
        rec.stop()

    def test_cleared_by_pending_result_early_return_branch(self) -> None:
        """Mutation-coverage (Fable-ревью LOW): ветка early-return с уже
        готовым ``_pending_result`` (авто-стоп по max-duration, повторный
        ``stop()`` после него) должна САМА снимать sticky-флаг — не
        полагаясь на то, что его снимет какая-то другая ветка."""
        rec = AudioRecorder()
        fake_audio = np.array([0.1, 0.2], dtype=np.float32)
        with rec._lock:
            rec._stop_timed_out = True
            rec._pending_result = (fake_audio, 1.23)
            rec._is_recording = False
            rec._thread = None
        result = rec.stop()
        self.assertIsNotNone(result)
        self.assertFalse(rec.is_stop_timed_out)

    def test_cleared_by_normal_join_success_path_with_real_chunks(self) -> None:
        """Mutation-coverage (Fable-ревью LOW): обычный успешный ``stop()``
        с реальными накопленными чанками (join() вернулся, worker мёртв) —
        путь, ОТДЕЛЬНЫЙ от обеих early-return-веток выше."""
        rec = AudioRecorder()
        release = threading.Event()
        worker = threading.Thread(target=release.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._started_at = time.monotonic()
            rec._thread = worker
            rec._chunks = [np.array([0.1, 0.2], dtype=np.float32)]
            rec._chunks_total_samples = 2
            # Протухший флаг от несвязанного прошлого инцидента — этот
            # успешный stop() обязан его снять.
            rec._stop_timed_out = True
        worker.start()
        release.set()

        audio, _duration = rec.stop(timeout_sec=1.0)
        self.assertEqual(audio.size, 2)
        self.assertFalse(rec.is_stop_timed_out)

    def test_cleared_by_successful_stop_after_prior_timeout(self) -> None:
        rec = AudioRecorder()
        release = threading.Event()
        stuck_thread = threading.Thread(target=release.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._started_at = time.monotonic()
            rec._thread = stuck_thread
        stuck_thread.start()

        with self.assertRaises(AudioRecorderStopTimeout):
            rec.stop(timeout_sec=0.01)
        self.assertTrue(rec.is_stop_timed_out)

        # Заклинивший worker в итоге доживает сам по себе.
        release.set()
        stuck_thread.join(timeout=1.0)
        self.assertFalse(stuck_thread.is_alive())

        rec.stop(timeout_sec=1.0)
        self.assertFalse(
            rec.is_stop_timed_out,
            "успешный повторный stop() после того, как воркер сам "
            "доиграл до конца, обязан снять sticky-флаг",
        )

    def test_true_after_abort_itself_times_out(self) -> None:
        """Fable-ревью, третий раунд (MEDIUM): ``abort()`` достижим НЕ ТОЛЬКО
        на shutdown, но и из рантайм-компенсации ошибки старта встречи
        (``MeetingSessionService`` → ``abort_recording_if_owner``) и
        recovery при её close (``meeting_session_service.py:400,967`` →
        ``recording_core_service.py:2203``). Заклин, обнаруженный ЧЕРЕЗ
        ``abort()``, обязан поднимать тот же sticky-флаг — симметрично
        exception-ветке ``stop()`` — иначе он навсегда классифицируется как
        ``DEFERRED_RECORDING`` вместо ``DEFERRED_WORKER_HUNG`` и
        ``WakeWordWatchdog`` молчит вечно (F1-класс инцидента 2026-08-07)."""
        rec = AudioRecorder()
        release = threading.Event()
        stuck_thread = threading.Thread(target=release.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._started_at = time.monotonic()
            rec._thread = stuck_thread
        stuck_thread.start()
        self.addCleanup(stuck_thread.join, 1.0)
        self.addCleanup(release.set)

        ok = rec.abort(timeout_sec=0.01)
        self.assertFalse(ok, "предпосылка: abort() сам таймаутит, воркер жив")
        self.assertTrue(
            rec.is_stop_timed_out,
            "заклин, пойманный через abort(), обязан поднимать sticky-флаг "
            "симметрично stop()",
        )

    def test_cleared_by_successful_abort_after_prior_timeout(self) -> None:
        rec = AudioRecorder()
        release = threading.Event()
        stuck_thread = threading.Thread(target=release.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._started_at = time.monotonic()
            rec._thread = stuck_thread
        stuck_thread.start()

        with self.assertRaises(AudioRecorderStopTimeout):
            rec.stop(timeout_sec=0.01)
        self.assertTrue(rec.is_stop_timed_out)

        release.set()
        ok = rec.abort(timeout_sec=1.0)
        self.assertTrue(ok)
        self.assertFalse(
            rec.is_stop_timed_out,
            "успешный abort() после прежнего таймаута обязан снять sticky-флаг",
        )


class ReinitIsWorkerHungGateUsesStickyFlagTest(unittest.TestCase):
    """BackendService._reinit_is_worker_hung_gate читает ИМЕННО sticky-флаг,
    а не комбинацию is_recording/is_worker_thread_alive."""

    class _FakeRecorder:
        def __init__(self, is_recording: bool, is_worker_thread_alive: bool,
                     is_stop_timed_out: bool) -> None:
            self.is_recording = is_recording
            self.is_worker_thread_alive = is_worker_thread_alive
            self.is_stop_timed_out = is_stop_timed_out

    def _gate(self, **kwargs) -> bool:
        from backend.service import BackendService

        stub = type("Stub", (), {})()
        stub.recorder = self._FakeRecorder(**kwargs)
        return BackendService._reinit_is_worker_hung_gate(stub)

    def test_false_when_sticky_flag_clear_even_if_old_combo_would_fire(self) -> None:
        """🔴 Ядро фикса: старая комбинация (is_recording=False,
        is_worker_thread_alive=True) БОЛЬШЕ НЕ ДОЛЖНА сама по себе
        включать гейт — только sticky-флаг решает."""
        self.assertFalse(
            self._gate(
                is_recording=False,
                is_worker_thread_alive=True,
                is_stop_timed_out=False,
            )
        )

    def test_true_when_sticky_flag_set(self) -> None:
        self.assertTrue(
            self._gate(
                is_recording=False,
                is_worker_thread_alive=True,
                is_stop_timed_out=True,
            )
        )

    def test_false_during_healthy_active_recording(self) -> None:
        self.assertFalse(
            self._gate(
                is_recording=True,
                is_worker_thread_alive=True,
                is_stop_timed_out=False,
            )
        )

    def test_never_raises_when_recorder_attribute_missing(self) -> None:
        from backend.service import BackendService

        stub = type("Stub", (), {})()
        stub.recorder = object()
        self.assertFalse(BackendService._reinit_is_worker_hung_gate(stub))


if __name__ == "__main__":
    unittest.main()
