"""Тесты auto_cleanup_old, get_storage_breakdown (StateStore) и throttle-категорий."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.state_store import StateStore
from backend.ipc_throttle import HEAVY_METHODS, MEDIUM_METHODS


def _make_store(tmp_dir: Path) -> StateStore:
    return StateStore(data_dir=tmp_dir)


def _add_item(store: StateStore, ts: str, text: str = "test") -> str:
    """Добавляет запись в историю с заданным ts и возвращает её id."""
    item = store.add_history_item(
        text=text,
        paste_status="ok",
        source_text=text,
    )
    item_id = item.id
    # Патчим ts в NDJSON файле напрямую (StateStore не принимает ts при вставке)
    lines = store.history_path.read_text().splitlines()
    new_lines = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            new_lines.append(line)
            continue
        if rec.get("id") == item_id:
            rec["ts"] = ts
        new_lines.append(json.dumps(rec, ensure_ascii=False))
    store.history_path.write_text("\n".join(new_lines) + "\n")
    return item_id


class TestAutoCleanupOld(unittest.TestCase):
    """Тесты StateStore.auto_cleanup_old()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)
        self.store = _make_store(self._data_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_store_returns_zero_deleted(self) -> None:
        result = self.store.auto_cleanup_old(days=365)
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(result["remaining"], 0)
        self.assertFalse(result["dry_run"])

    def test_raises_on_invalid_days(self) -> None:
        with self.assertRaises(ValueError):
            self.store.auto_cleanup_old(days=0)

    def test_recent_items_not_deleted(self) -> None:
        recent_ts = datetime.now().isoformat()
        _add_item(self.store, ts=recent_ts)
        result = self.store.auto_cleanup_old(days=365)
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(result["remaining"], 1)

    def test_old_items_deleted(self) -> None:
        old_ts = (datetime.now() - timedelta(days=400)).isoformat()
        _add_item(self.store, ts=old_ts)
        result = self.store.auto_cleanup_old(days=365)
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["remaining"], 0)

    def test_mixed_items_only_old_deleted(self) -> None:
        old_ts = (datetime.now() - timedelta(days=400)).isoformat()
        recent_ts = datetime.now().isoformat()
        _add_item(self.store, ts=old_ts, text="old")
        _add_item(self.store, ts=recent_ts, text="recent")
        result = self.store.auto_cleanup_old(days=365)
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["remaining"], 1)

    def test_dry_run_does_not_delete(self) -> None:
        old_ts = (datetime.now() - timedelta(days=400)).isoformat()
        _add_item(self.store, ts=old_ts)
        result = self.store.auto_cleanup_old(days=365, dry_run=True)
        self.assertEqual(result["deleted_count"], 1)
        self.assertTrue(result["dry_run"])
        # После dry_run запись должна оставаться
        items_after, _ = self.store.get_history_page(cursor=None, limit=100)
        self.assertEqual(len(items_after), 1)

    def test_tombstones_written_for_deleted_items(self) -> None:
        old_ts = (datetime.now() - timedelta(days=400)).isoformat()
        item_id = _add_item(self.store, ts=old_ts)
        self.store.auto_cleanup_old(days=365)
        # Удалённый item должен отсутствовать в активных
        items, _ = self.store.get_history_page(cursor=None, limit=100)
        ids = [i["id"] for i in items]
        self.assertNotIn(item_id, ids)

    def test_threshold_days_in_result(self) -> None:
        result = self.store.auto_cleanup_old(days=180)
        self.assertEqual(result["threshold_days"], 180)

    def test_oldest_item_age_days_present(self) -> None:
        old_ts = (datetime.now() - timedelta(days=400)).isoformat()
        _add_item(self.store, ts=old_ts)
        result = self.store.auto_cleanup_old(days=500, dry_run=True)
        self.assertIsNotNone(result["oldest_item_age_days"])
        self.assertGreaterEqual(result["oldest_item_age_days"], 390)

    def test_result_fields_present(self) -> None:
        result = self.store.auto_cleanup_old(days=365)
        for key in ("deleted_count", "remaining", "dry_run",
                    "threshold_days", "oldest_item_age_days"):
            self.assertIn(key, result, f"missing key: {key}")


class TestGetStorageBreakdown(unittest.TestCase):
    """Тесты StateStore.get_storage_breakdown()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)
        self.store = _make_store(self._data_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_required_keys(self) -> None:
        result = self.store.get_storage_breakdown()
        for key in ("ndjson_mb", "transcripts_mb", "audio_mb",
                    "total_mb", "oldest_item_age_days"):
            self.assertIn(key, result, f"missing key: {key}")

    def test_ndjson_mb_positive_after_write(self) -> None:
        recent_ts = datetime.now().isoformat()
        _add_item(self.store, ts=recent_ts)
        result = self.store.get_storage_breakdown()
        self.assertGreater(result["ndjson_mb"], 0.0)

    def test_transcripts_mb_zero_for_empty_dir(self) -> None:
        result = self.store.get_storage_breakdown()
        self.assertEqual(result["transcripts_mb"], 0.0)

    def test_transcripts_mb_increases_with_files(self) -> None:
        transcripts_dir = self._data_dir / "transcripts"
        transcripts_dir.mkdir()
        (transcripts_dir / "test.md").write_bytes(b"x" * 1024)
        result = self.store.get_storage_breakdown()
        self.assertGreater(result["transcripts_mb"], 0.0)

    def test_total_mb_is_sum_of_parts(self) -> None:
        result = self.store.get_storage_breakdown()
        expected = round(
            result["ndjson_mb"] + result["transcripts_mb"] + result["audio_mb"], 3
        )
        self.assertAlmostEqual(result["total_mb"], expected, places=2)

    def test_oldest_item_age_days_none_for_empty_store(self) -> None:
        result = self.store.get_storage_breakdown()
        self.assertIsNone(result["oldest_item_age_days"])

    def test_oldest_item_age_days_correct(self) -> None:
        old_ts = (datetime.now() - timedelta(days=100)).isoformat()
        _add_item(self.store, ts=old_ts)
        result = self.store.get_storage_breakdown()
        self.assertIsNotNone(result["oldest_item_age_days"])
        self.assertGreaterEqual(result["oldest_item_age_days"], 95)


class TestIpcHandlerThrottle(unittest.TestCase):
    """Тесты категорий throttle для новых методов."""

    def test_auto_cleanup_old_in_heavy(self) -> None:
        self.assertIn("auto_cleanup_old", HEAVY_METHODS)

    def test_get_disk_status_in_medium(self) -> None:
        self.assertIn("get_disk_status", MEDIUM_METHODS)

    def test_get_storage_breakdown_in_medium(self) -> None:
        self.assertIn("get_storage_breakdown", MEDIUM_METHODS)


if __name__ == "__main__":
    unittest.main()
