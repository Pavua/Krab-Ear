"""Synthetic v1 adapter; not a production interface or execution permit."""
import hashlib
import json

from core.maintenance_claim_journal import JournalConflict, JournalError
from response_v1 import valid_request


class LabV1:
    def __init__(self, store, *, owner_epoch, controller_epoch):
        store._check_process()
        if store._version != 5 or not store._active:
            raise JournalError("Требуется новый lab journal v5")
        stored_owner, stored_controller = store._connection.execute(
            "SELECT owner_epoch, controller_epoch FROM control").fetchone()
        if stored_owner != owner_epoch or stored_controller != controller_epoch:
            raise JournalError("Несовпадающие эпохи журнала")
        if getattr(store, "_v1_owner_bound", False):
            raise JournalError("Lab journal уже имеет владельца")
        self._store = store
        self._owner_epoch = owner_epoch
        self._controller_epoch = controller_epoch
        self._journal_id = store._connection.execute("SELECT journal_id FROM control").fetchone()[0]
        self._permits = {}
        self._uncertain = False
        store._v1_owner_bound = True

    def execute(self, request):
        if self._uncertain or type(request) is not dict:
            return None
        request = request.copy()
        if (not valid_request(request) or request["op"] == "status"
                or request["owner_epoch"] != self._owner_epoch
                or request["controller_epoch"] != self._controller_epoch):
            return None
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        payload = {"operation": request["op"], "controller_epoch": request["controller_epoch"]}
        for field in ("executor", "kind", "grant_id", "expected_revision"):
            if field in request:
                payload[field] = request[field]
        self._uncertain = True
        try:
            result = self._store._command("lab-client", request["client_incarnation"], request["request_id"],
                                          payload, v1_request=encoded, expected_journal_id=self._journal_id,
                                          expected_owner_epoch=self._owner_epoch)
            if result["permit"] is not None:
                self._permits[(result["grant_id"], result["claim_id"])] = result["permit"]
            response = {key: request[key] for key in ("v", "op", "request_id", "client_incarnation", "owner_epoch",
                                                       "controller_epoch")}
            response.update(request_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                            outcome=result["outcome"], grant_id=result["grant_id"], claim_id=result["claim_id"],
                            control_revision=result["control_revision"])
        except JournalConflict:
            self._uncertain = False
            return None
        except BaseException as exc:
            self._store._poisoned = True
            if isinstance(exc, Exception):
                return None
            raise
        self._uncertain = False
        return response
