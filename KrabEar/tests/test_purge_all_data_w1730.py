"""Tests for W1730/W1734/W1749: purge_all_data IPC handler — privacy-purge cascade.

Covers:
  1. purge_all_data deletes all history items.
  2. purge_all_data calls delete_all_chains() — recording_chains.json becomes empty.
  3. purge_all_data with no chains wired returns chains_deleted=0 (no crash).
  4. purge_all_data chain-manager error does not abort history deletion.
  5. purge_all_data calls semantic_searcher.purge_all() when wired.
  6. W1734 F1: HistoryService._recording_chain_mgr is wired from BackendService.__init__
     (functional wiring test — replaces inspect.getsource approach).
  7. W1734 FA: archive.ndjson is emptied after purge (real ArchiveManager persistence).
  8. W1734 FB: bookmarks.ndjson is emptied after purge (real BookmarkManager persistence).
  9. W1734 FB: call_sessions.ndjson is emptied after purge (real CallSessionStore persistence).
 10. W1734 FD: confirm guard — purge is rejected without explicit confirmation.
 11. W1734 FE: result dict contains complete/errors fields for partial-failure signalling.
 12. Real-StateStore persistence: history items gone after purge when read via second store.
 13. W1734 FC: privacy audit log entry is written with correct counts.
 14. W1749 CRITICAL-2: history.ndjson contains no transcript text after purge (compact verif).
 15. W1749 CRITICAL-2: transcripts/*.md files are deleted after purge.
 16. W1749 CRITICAL-1: partial failure pushes history.purge_incomplete via error_bus.
 17. W1749 E2E: full BackendService dispatch reachability guard (W746 class).
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService  # noqa: E402
from backend.recording_chain import RecordingChainManager  # noqa: E402
from backend.archive_manager import ArchiveManager  # noqa: E402
from backend.bookmarks import BookmarkManager  # noqa: E402
from backend.call_session_store import CallSessionStore  # noqa: E402
from backend.privacy_audit import PrivacyAuditLogger  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str) -> None:
        self.id = item_id
        self.ts = "2020-01-01T00:00:00+00:00"

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "text": "secret transcript"}


class FakeStore:
    """Minimal StateStore fake for purge_all_data tests."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}
        self._tombstones: list[dict] = []
        self._lock_obj = threading.Lock()

    def add_item(self, item_id: str) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id)
        self._items[item_id] = item
        return item

    def _lock(self):
        return self._lock_obj

    def _load_active_items_unlocked(self) -> list[FakeHistoryItem]:
        return list(self._items.values())

    def _append_ndjson(self, path: Any, payload: dict) -> None:
        self._tombstones.append(payload)

    @property
    def tombstones_path(self) -> str:
        return "fake_tombstones.ndjson"

    def compact_with_stats(self) -> dict:
        """Fake compact — clears items to simulate history.ndjson rewrite."""
        return {"before_active_count": len(self._items), "after_active_count": 0}

    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> dict:
        return {}

    def save_settings(self, settings: dict) -> dict:
        return settings


class SpyChainManager:
    """RecordingChainManager spy — records calls to delete_all_chains."""

    def __init__(self) -> None:
        self.delete_all_chains_called = 0
        self._chains_count = 0

    def delete_all_chains(self) -> int:
        self.delete_all_chains_called += 1
        n = self._chains_count
        self._chains_count = 0
        return n


class ErrorChainManager:
    """Chain manager that always raises on delete_all_chains."""

    def delete_all_chains(self) -> int:
        raise RuntimeError("disk full")


class SpySemanticSearcher:
    """SemanticSearcher spy — records calls to purge_all."""

    def __init__(self) -> None:
        self.purge_all_called = 0

    def purge_all(self) -> None:
        self.purge_all_called += 1


# ---------------------------------------------------------------------------
# Helper: build a HistoryService with a pre-populated store
# ---------------------------------------------------------------------------

