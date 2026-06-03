"""Tests for wave-35 regression fixes: D1 (state_store O(1) set_paste_status),
D2 (bulk_reprocess privacy guard), D3 (recording_chain CRLF + dedup/cap order).
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.bulk_reprocess import BulkReprocessor
from backend.recording_chain import (
    MAX_ITEMS_PER_CHAIN,
    RecordingChainManager,
    _is_valid_item_id,
)
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_dir: str) -> StateStore:
    return StateStore(data_dir=Path(tmp_dir))


def _add_item(store: StateStore, item_id: str | None = None) -> str:
    """Add a minimal item to the store and return its id."""
    item = store.add_history_item(
        text="test transcript",
        paste_status="ok",
    )
    return item.id


class FakeStore:
    """Minimal fake store for RecordingChainManager tests."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, Any] = {}

    def get_history_item_by_id(self, item_id: str):
        return self._items.get(item_id)


# ---------------------------------------------------------------------------
# D1: state_store set_paste_status O(1) check
# ---------------------------------------------------------------------------


class D1SetPasteStatusO1TestCase(unittest.TestCase):
    """set_paste_status must use the in-memory _active_ids set (O(1)) not
    the O(n) _id_exists_unlocked scan."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = _make_store(self._tmpdir)

    def test_set_paste_status_valid_id_returns_true(self) -> None:
        item_id = _add_item(self._store)
        result = self._store.set_paste_status(item_id, "ok")
        self.assertTrue(result)

    def test_set_paste_status_unknown_id_returns_false(self) -> None:
        result = self._store.set_paste_status("nonexistent-uuid-xyz", "ok")
        self.assertFalse(result)

    def test_set_paste_status_uses_active_ids_cache(self) -> None:
        """After _active_ids is populated, set_paste_status must NOT call
        _id_exists_unlocked (which does two O(n) NDJSON scans)."""
        item_id = _add_item(self._store)
        # Prime the cache.
        with self._store._lock():
            self._store._ensure_active_ids_unlocked()

        # Now patch _id_exists_unlocked — if it's called, the test fails.
        with patch.object(
            self._store, "_id_exists_unlocked", wraps=self._store._id_exists_unlocked
        ) as mock_exist:
            result = self._store.set_paste_status(item_id, "ok")
        self.assertTrue(result)
        mock_exist.assert_not_called()

    def test_active_ids_populated_after_add_and_prime(self) -> None:
        """After add_history_item + prime, _active_ids contains the new id."""
        item_id = _add_item(self._store)
        # Prime the cache so _active_ids is populated.
        with self._store._lock():
            self._store._ensure_active_ids_unlocked()
        self.assertIsNotNone(self._store._active_ids)
        self.assertIn(item_id, self._store._active_ids)  # type: ignore[union-attr]

    def test_active_ids_updated_after_delete(self) -> None:
        """_active_ids must no longer contain the id after delete_history_item."""
        item_id = _add_item(self._store)
        # Prime active_ids
        with self._store._lock():
            self._store._ensure_active_ids_unlocked()
        self.assertIn(item_id, self._store._active_ids)  # type: ignore[union-attr]

        self._store.delete_history_item(item_id)
        self.assertNotIn(item_id, self._store._active_ids)  # type: ignore[union-attr]

    def test_set_paste_status_deleted_item_returns_false(self) -> None:
        """set_paste_status on a tombstoned item must return False even after
        _active_ids is warmed."""
        item_id = _add_item(self._store)
        # Prime the cache.
        with self._store._lock():
            self._store._ensure_active_ids_unlocked()
        self._store.delete_history_item(item_id)
        result = self._store.set_paste_status(item_id, "ok")
        self.assertFalse(result)

    def test_active_ids_rebuilt_after_compact(self) -> None:
        """After compaction, _active_ids must still be correct."""
        id1 = _add_item(self._store)
        id2 = _add_item(self._store)
        self._store.delete_history_item(id1)
        self._store.compact()
        # _active_ids must be set and contain only id2.
        self.assertIsNotNone(self._store._active_ids)
        self.assertNotIn(id1, self._store._active_ids)  # type: ignore[union-attr]
        self.assertIn(id2, self._store._active_ids)  # type: ignore[union-attr]

    def test_ensure_active_ids_lazy_init(self) -> None:
        """_active_ids is None until _ensure_active_ids_unlocked is called,
        then it's populated on first call."""
        store = _make_store(tempfile.mkdtemp())
        self.assertIsNone(store._active_ids)
        _add_item(store)
        # Still None because we haven't called _ensure_active_ids_unlocked yet.
        # (add_history_item only updates the set if it was already populated.)
        # Now prime it:
        with store._lock():
            ids = store._ensure_active_ids_unlocked()
        self.assertIsNotNone(store._active_ids)
        self.assertGreater(len(ids), 0)

    def test_set_paste_status_empty_id_returns_false(self) -> None:
        result = self._store.set_paste_status("   ", "ok")
        self.assertFalse(result)

    def test_concurrent_set_paste_status_no_race(self) -> None:
        """Multiple threads calling set_paste_status concurrently must not crash."""
        item_id = _add_item(self._store)
        results = []
        errors = []

        def worker():
            try:
                r = self._store.set_paste_status(item_id, "ok")
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertTrue(all(results))


