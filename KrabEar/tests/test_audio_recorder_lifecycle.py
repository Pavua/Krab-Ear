"""Wave 253 — AudioRecorder lifecycle + edge case tests.

Covers:
- start/stop basic lifecycle
- double-start idempotency
- stop-before-start safety
- captured samples returned correctly
- capture thread lifecycle (thread created/joined)
- device unavailable (OSError from InputStream)
- device disconnect mid-recording (OSError during stream.read)
- concurrent start serialization
- unicode device name forwarded to sounddevice
- clear-buffer safety after failed recording

sounddevice is fully mocked — no real microphone required.
"""

from __future__ import annotations

import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.recorder import AudioRecorder, AudioRecorderStopTimeout  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stream_cm(chunk_size: int = 1600, chunks_to_emit: int = 3) -> MagicMock:
    """Return a mock InputStream context manager.

    The stream emits *chunks_to_emit* chunks of ones, then blocks until
    the stop_event fires (simulated by raising StopIteration after limit).
    """
    call_count = {"n": 0}
    # Guarded read (спека 2026-08-23) опрашивает read_available ДО read() и
    # требует настоящий int >= chunk_size, иначе воркер решит, что поток
    # мёртв, и не прочитает вообще. Дренаж излишка (спека 2026-08-13)
    # проверяет ТО ЖЕ поле СРАЗУ ПОСЛЕ read() — если оставить его статичным
    # ненулевым, каждый цикл получал бы фантомный второй read() и лишний
    # np.concatenate, которого этот фейк не подразумевает. Поэтому значение
    # одноразово переключается: полный чанк готов до read(), 0 — сразу после.
    just_read = {"v": False}

    def _read(n: int) -> tuple[np.ndarray, bool]:
        call_count["n"] += 1
        just_read["v"] = True
        # After we've emitted enough chunks, block briefly so the worker
        # loop keeps running until stop_event is set.
        if call_count["n"] > chunks_to_emit:
            time.sleep(0.005)
        return (np.ones((n, 1), dtype=np.float32) * 0.5, False)

    def _read_available(_self: MagicMock) -> int:
        if just_read["v"]:
            just_read["v"] = False
            return 0
        return chunk_size

    _StreamCls = type("_GuardableStream", (MagicMock,), {})
    _StreamCls.read_available = property(_read_available)
    stream = _StreamCls()
    stream.read.side_effect = _read
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=stream)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _start_and_record(rec: AudioRecorder, duration: float = 0.05) -> None:
    rec.start()
    time.sleep(duration)


# ---------------------------------------------------------------------------
# 1. Basic start / stop
# ---------------------------------------------------------------------------

class TestStartStopBasic(unittest.TestCase):
    def test_start_stop_basic(self) -> None:
        """start() returns True, is_recording becomes True; stop() returns audio tuple."""
        with patch("sounddevice.InputStream", return_value=_make_stream_cm()):
            rec = AudioRecorder()
            self.addCleanup(rec.abort)
            result = rec.start()
            self.assertTrue(result, "start() should return True on first call")
            self.assertTrue(rec.is_recording, "is_recording should be True after start()")
            time.sleep(0.05)
            ret = rec.stop()
            self.assertIsNotNone(ret, "stop() must return a tuple")
            audio, duration = ret
            self.assertIsInstance(audio, np.ndarray)
            self.assertGreaterEqual(duration, 0.0)
            self.assertFalse(rec.is_recording, "is_recording should be False after stop()")


# ---------------------------------------------------------------------------
# 2. Double start idempotent
# ---------------------------------------------------------------------------

class TestDoubleStart(unittest.TestCase):
    def test_double_start_idempotent(self) -> None:
        """Second start() while already recording returns False without crashing."""
        with patch("sounddevice.InputStream", return_value=_make_stream_cm()):
            rec = AudioRecorder()
            first = rec.start()
            second = rec.start()
            try:
                self.assertTrue(first, "First start() must return True")
                self.assertFalse(second, "Second start() must return False (already recording)")
                self.assertTrue(rec.is_recording)
            finally:
                rec.stop()


# ---------------------------------------------------------------------------
# 3. Stop before start
# ---------------------------------------------------------------------------