def _make_svc(
    tmpdir: str,
    item_ids: list[str] | None = None,
    chain_mgr: Any = None,
    semantic: Any = None,
) -> HistoryService:
    store = FakeStore(data_dir=tmpdir)
    if item_ids:
        for iid in item_ids:
            store.add_item(iid)
    svc = HistoryService(store=store)
    svc._recording_chain_mgr = chain_mgr
    svc._semantic_searcher = semantic
    return svc


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class PurgeAllDataConfirmGuardTestCase(unittest.TestCase):
    """W1734 FD: confirm guard — purge is rejected without explicit confirmation."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_purge_requires_confirm_param(self) -> None:
        """No confirm param → error, no deletion."""
        svc = _make_svc(self._tmpdir, item_ids=["a", "b"])
        result = svc.handle_purge_all_data({})
        self.assertFalse(result.get("ok"), "Must not succeed without confirm")
        self.assertEqual(result.get("error"), "confirmation_required")
        # History must NOT have been touched
        self.assertEqual(len(svc.store._tombstones), 0,
                         "No tombstones must be written without confirmation")

    def test_purge_confirm_false_is_rejected(self) -> None:
        """confirm=False must be rejected."""
        svc = _make_svc(self._tmpdir, item_ids=["a"])
        result = svc.handle_purge_all_data({"confirm": False})
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "confirmation_required")
        self.assertEqual(len(svc.store._tombstones), 0)

    def test_purge_confirm_true_proceeds(self) -> None:
        """confirm=True must allow the purge."""
        svc = _make_svc(self._tmpdir, item_ids=["a", "b"])
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["history_deleted"], 2)

    def test_purge_confirm_string_PURGE_ALL_proceeds(self) -> None:
        """confirm='PURGE_ALL' must also be accepted."""
        svc = _make_svc(self._tmpdir, item_ids=["x"])
        result = svc.handle_purge_all_data({"confirm": "PURGE_ALL"})
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["history_deleted"], 1)

    def test_purge_confirm_wrong_string_rejected(self) -> None:
        """confirm='yes' must be rejected."""
        svc = _make_svc(self._tmpdir, item_ids=["a"])
        result = svc.handle_purge_all_data({"confirm": "yes"})
        self.assertFalse(result.get("ok"))


class PurgeAllDataHistoryTestCase(unittest.TestCase):
    """W1730 F1: purge_all_data deletes all history items."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_purge_all_data_removes_all_items(self) -> None:
        """purge_all_data must tombstone every active history item."""
        svc = _make_svc(self._tmpdir, item_ids=["a", "b", "c"])
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["history_deleted"], 3)
        self.assertTrue(result["ok"])
        tombstoned_ids = {t["id"] for t in svc.store._tombstones}
        self.assertEqual(tombstoned_ids, {"a", "b", "c"})

    def test_purge_all_data_empty_history_returns_zero(self) -> None:
        """purge_all_data on empty history must return history_deleted=0."""
        svc = _make_svc(self._tmpdir, item_ids=[])
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["history_deleted"], 0)
        self.assertTrue(result["ok"])

    def test_purge_all_data_idempotent(self) -> None:
        """Second purge_all_data call returns history_deleted=0 (already empty)."""
        svc = _make_svc(self._tmpdir, item_ids=["x", "y"])
        svc.handle_purge_all_data({"confirm": True})
        svc.store._items.clear()
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["history_deleted"], 0)


