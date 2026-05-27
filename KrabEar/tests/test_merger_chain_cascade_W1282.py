"""Tests for RecordingMerger chain cascade fix (W1278 RC-A MED, W1282).

Covers:
- test_merge_replaces_originals_with_merged_in_chain
- test_merge_with_no_chain_membership_no_op
- test_merge_chain_failure_does_not_break_merge
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Настройка пути для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_merger import RecordingMerger  # noqa: E402
from backend.recording_chain import RecordingChainManager  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeHistoryItem:
    id: str
    ts: str
    text: str
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
        from dataclasses import asdict
        return asdict(self)


class FakeStore:
    """Минимальный фейк StateStore."""

    def __init__(self) -> None:
        self._items: dict[str, FakeHistoryItem] = {}
        self._deleted: list[str] = []
        self._added: list[FakeHistoryItem] = []

    def add_fake_item(
        self,
        item_id: str,
        text: str,
        ts: str = "2026-04-12T10:00:00",
        **kwargs: Any,
    ) -> FakeHistoryItem:
        item = FakeHistoryItem(id=item_id, ts=ts, text=text, **kwargs)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        return self._items.get(item_id)

    def delete_history_item(self, item_id: str) -> bool:
        if item_id in self._items:
            self._deleted.append(item_id)
            return True
        return False

    def add_history_item(
        self,
        text: str,
        paste_status: str = "merged",
        source_text: str = "",
        translated_text: str = "",
        translation_mode: str = "off",
        source_lang: str = "",
        target_lang: str = "",
        translation_status: str = "not_requested",
        diarization: dict | None = None,
        audio_duration_sec: float | None = None,
        confidence: float | None = None,
        tags: list | None = None,
        **kwargs: Any,
    ) -> FakeHistoryItem:
        import uuid
        item = FakeHistoryItem(
            id=str(uuid.uuid4()),
            ts="2026-04-12T12:00:00",
            text=text,
            paste_status=paste_status,
            source_text=source_text,
            translated_text=translated_text,
            translation_mode=translation_mode,
            source_lang=source_lang,
            target_lang=target_lang,
            translation_status=translation_status,
            diarization=diarization,
            audio_duration_sec=audio_duration_sec,
            confidence=confidence,
            tags=list(tags) if tags else [],
        )
        self._items[item.id] = item
        self._added.append(item)
        return item


class FakeChainManager:
    """Minimal fake that records calls to find_chains_containing/replace_items_in_chain."""

    def __init__(self, membership: dict[str, list[str]] | None = None) -> None:
        # membership: {chain_id: [item_ids]} returned by find_chains_containing
        self._membership = membership or {}
        self.find_calls: list[list[str]] = []
        self.replace_calls: list[tuple[str, list[str], str]] = []

    def find_chains_containing(self, item_ids: list[str]) -> dict[str, list[str]]:
        self.find_calls.append(list(item_ids))
        return dict(self._membership)

    def replace_items_in_chain(
        self, chain_id: str, old_ids: list[str], new_id: str
    ) -> bool:
        self.replace_calls.append((chain_id, list(old_ids), new_id))
        return True


class RaisingChainManager:
    """Fake that raises on replace_items_in_chain to test graceful degradation."""

    def find_chains_containing(self, item_ids: list[str]) -> dict[str, list[str]]:
        return {"chain-err": list(item_ids)}

    def replace_items_in_chain(
        self, chain_id: str, old_ids: list[str], new_id: str
    ) -> bool:
        raise RuntimeError("Simulated storage failure")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMergeReplaceOriginalsWithMergedInChain(unittest.TestCase):
    """W1282 primary test: merged item replaces originals in chain."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00") -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts)

    def test_merge_replaces_originals_with_merged_in_chain(self) -> None:
        """When delete_originals=True and originals are in chains,
        each chain should have those item_ids replaced with the merged item's id."""
        self._add("orig1", "Part one", ts="2026-04-12T10:00:00")
        self._add("orig2", "Part two", ts="2026-04-12T10:01:00")

        # Wire fake chain manager that says orig1+orig2 are in chain "chain-abc"
        fake_chain_mgr = FakeChainManager(
            membership={"chain-abc": ["orig1", "orig2"]}
        )
        self.merger.recording_chain_mgr = fake_chain_mgr

        result = self.merger.merge_items(
            ["orig1", "orig2"], self.store, delete_originals=True
        )

        new_id = result["id"]

        # find_chains_containing must have been called with the original ids
        self.assertEqual(len(fake_chain_mgr.find_calls), 1)
        self.assertCountEqual(fake_chain_mgr.find_calls[0], ["orig1", "orig2"])

        # replace_items_in_chain must have been called for "chain-abc"
        self.assertEqual(len(fake_chain_mgr.replace_calls), 1)
        called_chain_id, called_old_ids, called_new_id = fake_chain_mgr.replace_calls[0]
        self.assertEqual(called_chain_id, "chain-abc")
        self.assertCountEqual(called_old_ids, ["orig1", "orig2"])
        self.assertEqual(called_new_id, new_id)

        # originals must still have been deleted from the store
        self.assertIn("orig1", self.store._deleted)
        self.assertIn("orig2", self.store._deleted)

    def test_merge_replaces_across_multiple_chains(self) -> None:
        """When originals span multiple chains, each chain is updated."""
        self._add("m1", "Alpha", ts="2026-04-12T10:00:00")
        self._add("m2", "Beta", ts="2026-04-12T10:01:00")

        fake_chain_mgr = FakeChainManager(
            membership={
                "chain-x": ["m1"],
                "chain-y": ["m2"],
            }
        )
        self.merger.recording_chain_mgr = fake_chain_mgr

        result = self.merger.merge_items(["m1", "m2"], self.store, delete_originals=True)
        new_id = result["id"]

        replaced_chains = {c for c, _, _ in fake_chain_mgr.replace_calls}
        self.assertIn("chain-x", replaced_chains)
        self.assertIn("chain-y", replaced_chains)
        for chain_id, old_ids, called_new_id in fake_chain_mgr.replace_calls:
            self.assertEqual(called_new_id, new_id)


