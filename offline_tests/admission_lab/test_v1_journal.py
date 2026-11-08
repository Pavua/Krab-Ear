"""Synthetic v1/journal contract; no production authority or recovery."""
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import select
import subprocess
import sys
import time

import pytest

from core import maintenance_claim_journal as journal
from response_v1 import classify_response
from v1_journal import LabV1


def canonical(request):
    return json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def classify(request, response, *, owner="owner", controller="epoch", digest=None):
    if digest is None:
        digest = hashlib.sha256(canonical(request).encode()).hexdigest()
    return classify_response(request, response, lab_binding=True, expected_request_sha256=digest,
                             expected_owner_epoch=owner, expected_controller_epoch=controller)


@pytest.fixture
def lab(tmp_path):
    store = journal.create_v1_lab_journal(tmp_path / "lab.sqlite3", "inst", "epoch", "owner")
    try:
        store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
        store._connection.commit()
        yield store, LabV1(store, owner_epoch="owner", controller_epoch="epoch")
    finally:
        store.close()


def request(op, key, **fields):
    return {"v": 1, "op": op, "request_id": key, "client_incarnation": "inc", "owner_epoch": "owner",
            "controller_epoch": "epoch", **fields}


def test_full_canonical_request_is_recorded_and_replayed_before_cas(lab):
    store, owner = lab
    reserve = request("reserve", "reserve", executor="worker", kind="stt")
    reserved = owner.execute(reserve)
    grant = reserved["grant_id"]
    claim = request("claim", "claim", executor="worker", grant_id=grant)
    first = owner.execute(claim)
    retained = owner._permits[(grant, first["claim_id"])]
    begin = request("begin", "close", expected_revision=0)
    closed = owner.execute(begin)
    seal = request("seal", "seal", expected_revision=1)
    assert classify(seal, owner.execute(seal)) == "RECEIPT_ONLY"
    replay = owner.execute(claim)
    assert replay == first
    assert len(owner._permits) == 1 and owner._permits[(grant, first["claim_id"])] is retained
    assert owner.execute(begin) == closed
    assert classify(claim, replay) == "RECEIPT_ONLY"
    row = store._connection.execute("SELECT v1_request FROM command_ledger WHERE request_key='claim'").fetchone()
    assert row[0] == canonical(claim)
    assert store._connection.execute("SELECT COUNT(*), COUNT(claim_id) FROM scoped_grants").fetchone() == (1, 1)
    assert store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == (4,)
    assert store.status()["unknown"] is True


def test_changed_full_request_and_lost_pins_fail_closed(lab):
    store, owner = lab
    reserve = request("reserve", "reserve", executor="worker", kind="stt")
    first = owner.execute(reserve)
    assert classify(reserve, first) == "RECEIPT_ONLY"
    changed = dict(reserve, kind="tts")
    assert classify(changed, owner.execute(changed)) == "UNKNOWN"
    changed_epoch = dict(reserve, owner_epoch="other")
    assert classify(changed_epoch, owner.execute(changed_epoch), owner="other") == "UNKNOWN"
    assert classify(reserve, first, owner=None) == "UNKNOWN"
    assert classify(reserve, first, controller=None) == "UNKNOWN"
    assert classify(reserve, first, digest="0" * 64) == "UNKNOWN"
    assert classify_response(reserve, first, lab_binding=True, expected_request_sha256=None,
                             expected_owner_epoch="owner", expected_controller_epoch="epoch") == "UNKNOWN"
    assert store._connection.execute("SELECT COUNT(*) FROM scoped_grants").fetchone() == (1,)
    assert owner.execute(reserve) == first


