"""Тесты RecordingSpillWriter (R1 Фаза 1, Task 1).

Без sounddevice: писатель работает с чистыми numpy-чанками.
"""
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_spill import RecordingSpillWriter, finalize_part_to_wav  # noqa: E402


class RecordingSpillWriterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rescue_dir = Path(self._tmp.name) / "rescue"

    def tearDown(self):
        self._tmp.cleanup()

    def _writer(self, **kw):
        params = dict(rescue_dir=self.rescue_dir, sample_rate=16000, channels=1, source="dictation")
        params.update(kw)
        return RecordingSpillWriter(**params)

    def test_open_creates_part_and_meta(self):
        w = self._writer()
        self.assertTrue(w.open())
        self.assertTrue(w.part_path.exists())
        meta = json.loads((self.rescue_dir / f"{w.session_id}.meta.json").read_text())
        self.assertEqual(meta["sample_rate"], 16000)
        self.assertEqual(meta["channels"], 1)
        self.assertEqual(meta["source"], "dictation")
        self.assertIn("started_at_iso", meta)
        w.discard()

    def test_append_flushes_to_disk_immediately(self):
        w = self._writer()
        self.assertTrue(w.open())
        chunk = np.ones(1600, dtype=np.float32) * 0.5
        w.append(chunk)
        # Данные должны быть на диске БЕЗ close() — переживание kill -9.
        self.assertEqual(w.part_path.stat().st_size, 1600 * 4)
        w.discard()

    def test_close_keeps_files_discard_removes(self):
        w = self._writer()
        self.assertTrue(w.open())
        w.append(np.zeros(160, dtype=np.float32))
        w.close()
        self.assertTrue(w.part_path.exists())
        w.discard()
        self.assertFalse(w.part_path.exists())
        self.assertFalse((self.rescue_dir / f"{w.session_id}.meta.json").exists())

    def test_discard_idempotent(self):
        w = self._writer()
        self.assertTrue(w.open())
        w.discard()
        w.discard()  # не должно бросать

    def test_append_io_error_disables_writer_not_raises(self):
        w = self._writer()
        self.assertTrue(w.open())
        w._fh.close()  # симулируем умерший дескриптор
        w.append(np.zeros(160, dtype=np.float32))  # не должно бросить
        self.assertTrue(w.failed)
        # Повторный append — тихий no-op
        w.append(np.zeros(160, dtype=np.float32))
        w.discard()

    def test_open_failure_returns_false(self):
        # rescue_dir указывает на ФАЙЛ → mkdir внутри провалится
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("x")
        w = RecordingSpillWriter(rescue_dir=blocker / "sub", sample_rate=16000,
                                 channels=1, source="dictation")
        self.assertFalse(w.open())
        self.assertTrue(w.failed)


class FinalizePartTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rescue_dir = Path(self._tmp.name) / "rescue"

    def tearDown(self):
        self._tmp.cleanup()

    def _make_part(self, samples: np.ndarray, sample_rate=16000) -> Path:
        w = RecordingSpillWriter(rescue_dir=self.rescue_dir, sample_rate=sample_rate,
                                 channels=1, source="dictation")
        self.assertTrue(w.open())
        w.append(samples.astype(np.float32))
        w.close()
        return w.part_path

    def test_finalize_produces_wav_and_cleans_part(self):
        part = self._make_part(np.ones(16000, dtype=np.float32) * 0.25)  # 1с
        wav_path = finalize_part_to_wav(part)
        self.assertIsNotNone(wav_path)
        self.assertTrue(wav_path.name.endswith(".rescued.wav"))
        self.assertFalse(part.exists())
        self.assertFalse(part.with_name(part.name.replace(".f32.part", ".meta.json")).exists())
        with wave.open(str(wav_path), "rb") as wf:
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getnframes(), 16000)

    def test_finalize_truncated_tail_rounds_down(self):
        part = self._make_part(np.zeros(16000, dtype=np.float32))
        with part.open("ab") as fh:
            fh.write(b"\x01\x02\x03")  # обрыв посреди семпла
        wav_path = finalize_part_to_wav(part)
        self.assertIsNotNone(wav_path)
        with wave.open(str(wav_path), "rb") as wf:
            self.assertEqual(wf.getnframes(), 16000)

    def test_finalize_too_short_removes_garbage(self):
        part = self._make_part(np.zeros(1000, dtype=np.float32))  # 62мс < 0.5с
        self.assertIsNone(finalize_part_to_wav(part))
        self.assertFalse(part.exists())

    def test_finalize_missing_meta_returns_none_keeps_part(self):
        part = self._make_part(np.zeros(16000, dtype=np.float32))
        part.with_name(part.name.replace(".f32.part", ".meta.json")).unlink()
        self.assertIsNone(finalize_part_to_wav(part))
        # Без сайдкара не знаем sample_rate — файл НЕ трогаем (не наш мусор).
        self.assertTrue(part.exists())


if __name__ == "__main__":
    unittest.main()
