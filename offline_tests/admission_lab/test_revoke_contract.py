"""Ожидаемый offline-контракт; не production authority или recovery."""
from pathlib import Path
import json
import queue
import sqlite3
import tempfile
import threading

import pytest

from core import maintenance_claim_journal as journal


@pytest.fixture
def opened_journal():
    with tempfile.TemporaryDirectory(prefix="ear-revoke-lab-") as directory:
        store = journal.create_claim_journal(Path(directory) / "lab.sqlite3", "inst", "epoch")
        try:
            store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
            store._connection.commit()
            yield store
        finally:
            store.close()


def test_revoked_grant_cannot_be_claimed_after_maintenance_close(opened_journal):
    store = opened_journal
    grant = store.reserve_leaf("p", "inc", "reserve", "epoch", "exec", "stt")["grant_id"]
    store.begin_maintenance("p", "inc", "close", "epoch", 0)
    revoke = getattr(store, "revoke_unclaimed", None)
    assert callable(revoke), "offline journal must expose transactional revoke"
    revoke("p", "inc", "revoke", "epoch", grant)
    with pytest.raises(journal.JournalConflict):
        store.claim_leaf("p", "inc", "claim", "epoch", "exec", grant)


def test_claim_before_revoke_stays_unknown_and_replay_has_no_permit(opened_journal):
    store = opened_journal
    grant = store.reserve_leaf("p", "inc", "reserve", "epoch", "exec", "stt")["grant_id"]
    claim = store.claim_leaf("p", "inc", "claim", "epoch", "exec", grant)
    store.begin_maintenance("p", "inc", "close", "epoch", 0)
    result = store.revoke_unclaimed("p", "inc", "revoke", "epoch", grant)
    assert result["outcome"] == "CLAIMED_UNKNOWN"
    assert store.status()["unknown"] is True
    assert store.claim_leaf("p", "inc", "claim", "epoch", "exec", grant)["permit"] is None
    assert claim["permit"] is not None


def test_seal_revokes_unclaimed_grants_and_replays_without_authority(opened_journal):
    store = opened_journal
    grant = store.reserve_leaf("p", "inc", "reserve", "epoch", "exec", "stt")["grant_id"]
    store.begin_maintenance("p", "inc", "close", "epoch", 0)
    result = store.seal("p", "inc", "seal", "epoch", 1)
    assert result["outcome"] == "SEALED"
    assert store.seal("p", "inc", "seal", "epoch", 1)["permit"] is None
    with pytest.raises(journal.JournalConflict):
        store.claim_leaf("p", "inc", "claim", "epoch", "exec", grant)


def test_revoke_replay_is_same_outcome_and_different_key_cannot_undo_it(opened_journal):
    store = opened_journal
    grant = store.reserve_leaf("p", "inc", "reserve", "epoch", "exec", "stt")["grant_id"]
    store.begin_maintenance("p", "inc", "close", "epoch", 0)
    first = store.revoke_unclaimed("p", "inc", "revoke", "epoch", grant)
    replay = store.revoke_unclaimed("p", "inc", "revoke", "epoch", grant)
    assert first == replay
    assert replay["permit"] is None
    with pytest.raises(journal.JournalConflict):
        store.revoke_unclaimed("p", "inc", "new-key", "epoch", grant)
    with pytest.raises(journal.JournalConflict):
        store.claim_leaf("p", "inc", "claim", "epoch", "exec", grant)


def test_wrong_incarnation_cannot_revoke_another_grant(opened_journal):
    store = opened_journal
    grant = store.reserve_leaf("p", "inc", "reserve", "epoch", "exec", "stt")["grant_id"]
    store.begin_maintenance("p", "inc", "close", "epoch", 0)
    with pytest.raises(journal.JournalConflict):
        store.revoke_unclaimed("p", "other-inc", "revoke", "epoch", grant)
    assert store.claim_leaf("p", "inc", "claim", "epoch", "exec", grant)["permit"] is not None


def test_conflicting_revoke_replay_preserves_tombstone(opened_journal):
    store = opened_journal
    grant = store.reserve_leaf("p", "inc", "reserve", "epoch", "exec", "stt")["grant_id"]
    other = store.reserve_leaf("p", "inc", "other-reserve", "epoch", "exec", "stt")["grant_id"]
    store.begin_maintenance("p", "inc", "close", "epoch", 0)
    store.revoke_unclaimed("p", "inc", "revoke", "epoch", grant)
    with pytest.raises(journal.JournalConflict):
        store.revoke_unclaimed("p", "inc", "revoke", "epoch", other)
    with pytest.raises(journal.JournalConflict):
        store.revoke_unclaimed("p", "inc", "revoke", "wrong-epoch", grant)
    with pytest.raises(journal.JournalConflict):
        store.claim_leaf("p", "inc", "claim", "epoch", "exec", grant)
    assert store.claim_leaf("p", "inc", "other-claim", "epoch", "exec", other)["permit"] is not None


