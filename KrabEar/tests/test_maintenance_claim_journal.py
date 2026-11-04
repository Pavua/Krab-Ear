"""Synthetic claim/permit tests; не проверяют production admission."""

import copy
import pickle
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from core import maintenance_claim_journal as journal


class ClaimJournalTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="ear-claim-")
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / "claim.sqlite3"
        self.store = journal.create_claim_journal(self.path, "inst", "controller")
        self.addCleanup(self.store.close)
        self.store._connection.execute("UPDATE control SET admission = 'TEST_OPEN'")
        self.store._connection.commit()

    def reserve(self):
        return self.store.reserve_leaf("principal", "client", "reserve-1", "controller",
                                       "executor-1", "stt")

    def claim(self, grant_id, *, key="claim-1", executor="executor-1"):
        return self.store.claim_leaf("principal", "client", key, "controller", executor, grant_id)

    def test_fresh_claim_has_single_local_permit_replay_is_history_only(self):
        grant = self.reserve()["grant_id"]
        fresh = self.claim(grant)
        self.assertEqual(fresh["outcome"], "CLAIMED")
        self.assertTrue(fresh["fresh"])
        self.assertIsNotNone(fresh["permit"])
        self.assertFalse(fresh["drain_authority"])
        self.assertFalse(fresh["production_execution_authority"])
        replay = self.claim(grant)
        self.assertEqual(replay["claim_id"], fresh["claim_id"])
        self.assertFalse(replay["fresh"])
        self.assertIsNone(replay["permit"])

    def test_claim_remains_execution_unknown_until_terminal_is_proven(self):
        grant = self.reserve()["grant_id"]
        self.claim(grant)
        self.assertTrue(self.store.status()["unknown"])

    def test_claim_is_bound_to_recorded_executor(self):
        grant = self.reserve()["grant_id"]
        with self.assertRaises(journal.JournalConflict):
            self.claim(grant, executor="executor-2")
        fresh = self.claim(grant)
        self.assertTrue(fresh["fresh"])

    def test_different_request_key_cannot_claim_same_grant_twice(self):
        grant = self.reserve()["grant_id"]
        self.claim(grant)
        with self.assertRaises(journal.JournalConflict):
            self.claim(grant, key="another-claim")

    def test_previously_scoped_leaf_may_be_claimed_after_begin(self):
        grant = self.reserve()["grant_id"]
        self.store.begin_maintenance("principal", "client", "begin-1", "controller", 0)
        fresh = self.claim(grant)
        self.assertTrue(fresh["fresh"])
        self.assertEqual(self.reserve()["grant_id"], grant)
        self.assertEqual(self.store.reserve_leaf("principal", "client", "later", "controller",
                                                 "executor-1", "stt")["outcome"], "DENIED_CLOSED")

    def test_reopen_is_inspection_only_no_new_permit(self):
        grant = self.reserve()["grant_id"]
        self.claim(grant)
        self.store.begin_maintenance("principal", "client", "begin-1", "controller", 0)
        self.store.close()
        reopened = journal.open_claim_journal(self.path, "inst")
        try:
            self.assertFalse(reopened.status()["drain_authority"])
            self.assertTrue(reopened.status()["unknown"])
            with self.assertRaises(journal.JournalError):
                reopened.claim_leaf("principal", "client", "claim-1", "controller", "executor-1", grant)
        finally:
            reopened.close()

    def test_permit_scope_and_concurrent_consumption(self):
        grant = self.reserve()["grant_id"]
        fresh = self.claim(grant)
        permit = fresh["permit"]
        self.assertIs(copy.copy(permit), permit)
        self.assertIs(copy.deepcopy(permit), permit)
        with self.assertRaises(TypeError):
            pickle.dumps(permit)
        self.assertFalse(permit.consume(permit.journal_id, grant, fresh["claim_id"], "executor-2", "stt"))
        self.assertFalse(permit.consume(permit.journal_id, grant, fresh["claim_id"], "executor-1", "tts"))
        barrier = threading.Barrier(3, timeout=5)
        outcomes, errors = [], []

        def consume():
            try:
                barrier.wait()
                outcomes.append(permit.consume(permit.journal_id, grant, fresh["claim_id"], "executor-1", "stt"))
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=consume) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
        finally:
            barrier.abort()
            for worker in workers:
                if worker.ident is not None:
                    worker.join(timeout=5)
        self.assertFalse(errors)
        self.assertCountEqual(outcomes, [True, False])

    def test_corrupt_scope_poison_remains_after_repair(self):
        grant = self.reserve()["grant_id"]
        self.claim(grant)
        self.store._connection.execute("UPDATE scoped_grants SET kind='tts' WHERE grant_id=?", (grant,))
        self.store._connection.commit()
        self.assertEqual(self.store._connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
        with self.assertRaises(journal.JournalError):
            self.store.status()
        self.store._connection.execute("UPDATE scoped_grants SET kind='stt' WHERE grant_id=?", (grant,))
        self.store._connection.commit()
        with self.assertRaises(journal.JournalError):
            self.store.reserve_leaf("principal", "client", "later", "controller", "executor-1", "stt")

    def test_missing_claim_link_is_not_treated_as_available_grant(self):
        grant = self.reserve()["grant_id"]
        self.claim(grant)
        self.store._connection.execute("UPDATE scoped_grants SET claim_id=NULL WHERE grant_id=?", (grant,))
        self.store._connection.commit()
        self.assertEqual(self.store._connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
        with self.assertRaises(journal.JournalError):
            self.claim(grant, key="second-claim")

    def test_claim_commit_then_error_keeps_claim_and_never_returns_permit(self):
        grant = self.reserve()["grant_id"]
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
                self.claim(grant)
            self.assertTrue(self.store.status()["unknown"])
            stored = real_connection.execute(
                "SELECT claim_id FROM scoped_grants WHERE grant_id=?", (grant,)
            ).fetchone()[0]
            self.assertIsNotNone(stored)
            with self.assertRaises(journal.JournalError):
                self.claim(grant)
            self.assertEqual(real_connection.execute(
                "SELECT COUNT(*) FROM command_ledger WHERE operation='claim'"
            ).fetchone()[0], 1)
        finally:
            self.store._connection = real_connection

    def test_reopening_after_recorded_begin_is_rejected(self):
        self.reserve()
        self.store.begin_maintenance("principal", "client", "begin", "controller", 0)
        self.store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
        self.store._connection.commit()
        self.assertEqual(self.store._connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
        with self.assertRaises(journal.JournalError):
            self.store.reserve_leaf("principal", "client", "new", "controller", "executor-1", "stt")

    def test_revision_without_matching_begin_is_rejected(self):
        self.reserve()
        self.store._connection.execute("UPDATE control SET control_revision=77")
        self.store._connection.commit()
        with self.assertRaises(journal.JournalError):
            self.store.status()

    def test_fork_child_refuses_permit_and_inherited_journal_before_lock(self):
        script = r'''
import json, os, select, signal, sys
from core import maintenance_claim_journal as journal
store = journal.create_claim_journal(sys.argv[1], 'inst', 'epoch')
store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
store._connection.commit()
grant = store.reserve_leaf('p', 'c', 'reserve', 'epoch', 'executor', 'stt')['grant_id']
claim = store.claim_leaf('p', 'c', 'claim', 'epoch', 'executor', grant)
permit = claim['permit']
read_fd, write_fd = os.pipe()
permit._lock.acquire()
pid = os.fork()
if pid == 0:
    signal.alarm(4)
    os.close(read_fd)
    allowed = permit.consume(permit.journal_id, grant, claim['claim_id'], 'executor', 'stt')
    try:
        store.status()
    except journal.JournalError:
        rejected = True
    else:
        rejected = False
    os.write(write_fd, json.dumps([allowed, rejected]).encode())
    os._exit(0)
os.close(write_fd)
reaped = False
locked = True
try:
    ready, _, _ = select.select([read_fd], [], [], 3)
    if not ready:
        raise RuntimeError('fork child blocked on inherited lock')
    child_result = json.loads(os.read(read_fd, 1024))
    _, status = os.waitpid(pid, 0)
    reaped = True
    if status != 0 or child_result != [False, True]:
        raise RuntimeError('fork child did not fail closed')
    permit._lock.release()
    locked = False
    if not permit.consume(permit.journal_id, grant, claim['claim_id'], 'executor', 'stt'):
        raise RuntimeError('parent permit unexpectedly lost')
finally:
    if not reaped:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    if locked:
        permit._lock.release()
    os.close(read_fd)
    store.close()
'''
        directory = self.path.parent / "fork-namespace"
        directory.mkdir(mode=0o700)
        result = subprocess.run([sys.executable, "-c", script, str(directory / "fork.db")],
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


class ClaimActorTests(unittest.TestCase):
    def test_two_clients_only_one_fresh_claim(self):
        with tempfile.TemporaryDirectory(prefix="ear-claim-actor-") as directory:
            requests, replies = queue.Queue(), queue.Queue()
            ready = threading.Event()
            barrier = threading.Barrier(3, timeout=5)
            errors = []

            def owner():
                store = None
                try:
                    store = journal.create_claim_journal(Path(directory) / "actor.db", "inst", "epoch")
                    store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
                    store._connection.commit()
                    grant = store.reserve_leaf("p", "c", "reserve", "epoch", "executor", "stt")["grant_id"]
                    ready.set()
                    while True:
                        key = requests.get(timeout=5)
                        if key is None:
                            break
                        try:
                            result = store.claim_leaf("p", "c", key, "epoch", "executor", grant)
                        except journal.JournalConflict:
                            replies.put("conflict")
                        else:
                            replies.put("fresh" if result["fresh"] and result["permit"] else "unexpected")
                    replies.put(store._connection.execute(
                        "SELECT COUNT(*) FROM command_ledger WHERE operation='claim'"
                    ).fetchone()[0])
                except BaseException as exc:
                    errors.append(exc)
                    ready.set()
                finally:
                    if store is not None:
                        try:
                            store.close()
                        except BaseException as exc:
                            errors.append(exc)

            def client(key):
                try:
                    barrier.wait()
                    requests.put(key)
                except BaseException as exc:
                    errors.append(exc)

            owner_thread = threading.Thread(target=owner)
            clients = [threading.Thread(target=client, args=(key,)) for key in ("claim-1", "claim-2")]
            owner_thread.start()
            try:
                self.assertTrue(ready.wait(5))
                self.assertFalse(errors)
                for thread in clients:
                    thread.start()
                barrier.wait()
                for thread in clients:
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
            finally:
                barrier.abort()
                for thread in clients:
                    if thread.ident is not None:
                        thread.join(timeout=5)
                requests.put(None)
                owner_thread.join(timeout=5)
            self.assertFalse(owner_thread.is_alive())
            self.assertFalse(errors)
            self.assertCountEqual([replies.get_nowait(), replies.get_nowait()], ["fresh", "conflict"])
            self.assertEqual(replies.get_nowait(), 1)


if __name__ == "__main__":
    unittest.main()
