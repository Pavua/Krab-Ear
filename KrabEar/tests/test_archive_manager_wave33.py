"""Wave-33 tests for ArchiveManager: B1 field preservation, B2 purge-epoch, B3 privacy gate."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.archive_manager import ArchiveManager  # noqa: E402


# ---------------------------------------------------------------------------
# Fake helpers (mirror from test_archive_manager.py, self-contained)
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    """Minimal fake HistoryItem."""

    def __init__(self, item_id: str, text: str, ts: str = "2026-01-01T10:00:00") -> None:
        self.id = item_id
        self.text = text
        self.ts = ts

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "ts": self.ts, "text": self.text}


class FakeStore:
    """Minimal fake StateStore with restore_history_item_raw support."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir)
        self._items: dict[str, FakeHistoryItem] = {}
        self._deleted: set[str] = set()
        self._added: list[dict[str, Any]] = []
        self._raw_restored: list[dict[str, Any]] = []

    def add_fake_item(self, item_id: str, text: str) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        if item_id in self._deleted:
            return None
        return self._items.get(item_id)

    def delete_history_item(self, item_id: str) -> bool:
        if item_id in self._items:
            self._deleted.add(item_id)
            return True
        return False

    def add_history_item(self, text: str, **kwargs: Any) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id="new-" + text[:8], text=text)
        self._items[item.id] = item
        self._added.append({"text": text, **kwargs})
        return item

    def restore_history_item_raw(self, raw_dict: dict[str, Any]) -> str:
        item_id = str(raw_dict.get("id", "")).strip()
        if not item_id:
            import uuid as _uuid
            item_id = str(_uuid.uuid4())
        payload = dict(raw_dict)
        active_ids = {k for k in self._items if k not in self._deleted}
        if item_id in active_ids:
            item_id = item_id + "-restored"
        payload["id"] = item_id
        item = FakeHistoryItem(item_id=item_id, text=payload.get("text", ""))
        self._items[item_id] = item
        self._raw_restored.append(payload)
        return item_id


# ---------------------------------------------------------------------------
# B1: unarchive field preservation + original id
# ---------------------------------------------------------------------------

