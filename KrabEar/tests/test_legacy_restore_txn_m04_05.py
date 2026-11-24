"""M04-05: legacy-restore (Encryption OFF) — «всё или ничего».

До фикса ``handle_restore_history`` копировал ``shutil.copy2`` поверх живых
журналов по одному: сбой между файлами давал смесь (новая история + старые
tombstones), отсутствующий в бэкапе журнал оставался живым, удалённое после
бэкапа воскресало, битый бэкап копировался как есть, а семантический индекс
продолжал жить эмбеддингами другой истории.

Решения владельца (24.11): commit-протокол A5.2b2 (staging + durable маркер +
докатка + pre-restore копия), tombstones ОБЪЕДИНЯТЬ с живыми, семантический
индекс после restore — purge + флаг needs_reindex.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class _FakeSemantic:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def purge_all(self) -> None:
        self.calls.append("purge_all")

    def mark_reindex_required(self) -> bool:
        self.calls.append("mark_reindex_required")
        return True

    def remove_item(self, item_id: str) -> bool:
        return True


def _line(item_id: str, text: str = "t") -> str:
    return json.dumps({"id": item_id, "ts": "2026-01-01T00:00:00Z", "text": text}, ensure_ascii=False)


def _ids(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(x)["id"] for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


class LegacyRestoreTxnTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "profile"
        self.store = StateStore(self.data_dir)
        self.semantic = _FakeSemantic()
        self.svc = HistoryService(store=self.store, semantic_searcher=self.semantic)

    # ---------------------------------------------------------------- helpers

    def _backup(self, history: list[str], *, tombstones: list[str] | None = None,
                status: list[str] | None = None, name: str = "backup_20260101_000000") -> Path:
        backup = self.data_dir / "backups" / name
        backup.mkdir(parents=True)
        (backup / "history.ndjson").write_text("".join(x + "\n" for x in history), encoding="utf-8")
        if tombstones is not None:
            (backup / "history_tombstones.ndjson").write_text(
                "".join(json.dumps({"id": i}) + "\n" for i in tombstones), encoding="utf-8")
        if status is not None:
            (backup / "history_status.ndjson").write_text("".join(x + "\n" for x in status), encoding="utf-8")
        return backup

    def _live(self, name: str) -> Path:
        return self.data_dir / name

    def _restore(self, backup: Path, **extra):
        return self.svc.handle_restore_history({"backup_path": str(backup), **extra})

    def _fail_on_live(self, target_name: str):
        """Сбой при ЛЮБОЙ замене живого журнала target_name (copy2 или os.replace)."""
        data_dir = self.data_dir.resolve()
        real_replace = os.replace
        real_copy2 = shutil.copy2

        def is_target(dst) -> bool:
            p = Path(dst)
            return p.name == target_name and p.parent.resolve() == data_dir

        def replace(src, dst, *a, **k):
            if is_target(dst):
                raise OSError(28, "No space left on device (injected)")
            return real_replace(src, dst, *a, **k)

        def copy2(src, dst, *a, **k):
            if is_target(dst):
                raise OSError(28, "No space left on device (injected)")
            return real_copy2(src, dst, *a, **k)

        return patch.multiple("os", replace=replace), patch.multiple("shutil", copy2=copy2)

    # ------------------------------------------------------------------ tests

    def test_item_deleted_after_backup_does_not_resurrect(self):
        a = self.store.add_history_item(text="alpha")
        b = self.store.add_history_item(text="beta")
        backup = Path(self.svc.handle_backup_history({})["backup_path"])
        self.assertTrue(self.store.delete_history_item(a.id))
        res = self._restore(backup)
        self.assertEqual(res.get("restored_entries"), 1, res)
        active = {i.id for i in self.store._load_active_items_unlocked()}
        self.assertNotIn(a.id, active, "удалённое после бэкапа не должно воскресать")
        self.assertIn(b.id, active)
        self.assertNotIn(a.id, _ids(self._live("history.ndjson")),
                         "текст удалённой записи не должен вернуться на диск")
        self.assertIn(a.id, _ids(self._live("history_tombstones.ndjson")))

    def test_backup_tombstones_are_kept_in_union(self):
        backup = self._backup([_line("keep"), _line("gone-before-backup")], tombstones=["gone-before-backup"])
        self.store._append_tombstone_ndjson({"id": "live-deleted"})
        self._restore(backup)
        tomb = set(_ids(self._live("history_tombstones.ndjson")))
        self.assertEqual(tomb, {"gone-before-backup", "live-deleted"})
        self.assertEqual(_ids(self._live("history.ndjson")), ["keep"])

    def test_purged_item_is_not_rematerialized(self):
        backup = self._backup([_line("secret", "private text"), _line("ok")])
        self._live("history_purged_ids.ndjson").write_text(json.dumps({"id": "secret"}) + "\n", encoding="utf-8")
        self._restore(backup)
        body = self._live("history.ndjson").read_text(encoding="utf-8")
        self.assertNotIn("private text", body)
        self.assertEqual(_ids(self._live("history.ndjson")), ["ok"])

    def test_journal_missing_in_backup_means_empty_not_live(self):
        """Бэкап кладёт файл, только если он был: отсутствие = «пусто тогда»."""
        backup = self._backup([_line("x")])
        self._live("history_status.ndjson").write_text(
            json.dumps({"id": "x", "paste_status": "pasted"}) + "\n", encoding="utf-8")
        res = self._restore(backup)
        self.assertEqual(res.get("restored_entries"), 1, res)
        self.assertEqual(self._live("history_status.ndjson").read_text(encoding="utf-8"), "")

    def test_failure_between_journals_rolls_forward_to_full_set(self):
        """Сбой на втором журнале: после докатки набор целиком новый, не смесь."""
        self.store._append_tombstone_ndjson({"id": "live-deleted"})
        original_history = self._live("history.ndjson").read_bytes() if self._live("history.ndjson").exists() else b""
        backup = self._backup([_line("n1"), _line("n2")], tombstones=["n0"])
        p_os, p_sh = self._fail_on_live("history_tombstones.ndjson")
        with p_os, p_sh:
            try:
                res = self._restore(backup)
            except OSError:
                res = {"raised": True}
        self.assertFalse(res.get("ok", False), res)
        # Новый процесс/сервис: точка обслуживания докатывает транзакцию.
        svc2 = HistoryService(store=StateStore(self.data_dir), semantic_searcher=_FakeSemantic())
        verdict = svc2._recover_pending_restore()
        history_now = self._live("history.ndjson").read_bytes()
        tomb_now = set(_ids(self._live("history_tombstones.ndjson")))
        if history_now == original_history:
            self.fail(f"докатка не выполнена: {verdict}")
        self.assertEqual(_ids(self._live("history.ndjson")), ["n1", "n2"])
        self.assertEqual(tomb_now, {"n0", "live-deleted"}, "смесь: новая история со старыми tombstones")
        self.assertTrue(verdict and verdict.get("ok"), verdict)

    def test_malformed_or_encrypted_backup_refused_before_any_write(self):
        for bad in ("not json at all", "ENC1:abcdef"):
            with self.subTest(bad=bad):
                self.store.add_history_item(text="live")
                before = {n: self._live(n).read_bytes() for n in ("history.ndjson",) if self._live(n).exists()}
                backup = self._backup([_line("a"), bad], name=f"backup_2026010{len(bad) % 9}_000000")
                res = self._restore(backup)
                self.assertFalse(res.get("ok", True), res)
                self.assertEqual(res.get("reason"), "legacy_backup_unreadable")
                after = {n: self._live(n).read_bytes() for n in before}
                self.assertEqual(before, after)

    def test_semantic_index_purged_and_marked_for_reindex(self):
        backup = self._backup([_line("a")])
        res = self._restore(backup)
        self.assertEqual(self.semantic.calls, ["purge_all", "mark_reindex_required"])
        self.assertTrue(res.get("semantic_reindex_required"), res)

    def test_prerestore_copy_is_created_and_restorable(self):
        self.store.add_history_item(text="before restore")
        live_before = self._live("history.ndjson").read_bytes()
        backup = self._backup([_line("a")])
        res = self._restore(backup)
        self.assertTrue(res.get("ok"), res)
        pre = Path(res["pre_restore_backup"])
        self.assertTrue(pre.is_dir())
        self.assertTrue(pre.resolve().is_relative_to((self.data_dir / "backups").resolve()))
        self.assertEqual((pre / "history.ndjson").read_bytes(), live_before)
        listed = {b["path"] for b in self.svc.handle_list_backups({})["backups"]}
        self.assertIn(str(pre), listed, "страховочная копия должна быть видна как обычный бэкап")
        self.assertEqual(oct(pre.stat().st_mode & 0o777), "0o700")


    # ------------------------------------------------- докатка: fail-closed ветки

    def _pending_after_failure(self, backup: Path, **extra):
        p_os, p_sh = self._fail_on_live("history_tombstones.ndjson")
        with p_os, p_sh:
            res = self._restore(backup, **extra)
        self.assertTrue(res.get("pending"), res)
        from backend.encrypted_snapshot import restore_marker_dirs

        markers = restore_marker_dirs(self.data_dir)
        self.assertEqual(len(markers), 1)
        return markers[0]

    def test_tampered_staging_blocks_roll_forward_and_changes_nothing(self):
        backup = self._backup([_line("n1")], tombstones=["n0"])
        staging = self._pending_after_failure(backup)
        (staging / "history_tombstones.ndjson").write_text('{"id": "evil"}\n', encoding="utf-8")
        before = {n: self._live(n).read_bytes() for n in ("history.ndjson", "history_tombstones.ndjson")}
        verdict = self.svc._recover_pending_restore()
        self.assertFalse(verdict["ok"])
        self.assertTrue(verdict["pending"])
        self.assertEqual(before, {n: self._live(n).read_bytes() for n in before})
        refused = self._restore(backup)
        self.assertFalse(refused.get("ok", False), "незавершённая транзакция блокирует новый restore")

    def test_roll_forward_refused_when_policy_not_off_and_settings_not_in_txn(self):
        backup = self._backup([_line("n1")])
        self._pending_after_failure(backup)
        with patch("backend.history_encryption_policy.store_policy_reader", return_value=lambda: True):
            verdict = self.svc._recover_pending_restore()
        self.assertFalse(verdict["ok"], verdict)
        self.assertEqual(verdict["reason"], "snapshot_policy_mismatch")
        verdict = self.svc._recover_pending_restore()  # policy снова OFF
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(_ids(self._live("history.ndjson")), ["n1"])

    def test_restored_settings_are_part_of_the_transaction(self):
        backup = self._backup([_line("n1")])
        settings = dict(self.store.load_settings())
        settings["history_encryption_enabled"] = False
        settings["privacy_mode_enabled"] = False
        settings["ui_language"] = "es"
        (backup / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        staging = self._pending_after_failure(backup, restore_settings=True)
        self.assertTrue((staging / "settings.json").is_file(), "settings входят в staging транзакции")
        self.assertNotEqual(self.store.load_settings().get("ui_language"), "es")
        verdict = self.svc._recover_pending_restore()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(StateStore(self.data_dir).load_settings().get("ui_language"), "es")

    def test_commit_marker_failure_after_replacements_is_reported_pending(self):
        """Замены прошли, COMMITTED не записался → ответ pending, не «отказ до записи»."""
        import backend.legacy_restore as legacy_restore

        backup = self._backup([_line("n1")])
        real = legacy_restore._write_restore_marker

        def fail_on_committed(staging, marker):
            if marker.get("state") == "COMMITTED":
                raise OSError(5, "I/O error (injected)")
            return real(staging, marker)

        with patch.object(legacy_restore, "_write_restore_marker", fail_on_committed):
            res = self._restore(backup)
        self.assertFalse(res.get("ok", False))
        self.assertTrue(res.get("pending"), res)
        verdict = self.svc._recover_pending_restore()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(_ids(self._live("history.ndjson")), ["n1"])

    def test_roll_forward_by_module_path_invalidates_semantic_files(self):
        """Докатка мимо HistoryService (auto_backup на старте) тоже обнуляет индекс на диске.

        Иначе после краха посреди restore новый процесс поднимал бы снимок
        эмбеддингов ПРЕЖНЕЙ истории без флага needs_reindex.
        """
        from backend.encrypted_snapshot import recover_pending_restore_from_store
        from backend.semantic_search import SemanticSearcher

        snapshot = self.data_dir / "embeddings_snapshot.npz"
        snapshot.write_bytes(b"old-history-embeddings")
        backup = self._backup([_line("n1")])
        self._pending_after_failure(backup)
        verdict = recover_pending_restore_from_store(StateStore(self.data_dir))
        self.assertTrue(verdict["ok"] and verdict["rolled_forward"], verdict)
        self.assertFalse(snapshot.exists(), "снимок эмбеддингов прежней истории пережил докатку")
        self.assertTrue(SemanticSearcher(self.data_dir).status()["needs_reindex"])

    def test_roll_forward_by_module_path_resets_store_caches_in_live_process(self):
        """В живом процессе кэш id, набранный по частично заменённым файлам, сбрасывается."""
        from backend.encrypted_snapshot import recover_pending_restore_from_store

        backup = self._backup([_line("n1"), _line("n2")])
        self._pending_after_failure(backup)
        self.store._active_ids = {"stale-id"}
        verdict = recover_pending_restore_from_store(self.store)
        self.assertTrue(verdict["rolled_forward"], verdict)
        self.assertNotEqual(self.store._active_ids, {"stale-id"}, "кэш id пережил докатку")

    def test_pending_failure_resets_semantic_index_in_memory(self):
        backup = self._backup([_line("n1")])
        self._pending_after_failure(backup)
        self.assertEqual(self.semantic.calls, ["purge_all", "mark_reindex_required"])

    def test_concurrent_add_from_other_thread_is_serialized_not_mixed(self):
        """Запись из другого потока во время restore ждёт lock и ложится ПОСЛЕ транзакции."""
        import threading

        import backend.legacy_restore as legacy_restore

        backup = self._backup([_line("n1"), _line("n2")])
        entered = threading.Event()
        release = threading.Event()
        real = legacy_restore._replace_live
        added: dict = {}

        def slow_replace(tmp, target):
            entered.set()
            release.wait(10)
            return real(tmp, target)

        def add():
            added["item"] = self.store.add_history_item(text="concurrent")

        with patch.object(legacy_restore, "_replace_live", slow_replace):
            restorer = threading.Thread(target=lambda: added.update(res=self._restore(backup)))
            restorer.start()
            self.assertTrue(entered.wait(10))
            writer = threading.Thread(target=add)
            writer.start()
            writer.join(0.3)
            self.assertTrue(writer.is_alive(), "запись истории обязана ждать store-lock транзакции")
            release.set()
            restorer.join(10)
            writer.join(10)
        self.assertTrue(added["res"].get("ok"), added["res"])
        self.assertEqual(_ids(self._live("history.ndjson")), ["n1", "n2", added["item"].id])


if __name__ == "__main__":
    unittest.main()