def test_seal_rejects_open_or_stale_revision(opened_journal):
    store = opened_journal
    with pytest.raises(journal.JournalConflict):
        store.seal("p", "inc", "seal-open", "epoch", 0)
    store.begin_maintenance("p", "inc", "close", "epoch", 0)
    with pytest.raises(journal.JournalConflict):
        store.seal("p", "inc", "seal-stale", "epoch", 0)
    assert store.seal("p", "inc", "seal", "epoch", 1)["outcome"] == "SEALED"


def test_fresh_closed_journal_requires_begin_receipt_before_seal():
    with tempfile.TemporaryDirectory(prefix="ear-fresh-seal-") as directory:
        store = journal.create_claim_journal(Path(directory) / "lab.sqlite3", "inst", "epoch")
        try:
            with pytest.raises(journal.JournalConflict):
                store.seal("p", "inc", "too-early", "epoch", 0)
            assert store.status()["admission"] == "CLOSED"
            assert store.begin_maintenance("p", "inc", "close", "epoch", 0)["outcome"] == "ALREADY_CLOSED"
            assert store.seal("p", "inc", "seal", "epoch", 0)["outcome"] == "SEALED"
            assert store.status()["admission"] == "SEALED"
        finally:
            store.close()


def test_sealed_reopen_is_inspection_only_and_does_not_issue_permit():
    with tempfile.TemporaryDirectory(prefix="ear-revoke-reopen-") as directory:
        path = Path(directory) / "lab.sqlite3"
        store = journal.create_claim_journal(path, "inst", "epoch")
        try:
            store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
            store._connection.commit()
            grant = store.reserve_leaf("p", "inc", "reserve", "epoch", "exec", "stt")["grant_id"]
            store.begin_maintenance("p", "inc", "close", "epoch", 0)
            store.seal("p", "inc", "seal", "epoch", 1)
        finally:
            store.close()
        reopened = journal.open_claim_journal(path, "inst")
        try:
            assert reopened.status()["inspection_only"] is True
            assert reopened.status()["drain_authority"] is False
            with pytest.raises(journal.JournalError):
                reopened.claim_leaf("p", "inc", "claim", "epoch", "exec", grant)
        finally:
            reopened.close()


@pytest.mark.parametrize("fail_at", ["ledger", "after_commit"])
def test_revoke_transaction_failure_stays_unknown_with_consistent_rows(opened_journal, fail_at):
    store = opened_journal
    grant = store.reserve_leaf("p", "inc", "reserve", "epoch", "exec", "stt")["grant_id"]
    store.begin_maintenance("p", "inc", "close", "epoch", 0)
    real = store._connection

    class FaultConnection:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, statement, *args):
            if fail_at == "ledger" and statement.startswith("INSERT INTO command_ledger"):
                raise sqlite3.OperationalError("synthetic ledger failure")
            return real.execute(statement, *args)

        def commit(self):
            real.commit()
            if fail_at == "after_commit":
                raise sqlite3.OperationalError("synthetic lost commit acknowledgement")

    store._connection = FaultConnection()
    try:
        with pytest.raises(journal.JournalError):
            store.revoke_unclaimed("p", "inc", "revoke", "epoch", grant)
        assert store.status()["unknown"] is True
        row = real.execute("SELECT revoked FROM scoped_grants WHERE grant_id=?", (grant,)).fetchone()[0]
        count = real.execute("SELECT COUNT(*) FROM command_ledger WHERE operation='revoke'").fetchone()[0]
        assert (row, count) == ((0, 0) if fail_at == "ledger" else (1, 1))
        with pytest.raises(journal.JournalError):
            store.claim_leaf("p", "inc", "claim", "epoch", "exec", grant)
    finally:
        store._connection = real


@pytest.mark.parametrize("first", ["claim", "revoke"])
def test_actor_serializes_claim_and_revoke_in_each_order(first):
    with tempfile.TemporaryDirectory(prefix="ear-revoke-actor-") as directory:
        requests, responses = queue.Queue(), queue.Queue()
        ready = threading.Event()
        failures = []

        def owner():
            store = None
            try:
                store = journal.create_claim_journal(Path(directory) / "actor.sqlite3", "inst", "epoch")
                store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
                store._connection.commit()
                grant = store.reserve_leaf("p", "inc", "reserve", "epoch", "exec", "stt")["grant_id"]
                store.begin_maintenance("p", "inc", "close", "epoch", 0)
                ready.set()
                for _ in range(2):
                    op = requests.get(timeout=5)
                    try:
                        result = (store.claim_leaf("p", "inc", "claim", "epoch", "exec", grant)
                                  if op == "claim" else store.revoke_unclaimed("p", "inc", "revoke", "epoch", grant))
                        responses.put((op, result["outcome"], result["permit"] is not None))
                    except journal.JournalConflict:
                        responses.put((op, "conflict", False))
                responses.put(("unknown", store.status()["unknown"]))
            except BaseException as exc:
                failures.append(exc)
                ready.set()
            finally:
                if store is not None:
                    try:
                        store.close()
                    except BaseException as exc:
                        failures.append(exc)

        thread = threading.Thread(target=owner)
        thread.start()
        try:
            assert ready.wait(5) and not failures
            requests.put(first)
            initial = responses.get(timeout=5)
            requests.put("revoke" if first == "claim" else "claim")
            second = responses.get(timeout=5)
            unknown = responses.get(timeout=5)
        finally:
            thread.join(7)
        assert not thread.is_alive() and not failures
        if first == "claim":
            assert initial == ("claim", "CLAIMED", True)
            assert second == ("revoke", "CLAIMED_UNKNOWN", False)
            assert unknown == ("unknown", True)
        else:
            assert initial == ("revoke", "REVOKED", False)
            assert second == ("claim", "conflict", False)