class UnarchiveFieldPreservationTestCase(unittest.TestCase):
    """B1 (HIGH) — unarchive_items preserves all original fields + original id."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = ArchiveManager(store=self._store)

    def _write_rich_item_to_archive(self, item_id: str) -> dict[str, Any]:
        """Write a rich archived dict directly into archive.ndjson."""
        rich = {
            "id": item_id,
            "ts": "2026-05-26T12:00:00",
            "text": "Оригинальный текст",
            "paste_status": "ok",
            "source_text": "src",
            "translated_text": "translated",
            "translation_mode": "ru_es",
            "source_lang": "ru",
            "target_lang": "es",
            "translation_status": "done",
            "translation_engine": "opus-mt",
            "chat_id": "chat123",
            "message_id": "msg456",
            "cleaned_text": "cleaned",
            "llm_applied": True,
            "llm_latency_ms": 150,
            "diarization": {"speaker_0": [0.0, 2.5]},
            "audio_duration_sec": 5.0,
            "confidence": 0.95,
            "tags": ["важный", "встреча"],
            "favorite": True,
            "speaker_count": 2,
            "archived_at": "2026-05-26T11:00:00",
        }
        self._mgr._append_ndjson(self._mgr._archive_path, rich)
        return rich

    def test_unarchive_preserves_original_id(self) -> None:
        """unarchive_items does not mint a new UUID; original id is preserved."""
        self._write_rich_item_to_archive("original-id-42")
        self._mgr.unarchive_items(item_ids=["original-id-42"])

        self.assertEqual(len(self._store._raw_restored), 1)
        self.assertEqual(self._store._raw_restored[0]["id"], "original-id-42")

    def test_unarchive_preserves_all_metadata_fields(self) -> None:
        """restore_history_item_raw receives all original fields, excluding archived_at."""
        self._write_rich_item_to_archive("meta-full-1")
        self._mgr.unarchive_items(item_ids=["meta-full-1"])

        self.assertEqual(len(self._store._raw_restored), 1)
        restored = self._store._raw_restored[0]

        expected_fields = [
            "ts", "text", "paste_status", "source_text", "translated_text",
            "translation_mode", "source_lang", "target_lang", "translation_status",
            "translation_engine", "chat_id", "message_id", "cleaned_text",
            "llm_applied", "llm_latency_ms", "diarization", "audio_duration_sec",
            "confidence", "tags", "favorite", "speaker_count",
        ]
        for field in expected_fields:
            self.assertIn(field, restored, f"Field '{field}' missing from restored dict")

        self.assertNotIn("archived_at", restored, "archived_at must be stripped on restore")

    def test_unarchive_drops_archived_at_field(self) -> None:
        """archived_at is removed before reinserting into active store."""
        self._write_rich_item_to_archive("strip-archived-at")
        self._mgr.unarchive_items(item_ids=["strip-archived-at"])

        self.assertNotIn("archived_at", self._store._raw_restored[0])

    def test_unarchive_id_suffix_on_collision(self) -> None:
        """On id collision with active item, -restored suffix is appended (not new UUID)."""
        self._write_rich_item_to_archive("collide-33")
        self._store.add_fake_item("collide-33", "Already active")

        self._mgr.unarchive_items(item_ids=["collide-33"])

        restored_id = self._store._raw_restored[0]["id"]
        self.assertEqual(restored_id, "collide-33-restored")

    def test_unarchive_text_value_preserved(self) -> None:
        """Text content of the archived item is not modified."""
        self._write_rich_item_to_archive("text-check-1")
        self._mgr.unarchive_items(item_ids=["text-check-1"])

        self.assertEqual(self._store._raw_restored[0]["text"], "Оригинальный текст")

    def test_unarchive_numeric_field_values_preserved(self) -> None:
        """Numeric fields (confidence, duration, latency) are not coerced."""
        self._write_rich_item_to_archive("num-check-1")
        self._mgr.unarchive_items(item_ids=["num-check-1"])

        restored = self._store._raw_restored[0]
        self.assertAlmostEqual(restored["confidence"], 0.95)
        self.assertAlmostEqual(restored["audio_duration_sec"], 5.0)
        self.assertEqual(restored["llm_latency_ms"], 150)

    def test_unarchive_boolean_field_preserved(self) -> None:
        """Boolean fields (llm_applied, favorite) retain their type."""
        self._write_rich_item_to_archive("bool-check-1")
        self._mgr.unarchive_items(item_ids=["bool-check-1"])

        restored = self._store._raw_restored[0]
        self.assertIs(restored["llm_applied"], True)
        self.assertIs(restored["favorite"], True)

    def test_unarchive_complex_field_preserved(self) -> None:
        """Complex nested fields (diarization dict, tags list) are preserved."""
        self._write_rich_item_to_archive("complex-1")
        self._mgr.unarchive_items(item_ids=["complex-1"])

        restored = self._store._raw_restored[0]
        self.assertEqual(restored["diarization"], {"speaker_0": [0.0, 2.5]})
        self.assertEqual(restored["tags"], ["важный", "встреча"])


# ---------------------------------------------------------------------------
# B2: purge-epoch guard in unarchive_items
# ---------------------------------------------------------------------------

class UnarchivePurgeEpochTestCase(unittest.TestCase):
    """B2 (MED) — unarchive_items mirrors purge-epoch guard from archive_items."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = ArchiveManager(store=self._store)

    def _put_item_in_archive(self, item_id: str) -> None:
        self._mgr._append_ndjson(
            self._mgr._archive_path,
            {"id": item_id, "text": "data", "archived_at": "2026-01-01T00:00:00"},
        )

    def test_unarchive_succeeds_when_no_purge(self) -> None:
        """Normal unarchive (no purge) returns unarchived_count=1."""
        self._put_item_in_archive("epoch-ok-1")
        result = self._mgr.unarchive_items(item_ids=["epoch-ok-1"])
        self.assertEqual(result.get("unarchived_count"), 1)

    def test_unarchive_blocked_when_epoch_changes_before_lock(self) -> None:
        """If purge increments epoch between snapshot and lock, unarchive is cancelled."""
        self._put_item_in_archive("epoch-cancel-1")

        original_lock = self._mgr._lock
        epoch_bumped = threading.Event()

        class BumpingLock:
            """Context manager that bumps the purge epoch before yielding."""

            def __enter__(self_inner) -> None:
                # Simulate purge happening right before we enter the critical section.
                with self._mgr._epoch_lock:
                    self._mgr._purge_epoch += 1
                epoch_bumped.set()
                return original_lock.__enter__()

            def __exit__(self_inner, *args: Any) -> None:
                return original_lock.__exit__(*args)

        self._mgr._lock = BumpingLock()  # type: ignore[assignment]
        result = self._mgr.unarchive_items(item_ids=["epoch-cancel-1"])

        self.assertTrue(epoch_bumped.is_set())
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "purge_in_progress")

    def test_unarchive_blocked_returns_correct_structure(self) -> None:
        """When blocked by epoch (concurrent purge during lock), result has ok=False, reason=purge_in_progress."""
        self._put_item_in_archive("epoch-struct-1")

        # Simulate purge BETWEEN snapshot and lock acquisition: use the same
        # BumpingLock pattern that increments epoch inside __enter__.
        original_lock = self._mgr._lock

        class BumpingLock:
            def __enter__(self_inner) -> None:
                with self._mgr._epoch_lock:
                    self._mgr._purge_epoch += 1
                return original_lock.__enter__()

            def __exit__(self_inner, *args: Any) -> None:
                return original_lock.__exit__(*args)

        self._mgr._lock = BumpingLock()  # type: ignore[assignment]
        result = self._mgr.unarchive_items(item_ids=["epoch-struct-1"])

        self.assertIn("ok", result)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "purge_in_progress")

    def test_unarchive_after_clear_all_epoch_incremented(self) -> None:
        """clear_all() increments the purge epoch exactly once."""
        epoch_before = self._mgr._current_epoch()
        self._mgr.clear_all()
        epoch_after = self._mgr._current_epoch()
        self.assertEqual(epoch_after, epoch_before + 1)

    def test_epoch_not_bumped_by_normal_unarchive(self) -> None:
        """A normal unarchive does NOT bump the purge epoch."""
        self._put_item_in_archive("epoch-stable-1")
        epoch_before = self._mgr._current_epoch()
        self._mgr.unarchive_items(item_ids=["epoch-stable-1"])
        self.assertEqual(self._mgr._current_epoch(), epoch_before)

    def test_unarchive_concurrent_purge_race(self) -> None:
        """Concurrent clear_all during unarchive: one or the other wins, no PII resurrection."""
        for i in range(3):
            self._put_item_in_archive(f"race-{i}")

        results: list[dict[str, Any]] = []
        errors: list[Exception] = []

        def do_unarchive() -> None:
            try:
                r = self._mgr.unarchive_items(item_ids=["race-0", "race-1", "race-2"])
                results.append(r)
            except Exception as exc:
                errors.append(exc)

        def do_purge() -> None:
            try:
                self._mgr.clear_all()
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=do_unarchive)
        t2 = threading.Thread(target=do_purge)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(errors, [], f"Errors during race: {errors}")
        # Regardless of ordering, no exception was raised.
        self.assertTrue(len(results) == 1)


