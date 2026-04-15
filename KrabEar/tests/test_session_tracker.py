"""Тесты для SessionTracker."""
from backend.session_tracker import SessionTracker
import json
import sys
import tempfile
import unittest
from pathlib import Path

# Путь к KrabEar пакету
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestSessionTrackerBasic(unittest.TestCase):
    """Базовые тесты жизненного цикла сессии."""

    def setUp(self):
        self.tracker = SessionTracker()

    def test_start_returns_session_id(self):
        sid = self.tracker.start_session()
        self.assertIsInstance(sid, str)
        self.assertTrue(len(sid) > 0)

    def test_start_session_uuid_format(self):
        import uuid
        sid = self.tracker.start_session()
        # Проверяем что это валидный UUID
        parsed = uuid.UUID(sid)
        self.assertEqual(str(parsed), sid)

    def test_end_session_returns_record(self):
        self.tracker.start_session(audio_device="MacBook Pro Mic", quality_preset="max")
        result = self.tracker.end_session({
            "duration_sec": 5.2,
            "confidence": 0.92,
            "paste_status": "ok",
            "text": "Hello world test",
        })
        self.assertIsNotNone(result)
        self.assertEqual(result["audio_device"], "MacBook Pro Mic")
        self.assertEqual(result["quality_preset"], "max")
        self.assertAlmostEqual(result["duration_sec"], 5.2)
        self.assertAlmostEqual(result["confidence"], 0.92)
        self.assertEqual(result["paste_status"], "ok")
        self.assertEqual(result["word_count"], 3)  # "Hello world test"
        self.assertIsNotNone(result["ended_at"])
        self.assertIsNotNone(result["started_at"])

    def test_end_session_without_start_returns_none(self):
        result = self.tracker.end_session({"duration_sec": 1.0})
        self.assertIsNone(result)

    def test_get_sessions_empty(self):
        sessions = self.tracker.get_sessions()
        self.assertEqual(sessions, [])

    def test_get_sessions_returns_correct_count(self):
        for i in range(5):
            self.tracker.start_session()
            self.tracker.end_session({"duration_sec": float(i), "paste_status": "ok"})
        sessions = self.tracker.get_sessions(limit=3)
        self.assertEqual(len(sessions), 3)

    def test_get_sessions_ordered_newest_first(self):
        for i in range(3):
            self.tracker.start_session()
            self.tracker.end_session({"duration_sec": float(i + 1)})
        sessions = self.tracker.get_sessions()
        # Первая в списке должна быть последняя по времени (наибольшая duration)
        self.assertEqual(sessions[0]["duration_sec"], 3.0)

    def test_had_translation_from_translation_status(self):
        self.tracker.start_session()
        result = self.tracker.end_session({"translation_status": "ok"})
        self.assertTrue(result["had_translation"])

    def test_had_translation_false_when_off(self):
        self.tracker.start_session()
        result = self.tracker.end_session({"translation_status": "off"})
        self.assertFalse(result["had_translation"])

    def test_had_translation_false_when_not_requested(self):
        self.tracker.start_session()
        result = self.tracker.end_session({"translation_status": "not_requested"})
        self.assertFalse(result["had_translation"])