class PurgeAllDataChainsTestCase(unittest.TestCase):
    """W1730 F2: purge_all_data cascades to recording chains."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_purge_all_data_calls_delete_all_chains(self) -> None:
        """purge_all_data must call delete_all_chains() on the chain manager."""
        spy = SpyChainManager()
        spy._chains_count = 4
        svc = _make_svc(self._tmpdir, item_ids=["h1", "h2"], chain_mgr=spy)
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(spy.delete_all_chains_called, 1,
                         "delete_all_chains must be called exactly once")
        self.assertEqual(result["chains_deleted"], 4)

    def test_purge_all_data_no_chain_manager_returns_zero(self) -> None:
        """purge_all_data without chain manager wired returns chains_deleted=0, no crash."""
        svc = _make_svc(self._tmpdir, item_ids=["h1"], chain_mgr=None)
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["chains_deleted"], 0)
        self.assertTrue(result["ok"])

    def test_purge_all_data_chain_error_does_not_abort_history(self) -> None:
        """Chain manager error must not prevent history items from being deleted."""
        svc = _make_svc(self._tmpdir, item_ids=["h1", "h2"], chain_mgr=ErrorChainManager())
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["history_deleted"], 2,
                         "history must be deleted even when chain manager errors")
        self.assertTrue(result["ok"])

    def test_purge_all_data_recording_chains_json_empty_after_purge(self) -> None:
        """End-to-end: after purge_all_data, RecordingChainManager sees no chains."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-secret")
        svc = HistoryService(store=store)

        chain_store = FakeStore(data_dir=self._tmpdir)
        chain_mgr = RecordingChainManager(store=chain_store)
        cid = chain_mgr.start_chain("Secret meeting")
        chain_mgr.add_to_chain(cid, "item-secret")
        svc._recording_chain_mgr = chain_mgr

        self.assertEqual(len(chain_mgr.list_chains()), 1)

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(result["chains_deleted"], 1)
        self.assertEqual(chain_mgr.list_chains(), [],
                         "After purge_all_data, recording_chains must be empty")

    def test_purge_all_data_chain_state_persisted_empty(self) -> None:
        """After purge_all_data, a new RecordingChainManager loaded from same dir sees no chains."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-a")
        svc = HistoryService(store=store)

        chain_store = FakeStore(data_dir=self._tmpdir)
        chain_mgr = RecordingChainManager(store=chain_store)
        chain_mgr.start_chain("Work session")
        svc._recording_chain_mgr = chain_mgr

        svc.handle_purge_all_data({"confirm": True})

        chain_mgr2 = RecordingChainManager(store=chain_store)
        self.assertEqual(chain_mgr2.list_chains(), [],
                         "Reloaded chain manager must see no chains after purge")


class PurgeAllDataSemanticSearchTestCase(unittest.TestCase):
    """W1730 F3: purge_all_data cascades to semantic search index."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_purge_all_data_calls_semantic_purge_all(self) -> None:
        """purge_all_data must call semantic_searcher.purge_all() when wired."""
        spy = SpySemanticSearcher()
        svc = _make_svc(self._tmpdir, item_ids=["h1"], semantic=spy)
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(spy.purge_all_called, 1,
                         "purge_all must be called on semantic searcher")
        self.assertTrue(result["semantic_purged"])

    def test_purge_all_data_no_semantic_searcher_returns_false(self) -> None:
        """purge_all_data without semantic searcher wired returns semantic_purged=False."""
        svc = _make_svc(self._tmpdir, item_ids=["h1"], semantic=None)
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertFalse(result["semantic_purged"])


class PurgeAllDataArchiveTestCase(unittest.TestCase):
    """W1734 FA: archive.ndjson is wiped clean after purge_all_data."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _make_archive_manager(self) -> ArchiveManager:
        """Create a real ArchiveManager pointed at tmpdir."""
        store = FakeStore(data_dir=self._tmpdir)
        # ArchiveManager needs a store with data_dir attribute
        return ArchiveManager(store=store)

    def test_archive_clear_all_empties_store(self) -> None:
        """ArchiveManager.clear_all() must return count and leave archive empty."""
        mgr = self._make_archive_manager()
        archive_path = mgr._archive_path

        # Write 3 fake archived items directly (bypassing history)
        for i in range(3):
            with archive_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "id": f"arch-{i}",
                    "text": "secret transcript",
                    "archived_at": "2026-01-01T00:00:00+00:00",
                }) + "\n")

        # Sanity: items are there
        self.assertEqual(len(mgr._read_archive()), 3)

        deleted = mgr.clear_all()
        self.assertEqual(deleted, 3)

        # After clear_all: no items on disk
        self.assertEqual(len(mgr._read_archive()), 0,
                         "Archive must be empty after clear_all()")

        # Reload from disk: verify file exists but is empty (no transcript text)
        content = archive_path.read_text(encoding="utf-8").strip()
        self.assertEqual(content, "",
                         "archive.ndjson must contain no text after privacy purge")

    def test_archive_cleared_on_purge_all_data(self) -> None:
        """purge_all_data wipes archive when _archive_manager is wired."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-1")
        svc = HistoryService(store=store)

        archive_mgr = self._make_archive_manager()
        # Write an archived item
        with archive_mgr._archive_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "id": "arch-old",
                "text": "sensitive archived content",
            }) + "\n")
        svc._archive_manager = archive_mgr

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(result["archive_deleted"], 1)
        # File must be empty on disk
        reloaded = archive_mgr._read_archive()
        self.assertEqual(reloaded, [],
                         "No archived transcript text must remain after purge")

    def test_archive_not_wired_returns_zero(self) -> None:
        """purge_all_data without _archive_manager wired returns archive_deleted=0."""
        svc = _make_svc(self._tmpdir, item_ids=["a"])
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["archive_deleted"], 0)


