"""Edge-case тесты для backend/history_service.py.

Покрывает:
- handle_delete_history_item с несуществующим / пустым ID
- handle_export_history_srt: пустая история, item без диаризации, item без сегментов
- handle_get_storage_info: правильные размеры файлов
- handle_cleanup_old_history(days=0): должен вызывать RuntimeError
- handle_get_clipboard_history + handle_repaste_item round-trip
- Bulk delete без items — no-op (через handle_delete_history_item пустой ID)
- handle_filter_by_confidence: граничные значения + отсутствующий min_confidence
- handle_get_history_statistics: пустая история, одна запись
- handle_get_history_item: несуществующий ID
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from backend.history_service import HistoryService
    from backend.state_store import StateStore
    _SKIP = False
except ImportError:
    _SKIP = True


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class DeleteEdgeTestCase(unittest.TestCase):
    """Тесты handle_delete_history_item — edge cases."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_delete_nonexistent_id_raises_value_error(self):
        """StateStore tombstone-delete: несуществующий ID вызывает ValueError.

        Семантика изменена в wave-1762: StateStore.delete_history_item() проверяет
        существование записи перед tombstone, блокируя спам junk-ID в tombstones.ndjson.
        """
        with self.assertRaises(ValueError):
            self.svc.handle_delete_history_item({"id": "nonexistent-id-0000"})

    def test_delete_empty_id_raises(self):
        """Пустой id должен вызывать ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_delete_history_item({"id": ""})

    def test_delete_missing_id_param_raises(self):
        """Отсутствие параметра id должно вызывать ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_delete_history_item({})

    def test_delete_valid_item_returns_deleted_true(self):
        """Удаление существующей записи возвращает {'deleted': True}."""
        item = self.store.add_history_item(text="test record", paste_status="ok")
        result = self.svc.handle_delete_history_item({"id": item.id})
        self.assertTrue(result["deleted"])


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class ExportSrtEdgeTestCase(unittest.TestCase):
    """Тесты handle_export_history_srt — edge cases."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_srt_export_item_not_found_raises(self):
        """Запрос SRT для несуществующего ID вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_export_history_srt({"id": "no-such-item-xxxx"})

    def test_srt_export_missing_id_raises(self):
        """Отсутствие id вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_export_history_srt({})

    def test_srt_export_item_without_diarization(self):
        """Item без диаризации → single-segment SRT."""
        item = self.store.add_history_item(text="hello world", paste_status="ok")
        result = self.svc.handle_export_history_srt({"id": item.id})
        self.assertIn("content", result)
        self.assertIn("hello world", result["content"])
        self.assertEqual(result["item_id"], item.id)
        self.assertEqual(result["speakers"], 1)
        self.assertEqual(result["segments"], 1)
        self.assertIsNone(result["path"])

    def test_srt_export_diarization_no_turns(self):
        """Item с diarization enabled=True, но без speaker_turns → single SRT."""
        item = self.store.add_history_item(
            text="no turns text",
            paste_status="ok",
            diarization={"enabled": True, "speaker_turns": []},
        )
        result = self.svc.handle_export_history_srt({"id": item.id})
        self.assertIn("no turns text", result["content"])
        self.assertEqual(result["speakers"], 1)
        self.assertEqual(result["segments"], 1)

    def test_srt_export_with_speaker_turns(self):
        """Item с speaker_turns → multi-speaker SRT."""
        diar = {
            "enabled": True,
            "speaker_turns": [
                {"speaker": "SPEAKER_00", "text": "Hello", "start": 0.0, "end": 1.5},
                {"speaker": "SPEAKER_01", "text": "Hi there", "start": 1.5, "end": 3.0},
            ],
        }
        item = self.store.add_history_item(
            text="Hello Hi there", paste_status="ok", diarization=diar,
        )
        result = self.svc.handle_export_history_srt({"id": item.id})
        self.assertEqual(result["speakers"], 2)
        self.assertEqual(result["segments"], 2)
        self.assertIn("SPEAKER_00", result["content"])
        self.assertIn("SPEAKER_01", result["content"])


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class StorageInfoTestCase(unittest.TestCase):
    """Тесты handle_get_storage_info."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_storage_info_keys_present(self):
        """Все ожидаемые ключи присутствуют в ответе."""
        result = self.svc.handle_get_storage_info({})
        for key in (
            "history_bytes", "history_file_size_mb",
            "transcripts_count", "transcripts_size_mb",
            "reports_count", "total_bytes", "total_data_mb",
        ):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_storage_info_types(self):
        """history_bytes и total_bytes — int; MB-поля — float."""
        result = self.svc.handle_get_storage_info({})
        self.assertIsInstance(result["history_bytes"], int)
        self.assertIsInstance(result["total_bytes"], int)
        self.assertIsInstance(result["history_file_size_mb"], float)
        self.assertIsInstance(result["total_data_mb"], float)

    def test_storage_info_grows_after_add(self):
        """После добавления записи history_bytes > 0."""
        self.store.add_history_item(text="storage test", paste_status="ok")
        result = self.svc.handle_get_storage_info({})
        self.assertGreater(result["history_bytes"], 0)
        self.assertGreater(result["total_bytes"], 0)


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class CleanupOldHistoryEdgeTestCase(unittest.TestCase):
    """Тесты handle_cleanup_old_history — edge cases."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_cleanup_days_zero_raises(self):
        """older_than_days=0 должен вызывать RuntimeError (граница: не положительное)."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_cleanup_old_history({"older_than_days": 0})

    def test_cleanup_negative_days_raises(self):
        """Отрицательное older_than_days должно вызывать RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_cleanup_old_history({"older_than_days": -5})

    def test_cleanup_large_days_no_items_deleted(self):
        """С очень большим порогом (10000 дней) свежие записи не удаляются."""
        self.store.add_history_item(text="fresh record", paste_status="ok")
        result = self.svc.handle_cleanup_old_history({"older_than_days": 10000})
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(result["remaining"], 1)

    def test_cleanup_returns_expected_keys(self):
        """Ответ содержит deleted_count и remaining."""
        result = self.svc.handle_cleanup_old_history({"older_than_days": 1})
        self.assertIn("deleted_count", result)
        self.assertIn("remaining", result)


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class ClipboardHistoryRoundTripTestCase(unittest.TestCase):
    """Тесты clipboard history + repaste round-trip."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self._clipboard: list[dict] = []
        self.svc = HistoryService(store=store, clipboard_history=self._clipboard)

    def test_clipboard_history_initially_empty(self):
        result = self.svc.handle_get_clipboard_history({})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count"], 0)

    def test_clipboard_history_after_add(self):
        """После добавления элемента в clipboard_history он отображается."""
        entry = {"text": "hello paste", "ts": "2026-01-01T00:00:00Z", "history_id": "abc123"}
        self._clipboard.append(entry)
        result = self.svc.handle_get_clipboard_history({})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["text"], "hello paste")

    def test_repaste_round_trip(self):
        """repaste_item находит запись по history_id."""
        entry = {"text": "repaste text", "ts": "2026-01-01T00:00:00Z", "history_id": "xyz789"}
        self._clipboard.append(entry)
        result = self.svc.handle_repaste_item({"history_id": "xyz789"})
        self.assertTrue(result["found"])
        self.assertEqual(result["text"], "repaste text")
        self.assertEqual(result["history_id"], "xyz789")

    def test_repaste_missing_id_raises(self):
        """repaste без history_id вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_repaste_item({})

    def test_repaste_nonexistent_id_raises(self):
        """repaste с несуществующим history_id вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_repaste_item({"history_id": "no-such-entry"})

    def test_clipboard_limit_respected(self):
        """limit=1 возвращает только последний элемент."""
        for i in range(5):
            self._clipboard.append({
                "text": f"entry {i}", "ts": "2026-01-01T00:00:00Z",
                "history_id": f"id{i}",
            })
        result = self.svc.handle_get_clipboard_history({"limit": 1})
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["text"], "entry 4")


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class FilterByConfidenceEdgeTestCase(unittest.TestCase):
    """Тесты handle_filter_by_confidence — invalid params + границы."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_missing_min_confidence_raises(self):
        """Отсутствие min_confidence вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_filter_by_confidence({})

    def test_min_confidence_out_of_range_raises(self):
        """min_confidence > 1.0 вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_filter_by_confidence({"min_confidence": 1.5})

    def test_max_less_than_min_raises(self):
        """max_confidence < min_confidence вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_filter_by_confidence(
                {"min_confidence": 0.8, "max_confidence": 0.5}
            )

    def test_filter_returns_empty_on_empty_store(self):
        """Пустое хранилище возвращает count=0."""
        result = self.svc.handle_filter_by_confidence({"min_confidence": 0.0})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["avg_confidence"], 0.0)


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class GetHistoryStatisticsEdgeTestCase(unittest.TestCase):
    """Тесты handle_get_history_statistics."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_statistics_empty_store(self):
        """Пустая история: total_items=0, date_range=None."""
        result = self.svc.handle_get_history_statistics({})
        self.assertEqual(result["total_items"], 0)
        self.assertIsNone(result["date_range"])
        self.assertEqual(result["avg_confidence"], 0.0)

    def test_statistics_with_one_item(self):
        """Одна запись: total_items=1, ключи в наличии."""
        self.store.add_history_item(text="one item test", paste_status="ok")
        result = self.svc.handle_get_history_statistics({})
        self.assertEqual(result["total_items"], 1)
        self.assertIn("total_words", result)
        self.assertIn("languages", result)
        self.assertIn("daily_counts", result)


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class GetHistoryItemEdgeTestCase(unittest.TestCase):
    """Тесты handle_get_history_item — edge cases."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_get_nonexistent_item_raises(self):
        """Несуществующий ID вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_get_history_item({"id": "does-not-exist-9999"})

    def test_get_item_empty_id_raises(self):
        """Пустой id вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_get_history_item({"id": ""})

    def test_get_existing_item_has_word_count(self):
        """Существующий item содержит word_count и text_length."""
        item = self.store.add_history_item(text="one two three", paste_status="ok")
        result = self.svc.handle_get_history_item({"id": item.id})
        self.assertEqual(result["word_count"], 3)
        self.assertEqual(result["text_length"], len("one two three"))
        self.assertEqual(result["id"], item.id)


if __name__ == "__main__":
    unittest.main()