class TestSessionTrackerStats(unittest.TestCase):
    """Тесты get_session_stats."""

    def setUp(self):
        self.tracker = SessionTracker()

    def test_stats_empty(self):
        stats = self.tracker.get_session_stats()
        self.assertEqual(stats["total_sessions"], 0)
        self.assertEqual(stats["avg_duration_sec"], 0.0)

    def test_stats_single_session(self):
        self.tracker.start_session()
        self.tracker.end_session({
            "duration_sec": 4.0,
            "confidence": 0.8,
            "word_count": 10,
            "stt_latency_ms": 300,
            "paste_status": "ok",
            "had_diarization": True,
            "had_llm_rewrite": False,
            "had_translation": True,
        })
        stats = self.tracker.get_session_stats()
        self.assertEqual(stats["total_sessions"], 1)
        self.assertAlmostEqual(stats["avg_duration_sec"], 4.0)
        self.assertAlmostEqual(stats["avg_confidence"], 0.8)
        self.assertEqual(stats["avg_word_count"], 10.0)
        self.assertAlmostEqual(stats["avg_stt_latency_ms"], 300.0)
        self.assertEqual(stats["paste_ok_rate"], 1.0)
        self.assertEqual(stats["diarization_rate"], 1.0)
        self.assertEqual(stats["llm_rewrite_rate"], 0.0)
        self.assertEqual(stats["translation_rate"], 1.0)

    def test_stats_multiple_sessions(self):
        for i, paste in enumerate(["ok", "ok", "failed"]):
            self.tracker.start_session()
            self.tracker.end_session({
                "duration_sec": float(i + 1),
                "paste_status": paste,
            })
        stats = self.tracker.get_session_stats()
        self.assertEqual(stats["total_sessions"], 3)
        self.assertAlmostEqual(stats["paste_ok_rate"], 2 / 3, places=4)

    def test_stats_paste_ok_rate_zero(self):
        for _ in range(3):
            self.tracker.start_session()
            self.tracker.end_session({"paste_status": "failed"})
        stats = self.tracker.get_session_stats()
        self.assertEqual(stats["paste_ok_rate"], 0.0)


class TestSessionTrackerPersistence(unittest.TestCase):
    """Тесты опциональной персистентности в NDJSON."""

    def test_persists_to_ndjson(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = SessionTracker(data_dir=tmpdir)
            tracker.start_session(audio_device="USB Mic")
            tracker.end_session({"duration_sec": 2.5, "paste_status": "ok"})

            sessions_file = Path(tmpdir) / "sessions.ndjson"
            self.assertTrue(sessions_file.exists())
            lines = sessions_file.read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["audio_device"], "USB Mic")
            self.assertAlmostEqual(record["duration_sec"], 2.5)

    def test_multiple_sessions_appended(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = SessionTracker(data_dir=tmpdir)
            for _ in range(3):
                tracker.start_session()
                tracker.end_session({"duration_sec": 1.0})

            sessions_file = Path(tmpdir) / "sessions.ndjson"
            lines = sessions_file.read_text().strip().splitlines()
            self.assertEqual(len(lines), 3)

    def test_no_persistence_without_data_dir(self):
        tracker = SessionTracker(data_dir=None)
        tracker.start_session()
        result = tracker.end_session({"duration_sec": 1.0})
        # Просто не должно упасть
        self.assertIsNotNone(result)


class TestSessionTrackerMaxBuffer(unittest.TestCase):
    """Тесты ограничения размера буфера."""

    def test_max_sessions_limit(self):
        tracker = SessionTracker(max_sessions=5)
        for _ in range(10):
            tracker.start_session()
            tracker.end_session({"duration_sec": 1.0})
        sessions = tracker.get_sessions(limit=100)
        self.assertEqual(len(sessions), 5)

    def test_session_fields_present(self):
        tracker = SessionTracker()
        tracker.start_session(
            audio_device="Built-in Mic",
            quality_preset="balanced",
            stt_model="mlx-community/whisper-large-v3-turbo",
        )
        result = tracker.end_session({
            "duration_sec": 3.1,
            "stt_latency_ms": 250,
            "confidence": 0.88,
            "word_count": 12,
            "had_diarization": False,
            "had_llm_rewrite": True,
            "had_translation": False,
            "paste_status": "ok",
        })
        required_fields = [
            "session_id", "started_at", "ended_at", "duration_sec",
            "audio_device", "quality_preset", "stt_model",
            "stt_latency_ms", "confidence", "word_count",
            "had_diarization", "had_llm_rewrite", "had_translation", "paste_status",
        ]
        for field in required_fields:
            self.assertIn(field, result, f"Missing field: {field}")

        self.assertEqual(result["stt_model"], "mlx-community/whisper-large-v3-turbo")
        self.assertTrue(result["had_llm_rewrite"])
        self.assertFalse(result["had_diarization"])


if __name__ == "__main__":
    unittest.main()
