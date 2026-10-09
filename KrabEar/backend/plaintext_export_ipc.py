"""Доверенный client-consent протокол A5.3; транспорт не доказывает клик UI."""

from typing import Any
import uuid

from backend.plaintext_export_authorization import (
    PlaintextExportAuthorizer,
    REASON_PLAINTEXT_SESSION_EXPIRED,
)

# Имена описывают конечную запись, а не формат произвольного вызова.
LOCAL_PLAINTEXT_SINKS = frozenset({
    "history-md", "history-ndjson", "history-selected", "history-action-items",
    "history-meeting-report", "history-stats-report", "history-pdf",
})


def _session(value: object) -> str:
    if type(value) is not str or len(value) != 36:
        return ""
    try:
        return value if str(uuid.UUID(value)) == value else ""
    except ValueError:
        return ""


def _epoch(value: object) -> bytes:
    if type(value) is not str or len(value) != 64:
        return b""
    try:
        decoded = bytes.fromhex(value)
        return decoded if decoded.hex() == value else b""
    except ValueError:
        return b""


class PlaintextExportIPC:
    """Строгие параметры RPC, общий authorizer и стабильные причины отказа."""

    def __init__(self, authorizer: PlaintextExportAuthorizer) -> None:
        self.authorizer = authorizer

    def _invalid(self) -> dict[str, Any]:
        # Невалидный запрос тоже наблюдает UNKNOWN/privacy и отзывает старые grants.
        status = self.authorizer.get_policy()
        return {"ok": False, "reason": status.get("reason", REASON_PLAINTEXT_SESSION_EXPIRED)}

    def handle_get_policy(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.authorizer.get_policy()

    def handle_grant(self, params: dict[str, Any]) -> dict[str, Any]:
        session = _session(params.get("app_session_id"))
        epoch = _epoch(params.get("expected_epoch"))
        generation = params.get("expected_policy_generation")
        if not session or not epoch or type(generation) is not int or generation < 0:
            return self._invalid()
        result = self.authorizer.issue_grant(
            session, expected_epoch=epoch, expected_policy_generation=generation,
        )
        if not result.ok:
            return {"ok": False, "reason": result.reason}
        return {
            "ok": True, "capability": result.capability,
            "epoch": result.epoch.hex(), "policy_generation": result.policy_generation,
        }

    def handle_revoke(self, params: dict[str, Any]) -> dict[str, Any]:
        # Неизвестный/чужой context — идемпотентный no-op, чужую сессию не трогаем.
        self.authorizer.revoke(
            _session(params.get("app_session_id")), _epoch(params.get("epoch")),
            params.get("capability"),
        )
        return {"ok": True}

    def handle_validate(self, params: dict[str, Any]) -> dict[str, Any]:
        session = _session(params.get("app_session_id"))
        epoch = _epoch(params.get("epoch"))
        generation = params.get("expected_policy_generation")
        sequence = params.get("operation_seq")
        sink = params.get("sink_kind")
        capability = params.get("capability")
        if (
            not session or not epoch
            or type(generation) is not int or generation < 0
            or type(sequence) is not int or sequence < 1
            or type(sink) is not str or sink not in LOCAL_PLAINTEXT_SINKS
            or (capability is not None and type(capability) is not str)
        ):
            return self._invalid()
        result = self.authorizer.validate_for_write(
            session, epoch, capability, generation, sequence, sink,
        )
        if not result.ok:
            return {"ok": False, "reason": result.reason}
        return {
            "ok": True, "receipt": result.receipt, "epoch": epoch.hex(),
            "policy_generation": result.policy_generation,
            "operation_seq": sequence, "sink_kind": sink,
        }