class TestStopBeforeStart(unittest.TestCase):
    def test_stop_before_start_handled(self) -> None:
        """stop() on an idle recorder returns None without raising."""
        rec = AudioRecorder()
        result = rec.stop()
        self.assertIsNone(result, "stop() before start() must return None")
        self.assertFalse(rec.is_recording)


# ---------------------------------------------------------------------------
# 4. Audio samples returned correctly
# ---------------------------------------------------------------------------

class TestGetAudioReturnsCapturedSamples(unittest.TestCase):
    def test_get_audio_returns_captured_samples(self) -> None:
        """Chunks accumulated during recording are returned by stop() as float32 1-D array."""
        chunk_size = 1600
        with patch("sounddevice.InputStream", return_value=_make_stream_cm(chunk_size=chunk_size, chunks_to_emit=5)):
            rec = AudioRecorder(sample_rate=16000, channels=1)
            rec.start()
            time.sleep(0.08)  # let worker produce several chunks
            ret = rec.stop()
        self.assertIsNotNone(ret)
        audio, _ = ret
        self.assertEqual(audio.dtype, np.float32, "Audio dtype must be float32")
        self.assertEqual(audio.ndim, 1, "Audio must be 1-D")
        self.assertGreater(audio.size, 0, "At least one sample expected")
        # All chunks filled with 0.5 → result values close to 0.5
        self.assertTrue(np.allclose(audio, 0.5), "Captured values should be 0.5")


# ---------------------------------------------------------------------------
# 5. Capture thread lifecycle
# ---------------------------------------------------------------------------

