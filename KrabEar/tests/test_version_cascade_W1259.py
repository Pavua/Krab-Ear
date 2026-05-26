"""Tests for W1254 F1 HIGH: version cascade on archive, merge, and compact delete paths.

Covers:
- test_archive_items_purges_versions
- test_merge_items_purges_versions
- test_state_store_compact_purges_versions
- test_purge_fail_does_not_break_delete
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.archive_manager import ArchiveManager
from backend.recording_merger import RecordingMerger
from backend.state_store import StateStore
from backend.transcript_versioning import TranscriptVersionManager


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeHistoryItem:
    id: str
    ts: str = "2026-01-01T10:00:00"
    text: str = "test"
    paste_status: str = "success"
    source_text: str = ""
    translated_text: str = ""
    translation_mode: str = "off"
    source_lang: str = ""
    target_lang: str = ""
    translation_status: str = "not_requested"
    audio_duration_sec: float | None = None
    confidence: float | None = None
    diarization: dict | None = None
    tags: list = field(default_factory=list)
    favorite: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "text": self.text,
            "paste_status": self.paste_status,
        }


class FakeArchiveStore:
    """Fake StateStore for ArchiveManager tests."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}
        self._deleted: list[str] = []

    def add_item(self, item_id: str, text: str = "text") -> FakeHistoryItem:
        item = FakeHistoryItem(id=item_id, text=text)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        return self._items.get(item_id)

    def delete_history_item(self, item_id: str) -> bool:
        if item_id in self._items:
            self._deleted.append(item_id)
            return True
        return False


class FakeMergeStore:
    """Fake StateStore for RecordingMerger tests."""

    def __init__(self) -> None:
        self._items: dict[str, FakeHistoryItem] = {}
        self._deleted: list[str] = []
        self._added: list[FakeHistoryItem] = []
        self._next_id = 0

    def add_fake_item(self, item_id: str, text: str = "text") -> FakeHistoryItem:
        item = FakeHistoryItem(id=item_id, text=text)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        return self._items.get(item_id)

    def delete_history_item(self, item_id: str) -> bool:
        if item_id in self._items:
            self._deleted.append(item_id)
            return True
        return False

    def add_history_item(self, text: str, **kwargs: Any) -> FakeHistoryItem:
        self._next_id += 1
        new_id = f"merged_{self._next_id}"
        item = FakeHistoryItem(id=new_id, text=text)
        self._added.append(item)
        return item


# ---------------------------------------------------------------------------
# Tests: ArchiveManager
# ---------------------------------------------------------------------------