@pytest.mark.parametrize("first", ["claim", "revoke", "seal"])
def test_claim_revoke_and_seal_preserve_order_and_unknown_debt(lab, first):
    store, owner = lab
    grant = owner.execute(request("reserve", "reserve", executor="worker", kind="stt"))["grant_id"]
    claim = request("claim", "claim", executor="worker", grant_id=grant)
    revoke = request("revoke", "revoke", grant_id=grant)
    seal = request("seal", "seal", expected_revision=1)
    if first == "claim":
        claimed = owner.execute(claim)
        owner.execute(request("begin", "close", expected_revision=0))
        revoked = owner.execute(revoke)
        assert revoked["outcome"] == "CLAIMED_UNKNOWN" and classify(revoke, revoked) == "UNKNOWN"
        assert owner.execute(claim) == claimed
        assert len(owner._permits) == 1
    elif first == "revoke":
        owner.execute(request("begin", "close", expected_revision=0))
        assert classify(revoke, owner.execute(revoke)) == "RECEIPT_ONLY"
        assert owner.execute(claim) is None
    else:
        owner.execute(request("begin", "close", expected_revision=0))
        # С незаявленным неотзванным грантом seal отклоняется (дыра 2 закрыта)
        assert owner.execute(seal) is None
        # Отзываем незаявленный грант (путь деградации)
        assert classify(revoke, owner.execute(revoke)) == "RECEIPT_ONLY"
        sealed = owner.execute(seal)
        assert classify(seal, sealed) == "RECEIPT_ONLY"
        assert owner.execute(seal) == sealed
        assert owner.execute(claim) is None
    if first != "seal":
        assert classify(seal, owner.execute(seal)) == "RECEIPT_ONLY"
    assert store.status()["drain_authority"] is False
    if first == "claim":
        assert store.status()["unknown"] is True


def test_commit_error_yields_unknown_and_no_retry_authority(lab):
    store, owner = lab
    real = store._connection

    class LostCommit:
        def __getattr__(self, name):
            return getattr(real, name)

        def commit(self):
            real.commit()
            raise sqlite3.OperationalError("synthetic commit acknowledgement lost")

    command = request("reserve", "reserve", executor="worker", kind="stt")
    store._connection = LostCommit()
    try:
        assert classify(command, owner.execute(command)) == "UNKNOWN"
        assert classify(command, owner.execute(command)) == "UNKNOWN"
        assert store.status()["unknown"] is True
        assert real.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == (1,)
    finally:
        store._connection = real


@pytest.mark.parametrize("change", ["version", "op", "executor", "kind", "controller", "owner"])
def test_same_key_changed_request_does_not_create_second_grant(lab, change):
    store, owner = lab
    original = request("reserve", "reserve", executor="worker", kind="stt")
    assert owner.execute(original)["grant_id"]
    changed = dict(original)
    if change == "version":
        changed["v"] = 2
    elif change == "op":
        changed = request("begin", "reserve", expected_revision=0)
    elif change == "executor":
        changed["executor"] = "other"
    elif change == "kind":
        changed["kind"] = "tts"
    elif change == "controller":
        changed["controller_epoch"] = "other"
    else:
        changed["owner_epoch"] = "other"
    assert owner.execute(changed) is None
    assert store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == (1,)
    assert store._connection.execute("SELECT COUNT(*) FROM scoped_grants").fetchone() == (1,)


