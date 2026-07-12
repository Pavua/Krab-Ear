"""snapshot_range: срез сырого буфера по диапазону секунд (C2a, спека §2.3)."""
import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recorder import AudioRecorder  # noqa: E402


def _make_recorder_with_chunks(n_chunks: int, chunk_val_start: float = 0.0) -> AudioRecorder:
    """Рекордер с n_chunks чанками по 0.1с (1600 сэмплов при 16кГц).

    Значение сэмплов i-го чанка = chunk_val_start + i — по значению видно,
    какие чанки попали в срез.
    """
    rec = AudioRecorder(sample_rate=16000)
    with rec._lock:
        for i in range(n_chunks):
            rec._chunks.append(
                np.full(rec.chunk_size, chunk_val_start + i, dtype=np.float32)
            )
            rec._chunks_total_samples += rec.chunk_size
    return rec


class SnapshotRangeTestCase(unittest.TestCase):
    def test_middle_range_returns_exact_samples(self) -> None:
        rec = _make_recorder_with_chunks(30)  # 3.0 сек
        audio = rec.snapshot_range(1.0, 2.0)
        self.assertEqual(audio.size, 16000)  # ровно 1 секунда
        # Первый сэмпл диапазона — из чанка №10 (1.0с / 0.1с)
        self.assertAlmostEqual(float(audio[0]), 10.0, places=5)
        # Последний — из чанка №19
        self.assertAlmostEqual(float(audio[-1]), 19.0, places=5)

    def test_range_beyond_buffer_clamps(self) -> None:
        rec = _make_recorder_with_chunks(10)  # 1.0 сек
        audio = rec.snapshot_range(0.5, 99.0)
        self.assertEqual(audio.size, 8000)  # только доступная половина

    def test_empty_and_degenerate_ranges(self) -> None:
        rec = _make_recorder_with_chunks(10)
        self.assertEqual(rec.snapshot_range(2.0, 1.0).size, 0)   # from > to
        self.assertEqual(rec.snapshot_range(1.0, 1.0).size, 0)   # from == to
        self.assertEqual(rec.snapshot_range(5.0, 6.0).size, 0)   # за концом буфера
        empty = AudioRecorder(sample_rate=16000)
        self.assertEqual(empty.snapshot_range(0.0, 1.0).size, 0)  # пустой буфер

    def test_dtype_and_flat_shape(self) -> None:
        rec = _make_recorder_with_chunks(5)
        audio = rec.snapshot_range(0.0, 0.5)
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(audio.ndim, 1)


if __name__ == "__main__":
    unittest.main()