# ---------------------------------------------------------------------------
# B3: privacy gate in handle_archive_items
# ---------------------------------------------------------------------------

class HandleArchivePrivacyGateTestCase(unittest.TestCase):
    """B3 (LOW) — handle_archive_items rejects requests when privacy_mode is set."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = ArchiveManager(store=self._store)

    def test_handle_archive_items_blocked_in_privacy_mode(self) -> None:
        """With privacy_mode=True, handle_archive_items returns ok=False."""
        self._store.add_fake_item("priv-1", "Приватная запись")
        result = self._mgr.handle_archive_items({"item_ids": ["priv-1"], "privacy_mode": True})

        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "privacy_mode_enabled")

    def test_handle_archive_items_blocked_does_not_archive(self) -> None:
        """When blocked by privacy_mode, no items are written to archive."""
        self._store.add_fake_item("priv-2", "Не архивировать")
        self._mgr.handle_archive_items({"item_ids": ["priv-2"], "privacy_mode": True})

        archived = self._mgr.list_archived()
        ids = {item["id"] for item in archived}
        self.assertNotIn("priv-2", ids)

    def test_handle_archive_items_succeeds_without_privacy_mode(self) -> None:
        """Normal call (no privacy_mode) succeeds and archives the item."""
        self._store.add_fake_item("priv-3", "Архивировать нормально")
        result = self._mgr.handle_archive_items({"item_ids": ["priv-3"]})

        self.assertIn("archived_count", result)
        self.assertEqual(result["archived_count"], 1)

    def test_handle_archive_items_privacy_mode_false_allowed(self) -> None:
        """privacy_mode=False (explicit falsy) does not block archiving."""
        self._store.add_fake_item("priv-4", "Явно не приватный")
        result = self._mgr.handle_archive_items({"item_ids": ["priv-4"], "privacy_mode": False})

        self.assertIn("archived_count", result)
        self.assertEqual(result["archived_count"], 1)

    def test_handle_archive_items_privacy_mode_absent_allowed(self) -> None:
        """Absent privacy_mode key does not block archiving."""
        self._store.add_fake_item("priv-5", "Без privacy_mode ключа")
        result = self._mgr.handle_archive_items({"item_ids": ["priv-5"]})

        self.assertEqual(result.get("archived_count"), 1)

    def test_handle_archive_items_privacy_mode_item_stays_in_active(self) -> None:
        """When blocked, the item is not deleted from active store."""
        self._store.add_fake_item("priv-6", "Остаётся активной")
        self._mgr.handle_archive_items({"item_ids": ["priv-6"], "privacy_mode": True})

        # FakeStore.delete_history_item marks items as deleted; none should be marked.
        self.assertNotIn("priv-6", self._store._deleted)

    def test_handle_archive_privacy_mode_returns_ok_false_reason(self) -> None:
        """Result shape is {ok: False, reason: privacy_mode_enabled}."""
        result = self._mgr.handle_archive_items({"item_ids": [], "privacy_mode": True})
        self.assertIn("ok", result)
        self.assertIn("reason", result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "privacy_mode_enabled")


if __name__ == "__main__":
    unittest.main()
