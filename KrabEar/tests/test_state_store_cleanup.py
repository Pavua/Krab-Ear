"""Тесты get_storage_breakdown (StateStore) и throttle-категорий."""

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


if __name__ == "__main__":
    unittest.main()
