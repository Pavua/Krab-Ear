"""LiveSpeakerTracker (C2b): сшивка спикеров между окнами на fake-эмбеддингах."""
import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.meeting_session_service import LiveSpeakerTracker  # noqa: E402


def _emb(direction: int, dim: int = 8) -> list[float]:
    v = np.zeros(dim, dtype=np.float32)
    v[direction] = 1.0
    return v.tolist()


def _near(direction: int, dim: int = 8) -> list[float]:
    # cosine ~0.995 к _emb(direction) — заведомо выше порога 0.72
    v = np.zeros(dim, dtype=np.float32)
    v[direction] = 1.0
    v[(direction + 1) % dim] = 0.1
    return v.tolist()


class LiveSpeakerTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tr = LiveSpeakerTracker(threshold=0.72)

    def test_first_window_creates_speakers(self):
        self.tr.ingest(
            segments=[{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
                      {"start": 2.0, "end": 5.0, "speaker": "SPEAKER_01"}],
            embeddings={"SPEAKER_00": _emb(0), "SPEAKER_01": _emb(1)},
            now_ts=1000.0)
        snap = self.tr.snapshot()
        self.assertEqual([s["label"] for s in snap], ["Спикер 1", "Спикер 2"])
        self.assertAlmostEqual(snap[0]["talk_sec"], 2.0)
        self.assertAlmostEqual(snap[1]["talk_sec"], 3.0)
        self.assertEqual(snap[0]["last_active_ts"], 1000.0)

    def test_second_window_matches_same_speaker(self):
        self.tr.ingest(segments=[{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}],
                       embeddings={"SPEAKER_00": _emb(0)}, now_ts=1000.0)
        # В новом окне локальная метка ДРУГАЯ, но голос тот же (близкий вектор).
        self.tr.ingest(segments=[{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_01"}],
                       embeddings={"SPEAKER_01": _near(0)}, now_ts=1090.0)
        snap = self.tr.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertAlmostEqual(snap[0]["talk_sec"], 6.0)
        self.assertEqual(snap[0]["last_active_ts"], 1090.0)

    def test_below_threshold_creates_new_speaker(self):
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
                       embeddings={"SPEAKER_00": _emb(0)}, now_ts=1.0)
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
                       embeddings={"SPEAKER_00": _emb(1)}, now_ts=2.0)  # ортогонален
        self.assertEqual(len(self.tr.snapshot()), 2)

    def test_centroid_running_mean(self):
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "A"}],
                       embeddings={"A": _emb(0)}, now_ts=1.0)
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "B"}],
                       embeddings={"B": _near(0)}, now_ts=2.0)
        # Центроид сдвинулся: третье окно с _near(0) всё ещё матчится в того же.
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "C"}],
                       embeddings={"C": _near(0)}, now_ts=3.0)
        self.assertEqual(len(self.tr.snapshot()), 1)
        self.assertAlmostEqual(self.tr.snapshot()[0]["talk_sec"], 3.0)

    def test_segment_without_embedding_counts_no_speaker(self):
        # Метка есть в сегментах, но эмбеддинг отброшен (NaN в engine) — спикер не создаётся.
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"},
                                 {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_01"}],
                       embeddings={"SPEAKER_00": _emb(0)}, now_ts=1.0)
        self.assertEqual(len(self.tr.snapshot()), 1)

    def test_zero_norm_embedding_skipped(self):
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "A"}],
                       embeddings={"A": [0.0] * 8}, now_ts=1.0)
        self.assertEqual(self.tr.snapshot(), [])

    def test_registry_capped_at_max_speakers(self):
        # Fable-гейт Finding 1: шумная многочасовая встреча не должна плодить
        # фантомных «Спикеров N» без предела — реестр ограничен сверху.
        for i in range(20):
            self.tr.ingest(
                segments=[{"start": 0.0, "end": 1.0, "speaker": "S"}],
                embeddings={"S": _emb(i, dim=32)},  # 20 попарно ортогональных голосов
                now_ts=float(i))
        self.assertEqual(len(self.tr.snapshot()), 16)

    def test_snapshot_returns_copies(self):
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "A"}],
                       embeddings={"A": _emb(0)}, now_ts=1.0)
        snap = self.tr.snapshot()
        snap[0]["talk_sec"] = 999.0
        self.assertNotEqual(self.tr.snapshot()[0]["talk_sec"], 999.0)


if __name__ == "__main__":
    unittest.main()