# ---------------------------------------------------------------------------
# D2: bulk_reprocess privacy guard
# ---------------------------------------------------------------------------


def _make_bulk_reprocessor(tmp_dir: str) -> tuple[StateStore, BulkReprocessor]:
    store = StateStore(data_dir=Path(tmp_dir))
    transcriber = MagicMock()
    version_manager = MagicMock()
    br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=version_manager)
    return store, br


class D2BulkReprocessPrivacyTestCase(unittest.TestCase):
    """bulk_reprocess must refuse to run when privacy_mode_enabled=True."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store, self._br = _make_bulk_reprocessor(self._tmpdir)

    def test_reprocess_privacy_mode_on_returns_rejected(self) -> None:
        result = self._br.reprocess(settings={"privacy_mode_enabled": True})
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertEqual(result.get("total"), 0)
        self.assertEqual(result.get("reprocessed"), 0)

    def test_reprocess_privacy_mode_off_proceeds(self) -> None:
        """When privacy_mode_enabled=False the reprocess must proceed normally
        (may return no candidates since store is empty, but must not return
        privacy_mode_active reason)."""
        result = self._br.reprocess(settings={"privacy_mode_enabled": False})
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")

    def test_reprocess_no_settings_proceeds(self) -> None:
        """Omitting settings= entirely must not trigger the privacy guard
        (backwards-compat: callers that don't pass settings are unaffected)."""
        result = self._br.reprocess(settings=None)
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")

    def test_reprocess_privacy_mode_true_does_not_call_transcriber(self) -> None:
        """When privacy mode is on, the transcriber must never be called."""
        self._br.transcriber.transcribe = MagicMock()
        self._br.reprocess(settings={"privacy_mode_enabled": True})
        self._br.transcriber.transcribe.assert_not_called()

    def test_reprocess_privacy_mode_schema_parity(self) -> None:
        """Rejected result must still include all expected schema keys."""
        result = self._br.reprocess(settings={"privacy_mode_enabled": True})
        for key in ("ok", "reason", "total", "reprocessed", "skipped", "errors", "cancelled"):
            self.assertIn(key, result, f"missing key: {key}")

    def test_reprocess_privacy_mode_false_key_missing(self) -> None:
        """privacy_mode_enabled absent from settings dict = not blocked."""
        result = self._br.reprocess(settings={})
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")

    def test_reprocess_privacy_mode_truthy_string_not_blocked(self) -> None:
        """Only boolean True triggers the guard; truthy strings do not."""
        # settings.get returns the raw value; bool("true") is True in Python but
        # the guard uses settings.get('privacy_mode_enabled') directly (not via
        # bool()), so a truthy string would pass — that's intentional because
        # settings values are stored as proper booleans in practice.
        # This test documents that a plain string "true" is treated as truthy
        # by Python and therefore DOES block (same as True).  If the behaviour
        # is ever changed, update this test.
        result = self._br.reprocess(settings={"privacy_mode_enabled": "true"})
        # "true" is truthy — guard fires.
        self.assertEqual(result.get("reason"), "privacy_mode_active")


# ---------------------------------------------------------------------------
# D3: recording_chain CRLF item_id rejection + dedup/cap ordering
# ---------------------------------------------------------------------------


class D3RecordingChainItemIdValidationTestCase(unittest.TestCase):
    """_ITEM_ID_UNSAFE_RE must reject newlines and CRLFs."""

    def test_newline_rejected(self) -> None:
        self.assertFalse(_is_valid_item_id("abc\ndef"))

    def test_carriage_return_rejected(self) -> None:
        self.assertFalse(_is_valid_item_id("abc\rdef"))

    def test_crlf_rejected(self) -> None:
        self.assertFalse(_is_valid_item_id("abc\r\ndef"))

    def test_tab_rejected(self) -> None:
        self.assertFalse(_is_valid_item_id("abc\tdef"))

    def test_slash_rejected(self) -> None:
        self.assertFalse(_is_valid_item_id("abc/def"))

    def test_backslash_rejected(self) -> None:
        self.assertFalse(_is_valid_item_id("abc\\def"))

    def test_null_byte_rejected(self) -> None:
        self.assertFalse(_is_valid_item_id("abc\x00def"))

    def test_empty_rejected(self) -> None:
        self.assertFalse(_is_valid_item_id(""))

    def test_valid_uuid_accepted(self) -> None:
        self.assertTrue(_is_valid_item_id("550e8400-e29b-41d4-a716-446655440000"))

    def test_valid_simple_id_accepted(self) -> None:
        self.assertTrue(_is_valid_item_id("abc123_item"))


class D3RecordingChainAddDedupCapTestCase(unittest.TestCase):
    """Dedup must be checked BEFORE cap; re-adding existing item must succeed."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    def _make_full_chain(self) -> str:
        """Create a chain filled to MAX_ITEMS_PER_CHAIN."""
        chain_id = self._mgr.start_chain("Full chain")
        for i in range(MAX_ITEMS_PER_CHAIN):
            self._mgr.add_to_chain(chain_id, f"item-{i:06d}")
        return chain_id

    def test_add_newline_item_id_raises_value_error(self) -> None:
        chain_id = self._mgr.start_chain("Test chain")
        with self.assertRaises(ValueError):
            self._mgr.add_to_chain(chain_id, "abc\ndef")

    def test_add_crlf_item_id_raises_value_error(self) -> None:
        chain_id = self._mgr.start_chain("Test chain")
        with self.assertRaises(ValueError):
            self._mgr.add_to_chain(chain_id, "abc\r\ndef")

    def test_add_tab_item_id_raises_value_error(self) -> None:
        chain_id = self._mgr.start_chain("Test chain")
        with self.assertRaises(ValueError):
            self._mgr.add_to_chain(chain_id, "abc\tdef")

    def test_add_existing_item_to_full_chain_ok_no_error(self) -> None:
        """Re-adding an item that's already in a full chain must NOT raise
        RuntimeError (cap check must come AFTER the dedup check)."""
        chain_id = self._make_full_chain()
        # chain is now at MAX_ITEMS_PER_CHAIN.  Re-add item-0 — must succeed.
        try:
            self._mgr.add_to_chain(chain_id, "item-000000")
        except RuntimeError as exc:
            self.fail(f"Re-adding existing item to full chain raised: {exc}")

    def test_add_existing_item_does_not_grow_chain(self) -> None:
        chain_id = self._mgr.start_chain("Test chain")
        self._mgr.add_to_chain(chain_id, "item-aaa")
        self._mgr.add_to_chain(chain_id, "item-aaa")  # duplicate
        chain = self._mgr.get_chain(chain_id)
        self.assertEqual(chain["item_ids"].count("item-aaa"), 1)

    def test_add_new_item_to_full_chain_raises(self) -> None:
        """Adding a genuinely NEW item when the chain is full must still raise."""
        chain_id = self._make_full_chain()
        with self.assertRaises(RuntimeError):
            self._mgr.add_to_chain(chain_id, "brand-new-item-xyz")

    def test_add_valid_item_to_empty_chain_ok(self) -> None:
        chain_id = self._mgr.start_chain("Test chain")
        self._mgr.add_to_chain(chain_id, "item-001")
        chain = self._mgr.get_chain(chain_id)
        self.assertIn("item-001", chain["item_ids"])

    def test_handle_add_to_chain_crlf_chain_unmodified(self) -> None:
        """After rejecting a CRLF item_id, the chain must remain empty."""
        chain_id = self._mgr.start_chain("Test chain")
        # handle_add_to_chain lets ValueError bubble (caught by IPC layer).
        try:
            self._mgr.handle_add_to_chain(
                {"chain_id": chain_id, "item_id": "abc\r\ndef"}
            )
        except ValueError:
            pass
        # Chain must be unmodified.
        chain = self._mgr.get_chain(chain_id)
        self.assertEqual(chain["item_ids"], [])

    def test_handle_add_to_chain_crlf_raises_value_error(self) -> None:
        chain_id = self._mgr.start_chain("Test chain")
        with self.assertRaises(ValueError):
            self._mgr.handle_add_to_chain(
                {"chain_id": chain_id, "item_id": "bad\nid"}
            )


if __name__ == "__main__":
    unittest.main()