class PurgeAllDataBookmarksTestCase(unittest.TestCase):
    """W1734 FB: bookmarks.ndjson is wiped clean after purge_all_data."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_bookmarks_delete_all_empties_store(self) -> None:
        """BookmarkManager.delete_all() removes all active bookmarks from disk."""
        mgr = BookmarkManager(data_dir=Path(self._tmpdir))
        mgr.add("sess-1", 10.0, note="chapter 1")
        mgr.add("sess-2", 25.5, note="important bit")

        self.assertEqual(len(mgr.list_all()), 2)

        deleted = mgr.delete_all()
        self.assertEqual(deleted, 2)

        # Reload from same path: must see nothing
        mgr2 = BookmarkManager(data_dir=Path(self._tmpdir))
        self.assertEqual(mgr2.list_all(), [],
                         "No bookmarks must remain after delete_all()")

        # File must contain no item_id data
        content = mgr._path.read_text(encoding="utf-8").strip()
        self.assertEqual(content, "",
                         "bookmarks.ndjson must be empty after privacy purge")

    def test_bookmarks_cleared_on_purge_all_data(self) -> None:
        """purge_all_data wipes bookmarks when _bookmarks is wired."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-1")
        svc = HistoryService(store=store)

        bm_mgr = BookmarkManager(data_dir=Path(self._tmpdir))
        bm_mgr.add("item-1", 5.0, note="start")
        svc._bookmarks = bm_mgr

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(result["bookmarks_deleted"], 1)
        bm_mgr2 = BookmarkManager(data_dir=Path(self._tmpdir))
        self.assertEqual(bm_mgr2.list_all(), [],
                         "No bookmarks must remain after purge")

    def test_bookmarks_not_wired_returns_zero(self) -> None:
        """purge_all_data without _bookmarks wired returns bookmarks_deleted=0."""
        svc = _make_svc(self._tmpdir, item_ids=["a"])
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["bookmarks_deleted"], 0)


class PurgeAllDataCallSessionsTestCase(unittest.TestCase):
    """W1734 FB: call_sessions.ndjson is wiped clean after purge_all_data."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_call_session_delete_all_empties_store(self) -> None:
        """CallSessionStore.delete_all() removes all sessions from disk."""
        store = CallSessionStore(data_dir=Path(self._tmpdir))
        store.create(phone_number="+79001234567", goal_text="test call")
        store.create(phone_number="+79007654321", goal_text="second call")

        self.assertEqual(len(store.list_sessions(limit=100)), 2)

        deleted = store.delete_all()
        self.assertEqual(deleted, 2)

        # Reload: must see nothing
        store2 = CallSessionStore(data_dir=Path(self._tmpdir))
        self.assertEqual(store2.list_sessions(limit=100), [],
                         "No call sessions must remain after delete_all()")

        # File must be empty (no phone number metadata)
        content = store.sessions_path.read_text(encoding="utf-8").strip()
        self.assertEqual(content, "",
                         "call_sessions.ndjson must be empty after privacy purge")

    def test_call_sessions_cleared_on_purge_all_data(self) -> None:
        """purge_all_data wipes call_sessions when _call_session_store is wired."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-1")
        svc = HistoryService(store=store)

        cs_store = CallSessionStore(data_dir=Path(self._tmpdir))
        cs_store.create(phone_number="+1234567890", goal_text="demo")
        svc._call_session_store = cs_store

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(result["call_sessions_deleted"], 1)
        cs_store2 = CallSessionStore(data_dir=Path(self._tmpdir))
        self.assertEqual(cs_store2.list_sessions(limit=100), [],
                         "No call sessions must remain after purge")

    def test_call_sessions_not_wired_returns_zero(self) -> None:
        """purge_all_data without _call_session_store wired returns call_sessions_deleted=0."""
        svc = _make_svc(self._tmpdir, item_ids=["a"])
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["call_sessions_deleted"], 0)


