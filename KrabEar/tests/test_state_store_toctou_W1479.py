"""Tests for W1471 F2 fix in StateStore.

F1 (атомарность лока в auto_cleanup_old) удалён 03.09.2026 вместе с самим
методом — цепь автоочистки была мёртвой.

F2: import_history_ndjson uses tombstone IDs in addition to existing active IDs
    for dedup — prevents resurrection of deleted items after compaction.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
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
    """Add a history item with a specific timestamp, return its id."""
    item = store.add_history_item(text=text, paste_status="ok", source_text=text)
    item_id = item.id
    # Patch ts in the NDJSON file directly (StateStore doesn't accept ts on insert)
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


def _make_import_file(tmp_dir: Path, items: list[dict]) -> Path:
    """Write a list of dicts as NDJSON to an import file."""
    import_path = tmp_dir / "import.ndjson"
    with import_path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    return import_path


class TestImportHistoryNdjsonSkipsTombstonedIds(unittest.TestCase):
    """F2: import_history_ndjson must not re-import tombstoned (deleted) items."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)
        self.store = _make_store(self._data_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_import_history_ndjson_skips_tombstoned_ids(self) -> None:
        """Items present in tombstones_path must be skipped during import."""
        # Add an item then delete it (writes tombstone)
        item = self.store.add_history_item(text="to be deleted", paste_status="ok")
        item_id = item.id
        self.store.delete_history_item(item_id)

        # Verify item is gone from active
        items_before, _ = self.store.get_history_page(cursor=None, limit=100)
        self.assertEqual(len(items_before), 0)

        # Verify tombstone exists
        tombstone_ids = self.store._load_deleted_ids_unlocked()
        self.assertIn(item_id, tombstone_ids)

        # Build import file that contains the deleted item
        import_payload = {
            "id": item_id,
            "ts": datetime.now().isoformat(),
            "text": "to be deleted",
            "paste_status": "ok",
            "source_text": "to be deleted",
            "translated_text": "",
            "translation_mode": "off",
            "source_lang": "",
            "target_lang": "",
            "translation_status": "not_requested",
            "translation_engine": "",
        }
        import_path = _make_import_file(self._data_dir, [import_payload])

        result = self.store.import_history_ndjson(import_path)

        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], 0)

        # Item must still be absent from active history
        items_after, _ = self.store.get_history_page(cursor=None, limit=100)
        self.assertEqual(len(items_after), 0,
                         "Tombstoned item must not be re-imported")

    def test_import_doesnt_resurrect_deleted(self) -> None:
        """Deleted items MUST NOT be resurrected even after compaction clears tombstones.

        W1756 enforcing regression test (fail-before / pass-after):
          BEFORE fix: compact() clears tombstones_path; import sees no tombstone → item
            resurrected → imported==1, item appears in get_history_page.
          AFTER fix:  compact() writes tombstoned IDs to purged_ids_path before clearing
            tombstones_path; _load_deleted_ids_unlocked reads both sources;
            import_history_ndjson includes purged IDs in known_ids → skipped==1,
            item stays absent from active history.

        Revert of this fix (body-revert back to `known_ids = {..._iter_history_items_unlocked()}`)
        will cause this test to fail with: imported==1, len(items_after_import)==1.
        """
        # Step 1: add item and delete it (writes tombstone)
        item = self.store.add_history_item(text="resurrectable?", paste_status="ok")
        item_id = item.id
        self.store.delete_history_item(item_id)

        # Verify item is gone from active
        items_before_compact, _ = self.store.get_history_page(cursor=None, limit=100)
        self.assertEqual(len(items_before_compact), 0)

        # Step 2: compact — rewrites history.ndjson to active-only AND clears tombstones_path.
        # W1756 fix: compact also appends tombstoned IDs to purged_ids_path first.
        self.store.compact()

        # After compaction, tombstones_path MUST be empty (verify the prerequisite).
        tombstone_ids_post_compact = set()
        for payload in self.store._read_ndjson_unlocked(self.store.tombstones_path):
            item_id_check = str(payload.get("id", "")).strip()
            if item_id_check:
                tombstone_ids_post_compact.add(item_id_check)
        self.assertNotIn(
            item_id, tombstone_ids_post_compact,
            "Prerequisite: compact() must clear tombstones_path so that a naive fix fails"
        )

        # Item must still be absent from active history after compact
        items_after_compact, _ = self.store.get_history_page(cursor=None, limit=100)
        self.assertEqual(len(items_after_compact), 0,
                         "Item must not be active after delete+compact")

        # Step 3: import an NDJSON file containing the deleted item's ID and text.
        import_payload = {
            "id": item_id,
            "ts": datetime.now().isoformat(),
            "text": "resurrectable?",
            "paste_status": "ok",
            "source_text": "resurrectable?",
            "translated_text": "",
            "translation_mode": "off",
            "source_lang": "",
            "target_lang": "",
            "translation_status": "not_requested",
            "translation_engine": "",
        }
        import_path = _make_import_file(self._data_dir, [import_payload])
        result = self.store.import_history_ndjson(import_path)

        # ENFORCING assertions — these FAIL before the W1756 fix is applied:
        self.assertEqual(
            result["imported"], 0,
            f"Deleted item must NOT be re-imported (got imported={result['imported']}); "
            "W1756 fix restores tombstone-aware dedup via purged_ids_path"
        )
        self.assertEqual(
            result["skipped"], 1,
            f"Deleted item must be counted as skipped (got skipped={result['skipped']})"
        )
        self.assertEqual(result["errors"], 0)

        # Critically: the item must still be absent from active history
        items_after_import, _ = self.store.get_history_page(cursor=None, limit=100)
        self.assertEqual(
            len(items_after_import), 0,
            f"Deleted transcript must NOT reappear after import (found {len(items_after_import)} item(s)); "
            "this is a privacy guarantee violation (resurrection after compact)"
        )

    def test_import_new_items_not_affected_by_tombstone_check(self) -> None:
        """Items with IDs not in tombstones or existing history should be imported normally."""
        import_payload = {
            "id": "brand-new-id-xyz-12345",
            "ts": datetime.now().isoformat(),
            "text": "brand new import",
            "paste_status": "ok",
            "source_text": "brand new import",
            "translated_text": "",
            "translation_mode": "off",
            "source_lang": "",
            "target_lang": "",
            "translation_status": "not_requested",
            "translation_engine": "",
        }
        import_path = _make_import_file(self._data_dir, [import_payload])

        result = self.store.import_history_ndjson(import_path)

        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["errors"], 0)

        items, _ = self.store.get_history_page(cursor=None, limit=100)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "brand-new-id-xyz-12345")

    def test_load_deleted_ids_unlocked_returns_deleted_ids(self) -> None:
        """_load_deleted_ids_unlocked must return the same set as _load_deleted_ids_unlocked."""
        item = self.store.add_history_item(text="tombstone test", paste_status="ok")
        self.store.delete_history_item(item.id)

        with self.store._lock():
            deleted_ids = self.store._load_deleted_ids_unlocked()
            tombstone_ids = self.store._load_deleted_ids_unlocked()

        self.assertEqual(deleted_ids, tombstone_ids)
        self.assertIn(item.id, tombstone_ids)

    def test_tombstone_ids_included_in_skip_set_during_import(self) -> None:
        """Import of a batch where some items are tombstoned — only fresh ones imported."""
        # Add and delete item A
        item_a = self.store.add_history_item(text="item A", paste_status="ok")
        self.store.delete_history_item(item_a.id)

        # Build import file: item A (tombstoned) + item B (fresh)
        import_payloads = [
            {
                "id": item_a.id,
                "ts": datetime.now().isoformat(),
                "text": "item A",
                "paste_status": "ok",
                "source_text": "item A",
                "translated_text": "",
                "translation_mode": "off",
                "source_lang": "",
                "target_lang": "",
                "translation_status": "not_requested",
                "translation_engine": "",
            },
            {
                "id": "fresh-item-B-id-9999",
                "ts": datetime.now().isoformat(),
                "text": "item B",
                "paste_status": "ok",
                "source_text": "item B",
                "translated_text": "",
                "translation_mode": "off",
                "source_lang": "",
                "target_lang": "",
                "translation_status": "not_requested",
                "translation_engine": "",
            },
        ]
        import_path = _make_import_file(self._data_dir, import_payloads)

        result = self.store.import_history_ndjson(import_path)

        # item A skipped (tombstoned), item B imported
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], 0)

        items, _ = self.store.get_history_page(cursor=None, limit=100)
        ids = {i["id"] for i in items}
        self.assertNotIn(item_a.id, ids, "Tombstoned item A must not be imported")
        self.assertIn("fresh-item-B-id-9999", ids, "Fresh item B must be imported")