class TestMergeWithNoChainMembershipNoOp(unittest.TestCase):
    """When originals have no chain membership, chain manager is queried
    but replace is never called."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00") -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts)

    def test_merge_with_no_chain_membership_no_op(self) -> None:
        """No chains contain the originals → replace_items_in_chain never called."""
        self._add("n1", "First", ts="2026-04-12T10:00:00")
        self._add("n2", "Second", ts="2026-04-12T10:01:00")

        # membership is empty — no chains contain these items
        fake_chain_mgr = FakeChainManager(membership={})
        self.merger.recording_chain_mgr = fake_chain_mgr

        result = self.merger.merge_items(["n1", "n2"], self.store, delete_originals=True)

        # find must have been called
        self.assertEqual(len(fake_chain_mgr.find_calls), 1)
        # replace must NOT have been called
        self.assertEqual(len(fake_chain_mgr.replace_calls), 0)

        # merge still succeeded
        self.assertIn("text", result)
        self.assertIn("n1", self.store._deleted)
        self.assertIn("n2", self.store._deleted)

    def test_merge_without_delete_originals_skips_chain_lookup(self) -> None:
        """When delete_originals=False, chain manager is never queried at all."""
        self._add("k1", "Keep one", ts="2026-04-12T10:00:00")
        self._add("k2", "Keep two", ts="2026-04-12T10:01:00")

        fake_chain_mgr = FakeChainManager(membership={"chain-z": ["k1"]})
        self.merger.recording_chain_mgr = fake_chain_mgr

        self.merger.merge_items(["k1", "k2"], self.store, delete_originals=False)

        # No chain queries when originals are kept
        self.assertEqual(len(fake_chain_mgr.find_calls), 0)
        self.assertEqual(len(fake_chain_mgr.replace_calls), 0)

    def test_merge_no_chain_mgr_injected_still_works(self) -> None:
        """With no chain_mgr injected (None), merge proceeds normally."""
        self._add("z1", "Alpha", ts="2026-04-12T10:00:00")
        self._add("z2", "Beta", ts="2026-04-12T10:01:00")

        # recording_chain_mgr is None (default)
        self.assertIsNone(self.merger.recording_chain_mgr)

        result = self.merger.merge_items(["z1", "z2"], self.store, delete_originals=True)
        self.assertIn("text", result)
        self.assertIn("z1", self.store._deleted)
        self.assertIn("z2", self.store._deleted)


class TestMergeChainFailureDoesNotBreakMerge(unittest.TestCase):
    """When replace_items_in_chain raises, the merge result is still returned
    (ghost ref warning logged but merge not rolled back)."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00") -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts)

    def test_merge_chain_failure_does_not_break_merge(self) -> None:
        """Exception in replace_items_in_chain is caught; merge still succeeds."""
        self._add("e1", "Error test one", ts="2026-04-12T10:00:00")
        self._add("e2", "Error test two", ts="2026-04-12T10:01:00")

        self.merger.recording_chain_mgr = RaisingChainManager()

        # Must NOT raise — exception is swallowed with a log
        result = self.merger.merge_items(["e1", "e2"], self.store, delete_originals=True)

        # Merge still happened
        self.assertIn("text", result)
        self.assertIn("e1", self.store._deleted)
        self.assertIn("e2", self.store._deleted)
        self.assertEqual(len(self.store._added), 1)

    def test_merge_chain_find_failure_does_not_break_merge(self) -> None:
        """Exception in find_chains_containing is also caught gracefully."""

        class FindRaisingChainManager:
            def find_chains_containing(self, item_ids: list[str]) -> dict[str, list[str]]:
                raise OSError("Disk read error")

            def replace_items_in_chain(
                self, chain_id: str, old_ids: list[str], new_id: str
            ) -> bool:
                return False

        self._add("f1", "Find fail one", ts="2026-04-12T10:00:00")
        self._add("f2", "Find fail two", ts="2026-04-12T10:01:00")

        self.merger.recording_chain_mgr = FindRaisingChainManager()

        result = self.merger.merge_items(["f1", "f2"], self.store, delete_originals=True)
        self.assertIn("text", result)
        self.assertIn("f1", self.store._deleted)
        self.assertIn("f2", self.store._deleted)