class PurgeAllDataPartialFailureTestCase(unittest.TestCase):
    """W1734 FE: result dict contains complete/errors fields for partial-failure signalling."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_result_has_complete_true_when_no_errors(self) -> None:
        """When no secondary steps fail, complete=True and errors=[]."""
        svc = _make_svc(self._tmpdir, item_ids=["a"])
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertIn("complete", result)
        self.assertIn("errors", result)
        self.assertTrue(result["complete"])
        self.assertEqual(result["errors"], [])

    def test_result_has_complete_false_when_chain_errors(self) -> None:
        """When a secondary step errors, complete=False and step name is in errors."""
        svc = _make_svc(self._tmpdir, item_ids=["a"], chain_mgr=ErrorChainManager())
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertFalse(result["complete"],
                         "complete must be False when any secondary step raises")
        self.assertIn("chains", result["errors"])

    def test_errors_list_contains_no_pii(self) -> None:
        """errors list must contain only step names (no transcript/phone text)."""
        svc = _make_svc(self._tmpdir, item_ids=["a"], chain_mgr=ErrorChainManager())
        result = svc.handle_purge_all_data({"confirm": True})
        for err in result["errors"]:
            self.assertIsInstance(err, str)
            # Must be a short step label, not a stack trace or PII
            self.assertLess(len(err), 50, f"Error label too long: {err!r}")


class PurgeAllDataRealStateStoreTestCase(unittest.TestCase):
    """W1734 F: real StateStore persistence test — use real store, not FakeStore."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_real_statestore_history_empty_after_purge(self) -> None:
        """Create a real StateStore with items, purge, reload → empty history."""
        from backend.state_store import StateStore

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = HistoryService(store=store)

        # Add 3 items via the real store's keyword API
        for i in range(3):
            store.add_history_item(text=f"secret transcript {i}")

        # Verify items exist before purge
        before = store._load_active_items_with_lock()
        self.assertEqual(len(before), 3, "Should have 3 items before purge")

        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["history_deleted"], 3)
        self.assertTrue(result["ok"])

        # Reload from SECOND store pointing to same directory
        store2 = StateStore(data_dir=Path(self._tmpdir))
        after = store2._load_active_items_with_lock()
        self.assertEqual(after, [],
                         "Second StateStore from same dir must see empty history after purge")


