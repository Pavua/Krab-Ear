"""Illegible shares_index.json must fail CLOSED, never be overwritten by a near-empty index.

Read-modify-write wipe class: ``_load_index`` swallowed a parse/read failure and left
``_index = {}``; the next ``prepare_share`` / ``revoke_share`` then rewrote the whole
file, silently dropping every live share (their payload files stayed on disk with no
index entry — un-revocable plaintext) and every revocation tombstone.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

from _plaintext_export_test_helpers import off_authorizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ipc_errors import IpcOperationalError  # noqa: E402
from backend.sharing_manager import SharePersistenceError, SharingManager  # noqa: E402


class _Item:
    def __init__(self, item_id: str, text: str) -> None:
        self.id = item_id
        self.text = text
        self.ts = "2024-01-01T10:00:00+00:00"
        self.translated_text = ""
        self.source_lang = "ru"
        self.target_lang = "es"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "ts": self.ts, "translated_text": "",
                "source_lang": "ru", "target_lang": "es"}


class _Store:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.items = {"i1": _Item("i1", "секретный текст")}

    def get_history_item_by_id(self, item_id: str):
        return self.items.get(item_id)


class SharingIndexFailClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.store = _Store(self.dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mgr(self) -> SharingManager:
        return SharingManager(store=self.store, share_no_default_ttl=True,
                              plaintext_export_authorizer=off_authorizer())

    def _index_path(self) -> Path:
        return Path(self.dir) / "shares" / "shares_index.json"

    def _seed_valid_share(self) -> str:
        pkg = self._mgr().prepare_share(["i1"], format="text")
        return pkg.share_id

    def _corrupt(self, raw: bytes = b'{"abc": {"share_id": "abc", ') -> bytes:
        self._index_path().write_bytes(raw)
        return raw

    def _snapshot(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in self._index_path().parent.iterdir()}

    def _assert_late_unreadable_refuses_mutation(self, operation: str) -> None:
        share_id = self._seed_valid_share()
        good = self._index_path().read_bytes()
        for failure in (b'{"broken":', b'[1, 2, 3]', OSError("transient read")):
            with self.subTest(operation=operation, failure=repr(failure)):
                self._index_path().write_bytes(good)
                mgr = self._mgr()
                if operation == "prune":
                    with mgr._lock:
                        mgr._index[share_id]["expires_at"] = -1
                if isinstance(failure, bytes):
                    raw = self._corrupt(failure)
                    read_failure = nullcontext()
                else:
                    raw = good
                    read_failure = patch.object(mgr, "_read_index_file", side_effect=failure)
                before = self._snapshot()
                with read_failure, self.assertRaises(SharePersistenceError) as caught:
                    if operation == "prepare":
                        mgr.prepare_share(["i1"], format="text")
                    elif operation == "revoke":
                        mgr.revoke_share(share_id)
                    else:
                        with mgr._lock:
                            mgr._prune_expired_locked()
                self.assertEqual(caught.exception.reason, "share_index_unreadable")
                self.assertEqual(caught.exception.written_file_count, 0)
                self.assertFalse(caught.exception.partial)
                self.assertEqual(self._index_path().read_bytes(), raw)
                self.assertEqual(self._snapshot(), before)
                self.assertFalse(any(v.get("_reserved") for v in mgr._index.values()))

    def test_prepare_refuses_index_that_became_unreadable_after_init(self) -> None:
        self._assert_late_unreadable_refuses_mutation("prepare")

    def test_revoke_refuses_index_that_became_unreadable_after_init(self) -> None:
        self._assert_late_unreadable_refuses_mutation("revoke")

    def test_prune_refuses_index_that_became_unreadable_after_init(self) -> None:
        self._assert_late_unreadable_refuses_mutation("prune")

    def test_unreadable_after_payload_reports_one_written_file(self) -> None:
        self._seed_valid_share()
        mgr = self._mgr()
        raw = b'{"broken":'
        before = self._snapshot()
        original_save = mgr._save_index

        def corrupt_before_save(**kwargs):
            self._corrupt(raw)
            return original_save(**kwargs)

        with patch.object(mgr, "_save_index", side_effect=corrupt_before_save):
            result = mgr.handle_prepare_share({"item_ids": ["i1"], "format": "text"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "share_index_unreadable")
        self.assertTrue(result["partial"])
        self.assertEqual(result["written_file_count"], 1)
        self.assertEqual(self._index_path().read_bytes(), raw)
        after = self._snapshot()
        new_files = set(after) - set(before)
        self.assertEqual(len(new_files), 1)
        for name in set(before) - {"shares_index.json"}:
            self.assertEqual(after[name], before[name])
        self.assertIn("секретный текст", after[new_files.pop()].decode("utf-8"))

    def test_repair_after_post_payload_failure_retains_pending_and_new_package(self) -> None:
        share_id = self._seed_valid_share()
        good = self._index_path().read_bytes()
        mgr = self._mgr()
        original_save = mgr._save_index

        def corrupt_before_save(**kwargs):
            self._corrupt()
            return original_save(**kwargs)

        with patch.object(mgr, "_save_index", side_effect=corrupt_before_save):
            with self.assertRaises(SharePersistenceError) as caught:
                mgr.prepare_share(["i1"], format="text")
        self.assertEqual(caught.exception.reason, "share_index_unreadable")
        self.assertEqual(caught.exception.written_file_count, 1)
        pending_id, = set(mgr._index) - {share_id}
        pending = dict(mgr._index[pending_id])
        payload = self._index_path().parent / pending["filename"]
        payload_bytes = payload.read_bytes()
        self._index_path().write_bytes(good)
        new = mgr.prepare_share(["i1"], format="text")
        index = json.loads(self._index_path().read_text(encoding="utf-8"))
        self.assertEqual(set(index), {share_id, pending_id, new.share_id})
        self.assertEqual(index[pending_id], pending)
        self.assertEqual(payload.read_bytes(), payload_bytes)

    def test_prepare_retains_reservation_during_nested_prepare_and_new_package(self) -> None:
        mgr = self._mgr()
        original_persist = mgr._persist_package
        nested = []

        def prepare_while_reserved(package, **kwargs):
            if package.share_id == "firstid":
                self.assertTrue(mgr._index[package.share_id]["_reserved"])
                nested.append(mgr.prepare_share(["i1"], format="text"))
                self.assertTrue(mgr._index[package.share_id]["_reserved"])
            return original_persist(package, **kwargs)

        with patch.object(mgr, "generate_share_id", side_effect=["firstid", "firstid", "secondid"]):
            with patch.object(mgr, "_persist_package", side_effect=prepare_while_reserved):
                first = mgr.prepare_share(["i1"], format="text")
        self.assertEqual(first.share_id, "firstid")
        self.assertEqual(nested[0].share_id, "secondid")
        index = json.loads(self._index_path().read_text(encoding="utf-8"))
        self.assertEqual(set(index), {"firstid", "secondid"})
        self.assertEqual(index[first.share_id], first.to_dict())
        self.assertEqual(index[nested[0].share_id], nested[0].to_dict())
        self.assertEqual(mgr.get_shared(first.share_id), first)

    def test_repeated_prepare_and_revoke_keep_tombstones(self) -> None:
        mgr = self._mgr()
        first = mgr.prepare_share(["i1"], format="text")
        second = mgr.prepare_share(["i1"], format="text")
        self.assertTrue(mgr.revoke_share(first.share_id))
        third = mgr.prepare_share(["i1"], format="text")
        index = json.loads(self._index_path().read_text(encoding="utf-8"))
        self.assertEqual(set(index), {first.share_id, second.share_id, third.share_id})
        self.assertTrue(index[first.share_id]["is_revoked"])
        self.assertNotIn("content", index[first.share_id])
        self.assertEqual(index[second.share_id], second.to_dict())
        self.assertEqual(index[third.share_id], third.to_dict())
        self.assertFalse((self._index_path().parent / first.filename).exists())

    def test_initial_unreadable_recovery_preserves_reserved_slots(self) -> None:
        share_id = self._seed_valid_share()
        good = self._index_path().read_bytes()
        self._corrupt()
        mgr = self._mgr()
        reservation = {"share_id": "pending", "_reserved": True}
        with mgr._lock:
            mgr._index["pending"] = reservation
        self._index_path().write_bytes(good)
        with mgr._lock:
            mgr._ensure_index_writable_locked()
        self.assertFalse(mgr._index_unreadable)
        self.assertIn(share_id, mgr._index)
        self.assertEqual(mgr._index["pending"], reservation)
        self.assertIsNotNone(mgr.get_shared(share_id))

    def test_prepare_share_refuses_and_preserves_unreadable_index(self) -> None:
        self._seed_valid_share()
        raw = self._corrupt()
        mgr = self._mgr()
        files_before = sorted(p.name for p in (Path(self.dir) / "shares").iterdir())
        with self.assertRaises(SharePersistenceError):
            mgr.prepare_share(["i1"], format="text")
        self.assertEqual(self._index_path().read_bytes(), raw)
        # No new orphan payload file is written when the index cannot be updated.
        self.assertEqual(sorted(p.name for p in (Path(self.dir) / "shares").iterdir()), files_before)

    def test_non_dict_index_is_treated_as_unreadable(self) -> None:
        self._seed_valid_share()
        raw = self._corrupt(b"[1, 2, 3]")
        mgr = self._mgr()
        with self.assertRaises(SharePersistenceError):
            mgr.prepare_share(["i1"], format="text")
        self.assertEqual(self._index_path().read_bytes(), raw)

    def test_revoke_on_unreadable_index_raises_instead_of_reporting_not_found(self) -> None:
        share_id = self._seed_valid_share()
        raw = self._corrupt()
        mgr = self._mgr()
        with self.assertRaises(IpcOperationalError):
            mgr.revoke_share(share_id)
        self.assertEqual(self._index_path().read_bytes(), raw)

    def test_reads_do_not_report_anything_valid_when_unreadable(self) -> None:
        share_id = self._seed_valid_share()
        self._corrupt()
        mgr = self._mgr()
        self.assertIsNone(mgr.get_shared(share_id))
        self.assertEqual(mgr.list_shared(include_expired=True, include_revoked=True), [])

    def test_missing_index_is_a_normal_fresh_start(self) -> None:
        mgr = self._mgr()
        pkg = mgr.prepare_share(["i1"], format="text")
        self.assertIsNotNone(mgr.get_shared(pkg.share_id))

    def test_repaired_index_recovers_and_keeps_old_entries(self) -> None:
        share_id = self._seed_valid_share()
        good = self._index_path().read_bytes()
        self._corrupt()
        mgr = self._mgr()
        with self.assertRaises(SharePersistenceError):
            mgr.prepare_share(["i1"], format="text")
        self._index_path().write_bytes(good)  # operator restores the file
        pkg2 = mgr.prepare_share(["i1"], format="text")
        index = json.loads(self._index_path().read_text(encoding="utf-8"))
        self.assertIn(share_id, index)
        self.assertIn(pkg2.share_id, index)
        self.assertIsNotNone(mgr.get_shared(share_id))

    def test_purge_all_resets_unreadable_state(self) -> None:
        self._seed_valid_share()
        self._corrupt()
        mgr = self._mgr()
        mgr.purge_all()
        pkg = mgr.prepare_share(["i1"], format="text")
        self.assertIsNotNone(mgr.get_shared(pkg.share_id))


if __name__ == "__main__":
    unittest.main()
