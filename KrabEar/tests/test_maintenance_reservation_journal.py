"""Изолированный журнал решений; не тест production admission."""

from contextlib import closing
import json
import queue
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
import sqlite3
from unittest.mock import patch

from core import maintenance_reservation_journal as journal


class ReservationJournalTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="ear-reservations-")
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / "journal.sqlite3"
        self.store = journal.create_journal(self.path, "installation", "epoch-1")
        self.addCleanup(self.store.close)

    def reserve(self, key="key-1", *, kind="stt"):
        return self.store.acquire("principal", "client-1", key, "epoch-1", kind)

    def open_for_test(self):
        self.store._connection.execute("UPDATE control SET admission = 'TEST_OPEN'")
        self.store._connection.commit()

    def test_closed_decision_is_durable_and_never_grants_execution(self):
        first = self.reserve()
        self.assertEqual(first["outcome"], "DENIED_CLOSED")
        self.assertIsNone(first["grant_id"])
        self.assertFalse(first["execution_authority"])
        self.assertFalse(first["drain_authority"])
        self.assertEqual(self.reserve(), first)
        self.assertEqual(self.store.status()["ledger_count"], 1)

    def test_same_key_different_payload_or_operation_conflicts(self):
        self.reserve()
        with self.assertRaises(journal.JournalConflict):
            self.reserve(kind="tts")
        with self.assertRaises(journal.JournalConflict):
            self.store.begin_maintenance("principal", "client-1", "key-1", "epoch-1", 0)
        self.assertEqual(self.store.status()["ledger_count"], 1)

    def test_begin_closes_and_replay_is_historical(self):
        self.open_for_test()
        granted = self.reserve()
        self.assertEqual(granted["outcome"], "RESERVED")
        self.assertIsNotNone(granted["grant_id"])
        closed = self.store.begin_maintenance("principal", "client-1", "begin-1", "epoch-1", 0)
        self.assertEqual(closed["outcome"], "CLOSED")
        self.assertEqual(closed["control_revision"], 1)
        self.assertEqual(self.store.begin_maintenance(
            "principal", "client-1", "begin-1", "epoch-1", 0), closed)
        self.assertEqual(self.reserve(), granted)
        self.assertEqual(self.reserve("new-key")["outcome"], "DENIED_CLOSED")
        status = self.store.status()
        self.assertEqual(status["outstanding_count"], 1)
        self.assertEqual(status["control_revision"], 1)

    def test_reopen_is_inspection_only_and_open_state_is_rejected(self):
        self.reserve()
        self.store.close()
        reopened = journal.open_journal(self.path, "installation")
        try:
            self.assertEqual(reopened.status()["ledger_count"], 1)
            with self.assertRaises(sqlite3.OperationalError):
                reopened._connection.execute("UPDATE control SET control_revision = 999")
            with self.assertRaises(journal.JournalError):
                reopened.acquire("principal", "client-1", "key-2", "epoch-1", "stt")
        finally:
            reopened.close()

    def test_persisted_test_open_never_activates_on_restart(self):
        self.open_for_test()
        self.store.close()
        with self.assertRaises(journal.RecoveryRequired):
            journal.open_journal(self.path, "installation")

    def test_exception_after_actual_commit_is_unknown_and_cannot_repeat(self):
        self.open_for_test()
        real_connection = self.store._connection

        class CommitThenFail:
            def __getattr__(self, name):
                return getattr(real_connection, name)

            def commit(self):
                real_connection.commit()
                raise sqlite3.OperationalError("synthetic lost commit acknowledgement")

        self.store._connection = CommitThenFail()
        try:
            with self.assertRaises(journal.JournalError):
                self.reserve()
            status = self.store.status()
            self.assertTrue(status["unknown"])
            self.assertEqual(status["outstanding_count"], 1)
            with self.assertRaises(journal.JournalError):
                self.reserve()
            self.assertEqual(self.store.status()["ledger_count"], 1)
        finally:
            self.store._connection = real_connection

    def test_acknowledged_reservation_survives_closed_reopen(self):
        self.open_for_test()
        granted = self.reserve()
        self.store.begin_maintenance("principal", "client-1", "begin-1", "epoch-1", 0)
        self.store.close()
        reopened = journal.open_journal(self.path, "installation")
        try:
            status = reopened.status()
            self.assertEqual(status["ledger_count"], 2)
            self.assertEqual(status["outstanding_count"], 1)
            self.assertIsNotNone(granted["grant_id"])
            self.assertFalse(status["drain_authority"])
        finally:
            reopened.close()

    def test_unknown_persisted_outcome_is_rejected(self):
        self.reserve()
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE command_ledger SET outcome = 'ALIEN'")
            connection.commit()
        unexpected = None
        try:
            unexpected = journal.open_journal(self.path, "installation")
        except journal.JournalError:
            pass
        else:
            self.fail("unknown persisted outcome was accepted")
        finally:
            if unexpected is not None:
                unexpected.close()

    def test_sqlite_value_limit_is_installed_before_inspection(self):
        self.store.close()
        reopened = journal.open_journal(self.path, "installation")
        try:
            self.assertLessEqual(reopened._connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), 8192)
        finally:
            reopened.close()

    def test_control_and_schema_materialization_are_bounded(self):
        real_connection = self.store._connection

        class QueryGuard:
            def __getattr__(self, name):
                return getattr(real_connection, name)

            def execute(self, sql, *args):
                if "FROM sqlite_master" in sql or "FROM control" in sql:
                    if "LIMIT" not in sql.upper():
                        raise AssertionError("unbounded schema/control materialization")
                return real_connection.execute(sql, *args)

        self.store._connection = QueryGuard()
        try:
            self.assertEqual(self.store.status()["admission"], "CLOSED")
        finally:
            self.store._connection = real_connection

    def test_oversize_persisted_payload_is_rejected(self):
        self.reserve()
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE command_ledger SET payload = ?", ("x" * 9000,))
            connection.commit()
        with self.assertRaises(journal.JournalError):
            journal.open_journal(self.path, "installation")

    def test_semantically_invalid_rows_fail_even_when_sqlite_is_healthy(self):
        cases = {
            "operation_mismatch": (
                "UPDATE command_ledger SET operation='begin', outcome='CLOSED', "
                "grant_id=NULL WHERE request_key='acquire'"
            ),
            "negative_revision": (
                "UPDATE command_ledger SET result_revision=-1 WHERE request_key='acquire'"
            ),
            "invalid_grant": (
                "UPDATE command_ledger SET grant_id='not-a-uuid' WHERE request_key='acquire'"
            ),
            "noncanonical_payload": (
                "UPDATE command_ledger SET payload=payload || ' ' WHERE request_key='acquire'"
            ),
        }
        for label, statement in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "damaged.db"
                store = journal.create_journal(path, "inst", "epoch")
                try:
                    store._connection.execute("UPDATE control SET admission = 'TEST_OPEN'")
                    store._connection.commit()
                    store.acquire("p", "c", "acquire", "epoch", "stt")
                    store.begin_maintenance("p", "c", "begin", "epoch", 0)
                finally:
                    store.close()
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute(statement)
                    connection.commit()
                    self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
                unexpected = None
                try:
                    unexpected = journal.open_journal(path, "inst")
                except journal.JournalError:
                    pass
                else:
                    self.fail(f"semantic corruption accepted: {label}")
                finally:
                    if unexpected is not None:
                        unexpected.close()

    def test_actual_semantic_error_poison_remains_after_data_restored(self):
        self.open_for_test()
        self.reserve()
        self.store._connection.execute("UPDATE command_ledger SET result_revision=-1")
        self.store._connection.commit()
        with self.assertRaises(journal.JournalError):
            self.store.status()
        self.store._connection.execute("UPDATE command_ledger SET result_revision=0")
        self.store._connection.commit()
        with self.assertRaises(journal.JournalError):
            self.reserve("new-key")

    def test_control_cannot_reopen_after_begin(self):
        self.open_for_test()
        self.reserve()
        self.store.begin_maintenance("principal", "client-1", "begin", "epoch-1", 0)
        self.store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
        self.store._connection.commit()
        self.assertEqual(self.store._connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
        with self.assertRaises(journal.JournalError):
            self.reserve("new-key")

    def test_control_revision_requires_matching_begin_history(self):
        self.open_for_test()
        self.reserve()
        self.store._connection.execute("UPDATE control SET control_revision=77")
        self.store._connection.commit()
        with self.assertRaises(journal.JournalError):
            self.store.status()

    def test_controller_epoch_cannot_change_without_history_transition(self):
        self.open_for_test()
        self.reserve()
        self.store._connection.execute("UPDATE control SET controller_epoch='epoch-2'")
        self.store._connection.commit()
        self.assertEqual(self.store._connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
        with self.assertRaises(journal.JournalError):
            self.store.acquire("principal", "client-1", "new-key", "epoch-2", "stt")

    def test_invalid_identifiers_and_kind_do_not_change_ledger(self):
        for key in ("", "a" * 129, "ключ", "bad space"):
            with self.subTest(key=key), self.assertRaises(journal.JournalError):
                self.reserve(key)
        with self.assertRaises(journal.JournalError):
            self.reserve(kind=[])
        self.assertEqual(self.store.status()["ledger_count"], 0)

    def test_capacity_refuses_new_record_without_discarding_old(self):
        payload = json.dumps({"operation": "acquire", "controller_epoch": "epoch-1", "kind": "stt"},
                             sort_keys=True, separators=(",", ":"))
        self.store._connection.executemany(
            "INSERT INTO command_ledger "
            "(principal, incarnation, request_key, operation, payload, outcome, grant_id, result_revision) "
            "VALUES (?, ?, ?, 'acquire', ?, 'DENIED_CLOSED', NULL, 0)",
            [("principal", "client-1", f"existing-{index}", payload) for index in range(1024)],
        )
        self.store._connection.commit()
        with self.assertRaises(journal.JournalError):
            self.reserve("overflow")
        status = self.store.status()
        self.assertTrue(status["unknown"])
        self.assertEqual(status["ledger_count"], 1024)

    def test_status_validation_failure_poison_is_sticky(self):
        with patch.object(journal, "_validate", side_effect=journal.JournalError("synthetic")):
            with self.assertRaises(journal.JournalError):
                self.store.status()
        with self.assertRaises(journal.JournalError):
            self.reserve()

    def test_interrupted_conflict_rollback_poison_is_sticky(self):
        self.reserve()
        real_connection = self.store._connection

        class RollbackThenInterrupt:
            def __getattr__(self, name):
                return getattr(real_connection, name)

            def rollback(self):
                real_connection.rollback()
                raise KeyboardInterrupt

        self.store._connection = RollbackThenInterrupt()
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.reserve(kind="tts")
        finally:
            self.store._connection = real_connection
        with self.assertRaises(journal.JournalError):
            self.reserve("different-key")


class SerializedOwnerTests(unittest.TestCase):
    def test_two_clients_both_orders_share_one_owner(self):
        for first_kind in ("acquire", "begin"):
            with self.subTest(first=first_kind), tempfile.TemporaryDirectory() as directory:
                requests, replies = queue.Queue(), queue.Queue()
                ready = threading.Event()
                errors = []

                def owner():
                    store = None
                    try:
                        store = journal.create_journal(Path(directory) / "owner.db", "inst", "epoch")
                        store._connection.execute("UPDATE control SET admission = 'TEST_OPEN'")
                        store._connection.commit()
                        ready.set()
                        while True:
                            operation = requests.get(timeout=5)
                            if operation is None:
                                break
                            if operation == "acquire":
                                result = store.acquire("p", "c", "acquire", "epoch", "stt")
                            else:
                                result = store.begin_maintenance("p", "c", "begin", "epoch", 0)
                            replies.put((operation, result))
                        replies.put(("status", store.status()))
                    except BaseException as exc:
                        errors.append(exc)
                        ready.set()
                    finally:
                        if store is not None:
                            try:
                                store.close()
                            except BaseException as exc:
                                errors.append(exc)

                owner_thread = threading.Thread(target=owner)
                owner_thread.start()
                first_sent = threading.Event()
                barrier = threading.Barrier(3, timeout=5)

                def client(operation):
                    try:
                        barrier.wait()
                        if operation != first_kind and not first_sent.wait(5):
                            raise TimeoutError("synthetic first client did not submit")
                        requests.put(operation)
                        if operation == first_kind:
                            first_sent.set()
                    except BaseException as exc:
                        errors.append(exc)

                clients = [threading.Thread(target=client, args=(kind,))
                           for kind in ("acquire", "begin")]
                try:
                    self.assertTrue(ready.wait(5))
                    self.assertFalse(errors)
                    for client_thread in clients:
                        client_thread.start()
                    barrier.wait()
                    for client_thread in clients:
                        client_thread.join(timeout=5)
                        self.assertFalse(client_thread.is_alive())
                finally:
                    barrier.abort()
                    first_sent.set()
                    for client_thread in clients:
                        if client_thread.ident is not None:
                            client_thread.join(timeout=5)
                    requests.put(None)
                    owner_thread.join(timeout=5)
                self.assertFalse(owner_thread.is_alive())
                self.assertFalse(errors)
                result = dict(replies.get_nowait() for _ in range(3))
                self.assertEqual(result["begin"]["outcome"], "CLOSED")
                self.assertEqual(result["status"]["control_revision"], 1)
                expected = "RESERVED" if first_kind == "acquire" else "DENIED_CLOSED"
                self.assertEqual(result["acquire"]["outcome"], expected)
                self.assertEqual(result["status"]["outstanding_count"], int(first_kind == "acquire"))
                self.assertFalse(result["status"]["drain_authority"])

    def test_abrupt_owner_exit_preserves_commit_boundary(self):
        script = r'''
import json, os, sys
from pathlib import Path
from core import maintenance_reservation_journal as journal
path, scenario = Path(sys.argv[1]), sys.argv[2]
store = journal.create_journal(path, "inst", "epoch")
store._connection.execute("UPDATE control SET admission = 'TEST_OPEN'")
store._connection.commit()
real = store._connection
class ExitAtCommit:
    def __getattr__(self, name):
        return getattr(real, name)
    def commit(self):
        if scenario == 'after_commit':
            real.commit()
        print(scenario, flush=True)
        os._exit(0)
if scenario != 'after_ack':
    store._connection = ExitAtCommit()
grant = store.acquire('p', 'c', 'acquire', 'epoch', 'stt')
closed = store.begin_maintenance('p', 'c', 'begin', 'epoch', 0)
print(json.dumps({'scenario': scenario, 'grant': grant, 'closed': closed}), flush=True)
if sys.stdin.readline().strip() != 'exit':
    os._exit(2)
os._exit(0)
'''
        for scenario in ("before_commit", "after_commit", "after_ack"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "crash.db"
                process = subprocess.Popen([sys.executable, "-c", script, str(path), scenario],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                communicated = False
                try:
                    self.assertTrue(select.select([process.stdout], [], [], 10)[0], "no child receipt")
                    receipt = process.stdout.readline().strip()
                    if scenario == "after_ack":
                        ack = json.loads(receipt)
                        self.assertEqual(ack["scenario"], scenario)
                        process.stdin.write("exit\n")
                        process.stdin.flush()
                    else:
                        self.assertEqual(receipt, scenario)
                    stdout, stderr = process.communicate(timeout=10)
                    communicated = True
                    self.assertEqual(process.returncode, 0, stderr)
                    self.assertEqual(stdout, "")
                finally:
                    if not communicated:
                        if process.poll() is None:
                            process.kill()
                        process.communicate(timeout=10)
                if scenario == "after_ack":
                    reopened = journal.open_journal(path, "inst")
                    try:
                        status = reopened.status()
                        self.assertEqual(status["ledger_count"], 2)
                        self.assertEqual(status["outstanding_count"], 1)
                        self.assertFalse(status["drain_authority"])
                        records = reopened._connection.execute(
                            "SELECT request_key, outcome, grant_id, result_revision "
                            "FROM command_ledger ORDER BY sequence"
                        ).fetchall()
                        self.assertEqual(records, [
                            ("acquire", ack["grant"]["outcome"], ack["grant"]["grant_id"],
                             ack["grant"]["control_revision"]),
                            ("begin", ack["closed"]["outcome"], ack["closed"]["grant_id"],
                             ack["closed"]["control_revision"]),
                        ])
                    finally:
                        reopened.close()
                else:
                    with self.assertRaises(journal.RecoveryRequired):
                        journal.open_journal(path, "inst")
                    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
                        count = reader.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0]
                    self.assertEqual(count, int(scenario == "after_commit"))


if __name__ == "__main__":
    unittest.main()
