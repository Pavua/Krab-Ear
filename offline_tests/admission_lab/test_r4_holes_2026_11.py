"""Тесты R4 (2026-11-08): закрытие трёх дыр журнала допуска lab v5.

Покрывает:
1. Дыра 1: claim старого grant после CLOSED до revoke/seal должен отклоняться (fail-closed).
2. Дыра 2: seal при наличии незаявленных неотзванных grant'ов должен отклоняться
   (требует явного revoke_unclaimed для каждого гранта).
3. Дыра 3: ошибка после создания файла (до завершения инициализации) должна очищать
   недосозданный файл под lock_fd, позволяя повторный create по тому же пути.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import sqlite3
import tempfile
import uuid
import pytest

from core import maintenance_claim_journal as journal
from core.maintenance_claim_journal import JournalConflict, JournalError
from v1_journal import LabV1


def test_hole1_claim_rejected_after_closed():
    """Дыра 1: claim старого grant после перехода в CLOSED должен отклоняться."""
    with tempfile.TemporaryDirectory(prefix="ear-r4-hole1-") as directory:
        path = Path(directory) / "journal.sqlite3"
        store = journal.create_v1_lab_journal(path, "inst", "epoch", "owner")
        try:
            # Открываем допуск для тестов
            store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
            store._connection.commit()
            owner = LabV1(store, owner_epoch="owner", controller_epoch="epoch")

            # Получаем grant во время TEST_OPEN
            reserve_req = {
                "v": 1, "op": "reserve", "request_id": "res-1", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "executor": "exec", "kind": "stt"
            }
            res = owner.execute(reserve_req)
            grant_id = res["grant_id"]
            assert grant_id is not None

            # Закрываем допуск на обслуживание (begin -> CLOSED)
            begin_req = {
                "v": 1, "op": "begin", "request_id": "close", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "expected_revision": 0
            }
            begin_res = owner.execute(begin_req)
            assert begin_res["outcome"] == "CLOSED"
            assert store.status()["admission"] == "CLOSED"

            # Попытка claim старого grant'а после CLOSED ДОЛЖНА отклоняться fail-closed
            claim_req = {
                "v": 1, "op": "claim", "request_id": "claim-1", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "executor": "exec", "grant_id": grant_id
            }
            # В адаптере LabV1 JournalConflict транслируется в отказ (None)
            assert owner.execute(claim_req) is None

            # Проверяем также напрямую через _command, что возбуждается JournalConflict
            journal_id = store._connection.execute("SELECT journal_id FROM control").fetchone()[0]
            import json
            v1_encoded = json.dumps({
                "v": 1, "op": "claim", "request_id": "claim-direct", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "executor": "exec", "grant_id": grant_id
            }, sort_keys=True, separators=(",", ":"))
            with pytest.raises(JournalConflict):
                store._command(
                    "lab-client", "inc", "claim-direct",
                    {"operation": "claim", "controller_epoch": "epoch", "executor": "exec", "grant_id": grant_id},
                    v1_request=v1_encoded,
                    expected_journal_id=journal_id, expected_owner_epoch="owner"
                )
        finally:
            store.close()


def test_hole2_seal_rejected_with_unclaimed_grants():
    """Дыра 2: seal при наличии незаявленных неотзванных грантов должен отклоняться."""
    with tempfile.TemporaryDirectory(prefix="ear-r4-hole2-") as directory:
        path = Path(directory) / "journal.sqlite3"
        store = journal.create_v1_lab_journal(path, "inst", "epoch", "owner")
        try:
            store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
            store._connection.commit()
            owner = LabV1(store, owner_epoch="owner", controller_epoch="epoch")

            reserve_req = {
                "v": 1, "op": "reserve", "request_id": "res-1", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "executor": "exec", "kind": "stt"
            }
            res = owner.execute(reserve_req)
            grant_id = res["grant_id"]

            begin_req = {
                "v": 1, "op": "begin", "request_id": "close", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "expected_revision": 0
            }
            owner.execute(begin_req)
            assert store.status()["admission"] == "CLOSED"

            # seal при наличии незаявленного неотзванного grant_id ДОЛЖЕН отклоняться
            seal_req = {
                "v": 1, "op": "seal", "request_id": "seal-1", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "expected_revision": 1
            }
            assert owner.execute(seal_req) is None

            # Путь деградации/завершения: явно отзываем незаявленный грант
            revoke_req = {
                "v": 1, "op": "revoke", "request_id": "revoke-1", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "grant_id": grant_id
            }
            revoke_res = owner.execute(revoke_req)
            assert revoke_res["outcome"] == "REVOKED"

            # Теперь, когда все незаявленные гранты явно отозваны, seal успешен
            seal_res = owner.execute(seal_req)
            assert seal_res["outcome"] == "SEALED"
            assert store.status()["admission"] == "SEALED"
        finally:
            store.close()


def test_hole3_create_failure_cleans_up_and_allows_retry():
    """Дыра 3: сбой после создания файла должен очищать недосозданный файл под блокировкой и позволять повторный create."""
    with tempfile.TemporaryDirectory(prefix="ear-r4-hole3-") as directory:
        path = Path(directory) / "journal.sqlite3"

        # Имитируем ошибку на этапе инициализации SQLite таблиц
        real_connect = journal.foundation._connect_existing

        class FaultConn:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, *args):
                if "CREATE TABLE scoped_grants" in sql:
                    raise sqlite3.OperationalError("synthetic table creation failure")
                return self._real.execute(sql, *args)

        def fault_connect(p, read_only=False):
            conn = real_connect(p, read_only=read_only)
            return FaultConn(conn)

        # Перехватываем os.fsync для проверки дескрипторной безопасности:
        # fsync директории должен вызываться на ЖИВОМ дескрипторе (без EBADF).
        real_fsync = os.fsync
        fsync_calls = []

        def spy_fsync(fd):
            # Проверяем, что дескриптор валиден в момент вызова fsync
            os.fstat(fd)
            fsync_calls.append(fd)
            return real_fsync(fd)

        journal.foundation._connect_existing = fault_connect
        journal.os.fsync = spy_fsync
        try:
            with pytest.raises(JournalError):
                journal.create_claim_journal(path, "inst", "epoch")
        finally:
            journal.foundation._connect_existing = real_connect
            journal.os.fsync = real_fsync

        # Проверяем: fsync директории действительно был вызван на валидном FD
        assert len(fsync_calls) > 0, "fsync каталога не был вызван при очистке"

        # Проверяем: файл НЕ должен остаться на диске в поврежденном состоянии
        assert not path.exists(), "Поврежденный файл остался на диске после сбоя создания"

        # Повторный вызов create по тому же пути должен успешно создать журнал
        store = journal.create_claim_journal(path, "inst", "epoch")
        try:
            assert path.exists()
            assert store.status()["admission"] == "CLOSED"
        finally:
            store.close()


def test_hole1_validate_rejects_claim_after_closed():
    """_validate в v5 отвергает журнал, где claim записан после closed."""
    with tempfile.TemporaryDirectory(prefix="ear-r4-val1-") as directory:
        path = Path(directory) / "journal.sqlite3"
        store = journal.create_v1_lab_journal(path, "inst", "epoch", "owner")
        try:
            conn = store._connection
            conn.execute("UPDATE control SET admission='TEST_OPEN'")
            conn.commit()
            owner = LabV1(store, owner_epoch="owner", controller_epoch="epoch")

            # Легитимный reserve
            reserve_req = {
                "v": 1, "op": "reserve", "request_id": "res-1", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "executor": "exec", "kind": "stt"
            }
            res = owner.execute(reserve_req)
            grant_id = res["grant_id"]

            # Легитимный begin переводит admission в CLOSED
            begin_req = {
                "v": 1, "op": "begin", "request_id": "b1", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "expected_revision": 0
            }
            owner.execute(begin_req)

            # Вставляем поддельный claim в command_ledger после closed
            claim_id = str(uuid.uuid4())
            v1_req = json.dumps({
                "client_incarnation": "inc",
                "controller_epoch": "epoch",
                "executor": "exec",
                "grant_id": grant_id,
                "op": "claim",
                "owner_epoch": "owner",
                "request_id": "c1",
                "v": 1
            }, sort_keys=True, separators=(",", ":"))
            payload = json.dumps({
                "controller_epoch": "epoch",
                "executor": "exec",
                "grant_id": grant_id,
                "operation": "claim"
            }, sort_keys=True, separators=(",", ":"))
            conn.execute("UPDATE scoped_grants SET claim_id=? WHERE grant_id=?", (claim_id, grant_id))
            conn.execute(
                "INSERT INTO command_ledger (principal, incarnation, request_key, operation, payload, v1_request, outcome, grant_id, claim_id, result_revision) "
                "VALUES ('lab-client', 'inc', 'c1', 'claim', ?, ?, 'CLAIMED', ?, ?, 1)",
                (payload, v1_req, grant_id, claim_id)
            )
            conn.commit()
            with pytest.raises(JournalError, match="Claim после закрытия"):
                journal._validate(conn, "inst", allow_test_open=False, expected_version=5)
        finally:
            store.close()


def test_hole2_validate_rejects_seal_with_unclaimed_grants():
    """_validate в v5 отвергает журнал, где seal выполнен при незаявленных неотзванных грантах."""
    with tempfile.TemporaryDirectory(prefix="ear-r4-val2-") as directory:
        path = Path(directory) / "journal.sqlite3"
        store = journal.create_v1_lab_journal(path, "inst", "epoch", "owner")
        try:
            conn = store._connection
            conn.execute("UPDATE control SET admission='TEST_OPEN'")
            conn.commit()
            owner = LabV1(store, owner_epoch="owner", controller_epoch="epoch")

            # Легитимный reserve
            reserve_req = {
                "v": 1, "op": "reserve", "request_id": "res-1", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "executor": "exec", "kind": "stt"
            }
            owner.execute(reserve_req)

            # Легитимный begin (revision 0 -> 1)
            begin_req = {
                "v": 1, "op": "begin", "request_id": "b1", "client_incarnation": "inc",
                "owner_epoch": "owner", "controller_epoch": "epoch", "expected_revision": 0
            }
            owner.execute(begin_req)

            # Незаконный seal без явного revoke незаявленного гранта
            v1_seal = json.dumps({
                "client_incarnation": "inc",
                "controller_epoch": "epoch",
                "expected_revision": 1,
                "op": "seal",
                "owner_epoch": "owner",
                "request_id": "s1",
                "v": 1
            }, sort_keys=True, separators=(",", ":"))
            payload = json.dumps({
                "controller_epoch": "epoch",
                "expected_revision": 1,
                "operation": "seal"
            }, sort_keys=True, separators=(",", ":"))
            conn.execute(
                "INSERT INTO command_ledger (principal, incarnation, request_key, operation, payload, v1_request, outcome, grant_id, claim_id, result_revision) "
                "VALUES ('lab-client', 'inc', 's1', 'seal', ?, ?, 'SEALED', NULL, NULL, 2)",
                (payload, v1_seal)
            )
            conn.execute("UPDATE control SET admission='SEALED', control_revision=2")
            conn.commit()
            with pytest.raises(JournalError, match="Seal при незаявленных грантах"):
                journal._validate(conn, "inst", allow_test_open=False, expected_version=5)
        finally:
            store.close()


def test_legacy_v4_preserves_permissive_claim_and_auto_revoke():
    """Legacy v4 сохраняет историческое поведение: claim разрешен после begin, seal авто-отзывает."""
    with tempfile.TemporaryDirectory(prefix="ear-r4-v4-") as directory:
        path = Path(directory) / "journal_v4.sqlite3"
        store = journal.create_claim_journal(path, "inst", "epoch")
        try:
            store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
            store._connection.commit()

            # В v4 резервируем два гранта до begin:
            grant = store.reserve_leaf("p", "inc", "r1", "epoch", "exec", "stt")["grant_id"]
            grant2 = store.reserve_leaf("p", "inc", "r2", "epoch", "exec", "stt")["grant_id"]
            store.begin_maintenance("p", "inc", "close", "epoch", 0)

            # В v4 claim после begin разрешен:
            claimed = store.claim_leaf("p", "inc", "c1", "epoch", "exec", grant)
            assert claimed["grant_id"] == grant

            # В v4 seal не падает на незаявленных грантах, а автоматически отзывает их:
            sealed = store.seal("p", "inc", "s1", "epoch", 1)
            assert sealed["outcome"] == "SEALED"
            # Проверяем, что незаявленный grant2 действительно автоматически отозван:
            row = store._connection.execute("SELECT revoked FROM scoped_grants WHERE grant_id=?", (grant2,)).fetchone()
            assert row == (1,)
        finally:
            store.close()