class TestW1756RegressionGuard(unittest.TestCase):
    """W1756 body-revert guard — ensures structural invariants of the fix are present.

    A future silent body-revert of either the _load_deleted_ids_unlocked change
    or the compact purged_ids persistence will cause these tests to FAIL, alerting
    maintainers before the privacy regression reaches production.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)
        self.store = StateStore(data_dir=self._data_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_purged_ids_path_attribute_exists(self) -> None:
        """StateStore must expose purged_ids_path (W1756 — deleted-ID permanent registry)."""
        self.assertTrue(
            hasattr(self.store, "purged_ids_path"),
            "W1756: StateStore.purged_ids_path missing — body-revert detected"
        )
        self.assertIsInstance(self.store.purged_ids_path, Path)

    def test_compact_writes_to_purged_ids_path(self) -> None:
        """compact() must append tombstoned IDs to purged_ids_path before clearing tombstones.

        W1756 guard: if compact() is reverted to NOT write purged_ids_path,
        test_import_doesnt_resurrect_deleted will also fail.
        """
        item = self.store.add_history_item(text="compact guard", paste_status="ok")
        item_id = item.id
        self.store.delete_history_item(item_id)

        self.store.compact()

        purged_content = self.store.purged_ids_path.read_text(encoding="utf-8")
        self.assertIn(
            item_id, purged_content,
            "W1756: compact() must persist tombstoned IDs to purged_ids_path"
        )

    def test_load_deleted_ids_unlocked_reads_purged_ids_path(self) -> None:
        """_load_deleted_ids_unlocked must include IDs from purged_ids_path.

        W1756 guard: reverts to reading only tombstones_path will cause
        test_import_doesnt_resurrect_deleted to fail.
        """
        # Write an ID directly to purged_ids_path (simulates post-compact state)
        test_id = "w1756-guard-test-id-001"
        with self.store.purged_ids_path.open("a", encoding="utf-8") as fh:
            import json as _j
            fh.write(_j.dumps({"id": test_id}, ensure_ascii=False) + "\n")

        with self.store._lock():
            deleted = self.store._load_deleted_ids_unlocked()

        self.assertIn(
            test_id, deleted,
            "W1756: _load_deleted_ids_unlocked must read from purged_ids_path — "
            "body-revert detected (only reads tombstones_path)"
        )


if __name__ == "__main__":
    unittest.main()
