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


class TestSessionTrackerRequiredCases(unittest.TestCase):
    """Обязательные тест-кейсы Wave 134."""

    def setUp(self):
        self.tracker = SessionTracker()

    # test_start_session_records_metadata
    def test_start_session_records_metadata(self):
        """start_session записывает device, preset, model в активную сессию."""
        sid = self.tracker.start_session(
            audio_device="Rode NT-USB",
            quality_preset="max",
            stt_model="whisper-large-v3",
        )
        result = self.tracker.end_session({})
        self.assertIsNotNone(result)
        self.assertEqual(result["session_id"], sid)
        self.assertEqual(result["audio_device"], "Rode NT-USB")
        self.assertEqual(result["quality_preset"], "max")
        self.assertEqual(result["stt_model"], "whisper-large-v3")
        self.assertIsNotNone(result["started_at"])
        self.assertIsNotNone(result["ended_at"])

    # test_end_session_finalizes
    def test_end_session_finalizes(self):
        """end_session: активная сессия становится None после завершения."""
        self.tracker.start_session()
        self.tracker.end_session({"duration_sec": 1.5, "paste_status": "ok"})
        # Второй end без start → None
        result = self.tracker.end_session({})
        self.assertIsNone(result)

    # test_get_session_by_id (через get_sessions фильтрация)
    def test_get_session_by_id(self):
        """Можно найти сессию по session_id в списке get_sessions."""
        sid = self.tracker.start_session(audio_device="FocusRite")
        self.tracker.end_session({"duration_sec": 3.0})
        sessions = self.tracker.get_sessions(limit=10)
        found = next((s for s in sessions if s["session_id"] == sid), None)
        self.assertIsNotNone(found)
        self.assertEqual(found["audio_device"], "FocusRite")

    # test_active_sessions_filter
    def test_active_sessions_filter(self):
        """Незавершённые сессии не попадают в get_sessions."""
        # Стартуем но НЕ завершаем
        self.tracker.start_session(audio_device="PendingDevice")
        sessions = self.tracker.get_sessions()
        # Активная сессия не должна быть в списке
        device_names = [s.get("audio_device") for s in sessions]
        self.assertNotIn("PendingDevice", device_names)

    # test_persist_reload
    def test_persist_reload(self):
        """Данные в sessions.ndjson перечитываются корректно после записи."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = SessionTracker(data_dir=tmpdir)
            sid = tracker.start_session(audio_device="Reload-Mic")
            tracker.end_session({"duration_sec": 7.7, "paste_status": "ok"})

            sessions_file = Path(tmpdir) / "sessions.ndjson"
            lines = sessions_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["session_id"], sid)
            self.assertAlmostEqual(record["duration_sec"], 7.7, places=3)
            self.assertEqual(record["audio_device"], "Reload-Mic")

    # test_unicode_device_name
    def test_unicode_device_name(self):
        """Имя устройства с кириллицей/emoji сохраняется без потерь."""
        device_name = "Микрофон Краба 🎙️"
        self.tracker.start_session(audio_device=device_name)
        result = self.tracker.end_session({"duration_sec": 1.0})
        self.assertEqual(result["audio_device"], device_name)

        # И в NDJSON тоже
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker2 = SessionTracker(data_dir=tmpdir)
            tracker2.start_session(audio_device=device_name)
            tracker2.end_session({"duration_sec": 1.0})
            data = (Path(tmpdir) / "sessions.ndjson").read_text(encoding="utf-8")
            record = json.loads(data.strip())
            self.assertEqual(record["audio_device"], device_name)

    # test_concurrent_start_end
    def test_concurrent_start_end(self):
        """Параллельные завершения сессий не приводят к потере данных."""
        import threading

        results = []
        errors = []
        lock = threading.Lock()

        def run_session(device_name):
            try:
                tracker = SessionTracker()
                tracker.start_session(audio_device=device_name)
                rec = tracker.end_session({"duration_sec": 0.5, "paste_status": "ok"})
                with lock:
                    results.append(rec)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=run_session, args=(f"Device-{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        self.assertEqual(len(results), 8)
        for rec in results:
            self.assertIsNotNone(rec["session_id"])
            self.assertIsNotNone(rec["ended_at"])

    # test_concurrent_start_end на одном экземпляре
    def test_concurrent_shared_tracker(self):
        """Shared SessionTracker: только последняя start_session видна end_session."""
        # SessionTracker имеет одну активную сессию — sequential semantics ожидаемы.
        tracker = SessionTracker()
        # Последовательные пары должны работать без ошибок
        for i in range(10):
            tracker.start_session(audio_device=f"Dev-{i}")
            r = tracker.end_session({"duration_sec": float(i)})
            self.assertIsNotNone(r)
        sessions = tracker.get_sessions(limit=100)
        self.assertEqual(len(sessions), 10)


class TestSessionTrackerGetActiveSessionW1501(unittest.TestCase):
    """W1501 — get_active_session() locked accessor tests."""

    def setUp(self):
        self.tracker = SessionTracker()

    def test_get_active_session_returns_none_initially(self):
        self.assertIsNone(self.tracker.get_active_session())

    def test_get_active_session_returns_dict_after_start(self):
        sid = self.tracker.start_session(audio_device="TestMic", quality_preset="max")
        active = self.tracker.get_active_session()
        self.assertIsNotNone(active)
        self.assertEqual(active["session_id"], sid)
        self.assertEqual(active["audio_device"], "TestMic")
        self.assertEqual(active["quality_preset"], "max")

    def test_get_active_session_returns_none_after_end(self):
        self.tracker.start_session()
        self.tracker.end_session({"duration_sec": 1.0})
        self.assertIsNone(self.tracker.get_active_session())

    def test_get_active_session_returns_copy_not_reference(self):
        """Mutating the returned dict must not affect internal state."""
        self.tracker.start_session(audio_device="OrigMic")
        copy = self.tracker.get_active_session()
        copy["audio_device"] = "Tampered"
        # Internal state should be unchanged
        copy2 = self.tracker.get_active_session()
        self.assertEqual(copy2["audio_device"], "OrigMic")

    def test_session_tracker_get_active_session_thread_safe(self):
        """Concurrent readers and writer must not raise or produce torn reads."""
        import threading

        errors = []
        results = []
        lock = threading.Lock()
        stop_event = threading.Event()

        def reader():
            while not stop_event.is_set():
                try:
                    val = self.tracker.get_active_session()
                    with lock:
                        results.append(val)
                except Exception as exc:
                    with lock:
                        errors.append(exc)

        def writer():
            for i in range(20):
                self.tracker.start_session(audio_device=f"Dev-{i}")
                self.tracker.end_session({"duration_sec": float(i)})

        readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
        writer_thread = threading.Thread(target=writer)
        for r in readers:
            r.start()
        writer_thread.start()
        writer_thread.join()
        stop_event.set()
        for r in readers:
            r.join(timeout=2.0)

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        # Each result must be None or a valid dict with expected keys
        for val in results:
            if val is not None:
                self.assertIn("session_id", val)
                self.assertIn("audio_device", val)


if __name__ == "__main__":
    unittest.main()