def test_failed_private_permit_retention_poisoned_unknown(lab):
    store, owner = lab
    grant = owner.execute(request("reserve", "reserve", executor="worker", kind="stt"))["grant_id"]

    class FailedRetention(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("synthetic retention failure")

    owner._permits = FailedRetention()
    claim = request("claim", "claim", executor="worker", grant_id=grant)
    assert owner.execute(claim) is None
    assert owner.execute(claim) is None
    assert store.status()["unknown"] is True
    assert store._connection.execute("SELECT COUNT(claim_id) FROM scoped_grants").fetchone() == (1,)


def test_interrupted_private_permit_retention_stays_unknown(lab):
    store, owner = lab
    grant = owner.execute(request("reserve", "reserve", executor="worker", kind="stt"))["grant_id"]

    class InterruptedRetention(dict):
        def __setitem__(self, key, value):
            raise KeyboardInterrupt("synthetic interruption")

    owner._permits = InterruptedRetention()
    claim = request("claim", "claim", executor="worker", grant_id=grant)
    with pytest.raises(KeyboardInterrupt):
        owner.execute(claim)
    assert owner.execute(claim) is None
    assert store.status()["unknown"] is True


@pytest.mark.parametrize("corruption", ["missing", "owner", "payload", "noncanonical"])
def test_damaged_v1_binding_remains_unknown(lab, corruption):
    store, owner = lab
    original = request("reserve", "reserve", executor="worker", kind="stt")
    owner.execute(original)
    altered = dict(original)
    if corruption == "missing":
        value = "{}"
    elif corruption == "owner":
        altered["owner_epoch"] = ""
        value = canonical(altered)
    elif corruption == "payload":
        altered["kind"] = "tts"
        value = canonical(altered)
    else:
        value = json.dumps(original, sort_keys=True)
    store._connection.execute("UPDATE command_ledger SET v1_request=?", (value,))
    store._connection.commit()
    assert owner.execute(original) is None
    with pytest.raises(journal.JournalError):
        store.status()


def test_failed_ledger_insert_rolls_back_grant_and_binding(lab):
    store, owner = lab
    real = store._connection

    class FailInsert:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO command_ledger"):
                raise sqlite3.OperationalError("synthetic ledger insert failure")
            return real.execute(sql, *args)

    store._connection = FailInsert()
    try:
        assert owner.execute(request("reserve", "reserve", executor="worker", kind="stt")) is None
        assert real.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == (0,)
        assert real.execute("SELECT COUNT(*) FROM scoped_grants").fetchone() == (0,)
        assert store.status()["unknown"] is True
    finally:
        store._connection = real


def test_changed_journal_id_fails_before_transaction_effect(lab):
    store, owner = lab
    store._connection.execute("UPDATE control SET journal_id='00000000-0000-0000-0000-000000000001'")
    store._connection.commit()
    assert owner.execute(request("reserve", "reserve", executor="worker", kind="stt")) is None
    assert store._connection.execute("SELECT COUNT(*) FROM scoped_grants").fetchone() == (0,)


def test_owner_epoch_is_bound_to_journal_and_existing_rows(lab):
    store, owner = lab
    original = request("reserve", "reserve", executor="worker", kind="stt")
    owner.execute(original)
    with pytest.raises(journal.JournalError):
        LabV1(store, owner_epoch="other", controller_epoch="epoch")
    with pytest.raises(journal.JournalError):
        LabV1(store, owner_epoch="owner", controller_epoch="epoch")
    altered = dict(original, owner_epoch="other")
    store._connection.execute("UPDATE command_ledger SET v1_request=?", (canonical(altered),))
    store._connection.commit()
    assert owner.execute(request("begin", "begin", expected_revision=0)) is None
    with pytest.raises(journal.JournalError):
        store.status()


def test_downgraded_schema_cannot_be_reported_as_healthy(lab):
    store, _ = lab
    connection = store._connection
    journal_id = connection.execute("SELECT journal_id FROM control").fetchone()[0]
    connection.execute("DROP TABLE command_ledger")
    connection.execute("DROP TABLE control")
    connection.execute(journal._CONTROL_SQL)
    connection.execute(journal._LEDGER_SQL)
    connection.execute("INSERT INTO control VALUES (?, ?, ?, ?, ?, ?)",
                       (4, "inst", journal_id, "epoch", 0, "TEST_OPEN"))
    connection.commit()
    with pytest.raises(journal.JournalError):
        store.status()


def test_inherited_store_is_rejected_before_any_sql(lab):
    store, _ = lab
    connection, creator = store._connection, store._creator_pid

    class NoSql:
        def execute(self, *_args):
            raise AssertionError("inherited store executed SQL")

    store._connection, store._creator_pid = NoSql(), -1
    try:
        with pytest.raises(journal.JournalError):
            LabV1(store, owner_epoch="owner", controller_epoch="epoch")
    finally:
        store._connection, store._creator_pid = connection, creator


@pytest.mark.parametrize("pin", ["owner", "controller"])
@pytest.mark.parametrize("invalid", ["", "bad name", "x" * 129, "не-ascii"])
def test_invalid_creation_epoch_leaves_path_available(tmp_path, pin, invalid):
    path = tmp_path / "epochs.sqlite3"
    controller = invalid if pin == "controller" else "epoch"
    owner = invalid if pin == "owner" else "owner"
    with pytest.raises(journal.JournalError):
        journal.create_v1_lab_journal(path, "inst", controller, owner)
    assert not path.exists()
    store = journal.create_v1_lab_journal(path, "inst", "epoch", "owner")
    store.close()


@pytest.mark.parametrize("pin", ["owner", "controller"])
@pytest.mark.parametrize("form", ["object", "string_subclass"])
def test_epoch_pins_require_exact_strings(tmp_path, pin, form):
    class Spoof:
        def __eq__(self, _other):
            return True

        def __ne__(self, _other):
            return False

    class SpoofString(str):
        def __eq__(self, _other):
            return True

        def __ne__(self, _other):
            return False

    spoof = Spoof() if form == "object" else SpoofString("spoof")
    store = journal.create_v1_lab_journal(tmp_path / "pins.sqlite3", "inst", "epoch", "owner")
    try:
        owner_pin = spoof if pin == "owner" else "owner"
        controller_pin = spoof if pin == "controller" else "epoch"
        with pytest.raises(journal.JournalError):
            LabV1(store, owner_epoch=owner_pin, controller_epoch=controller_pin)
        assert store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == (0,)
        assert store._connection.execute("SELECT COUNT(*) FROM scoped_grants").fetchone() == (0,)
        valid = LabV1(store, owner_epoch="owner", controller_epoch="epoch")
        assert valid.execute(request("reserve", "valid", executor="worker", kind="stt"))["outcome"] == "DENIED_CLOSED"
    finally:
        store.close()


@pytest.mark.parametrize("pin", ["owner", "journal_id"])
def test_journal_context_rejects_string_subclasses_before_writing(lab, pin):
    store, owner = lab

    class SpoofString(str):
        def __eq__(self, _other):
            return True

        def __ne__(self, _other):
            return False

    command = request("reserve", "reserve", executor="worker", kind="stt")
    payload = {"operation": "reserve", "controller_epoch": "epoch", "executor": "worker", "kind": "stt"}
    journal_id = store._connection.execute("SELECT journal_id FROM control").fetchone()[0]
    owner_pin = SpoofString("spoof") if pin == "owner" else "owner"
    journal_pin = SpoofString("spoof") if pin == "journal_id" else journal_id
    with pytest.raises(journal.JournalError):
        store._command("lab-client", "inc", "reserve", payload, v1_request=canonical(command),
                       expected_journal_id=journal_pin, expected_owner_epoch=owner_pin)
    assert store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == (0,)
    assert store._connection.execute("SELECT COUNT(*) FROM scoped_grants").fetchone() == (0,)
    assert owner.execute(command)["outcome"] == "RESERVED"


def test_forged_owner_after_reserve_cannot_claim_with_another_epoch(lab):
    store, owner = lab
    reserve = request("reserve", "reserve", executor="worker", kind="stt")
    grant = owner.execute(reserve)["grant_id"]

    class SpoofString(str):
        def __eq__(self, _other):
            return True

        def __ne__(self, _other):
            return False

    # Обходим проверку конструктора: проверяем второй барьер самого журнала.
    owner._owner_epoch = SpoofString("spoof")
    claim = dict(request("claim", "claim", executor="worker", grant_id=grant), owner_epoch="other")
    assert owner.execute(claim) is None
    assert store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == (1,)
    assert store._connection.execute("SELECT COUNT(claim_id) FROM scoped_grants").fetchone() == (0,)
    assert not owner._permits
    assert store.status()["unknown"] is True


@pytest.mark.parametrize("field", ["controller", "principal", "incarnation", "key"])
def test_journal_rejects_forged_identity_and_controller_types(lab, field):
    store, owner = lab

    class SpoofString(str):
        def __eq__(self, _other):
            return True

        def __ne__(self, _other):
            return False

        __hash__ = str.__hash__

    command = request("reserve", "reserve", executor="worker", kind="stt")
    payload = {"operation": "reserve", "controller_epoch": "epoch", "executor": "worker", "kind": "stt"}
    principal, incarnation, key = "lab-client", "inc", "reserve"
    if field == "controller":
        payload["controller_epoch"] = SpoofString("wrong")
    elif field == "principal":
        principal = SpoofString("wrong")
    elif field == "incarnation":
        incarnation = SpoofString("wrong")
    else:
        key = SpoofString("wrong")
    journal_id = store._connection.execute("SELECT journal_id FROM control").fetchone()[0]
    with pytest.raises(journal.JournalError):
        store._command(principal, incarnation, key, payload, v1_request=canonical(command),
                       expected_journal_id=journal_id, expected_owner_epoch="owner")
    assert store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == (0,)
    assert store._connection.execute("SELECT COUNT(*) FROM scoped_grants").fetchone() == (0,)
    assert owner.execute(command)["outcome"] == "RESERVED"


def test_mutation_before_payload_snapshot_cannot_bypass_type_check(lab):
    store, _ = lab

    class SpoofString(str):
        def __eq__(self, _other):
            return True

        def __ne__(self, _other):
            return False

        __hash__ = str.__hash__

    command = request("reserve", "reserve", executor="worker", kind="stt")
    payload = {"operation": "reserve", "controller_epoch": "epoch", "executor": "worker", "kind": "stt"}
    source, start = inspect.getsourcelines(journal.ClaimJournal._command)
    snapshot_line = start + next(i for i, line in enumerate(source) if "payload = payload.copy()" in line)
    journal_id = store._connection.execute("SELECT journal_id FROM control").fetchone()[0]
    mutated = []

    def inject(frame, event, _argument):
        if frame.f_code is journal.ClaimJournal._command.__code__ and event == "line" and frame.f_lineno == snapshot_line:
            payload["controller_epoch"] = SpoofString("wrong")
            mutated.append(True)
        return inject

    prior_trace = sys.gettrace()
    sys.settrace(inject)
    try:
        with pytest.raises(journal.JournalError):
            store._command("lab-client", "inc", "reserve", payload, v1_request=canonical(command),
                           expected_journal_id=journal_id, expected_owner_epoch="owner")
    finally:
        sys.settrace(prior_trace)
    assert mutated == [True]
    assert store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == (0,)
    assert store._connection.execute("SELECT COUNT(*) FROM scoped_grants").fetchone() == (0,)


@pytest.mark.parametrize("field", ["operation", "executor", "kind", "expected_revision"])
def test_journal_rejects_subclassed_operation_fields(lab, field):
    store, _ = lab

    class StringSubclass(str):
        pass

    class IntSubclass(int):
        pass

    if field == "expected_revision":
        command = request("begin", "op", expected_revision=0)
    else:
        command = request("reserve", "op", executor="worker", kind="stt")
    payload = {"operation": command["op"], "controller_epoch": "epoch"}
    for name in ("executor", "kind", "grant_id", "expected_revision"):
        if name in command:
            payload[name] = command[name]
    payload[field] = IntSubclass(payload[field]) if field == "expected_revision" else StringSubclass(payload[field])
    journal_id = store._connection.execute("SELECT journal_id FROM control").fetchone()[0]
    with pytest.raises(journal.JournalError):
        store._command("lab-client", "inc", "op", payload, v1_request=canonical(command),
                       expected_journal_id=journal_id, expected_owner_epoch="owner")
    assert store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == (0,)
    assert store._connection.execute("SELECT COUNT(*) FROM scoped_grants").fetchone() == (0,)


@pytest.mark.parametrize("operation", ["claim", "revoke"])
def test_subclassed_grant_rejected_for_existing_actionable_grant(lab, operation):
    store, owner = lab

    class StringSubclass(str):
        pass

    grant = owner.execute(request("reserve", "reserve", executor="worker", kind="stt"))["grant_id"]
    if operation == "revoke":
        assert owner.execute(request("begin", "close", expected_revision=0))["outcome"] == "CLOSED"
    fields = {"grant_id": grant}
    if operation == "claim":
        fields["executor"] = "worker"
    command = request(operation, "op", **fields)
    payload = {"operation": operation, "controller_epoch": "epoch", **fields}
    payload["grant_id"] = StringSubclass(grant)
    journal_id = store._connection.execute("SELECT journal_id FROM control").fetchone()[0]
    before = store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone()
    with pytest.raises(journal.JournalError):
        store._command("lab-client", "inc", "op", payload, v1_request=canonical(command),
                       expected_journal_id=journal_id, expected_owner_epoch="owner")
    assert store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == before
    assert store._connection.execute("SELECT claim_id, revoked FROM scoped_grants").fetchone() == (None, 0)
    assert owner.execute(command)["outcome"] == {"claim": "CLAIMED", "revoke": "REVOKED"}[operation]


def test_journal_rejects_extra_payload_field_before_effect(lab):
    store, owner = lab
    command = request("reserve", "reserve", executor="worker", kind="stt")
    payload = {"operation": "reserve", "controller_epoch": "epoch", "executor": "worker", "kind": "stt", "extra": 1}
    journal_id = store._connection.execute("SELECT journal_id FROM control").fetchone()[0]
    with pytest.raises(journal.JournalError):
        store._command("lab-client", "inc", "reserve", payload, v1_request=canonical(command),
                       expected_journal_id=journal_id, expected_owner_epoch="owner")
    assert store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone() == (0,)
    assert store._connection.execute("SELECT COUNT(*) FROM scoped_grants").fetchone() == (0,)
    assert owner.execute(command)["outcome"] == "RESERVED"


@pytest.mark.parametrize("field", ["installation", "controller", "owner"])
def test_new_v5_journal_rejects_subclassed_context_before_creation(tmp_path, field):
    class SpoofString(str):
        pass

    path = tmp_path / "new.sqlite3"
    installation = SpoofString("inst") if field == "installation" else "inst"
    controller = SpoofString("epoch") if field == "controller" else "epoch"
    owner = SpoofString("owner") if field == "owner" else "owner"
    with pytest.raises(journal.JournalError):
        journal.create_v1_lab_journal(path, installation, controller, owner)
    assert not path.exists()
    store = journal.create_v1_lab_journal(path, "inst", "epoch", "owner")
    store.close()


def test_v5_inspection_rejects_subclassed_installation(tmp_path):
    class SpoofString(str):
        pass

    path = tmp_path / "inspect.sqlite3"
    store = journal.create_v1_lab_journal(path, "inst", "epoch", "owner")
    store.close()
    with pytest.raises(journal.JournalError):
        journal.inspect_v1_lab_journal(path, SpoofString("inst"))
    inspected = journal.inspect_v1_lab_journal(path, "inst")
    try:
        assert inspected.status()["unknown"] is True
    finally:
        inspected.close()


def test_v5_reopen_is_inspection_only_and_v4_open_rejects(lab, tmp_path):
    store, owner = lab
    owner.execute(request("begin", "close", expected_revision=0))
    store.close()
    path = tmp_path / "lab.sqlite3"
    with pytest.raises(journal.JournalError):
        journal.open_claim_journal(path, "inst")
    reopened = journal.inspect_v1_lab_journal(path, "inst")
    try:
        assert reopened.status()["inspection_only"] is True
        assert reopened.status()["unknown"] is True
        with pytest.raises(journal.JournalError):
            LabV1(reopened, owner_epoch="owner", controller_epoch="epoch")
    finally:
        reopened.close()


def assert_lost_ack_observation(command, committed, replay, counts, same_permit):
    assert classify(command, replay) == "RECEIPT_ONLY"
    assert replay["claim_id"] == committed
    assert set(replay) == {"v", "op", "request_id", "client_incarnation", "owner_epoch", "controller_epoch",
                           "request_sha256", "outcome", "grant_id", "claim_id", "control_revision"}
    assert counts == [1, 1, 2]
    assert same_permit is True


def read_bounded_line(fd, *, timeout):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) < 8192:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise TimeoutError("synthetic owner response timed out")
        chunk = os.read(fd, 1)
        if not chunk:
            raise EOFError("synthetic owner exited before complete response")
        data.extend(chunk)
        if chunk == b"\n":
            return data.decode("utf-8")
    raise ValueError("synthetic owner response too long")