class PurgeAllDataPrivacyAuditTestCase(unittest.TestCase):
    """W1734 FC: privacy audit log entry is written with correct counts."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        # Reset singleton so each test gets a fresh logger in tmpdir
        PrivacyAuditLogger.reset_instance()

    def tearDown(self) -> None:
        PrivacyAuditLogger.reset_instance()

    def test_privacy_audit_log_written_after_purge(self) -> None:
        """purge_all_data must emit a 'purge_all_data' entry in PrivacyAuditLogger."""
        audit_path = Path(self._tmpdir) / "privacy_audit.log"
        PrivacyAuditLogger._instance = PrivacyAuditLogger(log_path=audit_path)

        svc = _make_svc(self._tmpdir, item_ids=["a", "b"])
        svc.handle_purge_all_data({"confirm": True})

        entries = PrivacyAuditLogger._instance.read_entries()
        purge_entries = [e for e in entries
                         if e.get("action") == "purge_all_data"]
        self.assertEqual(len(purge_entries), 1,
                         "Exactly one purge_all_data audit entry must be written")

        details = purge_entries[0].get("details", {})
        self.assertEqual(details.get("history_deleted"), 2)
        # W1749: Fix tautological assertion — was comparing value to itself via default=same.
        # Assert the actual audit entry category (top-level field, not in details).
        self.assertEqual(purge_entries[0].get("category"), "privacy",
                         "Audit entry must have category='privacy'")
        # Assert details also contains the counts needed for compliance review
        self.assertIn("chains_deleted", details)
        self.assertIn("secondary_errors", details)

    def test_privacy_audit_failure_does_not_abort_purge(self) -> None:
        """If the audit logger raises, the purge must still complete."""
        class BrokenAuditLogger:
            def log_event(self, **kwargs: Any) -> None:
                raise OSError("disk full")

        import backend.privacy_audit as pa_module
        original_get = pa_module.get_privacy_audit_logger

        try:
            pa_module.get_privacy_audit_logger = lambda **kwargs: BrokenAuditLogger()
            svc = _make_svc(self._tmpdir, item_ids=["a"])
            # Must not raise
            result = svc.handle_purge_all_data({"confirm": True})
            self.assertTrue(result.get("ok"),
                            "purge must succeed even when audit log raises")
            self.assertEqual(result["history_deleted"], 1)
        finally:
            pa_module.get_privacy_audit_logger = original_get


class PurgeAllDataWiringTestCase(unittest.TestCase):
    """W1734 F4: verify BackendService wires collaborators into HistoryService functionally."""

    def test_service_wires_recording_chain_mgr_into_history(self) -> None:
        """BackendService.__init__ must set history._recording_chain_mgr = self._chains."""
        import tempfile as _tmpfile
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(_tmpfile.mkdtemp()))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._recording_chain_mgr,
            svc._chains,
            "BackendService must wire _chains into _history._recording_chain_mgr",
        )

    def test_service_wires_archive_manager_into_history(self) -> None:
        """BackendService.__init__ must wire _archive_manager into _history._archive_manager."""
        import tempfile as _tmpfile
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(_tmpfile.mkdtemp()))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._archive_manager,
            svc._archive_manager,
            "BackendService must wire _archive_manager into _history._archive_manager",
        )

    def test_service_wires_bookmarks_into_history(self) -> None:
        """BackendService.__init__ must wire _bookmarks into _history._bookmarks."""
        import tempfile as _tmpfile
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(_tmpfile.mkdtemp()))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._bookmarks,
            svc._bookmarks,
            "BackendService must wire _bookmarks into _history._bookmarks",
        )

    def test_service_wires_call_session_store_into_history(self) -> None:
        """BackendService.__init__ must wire _call_session_store into _history."""
        import tempfile as _tmpfile
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(_tmpfile.mkdtemp()))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._call_session_store,
            svc._call_session_store,
            "BackendService must wire _call_session_store into _history._call_session_store",
        )

    def test_purge_all_data_in_ipc_dispatch(self) -> None:
        """purge_all_data must be present in BackendService._build_dispatch_table.

        W1769: dispatch table consolidated inline in service.py (single source of
        truth); ipc_dispatch.py removed.
        """
        from backend.service import BackendService
        import inspect
        source = inspect.getsource(BackendService._build_dispatch_table)
        self.assertIn(
            '"purge_all_data"',
            source,
            "purge_all_data must be registered in BackendService._build_dispatch_table",
        )

    def test_purge_all_data_handler_exists_on_history_service(self) -> None:
        """HistoryService must have a handle_purge_all_data method."""
        from backend.history_service import HistoryService
        self.assertTrue(
            hasattr(HistoryService, "handle_purge_all_data"),
            "HistoryService must define handle_purge_all_data",
        )

    def test_delete_history_item_removes_from_chain_functional(self) -> None:
        """handle_delete_history_item removes item from its chain (chain wire functional)."""
        import tempfile as _tmpfile
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(_tmpfile.mkdtemp()))
        svc = BackendService(store=store)

        # Create an item and add it to a chain
        item = store.add_history_item(text="hello world")
        item_id = item.id
        cid = svc._chains.start_chain("test chain")
        svc._chains.add_to_chain(cid, item_id)

        # Verify item is in chain
        chain_before = svc._chains.get_chain(cid)
        self.assertIn(item_id, chain_before.get("item_ids", []))

        # Delete the item via history service (param name is "id", not "item_id")
        svc._history.handle_delete_history_item({"id": item_id})

        # Verify item_id is removed from chain
        chain_after = svc._chains.get_chain(cid)
        self.assertNotIn(
            item_id,
            chain_after.get("item_ids", []),
            "Deleted item must be removed from its chain",
        )


class PurgeAllDataCritical2RealEraseTestCase(unittest.TestCase):
    """W1749 CRITICAL-2: purge must physically erase transcript text from disk.

    Fail-before: history.ndjson still contained cleartext after tombstone-only purge.
    Pass-after:  compact() + .md deletion leave no recoverable text on disk.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_history_ndjson_contains_no_transcript_text_after_purge(self) -> None:
        """After purge_all_data, history.ndjson must NOT contain any transcript text."""
        from backend.state_store import StateStore

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = HistoryService(store=store)

        secret = "ultra secret transcript text CANARY_12345"
        store.add_history_item(text=secret)

        # Verify the text is in the file before purge
        raw_before = store.history_path.read_text(encoding="utf-8")
        self.assertIn(secret, raw_before, "Canary text must be in history.ndjson before purge")

        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["history_deleted"], 1)
        self.assertTrue(result["ok"])

        # After purge: raw file must NOT contain the transcript text
        raw_after = store.history_path.read_text(encoding="utf-8")
        self.assertNotIn(
            secret, raw_after,
            "After purge_all_data, history.ndjson must NOT contain any transcript text "
            "(compact() must have physically rewritten the file)",
        )

    def test_transcript_md_files_deleted_after_purge(self) -> None:
        """After purge_all_data, transcripts/*.md files must be deleted."""
        from backend.state_store import StateStore

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = HistoryService(store=store)
        store.add_history_item(text="some transcript")

        # Write a fake transcript .md file
        transcripts_dir = Path(self._tmpdir) / "transcripts"
        transcripts_dir.mkdir(exist_ok=True)
        md_file = transcripts_dir / "2026-01-01_secret.md"
        md_file.write_text("# Secret meeting\nThis should be deleted.", encoding="utf-8")

        self.assertTrue(md_file.exists(), "Transcript .md must exist before purge")

        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("transcripts_deleted"), 1,
                         "transcripts_deleted must be 1 after deleting the .md file")

        # .md must be gone
        self.assertFalse(md_file.exists(),
                         "Transcript .md file must be deleted by purge_all_data")

    def test_purge_with_multiple_transcript_files(self) -> None:
        """All .md files in transcripts/ are removed; transcripts_deleted = count of files."""
        from backend.state_store import StateStore

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = HistoryService(store=store)

        transcripts_dir = Path(self._tmpdir) / "transcripts"
        transcripts_dir.mkdir(exist_ok=True)
        n = 5
        for i in range(n):
            (transcripts_dir / f"transcript_{i}.md").write_text(f"text {i}", encoding="utf-8")

        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("transcripts_deleted"), n,
                         f"All {n} .md files must be reported as deleted")
        remaining = list(transcripts_dir.glob("*.md"))
        self.assertEqual(remaining, [],
                         f"No .md files must remain in transcripts/; found: {remaining}")

    def test_transcripts_deleted_zero_when_no_dir(self) -> None:
        """transcripts_deleted=0 when transcripts/ directory does not exist — no crash."""
        from backend.state_store import StateStore

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = HistoryService(store=store)
        # Ensure no transcripts/ directory
        transcripts_dir = Path(self._tmpdir) / "transcripts"
        if transcripts_dir.exists():
            import shutil
            shutil.rmtree(str(transcripts_dir))

        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("transcripts_deleted", 0), 0)