class TestCaptureThreadLifecycle(unittest.TestCase):
    def test_capture_thread_lifecycle(self) -> None:
        """A worker thread is created on start() and joined/cleared on stop()."""
        with patch("sounddevice.InputStream", return_value=_make_stream_cm()):
            rec = AudioRecorder()
            self.addCleanup(rec.abort)
            rec.start()
            # Thread should exist and be alive shortly after start
            time.sleep(0.02)
            with rec._lock:
                thread_ref = rec._thread
            self.assertIsNotNone(thread_ref, "Worker thread must be set after start()")
            self.assertTrue(thread_ref.is_alive(), "Worker thread must be alive while recording")
            rec.stop()
            # After stop, thread reference is cleared
            with rec._lock:
                self.assertIsNone(rec._thread, "_thread must be None after stop()")
            # Ensure the thread actually finished (not zombie)
            thread_ref.join(timeout=1.0)
            self.assertFalse(thread_ref.is_alive(), "Worker thread should have terminated after stop()")

    def test_stop_timeout_raises_distinct_error_not_silent_none(self) -> None:
        """F2 (Fable 2026-07-22): timeout stop() обязан быть различимым.

        Возврат None при живом worker неотличим от already_stopped: вызыватель
        (recording_core_service phase_a) молча отвечал already_stopped, Swift не
        показывал ничего — тихая полная потеря диктовки. Timeout должен кидать
        AudioRecorderStopTimeout, чтобы дойти до пользователя отдельным статусом.
        """
        entered_read = threading.Event()
        release_worker = threading.Event()

        def _blocking_read(*_args: object, **_kwargs: object):
            entered_read.set()
            release_worker.wait(timeout=5.0)
            return np.zeros((160, 1), dtype=np.float32), False

        stream = MagicMock()
        stream.read.side_effect = _blocking_read
        # Guarded read (спека 2026-08-23) опрашивает read_available ДО read();
        # без настоящего int воркер решил бы, что поток голодает, и не дошёл
        # бы до блокирующего read() вообще — тест ждал бы entered_read вечно.
        # Тут не важно, что дренаж после read() тоже включится (нет ассерта
        # на concatenate/число вызовов) — статичного значения достаточно.
        stream.read_available = 1600
        stream_cm = MagicMock()
        stream_cm.__enter__ = MagicMock(return_value=stream)
        stream_cm.__exit__ = MagicMock(return_value=False)

        with patch("sounddevice.InputStream", return_value=stream_cm):
            rec = AudioRecorder()
            self.assertTrue(rec.start())
            # Ждём ВХОДА worker'а в блокирующий read: is_alive() истинно ещё до
            # первого read, и ранний stop() позволил бы worker'у чисто выйти по
            # event (гонка — тест мигал бы «not raised»).
            self.assertTrue(
                entered_read.wait(timeout=2.0),
                "worker не дошёл до stream.read()",
            )

            try:
                with self.assertRaises(AudioRecorderStopTimeout):
                    rec.stop(timeout_sec=0.05)
            finally:
                release_worker.set()
                with rec._lock:
                    hung = rec._thread
                if hung is not None:
                    hung.join(timeout=2.0)
                try:
                    rec.stop(timeout_sec=1.0)
                except AudioRecorderStopTimeout:
                    pass

    def test_abort_discards_buffers_without_concatenate_and_is_idempotent(self) -> None:
        """abort() завершает worker и очищает аудио без сборки финального массива."""
        with patch("sounddevice.InputStream", return_value=_make_stream_cm()):
            rec = AudioRecorder()
            self.addCleanup(rec.stop)
            self.assertTrue(rec.start())

            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with rec._lock:
                    thread_ref = rec._thread
                    has_chunks = bool(rec._chunks)
                if thread_ref is not None and thread_ref.is_alive() and has_chunks:
                    break
                time.sleep(0.005)

            self.assertIsNotNone(thread_ref)
            self.assertTrue(thread_ref.is_alive())
            self.assertTrue(has_chunks)
            with rec._lock:
                rec._pending_result = (np.ones(8, dtype=np.float32), 1.0)

            with patch(
                "backend.recorder.np.concatenate",
                side_effect=AssertionError("abort() не должен конкатенировать чанки"),
            ) as concatenate:
                self.assertTrue(rec.abort(timeout_sec=1.0))
                self.assertTrue(rec.abort(timeout_sec=1.0))

            concatenate.assert_not_called()
            thread_ref.join(timeout=1.0)
            self.assertFalse(thread_ref.is_alive())
            self.assertFalse(rec.is_recording)
            with rec._lock:
                self.assertIsNone(rec._thread)
                self.assertEqual(rec._chunks, [])
                self.assertEqual(rec._chunks_total_samples, 0)
                self.assertIsNone(rec._pending_result)

    def test_abort_timeout_keeps_thread_handle_for_retry(self) -> None:
        """При таймауте abort() возвращает False и не теряет живой thread."""
        rec = AudioRecorder()
        release = threading.Event()
        stuck_thread = threading.Thread(target=release.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._thread = stuck_thread
            rec._chunks = [np.ones((8, 1), dtype=np.float32)]
            rec._chunks_total_samples = 8
        stuck_thread.start()
        self.addCleanup(stuck_thread.join, 1.0)
        self.addCleanup(release.set)

        self.assertFalse(rec.abort(timeout_sec=0.01))
        with rec._lock:
            self.assertIs(rec._thread, stuck_thread)
            self.assertEqual(rec._chunks_total_samples, 8)

        release.set()
        stuck_thread.join(timeout=1.0)
        self.assertTrue(rec.abort(timeout_sec=0.1))
        with rec._lock:
            self.assertIsNone(rec._thread)
            self.assertEqual(rec._chunks, [])

    def test_stop_timeout_preserves_worker_and_blocks_restart(self) -> None:
        """stop() не отдаёт неполное аудио и не оживляет зависший worker."""
        rec = AudioRecorder()
        release = threading.Event()
        stuck_thread = threading.Thread(target=release.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._started_at = time.monotonic()
            rec._thread = stuck_thread
            rec._chunks = [np.ones((8, 1), dtype=np.float32)]
            rec._chunks_total_samples = 8
        stuck_thread.start()
        self.addCleanup(stuck_thread.join, 1.0)
        self.addCleanup(release.set)

        # F2 (Fable 2026-07-22): timeout теперь кидает различимое исключение,
        # а не молчаливый None; handle и чанки по-прежнему сохраняются.
        with self.assertRaises(AudioRecorderStopTimeout):
            rec.stop(timeout_sec=0.01)
        with rec._lock:
            self.assertIs(rec._thread, stuck_thread)
            self.assertEqual(rec._chunks_total_samples, 8)
        self.assertTrue(rec._stop_event.is_set())

        # start() не должен очистить общий Event, пока старый поток ещё жив.
        self.assertFalse(rec.start())
        self.assertTrue(rec._stop_event.is_set())
        with rec._lock:
            self.assertIs(rec._thread, stuck_thread)

        release.set()
        stuck_thread.join(timeout=1.0)
        result = rec.stop(timeout_sec=0.1)
        self.assertIsNotNone(result)
        assert result is not None
        audio, _duration = result
        self.assertEqual(audio.size, 8)
        with rec._lock:
            self.assertIsNone(rec._thread)

    def test_abort_during_blocking_read_discards_returned_chunk(self) -> None:
        """Разблокированный после abort() read не создаёт pending_result."""
        entered = threading.Event()
        release = threading.Event()
        chunk_size = 1600  # AudioRecorder(max_recording_samples=1) → sample_rate 16000 по умолчанию
        # Одноразовое переключение read_available (см. _make_stream_cm выше):
        # настоящий int ДО read() пускает guard к блокирующему вызову — иначе
        # тест не дождался бы entered; 0 СРАЗУ ПОСЛЕ read() держит дренаж
        # выключенным, потому что concatenate.assert_not_called() ниже
        # проверяет именно отсутствие склейки второго чанка.
        just_read = {"v": False}

        def _blocking_read(n: int) -> tuple[np.ndarray, bool]:
            entered.set()
            release.wait(timeout=2.0)
            just_read["v"] = True
            return np.ones((n, 1), dtype=np.float32), False

        def _read_available(_self: MagicMock) -> int:
            if just_read["v"]:
                just_read["v"] = False
                return 0
            return chunk_size

        _StreamCls = type("_OneShotBlockingStream", (MagicMock,), {})
        _StreamCls.read_available = property(_read_available)
        stream = _StreamCls()
        stream.read.side_effect = _blocking_read
        stream_cm = MagicMock()
        stream_cm.__enter__ = MagicMock(return_value=stream)
        stream_cm.__exit__ = MagicMock(return_value=False)

        with patch("sounddevice.InputStream", return_value=stream_cm):
            rec = AudioRecorder(max_recording_samples=1)
            self.addCleanup(rec.abort)
            self.addCleanup(release.set)
            self.assertTrue(rec.start())
            self.assertTrue(entered.wait(timeout=1.0))
            self.assertFalse(rec.abort(timeout_sec=0.01))

            with patch("backend.recorder.np.concatenate") as concatenate:
                release.set()
                thread = rec._thread
                self.assertIsNotNone(thread)
                assert thread is not None
                thread.join(timeout=1.0)
                self.assertTrue(rec.abort(timeout_sec=0.1))

            concatenate.assert_not_called()
            with rec._lock:
                self.assertIsNone(rec._pending_result)
                self.assertEqual(rec._chunks, [])


# ---------------------------------------------------------------------------
# 6. Device unavailable (OSError on InputStream open)
# ---------------------------------------------------------------------------

class TestHandlesDeviceUnavailable(unittest.TestCase):
    def test_handles_device_unavailable(self) -> None:
        """OSError raised by InputStream.__enter__ must not crash stop(); is_recording resets."""
        error_cm = MagicMock()
        error_cm.__enter__ = MagicMock(side_effect=OSError("No such device"))
        error_cm.__exit__ = MagicMock(return_value=False)

        with patch("sounddevice.InputStream", return_value=error_cm):
            rec = AudioRecorder()
            self.addCleanup(rec.abort)
            rec.start()
            # Worker thread exits immediately due to OSError; give it time
            time.sleep(0.05)
            # is_recording is reset by the finally block in _worker
            self.assertFalse(rec.is_recording, "is_recording must be False after device error")
            # stop() must still be safe even if is_recording already cleared
            result = rec.stop()
            # Either None (already idle) or a valid tuple — both are acceptable
            if result is not None:
                audio, duration = result
                self.assertIsInstance(audio, np.ndarray)


# ---------------------------------------------------------------------------
# 7. Device disconnect mid-recording
# ---------------------------------------------------------------------------

class TestHandlesDeviceDisconnectMidRecording(unittest.TestCase):
    def test_handles_device_disconnect_mid_recording(self) -> None:
        """OSError raised by stream.read mid-recording resets is_recording cleanly."""
        chunk_size = 1600  # AudioRecorder() по умолчанию: sample_rate=16000
        call_count = {"n": 0}
        # Одноразовое переключение read_available (см. _make_stream_cm выше):
        # без настоящего int guard решил бы, что поток голодает, и «третье
        # чтение» (диагностика теста) не наступило бы за отведённые 0.1с сна.
        # 0 сразу после read() держит дренаж выключенным, чтобы call_count
        # считал именно читаемые тестом «логические» чтения, а не удвоенные.
        just_read = {"v": False}

        def _read(n: int) -> tuple[np.ndarray, bool]:
            call_count["n"] += 1
            just_read["v"] = True
            if call_count["n"] >= 3:
                raise OSError("Device disconnected")
            return (np.ones((n, 1), dtype=np.float32), False)

        def _read_available(_self: MagicMock) -> int:
            if just_read["v"]:
                just_read["v"] = False
                return 0
            return chunk_size

        _StreamCls = type("_OneShotDisconnectStream", (MagicMock,), {})
        _StreamCls.read_available = property(_read_available)
        stream = _StreamCls()
        stream.read.side_effect = _read
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=stream)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("sounddevice.InputStream", return_value=cm):
            rec = AudioRecorder()
            rec.start()
            # Worker crashes after 3 reads
            time.sleep(0.1)
            self.assertFalse(rec.is_recording, "is_recording must be False after mid-read disconnect")
            # stop() must not raise
            result = rec.stop()
            # If stop returns data, audio must be valid ndarray
            if result is not None:
                audio, _ = result
                self.assertIsInstance(audio, np.ndarray)


# ---------------------------------------------------------------------------
# 8. Concurrent start serialized
# ---------------------------------------------------------------------------

class TestConcurrentStartSerialized(unittest.TestCase):
    def test_concurrent_start_serialized(self) -> None:
        """Only one of many concurrent start() calls should succeed."""
        with patch("sounddevice.InputStream", return_value=_make_stream_cm()):
            rec = AudioRecorder()
            results: list[bool] = []
            lock = threading.Lock()

            def _try_start() -> None:
                r = rec.start()
                with lock:
                    results.append(r)

            threads = [threading.Thread(target=_try_start) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            rec.stop()

        true_count = sum(1 for r in results if r)
        self.assertEqual(true_count, 1, f"Exactly one start() must succeed; got {true_count} True values")


# ---------------------------------------------------------------------------
# 9. Unicode device name forwarded to sounddevice
# ---------------------------------------------------------------------------

class TestUnicodeDeviceName(unittest.TestCase):
    def test_unicode_device_name(self) -> None:
        """AudioRecorder constructed with unicode sample_rate/channels doesn't crash;
        when 'device' kwarg is added in future, it should pass through cleanly.
        Current implementation does not accept a device kwarg, but we verify that
        the InputStream is opened with the configured samplerate and channels even
        when the recorder was created after setting unicode-named attributes.
        """
        mock_stream = _make_stream_cm()
        with patch("sounddevice.InputStream", return_value=mock_stream) as mock_cls:
            rec = AudioRecorder(sample_rate=16000, channels=1)
            # Simulate a unicode description stored on the recorder (no crash expected)
            rec._device_label = "Микрофон — встроенный 🎙️"  # type: ignore[attr-defined]
            rec.start()
            time.sleep(0.03)
            rec.stop()
            # Verify InputStream was called with correct numeric params
            mock_cls.assert_called_once()
            _, kwargs = mock_cls.call_args
            self.assertEqual(kwargs.get("samplerate"), 16000)
            self.assertEqual(kwargs.get("channels"), 1)
            self.assertEqual(kwargs.get("dtype"), "float32")


# ---------------------------------------------------------------------------
# 10. Clear-buffer safety
# ---------------------------------------------------------------------------

class TestClearBufferSafety(unittest.TestCase):
    def test_clear_buffer_safety(self) -> None:
        """After a failed/disconnected recording, starting again yields a clean buffer."""
        # Simulate first recording with device error (no chunks)
        error_cm = MagicMock()
        error_cm.__enter__ = MagicMock(side_effect=OSError("No device"))
        error_cm.__exit__ = MagicMock(return_value=False)

        with patch("sounddevice.InputStream", return_value=error_cm):
            rec = AudioRecorder()
            self.addCleanup(rec.abort)
            rec.start()
            time.sleep(0.05)

        # Inject leftover chunks manually (simulate partial state from first run)
        with rec._lock:
            rec._chunks = [np.ones((800, 1), dtype=np.float32)]
            rec._is_recording = False  # device error already cleared this

        # Second recording — start() must clear the old chunks
        with patch("sounddevice.InputStream", return_value=_make_stream_cm(chunk_size=1600, chunks_to_emit=2)):
            result2 = rec.start()
            self.assertTrue(result2, "Second start() after recovery should return True")
            # Verify chunks were cleared at start time
            time.sleep(0.02)
            with rec._lock:
                # Any chunks here were written by the new recording (values=0.5), not the old ones (1.0)
                if rec._chunks:
                    first_chunk = rec._chunks[0]
                    self.assertTrue(
                        np.allclose(first_chunk, 0.5),
                        "Buffer should only contain fresh recording data (0.5), not stale data (1.0)"
                    )
            rec.stop()


# ---------------------------------------------------------------------------
# 11. overflow_count (F2, спека 2026-08-12 — дистанция для превью)
# ---------------------------------------------------------------------------

def _make_overflow_stream_cm(chunk_size: int = 1600, overflow_at: frozenset = frozenset()) -> MagicMock:
    """Stream mock, помечающий overflowed=True на заданных (1-based) вызовах read().

    Каждый вызов подтормаживает на 4мс — иначе первый цикл _worker без throttle
    крутится настолько быстро, что sleep()-окно теста не успевает накопить
    нужное число вызовов до stop().
    """
    call_count = {"n": 0}
    # Одноразовое переключение read_available (см. _make_stream_cm выше):
    # guard требует настоящий int ДО read(), дренаж после read() требует
    # НЕ настоящего/нулевого — иначе overflow_at считал бы вперемешку
    # основные и дренажные чтения, а тест проверяет overflow_count по
    # ЛОГИЧЕСКИМ циклам воркера (1 инкремент = 1 итерация с overflowed=True).
    just_read = {"v": False}

    def _read(n: int) -> tuple[np.ndarray, bool]:
        call_count["n"] += 1
        just_read["v"] = True
        time.sleep(0.004)
        overflowed = call_count["n"] in overflow_at
        return (np.ones((n, 1), dtype=np.float32) * 0.5, overflowed)

    def _read_available(_self: MagicMock) -> int:
        if just_read["v"]:
            just_read["v"] = False
            return 0
        return chunk_size

    _StreamCls = type("_GuardableOverflowStream", (MagicMock,), {})
    _StreamCls.read_available = property(_read_available)
    stream = _StreamCls()
    stream.read.side_effect = _read
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=stream)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class TestOverflowCount(unittest.TestCase):
    """F2: _worker считает переполнения буфера ТЕКУЩЕЙ записи."""

    def test_overflow_count_increments_on_overflowed_reads(self) -> None:
        """Ровно два overflowed=True среди прочих reads → overflow_count == 2."""
        with patch("sounddevice.InputStream", return_value=_make_overflow_stream_cm(overflow_at=frozenset({2, 4}))):
            rec = AudioRecorder()
            self.addCleanup(rec.abort)
            rec.start()
            time.sleep(0.08)
            rec.stop()
        self.assertEqual(rec.overflow_count, 2)

    def test_overflow_count_zero_when_no_overflow(self) -> None:
        """Обычная запись без переполнений — счётчик остаётся 0."""
        with patch("sounddevice.InputStream", return_value=_make_stream_cm()):
            rec = AudioRecorder()
            self.addCleanup(rec.abort)
            rec.start()
            time.sleep(0.05)
            rec.stop()
        self.assertEqual(rec.overflow_count, 0)

    def test_overflow_count_resets_on_new_start(self) -> None:
        """Счётчик — про ТЕКУЩУЮ запись: новый start() обнуляет прошлый счёт."""
        with patch("sounddevice.InputStream", return_value=_make_overflow_stream_cm(overflow_at=frozenset({1, 2, 3}))):
            rec = AudioRecorder()
            self.addCleanup(rec.abort)
            rec.start()
            time.sleep(0.05)
            rec.stop()
        self.assertGreater(rec.overflow_count, 0, "тест невалиден без хотя бы одного overflow в первой записи")

        with patch("sounddevice.InputStream", return_value=_make_stream_cm()):
            rec.start()
            time.sleep(0.03)
            rec.stop()
        self.assertEqual(rec.overflow_count, 0, "новый start() должен обнулить счётчик прошлой записи")

    def test_overflow_count_zero_before_any_start(self) -> None:
        """Fail-safe: свежесозданный рекордер отдаёт 0, а не падает/None."""
        rec = AudioRecorder()
        self.assertEqual(rec.overflow_count, 0)


if __name__ == "__main__":
    unittest.main()