# ---------------------------------------------------------------------------
# Tests for new RecordingChainManager helpers
# ---------------------------------------------------------------------------


class TestFindChainsContaining(unittest.TestCase):
    """Unit tests for RecordingChainManager.find_chains_containing."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        # Minimal fake store with data_dir
        class _FakeStore:
            data_dir = Path(self._tmpdir.name)
        self._chain_mgr = RecordingChainManager(store=_FakeStore())

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _make_chain(self, name: str, item_ids: list[str]) -> str:
        chain_id = self._chain_mgr.start_chain(name)
        for iid in item_ids:
            self._chain_mgr.add_to_chain(chain_id, iid)
        return chain_id

    def test_find_chains_containing_single_chain(self) -> None:
        chain_id = self._make_chain("Meeting A", ["item1", "item2", "item3"])
        result = self._chain_mgr.find_chains_containing(["item1", "item2"])
        self.assertIn(chain_id, result)
        self.assertCountEqual(result[chain_id], ["item1", "item2"])

    def test_find_chains_containing_no_match(self) -> None:
        self._make_chain("Meeting B", ["a", "b"])
        result = self._chain_mgr.find_chains_containing(["x", "y"])
        self.assertEqual(result, {})

    def test_find_chains_containing_multiple_chains(self) -> None:
        chain1 = self._make_chain("Chain 1", ["orig1", "extra"])
        chain2 = self._make_chain("Chain 2", ["orig2", "other"])
        result = self._chain_mgr.find_chains_containing(["orig1", "orig2"])
        self.assertIn(chain1, result)
        self.assertIn(chain2, result)
        self.assertEqual(result[chain1], ["orig1"])
        self.assertEqual(result[chain2], ["orig2"])

    def test_find_chains_containing_partial_overlap(self) -> None:
        chain_id = self._make_chain("Partial", ["p1", "p2", "p3"])
        result = self._chain_mgr.find_chains_containing(["p1", "p3", "unknown"])
        self.assertIn(chain_id, result)
        self.assertCountEqual(result[chain_id], ["p1", "p3"])


class TestReplaceItemsInChain(unittest.TestCase):
    """Unit tests for RecordingChainManager.replace_items_in_chain."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()

        class _FakeStore:
            data_dir = Path(self._tmpdir.name)
        self._chain_mgr = RecordingChainManager(store=_FakeStore())

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _make_chain(self, name: str, item_ids: list[str]) -> str:
        chain_id = self._chain_mgr.start_chain(name)
        for iid in item_ids:
            self._chain_mgr.add_to_chain(chain_id, iid)
        return chain_id

    def test_replace_items_in_chain_basic(self) -> None:
        chain_id = self._make_chain("Replace test", ["orig1", "orig2", "keep"])
        changed = self._chain_mgr.replace_items_in_chain(
            chain_id, ["orig1", "orig2"], "merged-new"
        )
        self.assertTrue(changed)
        chain = self._chain_mgr.get_chain(chain_id)
        self.assertIn("merged-new", chain["item_ids"])
        self.assertNotIn("orig1", chain["item_ids"])
        self.assertNotIn("orig2", chain["item_ids"])
        self.assertIn("keep", chain["item_ids"])

    def test_replace_items_in_chain_preserves_order(self) -> None:
        """Merged item appears at the position of the first matched original."""
        chain_id = self._make_chain("Order test", ["before", "orig1", "orig2", "after"])
        self._chain_mgr.replace_items_in_chain(chain_id, ["orig1", "orig2"], "merged")
        chain = self._chain_mgr.get_chain(chain_id)
        ids = chain["item_ids"]
        self.assertEqual(ids, ["before", "merged", "after"])

    def test_replace_items_in_chain_nonexistent_chain_returns_false(self) -> None:
        changed = self._chain_mgr.replace_items_in_chain(
            "nonexistent-chain-id", ["orig"], "new"
        )
        self.assertFalse(changed)

    def test_replace_items_in_chain_no_match_returns_false(self) -> None:
        chain_id = self._make_chain("No match", ["a", "b"])
        changed = self._chain_mgr.replace_items_in_chain(chain_id, ["x", "y"], "z")
        self.assertFalse(changed)

    def test_replace_items_deduplicated(self) -> None:
        """If an item appears multiple times, merged item appears only once."""
        chain_id = self._make_chain("Dedup test", ["orig", "other"])
        # Even though orig is listed once, ensure no double-insertion
        self._chain_mgr.replace_items_in_chain(chain_id, ["orig"], "merged")
        chain = self._chain_mgr.get_chain(chain_id)
        self.assertEqual(chain["item_ids"].count("merged"), 1)


class TestLateInjectionAttribute(unittest.TestCase):
    """Verify that RecordingMerger has recording_chain_mgr = None by default."""

    def test_recording_chain_mgr_defaults_to_none(self) -> None:
        merger = RecordingMerger()
        self.assertIsNone(merger.recording_chain_mgr)

    def test_recording_chain_mgr_can_be_set(self) -> None:
        merger = RecordingMerger()
        fake = object()
        merger.recording_chain_mgr = fake
        self.assertIs(merger.recording_chain_mgr, fake)


if __name__ == "__main__":
    unittest.main()