def test_incomplete_line_cannot_bypass_read_deadline():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"event":')
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            read_bounded_line(read_fd, timeout=0.05)
        assert time.monotonic() - start < 5.0
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize("mutation", ["none", "changed_id", "new_permit"])
def test_cross_process_lost_ack_replay_and_negative_controls(tmp_path, mutation):
    script = r'''
import json, sys, uuid
from core import maintenance_claim_journal as journal
from v1_journal import LabV1
store = journal.create_v1_lab_journal(sys.argv[1], "inst", "epoch", "owner")
try:
    store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
    store._connection.commit()
    owner = LabV1(store, owner_epoch="owner", controller_epoch="epoch")
    original_command = store._command
    seen_claim = False
    def synthetic_mutant(*args, **kwargs):
        global seen_claim
        result = original_command(*args, **kwargs)
        if args[3]["operation"] == "claim":
            if seen_claim and sys.argv[2] == "changed_id":
                result["claim_id"] = str(uuid.uuid4())
            if seen_claim and sys.argv[2] == "new_permit":
                result["permit"] = object()
            seen_claim = True
        return result
    store._command = synthetic_mutant
    print(json.dumps({"event": "ready"}), flush=True)
    first = True
    original_permit = None
    for line in sys.stdin:
        data = json.loads(line)
        if data == {"control": "status"}:
            grants, claims = store._connection.execute("SELECT COUNT(*), COUNT(claim_id) FROM scoped_grants").fetchone()
            records = store._connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0]
            same = len(owner._permits) == 1 and next(iter(owner._permits.values())) is original_permit
            print(json.dumps({"counts": [grants, claims, records], "private_permits": len(owner._permits),
                              "same_permit": same}), flush=True)
            continue
        result = owner.execute(data)
        if first and data.get("op") == "claim" and result is not None:
            first = False
            original_permit = owner._permits[(result["grant_id"], result["claim_id"])]
            print(json.dumps({"event": "committed", "claim_id": result["claim_id"],
                              "private_permits": len(owner._permits)}), flush=True)
            if sys.stdin.readline().strip() != "DROP":
                raise RuntimeError("expected drop")
            continue
        print(json.dumps({"response": result}), flush=True)
finally:
    store.close()
'''
    env_path = str(Path(__file__).parent)
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = env_path + os.pathsep + os.environ.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(tmp_path / "child.sqlite3"), mutation],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=child_env,
    )

    def receive():
        return json.loads(read_bounded_line(process.stdout.fileno(), timeout=5))

    def send(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    try:
        assert receive() == {"event": "ready"}
        reserve = request("reserve", "reserve", executor="worker", kind="stt")
        send(reserve)
        reserved = receive()["response"]
        command = request("claim", "claim", executor="worker", grant_id=reserved["grant_id"])
        send(command)
        committed = receive()
        assert committed["event"] == "committed"
        assert committed["private_permits"] == 1
        process.stdin.write("DROP\n")
        process.stdin.flush()
        send(command)
        replay = receive()["response"]
        send({"control": "status"})
        observed = receive()
        assert observed["private_permits"] == 1
        counts = observed["counts"]
        if mutation == "none":
            assert_lost_ack_observation(command, committed["claim_id"], replay, counts, observed["same_permit"])
            with pytest.raises(AssertionError):
                assert_lost_ack_observation(command, committed["claim_id"], replay, [1, 1, 3], True)
        else:
            assert classify(command, replay) == "RECEIPT_ONLY"
            assert counts == [1, 1, 2]
            if mutation == "changed_id":
                assert replay["claim_id"] != committed["claim_id"] and observed["same_permit"] is True
            else:
                assert replay["claim_id"] == committed["claim_id"] and observed["same_permit"] is False
            with pytest.raises(AssertionError):
                assert_lost_ack_observation(command, committed["claim_id"], replay, counts, observed["same_permit"])
    finally:
        try:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        finally:
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
    assert process.returncode == 0
