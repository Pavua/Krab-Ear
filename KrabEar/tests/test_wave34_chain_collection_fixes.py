"""Tests for wave-34 fixes: privacy gates + DoS caps + item_id validation.

Covers:
  A1 (HIGH) - recording_chain get_chain / merge_chain_text privacy gate
  A2 (HIGH) - collection_manager get_collection_items privacy gate
  A3 (MED)  - recording_chain MAX_CHAINS / MAX_ITEMS_PER_CHAIN / MAX_CHAIN_NAME_LEN
  A4 (LOW)  - item_id format validation in recording_chain + collection_manager
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_chain import (  # noqa: E402
    MAX_CHAIN_NAME_LEN,
    MAX_CHAINS,
    MAX_ITEMS_PER_CHAIN,
    RecordingChainManager,
)
from backend.collection_manager import CollectionManager  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class FakeHistoryItem:
    def __init__(self, item_id: str, text: str = "transcript text") -> None:
        self.id = item_id
        self.text = text

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text}


class FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}

    def add_fake_item(self, item_id: str, text: str = "transcript text") -> None:
        self._items[item_id] = FakeHistoryItem(item_id, text)

    def get_history_item_by_id(self, item_id: str):
        return self._items.get(item_id)


# ---------------------------------------------------------------------------
# A1: RecordingChain privacy gate (get_chain + merge_chain_text)
# ---------------------------------------------------------------------------


class TestRecordingChainPrivacyGate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._privacy = False
        self._mgr = RecordingChainManager(
            store=self._store,
            settings_fn=lambda: {"privacy_mode_enabled": self._privacy},
        )

    def _make_chain_with_item(self) -> tuple[str, str]:
        """Returns (chain_id, item_id) with one history item containing text."""
        chain_id = self._mgr.start_chain("Test chain")
        item_id = "abc123def456"
        self._store.add_fake_item(item_id, text="secret transcript content")
        self._mgr.add_to_chain(chain_id, item_id)
        return chain_id, item_id

    def test_get_chain_returns_items_when_privacy_off(self) -> None:
        chain_id, _ = self._make_chain_with_item()
        result = self._mgr.get_chain(chain_id)
        # items list must be populated
        self.assertTrue(len(result["items"]) > 0)
        self.assertNotIn("privacy_mode", result)

    def test_get_chain_returns_empty_items_when_privacy_on(self) -> None:
        chain_id, _ = self._make_chain_with_item()
        self._privacy = True
        result = self._mgr.get_chain(chain_id)
        # transcript items must be suppressed
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total_word_count"], 0)
        self.assertEqual(result["total_duration_sec"], 0.0)
        self.assertTrue(result.get("privacy_mode"))
        # structural metadata is still present
        self.assertEqual(result["chain_id"], chain_id)
        self.assertIn("name", result)

    def test_get_chain_privacy_still_raises_for_missing_chain(self) -> None:
        self._privacy = True
        with self.assertRaises(KeyError):
            self._mgr.get_chain("nonexistent-chain-id")

    def test_merge_chain_text_returns_text_when_privacy_off(self) -> None:
        chain_id, _ = self._make_chain_with_item()
        text = self._mgr.merge_chain_text(chain_id)
        self.assertIn("secret transcript content", text)

    def test_merge_chain_text_returns_empty_when_privacy_on(self) -> None:
        chain_id, _ = self._make_chain_with_item()
        self._privacy = True
        text = self._mgr.merge_chain_text(chain_id)
        self.assertEqual(text, "")

    def test_handle_get_chain_privacy_on_suppresses_items(self) -> None:
        chain_id, _ = self._make_chain_with_item()
        self._privacy = True
        result = self._mgr.handle_get_chain({"chain_id": chain_id})
        self.assertEqual(result["items"], [])
        self.assertTrue(result.get("privacy_mode"))

    def test_handle_merge_chain_text_privacy_on_returns_empty(self) -> None:
        chain_id, _ = self._make_chain_with_item()
        self._privacy = True
        result = self._mgr.handle_merge_chain_text({"chain_id": chain_id})
        self.assertEqual(result["text"], "")

    def test_no_settings_fn_means_privacy_off(self) -> None:
        """When no settings_fn supplied, privacy gate defaults to False."""
        mgr_no_settings = RecordingChainManager(store=self._store)
        chain_id = mgr_no_settings.start_chain("no settings chain")
        item_id = "abc123nofn"
        self._store.add_fake_item(item_id, text="visible text")
        mgr_no_settings.add_to_chain(chain_id, item_id)
        result = mgr_no_settings.get_chain(chain_id)
        self.assertTrue(len(result["items"]) > 0)


# ---------------------------------------------------------------------------
# A2: CollectionManager privacy gate (get_collection_items)
# ---------------------------------------------------------------------------


class TestCollectionManagerPrivacyGate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._privacy = False
        self._mgr = CollectionManager(
            store=self._store,
            settings_fn=lambda: {"privacy_mode_enabled": self._privacy},
        )

    def _make_collection_with_item(self) -> tuple[str, str]:
        col_name = "My Collection"
        item_id = "col_item_abc123"
        self._mgr.create_collection(col_name)
        self._store.add_fake_item(item_id, text="collection transcript")
        self._mgr.add_to_collection(col_name, item_id)
        return col_name, item_id

    def test_get_items_returns_data_when_privacy_off(self) -> None:
        col_name, _ = self._make_collection_with_item()
        items = self._mgr.get_collection_items(col_name)
        self.assertTrue(len(items) > 0)

    def test_get_items_returns_empty_when_privacy_on(self) -> None:
        col_name, _ = self._make_collection_with_item()
        self._privacy = True
        items = self._mgr.get_collection_items(col_name)
        self.assertEqual(items, [])

    def test_get_items_privacy_still_raises_for_missing_collection(self) -> None:
        self._privacy = True
        with self.assertRaises(KeyError):
            self._mgr.get_collection_items("nonexistent")

    def test_handle_get_collection_items_privacy_on_includes_reason(self) -> None:
        col_name, _ = self._make_collection_with_item()
        self._privacy = True
        result = self._mgr.handle_get_collection_items({"collection_name": col_name})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_no_settings_fn_means_privacy_off(self) -> None:
        mgr_no_settings = CollectionManager(store=self._store)
        mgr_no_settings.create_collection("plain col")
        item_id = "plain_item_abc"
        self._store.add_fake_item(item_id, text="plain text")
        mgr_no_settings.add_to_collection("plain col", item_id)
        items = mgr_no_settings.get_collection_items("plain col")
        self.assertTrue(len(items) > 0)


# ---------------------------------------------------------------------------
# A3: RecordingChain DoS caps
# ---------------------------------------------------------------------------


class TestRecordingChainDoSCaps(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    def test_chain_name_too_long_raises(self) -> None:
        long_name = "x" * (MAX_CHAIN_NAME_LEN + 1)
        with self.assertRaises(ValueError):
            self._mgr.start_chain(long_name)

    def test_chain_name_at_limit_ok(self) -> None:
        ok_name = "y" * MAX_CHAIN_NAME_LEN
        chain_id = self._mgr.start_chain(ok_name)
        self.assertIsNotNone(chain_id)

    def test_max_chains_raises_after_limit(self) -> None:
        """After MAX_CHAINS chains are created, the next should fail."""
        # Create exactly MAX_CHAINS chains (can be slow for large limits, so
        # patch the internal counter for speed)
        mgr = self._mgr
        # Directly stuff the internal dict to simulate being at the limit
        for i in range(MAX_CHAINS):
            mgr._data["chains"][f"fake-chain-{i}"] = {
                "chain_id": f"fake-chain-{i}",
                "name": f"chain {i}",
                "created_at": "2026-01-01T00:00:00+00:00",
                "ended_at": None,
                "item_ids": [],
            }
        # Now the next start_chain should raise RuntimeError
        with self.assertRaises(RuntimeError):
            mgr.start_chain("overflow chain")

    def test_handle_start_chain_limit_returns_error_envelope(self) -> None:
        mgr = self._mgr
        for i in range(MAX_CHAINS):
            mgr._data["chains"][f"fake-chain-{i}"] = {
                "chain_id": f"fake-chain-{i}",
                "name": f"c{i}",
                "created_at": "2026-01-01T00:00:00+00:00",
                "ended_at": None,
                "item_ids": [],
            }
        result = mgr.handle_start_chain({"name": "overflow"})
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result.get("reason"), "limit_exceeded")

    def test_max_items_per_chain_raises_after_limit(self) -> None:
        chain_id = self._mgr.start_chain("big chain")
        chain = self._mgr._data["chains"][chain_id]
        # Stuff the item_ids list to the limit
        chain["item_ids"] = [f"item-{i}" for i in range(MAX_ITEMS_PER_CHAIN)]
        with self.assertRaises(RuntimeError):
            self._mgr.add_to_chain(chain_id, "overflow-item")

    def test_handle_add_to_chain_limit_returns_error_envelope(self) -> None:
        chain_id = self._mgr.start_chain("big chain2")
        chain = self._mgr._data["chains"][chain_id]
        chain["item_ids"] = [f"item-{i}" for i in range(MAX_ITEMS_PER_CHAIN)]
        result = self._mgr.handle_add_to_chain(
            {"chain_id": chain_id, "item_id": "new-overflow-item"}
        )
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result.get("reason"), "limit_exceeded")


# ---------------------------------------------------------------------------
# A4: item_id format validation
# ---------------------------------------------------------------------------


class TestItemIdValidation(unittest.TestCase):
    """Tests for basic item_id format guard in recording_chain + collection_manager."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._chain_mgr = RecordingChainManager(store=self._store)
        self._col_mgr = CollectionManager(store=self._store)

    # -- Recording chain --

    def test_chain_add_valid_id_succeeds(self) -> None:
        chain_id = self._chain_mgr.start_chain("validation test")
        # Should not raise
        self._chain_mgr.add_to_chain(chain_id, "a1b2c3d4-e5f6-7890-abcd-ef1234567890")

    def test_chain_add_empty_id_raises(self) -> None:
        chain_id = self._chain_mgr.start_chain("empty id test")
        with self.assertRaises(ValueError):
            self._chain_mgr.add_to_chain(chain_id, "")

    def test_chain_add_path_separator_raises(self) -> None:
        chain_id = self._chain_mgr.start_chain("path sep test")
        for bad_id in ["../evil", "/etc/passwd", "good\\bad", "a.b"]:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    self._chain_mgr.add_to_chain(chain_id, bad_id)

    def test_chain_add_null_byte_raises(self) -> None:
        chain_id = self._chain_mgr.start_chain("null byte test")
        with self.assertRaises(ValueError):
            self._chain_mgr.add_to_chain(chain_id, "abc\x00def")

    # -- Collection manager --

    def test_collection_add_valid_id_succeeds(self) -> None:
        self._col_mgr.create_collection("id-test-col")
        # Should not raise
        self._col_mgr.add_to_collection("id-test-col", "a1b2c3d4-e5f6-7890")

    def test_collection_add_path_separator_raises(self) -> None:
        self._col_mgr.create_collection("path-sep-col")
        for bad_id in ["../evil", "/etc/passwd", "a/b", "a\\b", "a.b"]:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    self._col_mgr.add_to_collection("path-sep-col", bad_id)

    def test_collection_add_null_byte_raises(self) -> None:
        self._col_mgr.create_collection("null-byte-col")
        with self.assertRaises(ValueError):
            self._col_mgr.add_to_collection("null-byte-col", "abc\x00def")


if __name__ == "__main__":
    unittest.main()