class PurgeAllDataLoudErrorTestCase(unittest.TestCase):
    """W1749 CRITICAL-1: partial failure must push history.purge_incomplete via error_bus."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_push_error_called_when_secondary_step_fails(self) -> None:
        """When a secondary step raises, _push_error must be called with history.purge_incomplete."""
        svc = _make_svc(self._tmpdir, item_ids=["a", "b"], chain_mgr=ErrorChainManager())

        push_calls: list[dict] = []

        def fake_push_error(code: str, message_debug: str, context: Any = None) -> None:
            push_calls.append({"code": code, "message_debug": message_debug, "context": context})

        svc._push_error = fake_push_error  # type: ignore[method-assign]

        result = svc.handle_purge_all_data({"confirm": True})
        self.assertFalse(result["complete"])
        self.assertIn("chains", result["errors"])

        self.assertEqual(len(push_calls), 1, "_push_error must be called exactly once")
        self.assertEqual(push_calls[0]["code"], "history.purge_incomplete",
                         "_push_error must use code='history.purge_incomplete'")
        self.assertIn("chains", push_calls[0]["message_debug"],
                      "message_debug must mention the failed step name")

    def test_no_push_error_when_all_steps_succeed(self) -> None:
        """When all secondary steps succeed, _push_error must NOT be called."""
        svc = _make_svc(self._tmpdir, item_ids=["a"])
        push_calls: list[dict] = []

        def fake_push_error(code: str, message_debug: str, context: Any = None) -> None:
            push_calls.append({"code": code})

        svc._push_error = fake_push_error  # type: ignore[method-assign]

        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result["complete"])
        self.assertEqual(push_calls, [],
                         "_push_error must NOT be called when purge completes fully")

    def test_error_bus_receives_purge_incomplete_via_real_bus(self) -> None:
        """When a secondary step fails, error_bus.push is called with history.purge_incomplete."""
        # Use a minimal spy rather than constructing a full ErrorBus (which needs
        # event_bus + registry constructor args that are not available in unit tests).
        class _SpyBus:
            def __init__(self) -> None:
                self.pushed: list = []

            def push(self, err: Any) -> None:
                self.pushed.append(err)

        svc = _make_svc(self._tmpdir, item_ids=["a"], chain_mgr=ErrorChainManager())
        spy_bus = _SpyBus()
        svc._error_bus = spy_bus  # type: ignore[attr-defined]

        svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(len(spy_bus.pushed), 1,
                         "error_bus.push must be called exactly once on partial failure")
        pushed_err = spy_bus.pushed[0]
        # KrabError is a Pydantic model — access attribute or use dict serialisation
        code = getattr(pushed_err, "code", None) or (pushed_err.get("code") if isinstance(pushed_err, dict) else None)
        self.assertEqual(code, "history.purge_incomplete",
                         f"error_bus must receive history.purge_incomplete; got code={code!r}")


class PurgeAllDataE2EBackendServiceTestCase(unittest.TestCase):
    """W1749 E2E: full BackendService dispatch — guards live reachability (W746 class)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_purge_all_data_via_handle_request_succeeds(self) -> None:
        """handle_request({'method': 'purge_all_data', 'params': {'confirm': True}}) → ok."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)

        store.add_history_item(text="e2e test transcript")

        response = svc.handle_request({
            "id": "test-1",
            "method": "purge_all_data",
            "params": {"confirm": True},
        })

        self.assertTrue(response.get("ok"), f"Expected ok=True, got: {response}")
        result = response.get("result", response)
        history_deleted = result.get("history_deleted", response.get("history_deleted", -1))
        self.assertEqual(history_deleted, 1, "Must report 1 history item deleted")

    def test_purge_all_data_via_handle_request_confirm_required(self) -> None:
        """handle_request without confirm → confirmation_required, zero tombstones."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)
        store.add_history_item(text="must not be deleted")

        response = svc.handle_request({
            "id": "test-2",
            "method": "purge_all_data",
            "params": {},
        })

        # Response should indicate no deletion happened
        result = response.get("result", response)
        error_field = result.get("error", response.get("error"))
        self.assertEqual(error_field, "confirmation_required",
                         f"Expected confirmation_required error, got: {response}")

        # Item must still be in history
        items = store._load_active_items_with_lock()
        self.assertEqual(len(items), 1, "Item must NOT be deleted without confirmation")


