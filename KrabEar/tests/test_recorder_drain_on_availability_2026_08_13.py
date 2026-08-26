"""Поток захвата забирает ВСЁ накопленное, а не фиксированный кусок.

Спека: docs/superpowers/specs/2026-08-13-recorder-drain-on-availability-design.md

Корень (живой замер): `stream.read(chunk_size)` забирает ровно 100 мс. Если
планировщик не дал треду процессор дольше — за паузу накопилось больше, и
отставание растёт с каждым циклом; никакой буфер этого не лечит. Замер
точного паттерна внедрения: при голодании 250 и 500 мс дренаж даёт 0/12
переполнений против 8-10/12 без него.

Здесь же чинится побочный дефект, который дренаж обнажает: на границе лимита
длительности чанк отбрасывался ЦЕЛИКОМ (100 мс потери; с дренажом было бы до
~500 мс). Теперь обрезается по границе — влезающая часть сохраняется.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recorder import AudioRecorder  # noqa: E402


class _DrainableStream:
    """Дак-тайп поток с НАСТОЯЩИМ int в read_available.

    Намеренно НЕ MagicMock: у того `int(read_available)` вернул бы 1, и
    дренаж прочитал бы фантом. Реальный контракт sounddevice — целое число.
    """

    def __init__(self, block_frames: int, backlog_frames: int,
                 overflow_on_first: bool = False,
                 overflow_on_drain: bool = False,
                 max_reads: int = 1):
        self.block_frames = block_frames
        self.read_available = backlog_frames
        self._overflow_first = overflow_on_first
        self._overflow_drain = overflow_on_drain
        self.read_calls: list[int] = []
        self._reads_left = max_reads
        self.stop_event: threading.Event | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self, frames):
        self.read_calls.append(int(frames))
        n = len(self.read_calls)
        first = n == 1
        if first:
            of = self._overflow_first
        elif n == 2:
            of = self._overflow_drain
            self.read_available = 0  # дренаж опустошил накопленное
        else:
            of = False
        # 🔴 Останов ставим на ТРЕТЬЕМ чтении (начало второго цикла), а НЕ во
        # время дренажа: рабочий цикл проверяет _stop_event сразу после чтения
        # и осознанно отбрасывает этот чанк (корректный shutdown). Останов
        # внутри дренажа выбросил бы данные первого цикла и тест проверял бы
        # не то.
        if n >= 3 and self.stop_event is not None:
            self.stop_event.set()
        data = np.full((int(frames), 1), 0.25 if first else 0.5, dtype=np.float32)
        return data, of


def _run_worker_once(recorder: AudioRecorder, stream) -> None:
    """Прогоняет _worker ровно один цикл с подставленным потоком."""
    stream.stop_event = recorder._stop_event
    recorder._stop_event.clear()
    with recorder._lock:
        recorder._is_recording = True
        recorder._started_at = 0.0
        recorder._chunks = []
        recorder._chunks_total_samples = 0
    with patch("backend.recorder.sd") as mock_sd:
        mock_sd.InputStream.return_value = stream
        t = threading.Thread(target=recorder._worker, daemon=True)
        t.start()
        t.join(timeout=5.0)
    assert not t.is_alive(), "worker не завершился"


class DrainOnAvailabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = AudioRecorder()

    def test_backlog_accumulated_during_starvation_is_drained(self) -> None:
        """Накопленное сверх блока обязано быть забрано ТЕМ ЖЕ циклом."""
        block = self.rec.chunk_size
        backlog = block * 3  # 300 мс «пропущенных», пока тред не планировали
        stream = _DrainableStream(block, backlog)

        _run_worker_once(self.rec, stream)

        # Третье чтение — блокирующее чтение СЛЕДУЮЩЕГО цикла, оно же ставит
        # останов; проверяем префикс, а не полный список.
        self.assertEqual(
            stream.read_calls[:2], [block, backlog],
            "после блокирующего чтения блока обязан идти дренаж read_available, "
            f"получено {stream.read_calls}",
        )
        with self.rec._lock:
            total = self.rec._chunks_total_samples
        self.assertEqual(
            total, block + backlog,
            "аудио из дренажа обязано попасть в запись, иначе отставание "
            "растёт с каждым циклом и буфер переполняется",
        )

    def test_no_backlog_means_no_second_read(self) -> None:
        """Нет накопленного — лишнего чтения быть не должно (прежний путь)."""
        block = self.rec.chunk_size
        # Guarded read (спека 2026-08-23) опрашивает read_available ДО read()
        # и требует настоящий int >= chunk_size — backlog=0 заставил бы guard
        # честно решить, что поток голодает, и вообще не дошёл бы до read().
        # Заводим стрим с block «готовым» кадром для guard'а, а сразу после
        # read() вручную обнуляем read_available — реалистичная модель
        # «накопленного сверх блока нет», ровно то, что тест и проверяет.
        stream = _DrainableStream(block, block, max_reads=1)
        stream.stop_event = self.rec._stop_event

        orig_read = stream.read

        def read_and_stop(frames):
            out = orig_read(frames)
            stream.read_available = 0
            self.rec._stop_event.set()
            return out

        stream.read = read_and_stop
        _run_worker_once(self.rec, stream)

        self.assertEqual(stream.read_calls, [block])

    def test_overflow_flag_from_drain_read_is_counted(self) -> None:
        """Переполнение, обнаруженное ВТОРЫМ чтением, не должно потеряться."""
        block = self.rec.chunk_size
        stream = _DrainableStream(block, block, overflow_on_first=False,
                                  overflow_on_drain=True)

        _run_worker_once(self.rec, stream)

        self.assertGreaterEqual(
            self.rec.overflow_count, 1,
            "флаг переполнения от дренажного чтения обязан учитываться — "
            "иначе F2-бэкофф превью ослепнет",
        )

    def test_magicmock_stream_keeps_old_behaviour(self) -> None:
        """🔴 Регресс-гард: у MagicMock `int(read_available)` вернул бы 1, и
        дренаж прочитал бы фантомный сэмпл, тихо ломая существующие тесты
        рекордера. Дренаж обязан включаться ТОЛЬКО на настоящем целом.

        Guarded read (спека 2026-08-23) опрашивает то же read_available ДО
        read() и требует настоящий int, иначе честно решает, что поток
        голодает, и вообще не читает — а тест как раз проверяет, что чтение
        СЛУЧАЕТСЯ, просто без дренажа. Поэтому read_available здесь —
        одноразовое динамическое свойство: настоящий int для guard'а до
        первого read(), а после него — обычный (не-int) MagicMock, как у
        неконфигурированного теста-дублёра, чтобы дренажная ветка молчала.
        """
        block = self.rec.chunk_size
        calls: list[int] = []
        read_done = {"v": False}

        def _read_available(_self: MagicMock):
            if read_done["v"]:
                return MagicMock()  # не int → дренаж выключен, замысел теста
            return block

        _StreamCls = type("_OneShotAvailabilityStream", (MagicMock,), {})
        _StreamCls.read_available = property(_read_available)
        mock_stream = _StreamCls()
        mock_stream.__enter__ = MagicMock(return_value=mock_stream)
        mock_stream.__exit__ = MagicMock(return_value=False)

        def fake_read(frames):
            calls.append(int(frames))
            read_done["v"] = True
            self.rec._stop_event.set()
            return np.zeros((int(frames), 1), dtype=np.float32), False

        mock_stream.read = fake_read
        _run_worker_once(self.rec, mock_stream)

        self.assertEqual(
            calls, [block],
            "на MagicMock-потоке дренаж обязан быть выключен (read_available "
            "не настоящее целое) — иначе прочитали бы фантом",
        )


class MaxDurationTruncationTest(unittest.TestCase):
    """Лимит длительности: чанк ОБРЕЗАЕТСЯ по границе, а не выбрасывается."""

    def test_partial_chunk_at_cap_is_kept_not_discarded(self) -> None:
        rec = AudioRecorder()
        block = rec.chunk_size
        # Потолок ровно на 1.5 блока: первый чанк влезает, второй — наполовину.
        cap = block + block // 2
        with rec._lock:
            rec._max_recording_samples = cap
        # Guarded read (спека 2026-08-23) требует настоящий int read_available
        # >= chunk_size ДО КАЖДОГО read(), иначе guard решит, что поток
        # голодает, и не прочитает вообще (cap так и не будет достигнут).
        # Дренаж (спека 2026-08-13) не в фокусе этого теста — он проверяет
        # обрезку РОВНО НА ГРАНИЦЕ второго блока, поэтому read_available —
        # одноразовое свойство: полный блок готов до read(), 0 сразу после
        # (равномерный приток без накопленного излишка). Свойство заведено
        # на отдельном классе, а не на общем _DrainableStream, чтобы не
        # задеть остальные тесты файла, которые используют его как обычный
        # атрибут — иначе дренаж склеил бы оба блока в один чанк за одну
        # итерацию, и тест перестал бы проверять именно границу обрезки.
        class _EvenlyPacedStream:
            def __init__(self, block_frames: int) -> None:
                self.block_frames = block_frames
                self.read_calls: list[int] = []
                self._just_read = False

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            @property
            def read_available(self) -> int:
                if self._just_read:
                    self._just_read = False
                    return 0
                return self.block_frames

            def read(self, frames):
                self.read_calls.append(int(frames))
                self._just_read = True
                return np.full((int(frames), 1), 0.5, dtype=np.float32), False

        stream = _EvenlyPacedStream(block)
        rec._stop_event.clear()
        with rec._lock:
            rec._is_recording = True
            rec._started_at = 0.0
            rec._chunks = []
            rec._chunks_total_samples = 0
        with patch("backend.recorder.sd") as mock_sd:
            mock_sd.InputStream.return_value = stream
            t = threading.Thread(target=rec._worker, daemon=True)
            t.start()
            t.join(timeout=5.0)
        self.assertFalse(t.is_alive())

        audio, _dur = rec._pending_result
        self.assertEqual(
            audio.size, cap,
            "на границе лимита влезающая часть чанка обязана сохраняться: "
            f"ожидали ровно потолок {cap}, получили {audio.size} — прежний код "
            "выбрасывал не влезающий чанк ЦЕЛИКОМ",
        )


if __name__ == "__main__":
    unittest.main()
