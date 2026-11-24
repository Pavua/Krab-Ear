"""M04-05 (ревью EAR2236-RESTORE-QUAL): устойчивость legacy-restore.

B — краш посреди restore и повторный запуск:
  * докатка может случиться НЕ сразу (auto_backup выключен/privacy ON → первая
    точка обслуживания — следующий backup/restore/листинг). Между крахом и
    докаткой пользователь удаляет и purge'ит записи; докатка обязана объединить
    ledger с ТЕКУЩИМ живым, а не писать tombstones/историю на момент краха;
  * жёсткий kill ДО маркера оставляет staging без маркера: следующий restore
    под тем же lock'ом его убирает (иначе retention заблокирован навсегда,
    а plaintext-копия журналов лежит в data_dir).
C — история и семантический индекс — раздельные статусы: ``history_committed``
  и ``semantic_reindex_required`` во всех исходах (успех, pending, докатка);
  без загруженного searcher индекс обнуляется на диске.
D — сбой записи до маркера: живые файлы целы, staging нет, повторный restore
  проходит. После маркера — см. test_legacy_restore_txn_m04_05 (pending + докатка).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.legacy_restore as legacy_restore  # noqa: E402
from backend.encrypted_snapshot import (  # noqa: E402
    RESTORE_STAGING_PREFIX,
    recover_pending_restore_from_store,
    restore_marker_dirs,
    restore_staging_dirs,
)
from backend.history_service import HistoryService  # noqa: E402
from backend.semantic_search import SemanticSearcher  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
import tests.test_legacy_restore_txn_m04_05 as base  # noqa: E402  (модуль, не класс: иначе pytest соберёт чужие тесты)

_FakeSemantic = base._FakeSemantic
_ids = base._ids
_line = base._line


def _ids_nl(path: Path) -> list[str]:
    """Как ``_ids``, но делит только по ``\\n`` — как writer/reader журналов."""
    return [json.loads(x)["id"] for x in path.read_text(encoding="utf-8").split("\n") if x.strip()]


LIVE = ("history.ndjson", "history_tombstones.ndjson", "history_status.ndjson")


class LegacyRestoreQualTest(unittest.TestCase):
    _backup = base.LegacyRestoreTxnTest._backup
    _live = base.LegacyRestoreTxnTest._live
    _restore = base.LegacyRestoreTxnTest._restore
    _fail_on_live = base.LegacyRestoreTxnTest._fail_on_live
    _pending_after_failure = base.LegacyRestoreTxnTest._pending_after_failure

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "profile"
        self.store = StateStore(self.data_dir)
        self.semantic = _FakeSemantic()
        self.svc = HistoryService(store=self.store, semantic_searcher=self.semantic)

    def _snapshot_live(self) -> dict:
        return {n: self._live(n).read_bytes() for n in LIVE if self._live(n).exists()}

    # ------------------------------------------------------------------ B

    def test_delayed_roll_forward_keeps_deletions_made_after_crash(self):
        backup = self._backup([_line("n1"), _line("n2", "private text"), _line("n3")], tombstones=["n0"])
        self._pending_after_failure(backup)  # history уже заменена, tombstones — нет
        # Окно до докатки: процесс работает, пользователь удаляет и purge'ит.
        self.store._append_tombstone_ndjson({"id": "n1"})
        with open(self._live("history_purged_ids.ndjson"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "n2"}) + "\n")

        svc2 = HistoryService(store=StateStore(self.data_dir), semantic_searcher=_FakeSemantic())
        verdict = svc2._recover_pending_restore()

        self.assertTrue(verdict and verdict.get("ok"), verdict)
        self.assertIn("n1", _ids(self._live("history_tombstones.ndjson")),
                      "удаление, сделанное после краха, потеряно докаткой")
        self.assertEqual(_ids(self._live("history.ndjson")), ["n3"])
        self.assertNotIn("private text", self._live("history.ndjson").read_text(encoding="utf-8"),
                         "текст purged-записи вернулся на диск докаткой")
        self.assertEqual(restore_marker_dirs(self.data_dir), [])
        # Удалённые записи staging — не «записи окна»: счётчики окна их не считают.
        self.assertEqual((verdict["records_carried"], verdict["records_excluded_deleted"]), (0, 0), verdict)

    def test_status_window_delta_carried_but_prerestore_status_dropped(self):
        status = self._live("history_status.ndjson")
        status.write_text(json.dumps({"id": "old", "status": "archived"}) + "\n", encoding="utf-8")
        backup = self._backup([_line("n1")])
        p_os, p_sh = self._fail_on_live("history.ndjson")  # живые файлы ещё прежние
        with p_os, p_sh:
            self.assertTrue(self._restore(backup).get("pending"))
        window = json.dumps({"id": "n1", "status": "starred"})
        with open(status, "a", encoding="utf-8") as fh:
            fh.write(window + "\n")
        verdict = self.svc._recover_pending_restore()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(status.read_text(encoding="utf-8").splitlines(), [window],
                         "статус прежней истории перенесён или изменение окна потеряно")
        self.assertEqual(verdict["delta_records_carried"], 1, verdict)

    def test_repeated_status_transitions_in_window_keep_order(self):
        """ok → failed → ok в окне: строки идентичны (без времени), last-write-wins по порядку.

        Дедуп по тексту выбросил бы последнее ``ok`` — запись осталась бы ``failed``.
        Повтор совпадает и со строкой pre-restore набора — её тоже нельзя считать «известной».
        """
        status = self._live("history_status.ndjson")
        ok = json.dumps({"id": "n1", "paste_status": "ok"})
        failed = json.dumps({"id": "n1", "paste_status": "failed"})
        status.write_text(ok + "\n", encoding="utf-8")  # pre-restore: n1 уже ok
        backup = self._backup([_line("n1")], status=[ok])
        self._pending_after_failure(backup)  # status-журнал ещё прежний
        with open(status, "a", encoding="utf-8") as fh:
            fh.write(failed + "\n" + ok + "\n" + failed + "\n" + ok + "\n")
        verdict = self.svc._recover_pending_restore()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(status.read_text(encoding="utf-8").splitlines(), [ok, failed, ok, failed, ok])
        self.assertEqual(verdict["delta_records_carried"], 4, verdict)
        self.assertEqual(StateStore(self.data_dir)._load_status_overrides_unlocked(), {"n1": "ok"})

    def test_status_window_after_status_already_replaced(self):
        """Крах после замены status: окно = хвост после байтов staging."""
        status = self._live("history_status.ndjson")
        ok = json.dumps({"id": "n1", "paste_status": "ok"})
        failed = json.dumps({"id": "n1", "paste_status": "failed"})
        backup = self._backup([_line("n1")], status=[ok])
        import backend.legacy_restore as lr

        real = lr._commit_marker

        def crash_after_replace(*a, **k):
            raise lr.SnapshotOperationRefused("snapshot_fsync_failed", "injected", pending=True)

        with patch.object(lr, "_commit_marker", crash_after_replace):
            self.assertTrue(self._restore(backup).get("pending"))
        self.assertEqual(status.read_text(encoding="utf-8").splitlines(), [ok])
        with open(status, "a", encoding="utf-8") as fh:
            fh.write(failed + "\n" + ok + "\n")
        self.assertIs(lr._commit_marker, real)
        verdict = self.svc._recover_pending_restore()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(status.read_text(encoding="utf-8").splitlines(), [ok, failed, ok])
        self.assertEqual(verdict["delta_records_carried"], 2, verdict)

    def test_status_window_when_status_journal_absent_before_restore(self):
        status = self._live("history_status.ndjson")
        status.unlink(missing_ok=True)  # журнала не было на момент restore
        from_backup = json.dumps({"id": "n1", "paste_status": "failed"})
        backup = self._backup([_line("n1")], status=[from_backup])
        self._pending_after_failure(backup)  # status ещё не заменён (и не существовал)
        window = json.dumps({"id": "n1", "paste_status": "ok"})
        status.write_text(window + "\n", encoding="utf-8")
        verdict = self.svc._recover_pending_restore()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(status.read_text(encoding="utf-8").splitlines(), [from_backup, window])
        self.assertNotIn("status_window_base_unknown: history_status.ndjson", verdict["warnings"], verdict)

    def test_prerestore_junk_line_is_not_reported_as_window_record(self):
        history = self._live("history.ndjson")
        with open(history, "a", encoding="utf-8") as fh:
            fh.write("junk line before restore\n")
        backup = self._backup([_line("n1")])
        p_os, p_sh = self._fail_on_live("history.ndjson")
        with p_os, p_sh:
            self.assertTrue(self._restore(backup).get("pending"))
        verdict = self.svc._recover_pending_restore()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["records_unparsable"], 0, verdict)
        self.assertEqual(_ids(history), ["n1"])

    def test_unicode_line_separators_inside_records_survive_restore_and_window(self):
        """Writer — ``json.dumps(ensure_ascii=False) + "\\n"``, reader — ``for line in fh``.

        ``str.splitlines()`` рвал запись с U+2028/U+0085 в тексте: restore бэкапа
        отказывал «не JSON», а запись окна pending терялась как неразбираемая.
        """
        weird = "строка\u2028вторая\x85третья\u2029конец"
        rec = json.dumps({"id": "n1", "ts": "2026-01-01T00:00:00Z", "text": weird}, ensure_ascii=False)
        backup = self._backup([rec])
        self._pending_after_failure(backup)
        fresh = self.store.add_history_item(text="окно " + weird)
        verdict = self.svc._recover_pending_restore()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["records_unparsable"], 0, verdict)
        self.assertEqual(_ids_nl(self._live("history.ndjson")), ["n1", fresh.id])
        self.assertIn(rec + "\n", self._live("history.ndjson").read_text(encoding="utf-8"))
        # Запись с U+2028 из pre-restore набора — не запись окна (крах до первой замены).
        p_os, p_sh = self._fail_on_live("history.ndjson")
        with p_os, p_sh:
            self.assertTrue(self._restore(self._backup([_line("m1")], name="backup_20260104_000000")).get("pending"))
        verdict = self.svc._recover_pending_restore()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(_ids_nl(self._live("history.ndjson")), ["m1"], "pre-restore запись с U+2028 перенесена как окно")
        res = self._restore(self._backup([rec], name="backup_20260103_000000"))
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(_ids_nl(self._live("history.ndjson")), ["n1"])

    def test_status_window_start_boundaries(self):
        start = legacy_restore._status_window_start
        self.assertEqual(start("a\nb\nc\n", bases=["a\n", "a\nb\n", None]), (2, True), "самая длинная база")
        self.assertEqual(start("a\nb\n", bases=["a"]), (1, True), "база без \\n, но граница строки")
        self.assertEqual(start("ab\n", bases=["a"]), (0, False), "совпадение посреди строки — не база")
        self.assertEqual(start("x\n", bases=["a\n"]), (0, False))
        self.assertEqual(start("x\n", bases=[""]), (0, True))
        self.assertEqual(start("a\u2028b\nc\n", bases=["a\u2028b\n"]), (1, True), "U+2028 — не граница строки")

    def test_dictation_in_pending_window_survives_delayed_roll_forward(self):
        backup = self._backup([_line("n1")])
        self._pending_after_failure(backup)  # history уже заменена
        fresh = self.store.add_history_item(text="продиктовано после краха")
        gone = self.store.add_history_item(text="продиктовано и удалено в окне")
        self.assertTrue(self.store.delete_history_item(gone.id))
        with open(self._live("history.ndjson"), "a", encoding="utf-8") as fh:  # правка n1 в окне
            fh.write(_line("n1", "edited") + "\n")
        verdict = self.svc._recover_pending_restore()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(_ids(self._live("history.ndjson")), ["n1", fresh.id],
                         "запись окна потеряна, удалённая воскресла или id задвоен")
        self.assertEqual(verdict["records_carried"], 1, verdict)
        self.assertEqual(verdict["records_excluded_deleted"], 1, verdict)

    def test_prerestore_records_are_not_carried_when_crash_before_first_replace(self):
        old = self.store.add_history_item(text="до restore")
        backup = self._backup([_line("n1")])
        p_os, p_sh = self._fail_on_live("history.ndjson")  # первая же замена
        with p_os, p_sh:
            res = self._restore(backup)
        self.assertTrue(res.get("pending"), res)
        self.assertIn(old.id, _ids(self._live("history.ndjson")), "живой файл ещё прежний")
        fresh = self.store.add_history_item(text="окно")
        verdict = self.svc._recover_pending_restore()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(_ids(self._live("history.ndjson")), ["n1", fresh.id],
                         "прежняя история перенесена — restore фактически не выполнен")
        self.assertEqual(verdict["records_carried"], 1, verdict)

    def test_roll_forward_is_idempotent_after_second_crash(self):
        backup = self._backup([_line("n1"), _line("n2")], tombstones=["n0"])
        self._pending_after_failure(backup)
        self.store._append_tombstone_ndjson({"id": "n1"})
        # Повторный сбой посреди докатки — маркер остаётся, следующий проход доводит.
        p_os, p_sh = self._fail_on_live("history_status.ndjson")
        with p_os, p_sh:
            first = self.svc._recover_pending_restore()
        self.assertFalse(first["ok"], first)
        self.assertTrue(first["pending"], first)
        second = self.svc._recover_pending_restore()
        self.assertTrue(second["ok"], second)
        self.assertEqual(_ids(self._live("history.ndjson")), ["n2"])
        self.assertEqual(set(_ids(self._live("history_tombstones.ndjson"))), {"n0", "n1"})

    def test_orphan_staging_without_marker_is_swept_by_next_restore(self):
        orphan = self.data_dir / f"{RESTORE_STAGING_PREFIX}legacy-restore-dead"
        orphan.mkdir(mode=0o700)
        (orphan / "history.ndjson").write_text(_line("old", "plaintext copy") + "\n", encoding="utf-8")
        self.assertEqual(restore_staging_dirs(self.data_dir), [orphan])

        res = self._restore(self._backup([_line("n1")]))

        self.assertTrue(res.get("ok"), res)
        self.assertFalse(orphan.exists(), "staging без маркера пережил следующий restore")
        self.assertEqual(restore_staging_dirs(self.data_dir), [], "retention остался бы заблокирован")

    def test_staging_with_marker_is_not_swept(self):
        backup = self._backup([_line("n1")])
        staging = self._pending_after_failure(backup)
        self.assertEqual(legacy_restore.sweep_orphan_restore_staging_locked(self.data_dir), [])
        self.assertTrue(staging.is_dir(), "незавершённую транзакцию нельзя убирать как сироту")
        with self.assertRaises(legacy_restore.SnapshotOperationRefused):
            legacy_restore.apply_legacy_restore_locked(
                data_dir=self.data_dir, files={}, backup_dir=backup, pre_restore_backup="x")
        self.assertTrue(staging.is_dir())

    # ------------------------------------------------------------------ C

    def test_success_reports_history_and_index_separately(self):
        res = self._restore(self._backup([_line("n1")]))
        self.assertIs(res.get("history_committed"), True, res)
        self.assertIs(res.get("semantic_reindex_required"), True, res)

    def test_index_failure_does_not_hide_committed_history(self):
        def boom():
            raise OSError(5, "I/O error (injected)")

        self.semantic.purge_all = boom
        res = self._restore(self._backup([_line("n1")]))
        self.assertTrue(res.get("ok"), res)
        self.assertIs(res.get("history_committed"), True, res)
        self.assertIs(res.get("semantic_reindex_required"), False, res)

    def test_restore_without_loaded_searcher_invalidates_index_on_disk(self):
        svc = HistoryService(store=self.store, semantic_searcher=None)
        snapshot = self.data_dir / "embeddings_snapshot.npz"
        snapshot.write_bytes(b"old-history-embeddings")
        res = svc.handle_restore_history({"backup_path": str(self._backup([_line("n1")]))})
        self.assertTrue(res.get("ok"), res)
        self.assertFalse(snapshot.exists(), "эмбеддинги прежней истории пережили restore")
        self.assertTrue(SemanticSearcher(self.data_dir).status()["needs_reindex"])
        self.assertIs(res.get("semantic_reindex_required"), True, res)

    def test_pending_response_reports_index_status(self):
        p_os, p_sh = self._fail_on_live("history_tombstones.ndjson")
        with p_os, p_sh:
            res = self._restore(self._backup([_line("n1")]))
        self.assertTrue(res.get("pending"), res)
        self.assertIs(res.get("history_committed"), False, res)
        self.assertIs(res.get("semantic_reindex_required"), True, res)

    def test_roll_forward_verdict_reports_index_status(self):
        backup = self._backup([_line("n1")])
        self._pending_after_failure(backup)
        with patch("backend.semantic_search.invalidate_index_files", return_value=False):
            verdict = recover_pending_restore_from_store(StateStore(self.data_dir))
        self.assertTrue(verdict["ok"] and verdict["rolled_forward"], verdict)
        self.assertIs(verdict.get("semantic_reindex_required"), False, verdict)

    def test_roll_forward_verdict_reports_index_reset_ok(self):
        backup = self._backup([_line("n1")])
        self._pending_after_failure(backup)
        verdict = recover_pending_restore_from_store(StateStore(self.data_dir))
        self.assertIs(verdict.get("semantic_reindex_required"), True, verdict)

    # ------------------------------------------------------------------ D

    def _assert_refused_before_marker(self, res, before, reason):
        self.assertFalse(res.get("ok", True), res)
        self.assertFalse(res.get("pending"), res)
        self.assertEqual(res.get("reason"), reason, res)
        self.assertIs(res.get("history_committed"), False, res)
        self.assertEqual(self._snapshot_live(), before, "живые журналы изменены до маркера")
        self.assertEqual(restore_staging_dirs(self.data_dir), [], "staging не убран после отказа")
        self.assertEqual(self.semantic.calls, [], "индекс сброшен, хотя история не менялась")
        again = self._restore(self._backup([_line("r1")], name="backup_20260102_000000"))
        self.assertTrue(again.get("ok"), again)
        self.assertEqual(_ids(self._live("history.ndjson")), ["r1"])

    def test_staging_write_failure_before_marker(self):
        self.store.add_history_item(text="live")
        before = self._snapshot_live()
        backup = self._backup([_line("n1")])
        real = legacy_restore._write_file_durable

        def fail_in_staging(path, blob):
            if Path(path).parent.name.startswith(RESTORE_STAGING_PREFIX):
                raise OSError(28, "No space left on device (injected)")
            return real(path, blob)

        with patch.object(legacy_restore, "_write_file_durable", fail_in_staging):
            res = self._restore(backup)
        self._assert_refused_before_marker(res, before, "snapshot_fsync_failed")

    def test_committing_marker_write_failure(self):
        self.store.add_history_item(text="live")
        before = self._snapshot_live()
        backup = self._backup([_line("n1")])

        def fail_marker(staging, marker):
            raise OSError(28, "No space left on device (injected)")

        with patch.object(legacy_restore, "_write_restore_marker", fail_marker):
            res = self._restore(backup)
        self._assert_refused_before_marker(res, before, "snapshot_fsync_failed")

    def test_prerestore_copy_failure(self):
        self.store.add_history_item(text="live")
        before = self._snapshot_live()
        backup = self._backup([_line("n1")])
        real = legacy_restore._write_file_durable

        def fail_prerestore(path, blob):
            if Path(path).parent.name.startswith(legacy_restore.PRERESTORE_DIR_PREFIX):
                raise OSError(28, "No space left on device (injected)")
            return real(path, blob)

        with patch.object(legacy_restore, "_write_file_durable", fail_prerestore):
            res = self._restore(backup)
        self._assert_refused_before_marker(res, before, "legacy_restore_prerestore_failed")


if __name__ == "__main__":
    unittest.main()
