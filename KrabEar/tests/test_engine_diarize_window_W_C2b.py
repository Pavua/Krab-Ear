"""AudioEngine.diarize_window (C2b): сегменты + speaker_embeddings из одного прогона.

Pipeline мокается объектом-фейком — тест не требует pyannote/torch (ubuntu-CI safe).
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _Turn:
    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end


class _FakeAnnotation:
    """Мимикрия pyannote.core.Annotation: itertracks + labels."""

    def __init__(self, tracks):
        self._tracks = tracks  # list[(start, end, label)]

    def itertracks(self, yield_label=False):
        for start, end, label in self._tracks:
            yield _Turn(start, end), "_", label

    def labels(self):
        seen = []
        for _, _, label in self._tracks:
            if label not in seen:
                seen.append(label)
        return seen


class _FakeDiarizeOutput:
    """Мимикрия pyannote 4.x DiarizeOutput."""

    def __init__(self, tracks, embeddings):
        self.speaker_diarization = _FakeAnnotation(tracks)
        self.speaker_embeddings = embeddings


class _FakePipeline:
    def __init__(self, output, lock_probe=None):
        self._output = output
        self._lock_probe = lock_probe
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if self._lock_probe is not None:
            self._lock_probe()
        return self._output


def _make_engine():
    # Лёгкая инстанциация: не грузим модели, только объект.
    from core.engine import AudioEngine
    eng = AudioEngine.__new__(AudioEngine)
    eng._diarization_pipeline = None
    eng._diarization_load_error = ""
    eng._diarization_load_lock = threading.RLock()
    eng._diarization_run_lock = threading.Lock()
    return eng


class DiarizeWindowTests(unittest.TestCase):
    def _run(self, tracks, embeddings):
        eng = _make_engine()
        out = _FakeDiarizeOutput(tracks, embeddings)
        eng._diarization_pipeline = _FakePipeline(out)
        return eng.diarize_window("/tmp/win.wav")

    def test_segments_and_embeddings_shape(self):
        tracks = [(0.0, 2.5, "SPEAKER_00"), (2.5, 4.0, "SPEAKER_01"),
                  (4.0, 6.0, "SPEAKER_00")]
        emb = np.stack([np.ones(256, dtype=np.float32),
                        np.full(256, 2.0, dtype=np.float32)])
        result = self._run(tracks, emb)
        self.assertEqual(len(result["segments"]), 3)
        self.assertEqual(result["segments"][0],
                         {"start": 0.0, "end": 2.5, "speaker": "SPEAKER_00"})
        self.assertEqual(set(result["speaker_embeddings"]),
                         {"SPEAKER_00", "SPEAKER_01"})
        self.assertEqual(len(result["speaker_embeddings"]["SPEAKER_00"]), 256)

    def test_nan_embedding_row_skipped(self):
        # pyannote отдаёт NaN-строку для спикера без чистых фреймов — не тащим её в сшивку.
        tracks = [(0.0, 1.0, "SPEAKER_00"), (1.0, 2.0, "SPEAKER_01")]
        emb = np.stack([np.ones(256, dtype=np.float32),
                        np.full(256, np.nan, dtype=np.float32)])
        result = self._run(tracks, emb)
        self.assertEqual(set(result["speaker_embeddings"]), {"SPEAKER_00"})
        self.assertEqual(len(result["segments"]), 2)  # сегменты остаются

    def test_no_embeddings_attr(self):
        # Annotation без speaker_embeddings (старый pyannote) — пустой словарь, без падения.
        eng = _make_engine()
        ann = _FakeAnnotation([(0.0, 1.0, "SPEAKER_00")])
        eng._diarization_pipeline = _FakePipeline(ann)
        result = eng.diarize_window("/tmp/win.wav")
        self.assertEqual(result["speaker_embeddings"], {})
        self.assertEqual(len(result["segments"]), 1)

    def test_run_lock_held_during_inference(self):
        eng = _make_engine()
        held = []
        out = _FakeDiarizeOutput([(0.0, 1.0, "SPEAKER_00")],
                                 np.ones((1, 256), dtype=np.float32))
        eng._diarization_pipeline = _FakePipeline(
            out, lock_probe=lambda: held.append(eng._diarization_run_lock.locked()))
        eng.diarize_window("/tmp/win.wav")
        self.assertEqual(held, [True])

    def test_full_diarization_shares_run_lock(self):
        # Sibling-gate: _run_diarization_impl держит ТОТ ЖЕ лок во время инференса.
        eng = _make_engine()
        held = []
        out = _FakeDiarizeOutput([(0.0, 1.0, "SPEAKER_00")],
                                 np.ones((1, 256), dtype=np.float32))
        eng._diarization_pipeline = _FakePipeline(
            out, lock_probe=lambda: held.append(eng._diarization_run_lock.locked()))
        with patch.object(type(eng), "_prepare_audio_for_diarization",
                          lambda self, p: (p, False), create=True):
            eng._run_diarization_impl("/tmp/full.wav")
        self.assertEqual(held, [True])


if __name__ == "__main__":
    unittest.main()