def test_inspection_without_grants_is_still_unknown_after_reopen():
    with tempfile.TemporaryDirectory(prefix="ear-empty-reopen-") as directory:
        path = Path(directory) / "lab.sqlite3"
        store = journal.create_claim_journal(path, "inst", "epoch")
        store.close()
        reopened = journal.open_claim_journal(path, "inst")
        try:
            assert reopened.status()["unknown"] is True
            assert reopened.status()["drain_authority"] is False
        finally:
            reopened.close()


def test_forged_sealed_control_without_ledger_fails_validation(opened_journal):
    store = opened_journal
    store._connection.execute("UPDATE control SET admission='SEALED'")
    store._connection.commit()
    with pytest.raises(journal.JournalError):
        store.status()


def test_forged_seal_after_denied_reservation_without_begin_fails_validation():
    with tempfile.TemporaryDirectory(prefix="ear-forged-seal-") as directory:
        store = journal.create_claim_journal(Path(directory) / "lab.sqlite3", "inst", "epoch")
        try:
            assert store.reserve_leaf("p", "inc", "reserve", "epoch", "exec", "stt")["outcome"] == "DENIED_CLOSED"
            payload = json.dumps({"operation": "seal", "controller_epoch": "epoch", "expected_revision": 0},
                                 sort_keys=True, separators=(",", ":"))
            store._connection.execute("UPDATE control SET admission='SEALED', control_revision=1")
            store._connection.execute(
                "INSERT INTO command_ledger (principal,incarnation,request_key,operation,payload,outcome,"
                "grant_id,claim_id,result_revision) VALUES (?,?,?,?,?,'SEALED',NULL,NULL,1)",
                ("p", "inc", "forged", "seal", payload),
            )
            store._connection.commit()
            with pytest.raises(journal.JournalError):
                store.status()
        finally:
            store.close()


@pytest.mark.parametrize("outcome", ["CLOSED", "ALREADY_CLOSED"])
def test_forged_begin_after_seal_fails_validation(opened_journal, outcome):
    store = opened_journal
    store.begin_maintenance("p", "inc", "close", "epoch", 0)
    store.seal("p", "inc", "seal", "epoch", 1)
    revision = 3 if outcome == "CLOSED" else 2
    payload = json.dumps({"operation": "begin", "controller_epoch": "epoch", "expected_revision": 2},
                         sort_keys=True, separators=(",", ":"))
    store._connection.execute("UPDATE control SET admission='CLOSED', control_revision=?", (revision,))
    store._connection.execute(
        "INSERT INTO command_ledger (principal,incarnation,request_key,operation,payload,outcome,"
        "grant_id,claim_id,result_revision) VALUES (?,?,?,?,?,?,NULL,NULL,?)",
        ("p", "inc", "forged", "begin", payload, outcome, revision),
    )
    store._connection.commit()
    with pytest.raises(journal.JournalError):
        store.status()


@pytest.mark.parametrize("fail_at", ["ledger", "after_commit"])
def test_seal_failure_cannot_leave_partial_tombstones(opened_journal, fail_at):
    store = opened_journal
    for index in range(2):
        store.reserve_leaf("p", "inc", f"reserve-{index}", "epoch", "exec", "stt")
    store.begin_maintenance("p", "inc", "close", "epoch", 0)
    real = store._connection

    class FaultConnection:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, statement, *args):
            if fail_at == "ledger" and statement.startswith("INSERT INTO command_ledger"):
                raise sqlite3.OperationalError("synthetic ledger failure")
            return real.execute(statement, *args)

        def commit(self):
            real.commit()
            if fail_at == "after_commit":
                raise sqlite3.OperationalError("synthetic lost commit acknowledgement")

    store._connection = FaultConnection()
    try:
        with pytest.raises(journal.JournalError):
            store.seal("p", "inc", "seal", "epoch", 1)
        assert store.status()["unknown"] is True
        admission = real.execute("SELECT admission FROM control").fetchone()[0]
        revoked = real.execute("SELECT SUM(revoked) FROM scoped_grants").fetchone()[0]
        count = real.execute("SELECT COUNT(*) FROM command_ledger WHERE operation='seal'").fetchone()[0]
        assert (admission, revoked, count) == (("CLOSED", 0, 0) if fail_at == "ledger" else ("SEALED", 2, 1))
        with pytest.raises(journal.JournalError):
            store.seal("p", "inc", "seal", "epoch", 1)
    finally:
        store._connection = real