class PurgeRotatesEncryptionKeyTestCase(unittest.TestCase):
    """Crypto-audit (2026-06-20): purge_all_data удаляет ключ шифрования из
    Keychain и сбрасывает крипто-кэш StateStore.

    Иначе выживший AES-256 ключ расшифровал бы pre-purge бэкап history.ndjson
    (Time Machine / iCloud) — privacy-дыра.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_purge_deletes_keychain_key_and_resets_cache(self) -> None:
        from unittest.mock import patch

        svc = _make_svc(self._tmp, item_ids=["a", "b"])
        # Симулируем «шифрование инициализировано» в кэше StateStore.
        svc.store._history_crypto_initialized = True
        svc.store._history_crypto_instance = object()

        with patch("backend.crypto_keystore.delete_history_key") as mock_del:
            result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"))
        # 1. Ключ удалён из Keychain.
        self.assertTrue(mock_del.called, "delete_history_key должен вызываться при purge")
        # 2. Крипто-кэш сброшен — следующая запись перечитает/перегенерирует ключ.
        self.assertFalse(svc.store._history_crypto_initialized)
        self.assertIsNone(svc.store._history_crypto_instance)

    def test_purge_survives_keystore_unavailable(self) -> None:
        """KeystoreUnavailable (нет Keychain / Linux) НЕ должен ломать purge."""
        from unittest.mock import patch
        from backend.crypto_keystore import KeystoreUnavailable

        svc = _make_svc(self._tmp, item_ids=["a"])
        with patch(
            "backend.crypto_keystore.delete_history_key",
            side_effect=KeystoreUnavailable("no keychain"),
        ):
            result = svc.handle_purge_all_data({"confirm": True})
        # KeystoreUnavailable ловится внутри — purge успешен, не в errors.
        self.assertTrue(result.get("ok"))
        self.assertNotIn("encryption_key", result.get("errors", []))


if __name__ == "__main__":
    unittest.main()