class TestArchiveItemsPurgesVersions(unittest.TestCase):
    """archive_items должен чистить версии через transcript_versioner."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = FakeArchiveStore(Path(self.temp_dir.name))
        self.versioner = TranscriptVersionManager(self.temp_dir.name)
        self.manager = ArchiveManager(
            store=self.store,
            transcript_versioner=self.versioner,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_archive_items_purges_versions(self) -> None:
        """Версии удаляются после архивирования записи."""
        self.store.add_item("item_a", "Привет мир")
        self.versioner.save_version("item_a", "Привет мир", source="manual")
        self.versioner.save_version("item_a", "Привет мир v2", source="manual")

        self.assertEqual(len(self.versioner.get_versions("item_a")), 2)

        result = self.manager.archive_items(["item_a"])

        self.assertEqual(result.archived_count, 1)
        # Versions must be purged
        self.assertEqual(len(self.versioner.get_versions("item_a")), 0)

    def test_archive_items_without_versioner_no_error(self) -> None:
        """Без versioner архивирование работает нормально."""
        self.store.add_item("item_b", "Текст")
        manager_no_ver = ArchiveManager(store=self.store)
        result = manager_no_ver.archive_items(["item_b"])
        self.assertEqual(result.archived_count, 1)

    def test_archive_preserves_versions_of_other_items(self) -> None:
        """Версии других записей НЕ трогаются при архивировании."""
        self.store.add_item("item_c", "Text C")
        self.store.add_item("item_d", "Text D")
        self.versioner.save_version("item_c", "Text C", source="manual")
        self.versioner.save_version("item_d", "Text D", source="manual")

        self.manager.archive_items(["item_c"])

        # item_d versions intact
        self.assertEqual(len(self.versioner.get_versions("item_d")), 1)
        # item_c versions purged
        self.assertEqual(len(self.versioner.get_versions("item_c")), 0)


class TestArchivePurgeFailDoesNotBreakDelete(unittest.TestCase):
    """Ошибка purge не должна прерывать архивирование."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = FakeArchiveStore(Path(self.temp_dir.name))
        self.bad_versioner = MagicMock()
        self.bad_versioner.purge_versions_for_item.side_effect = RuntimeError("disk full")
        self.manager = ArchiveManager(
            store=self.store,
            transcript_versioner=self.bad_versioner,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_purge_fail_does_not_break_archive(self) -> None:
        """RuntimeError в purge_versions_for_item не должен прерывать archive_items."""
        self.store.add_item("item_e", "Text E")

        # Should not raise
        result = self.manager.archive_items(["item_e"])

        self.assertEqual(result.archived_count, 1)
        self.assertIn("item_e", self.store._deleted)
        self.bad_versioner.purge_versions_for_item.assert_called_once_with("item_e")


# ---------------------------------------------------------------------------
# Tests: RecordingMerger
# ---------------------------------------------------------------------------


class TestMergeItemsPurgesVersions(unittest.TestCase):
    """merge_items с delete_originals=True должен чистить версии оригиналов."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = FakeMergeStore()
        self.versioner = TranscriptVersionManager(self.temp_dir.name)
        self.merger = RecordingMerger(transcript_versioner=self.versioner)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_merge_items_purges_versions_when_delete_originals(self) -> None:
        """Версии оригиналов удаляются при merge с delete_originals=True."""
        self.store.add_fake_item("a1", "Текст один")
        self.store.add_fake_item("a2", "Текст два")
        self.versioner.save_version("a1", "Текст один", source="stt_raw")
        self.versioner.save_version("a2", "Текст два", source="stt_raw")

        self.merger.merge_items(["a1", "a2"], self.store, delete_originals=True)

        self.assertEqual(len(self.versioner.get_versions("a1")), 0)
        self.assertEqual(len(self.versioner.get_versions("a2")), 0)

    def test_merge_items_keeps_versions_when_not_deleting(self) -> None:
        """Версии оригиналов НЕ удаляются при merge с delete_originals=False."""
        self.store.add_fake_item("b1", "Текст три")
        self.store.add_fake_item("b2", "Текст четыре")
        self.versioner.save_version("b1", "Текст три", source="stt_raw")
        self.versioner.save_version("b2", "Текст четыре", source="stt_raw")

        self.merger.merge_items(["b1", "b2"], self.store, delete_originals=False)

        # Versions must remain
        self.assertEqual(len(self.versioner.get_versions("b1")), 1)
        self.assertEqual(len(self.versioner.get_versions("b2")), 1)

    def test_merge_without_versioner_no_error(self) -> None:
        """RecordingMerger без versioner работает нормально."""
        self.store.add_fake_item("c1", "T1")
        self.store.add_fake_item("c2", "T2")
        merger_no_ver = RecordingMerger()
        # Should not raise
        result = merger_no_ver.merge_items(["c1", "c2"], self.store, delete_originals=True)
        self.assertIn("merged_from", result)


class TestMergePurgeFailDoesNotBreakDelete(unittest.TestCase):
    """Ошибка purge не должна прерывать слияние."""

    def setUp(self) -> None:
        self.store = FakeMergeStore()
        self.bad_versioner = MagicMock()
        self.bad_versioner.purge_versions_for_item.side_effect = RuntimeError("io error")
        self.merger = RecordingMerger(transcript_versioner=self.bad_versioner)

    def test_purge_fail_does_not_break_merge(self) -> None:
        """RuntimeError в purge не должен прерывать merge_items."""
        self.store.add_fake_item("d1", "T1")
        self.store.add_fake_item("d2", "T2")

        # Should not raise
        result = self.merger.merge_items(["d1", "d2"], self.store, delete_originals=True)

        self.assertIn("d1", self.store._deleted)
        self.assertIn("d2", self.store._deleted)
        self.assertIn("merged_from", result)


# ---------------------------------------------------------------------------
# Tests: StateStore compaction
# ---------------------------------------------------------------------------


class TestStateStoreCompactPurgesVersions(unittest.TestCase):
    """compact() должен чистить версии tombstoned-записей через _transcript_versioner."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp_dir.name)
        self.store = StateStore(data_dir=data_dir)
        self.versioner = TranscriptVersionManager(self.temp_dir.name)
        # Late inject
        self.store._transcript_versioner = self.versioner

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_state_store_compact_purges_versions(self) -> None:
        """Версии удалённых (tombstoned) записей очищаются при compaction."""
        item = self.store.add_history_item(text="Компактирование тест")
        item_id = item.id
        self.versioner.save_version(item_id, "Компактирование тест", source="stt_raw")

        # Soft delete (tombstone)
        self.store.delete_history_item(item_id)

        # Verify version exists before compaction
        self.assertEqual(len(self.versioner.get_versions(item_id)), 1)

        # Compact
        self.store.compact()

        # Version must be purged
        self.assertEqual(len(self.versioner.get_versions(item_id)), 0)

    def test_compact_keeps_versions_of_active_items(self) -> None:
        """Версии активных (не удалённых) записей НЕ трогаются при compaction."""
        item1 = self.store.add_history_item(text="Активная запись")
        item2 = self.store.add_history_item(text="Удалённая запись")
        self.versioner.save_version(item1.id, "Активная запись", source="manual")
        self.versioner.save_version(item2.id, "Удалённая запись", source="manual")

        self.store.delete_history_item(item2.id)
        self.store.compact()

        # item1 version intact
        self.assertEqual(len(self.versioner.get_versions(item1.id)), 1)
        # item2 version purged
        self.assertEqual(len(self.versioner.get_versions(item2.id)), 0)

    def test_compact_without_versioner_no_error(self) -> None:
        """Compaction без versioner работает нормально."""
        store = StateStore(data_dir=Path(self.temp_dir.name) / "no_ver")
        store.data_dir.mkdir(parents=True, exist_ok=True)
        item = store.add_history_item(text="test")
        store.delete_history_item(item.id)
        # Should not raise
        store.compact()

    def test_compact_purge_fail_does_not_break_compaction(self) -> None:
        """Ошибка purge не должна прерывать compaction."""
        bad_versioner = MagicMock()
        bad_versioner.purge_versions_for_item.side_effect = RuntimeError("fail")
        self.store._transcript_versioner = bad_versioner

        item = self.store.add_history_item(text="Test compact fail")
        self.store.delete_history_item(item.id)

        # Should not raise
        self.store.compact()
        bad_versioner.purge_versions_for_item.assert_called_once_with(item.id)


# ---------------------------------------------------------------------------
# Tests: purge_versions_for_item itself
# ---------------------------------------------------------------------------


class TestPurgeVersionsForItem(unittest.TestCase):
    """Unit tests for TranscriptVersionManager.purge_versions_for_item."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_purge_removes_all_versions_for_item(self) -> None:
        self.manager.save_version("x1", "v1", source="manual")
        self.manager.save_version("x1", "v2", source="manual")
        self.manager.save_version("x2", "other", source="manual")

        count = self.manager.purge_versions_for_item("x1")

        self.assertEqual(count, 2)
        self.assertEqual(len(self.manager.get_versions("x1")), 0)
        # x2 untouched
        self.assertEqual(len(self.manager.get_versions("x2")), 1)

    def test_purge_empty_item_id_returns_zero(self) -> None:
        count = self.manager.purge_versions_for_item("")
        self.assertEqual(count, 0)

    def test_purge_nonexistent_item_returns_zero(self) -> None:
        count = self.manager.purge_versions_for_item("no_such_id")
        self.assertEqual(count, 0)

    def test_purge_strips_whitespace_from_item_id(self) -> None:
        self.manager.save_version("spaced_id", "text", source="manual")
        count = self.manager.purge_versions_for_item("  spaced_id  ")
        self.assertEqual(count, 1)
        self.assertEqual(len(self.manager.get_versions("spaced_id")), 0)

    def test_purge_persists_between_manager_instances(self) -> None:
        """Purge must persist — new instance sees empty versions."""
        self.manager.save_version("persist1", "text", source="manual")
        self.manager.purge_versions_for_item("persist1")

        new_manager = TranscriptVersionManager(self.temp_dir.name)
        self.assertEqual(len(new_manager.get_versions("persist1")), 0)


if __name__ == "__main__":
    unittest.main()
