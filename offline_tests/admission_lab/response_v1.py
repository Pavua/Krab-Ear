"""Изолированный synthetic response classifier; не production auth или permit."""

import hashlib
import hmac
import json
import re
import uuid

_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z", re.ASCII)
_COMMON = frozenset({"v", "op", "request_id", "client_incarnation", "owner_epoch", "controller_epoch"})
_REQUEST_FIELDS = {
    "reserve": _COMMON | {"executor", "kind"},
    "claim": _COMMON | {"executor", "grant_id"},
    "begin": _COMMON | {"expected_revision"},
    "revoke": _COMMON | {"grant_id"},
    "seal": _COMMON | {"expected_revision"},
    "status": _COMMON,
}
_OUTCOMES = {
    "reserve": {"RESERVED", "DENIED_CLOSED"},
    "claim": {"CLAIMED"},
    "begin": {"CLOSED", "ALREADY_CLOSED"},
    "revoke": {"REVOKED", "CLAIMED_UNKNOWN"},
    "seal": {"SEALED"},
}
_MAX_REVISION = (1 << 63) - 1


def _name(value):
    return type(value) is str and _NAME.fullmatch(value) is not None


def _uuid(value):
    if type(value) is not str or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _number(value):
    return type(value) is int and 0 <= value <= _MAX_REVISION


def classify_response(request, response, *, lab_binding, expected_request_sha256,
                      expected_owner_epoch, expected_controller_epoch):
    """Строгая классификация fixture; lab_binding не проверяет подлинность."""
    if lab_binding is not True or type(request) is not dict or type(response) is not dict:
        return "UNKNOWN"
    request = request.copy()
    response = response.copy()
    if any(type(key) is not str for key in request) or any(type(key) is not str for key in response):
        return "UNKNOWN"
    op = request.get("op")
    if type(op) is not str or op not in _REQUEST_FIELDS or set(request) != _REQUEST_FIELDS[op]:
        return "UNKNOWN"
    if type(request.get("v")) is not int or request["v"] != 1:
        return "UNKNOWN"
    if any(not _name(request[key]) for key in _COMMON - {"v", "op"}):
        return "UNKNOWN"
    if op in {"reserve", "claim"} and not _name(request["executor"]):
        return "UNKNOWN"
    if op == "reserve" and (type(request["kind"]) is not str or request["kind"] not in {"stt", "tts"}):
        return "UNKNOWN"
    if op in {"claim", "revoke"} and not _uuid(request["grant_id"]):
        return "UNKNOWN"
    if op in {"begin", "seal"} and (not _number(request["expected_revision"])
                                    or request["expected_revision"] == _MAX_REVISION):
        return "UNKNOWN"
    if (not _name(expected_owner_epoch) or not _name(expected_controller_epoch)
            or request["owner_epoch"] != expected_owner_epoch
            or request["controller_epoch"] != expected_controller_epoch
            or type(expected_request_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_request_sha256) is None):
        return "UNKNOWN"
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    if (not hmac.compare_digest(digest, expected_request_sha256)
            or type(response.get("request_sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", response["request_sha256"]) is None
            or not hmac.compare_digest(response["request_sha256"], expected_request_sha256)):
        return "UNKNOWN"
    if any(type(response.get(key)) is not type(request[key]) or response.get(key) != request[key]
           for key in _COMMON):
        return "UNKNOWN"
    if op == "status":
        fields = _COMMON | {"request_sha256", "outcome", "control_revision", "outstanding_count", "claimed_count",
                            "unknown", "drain_authority"}
        if (set(response) != fields or type(response["outcome"]) is not str or response["outcome"] != "OBSERVED"
                or not all(_number(response[key]) for key in ("control_revision", "outstanding_count", "claimed_count"))
                or response["claimed_count"] > response["outstanding_count"]
                or type(response["unknown"]) is not bool or response["drain_authority"] is not False
                or (response["claimed_count"] > 0 and response["unknown"] is False)):
            return "UNKNOWN"
        return "UNKNOWN" if response["unknown"] else "RECEIPT_ONLY"
    if set(response) != _COMMON | {"request_sha256", "outcome", "grant_id", "claim_id", "control_revision"}:
        return "UNKNOWN"
    outcome = response["outcome"]
    if type(outcome) is not str or outcome not in _OUTCOMES[op] or not _number(response["control_revision"]):
        return "UNKNOWN"
    grant, claim = response["grant_id"], response["claim_id"]
    if outcome in {"RESERVED", "CLAIMED", "REVOKED", "CLAIMED_UNKNOWN"}:
        if not _uuid(grant) or (op in {"claim", "revoke"} and grant != request["grant_id"]):
            return "UNKNOWN"
    elif grant is not None:
        return "UNKNOWN"
    if outcome == "CLAIMED":
        if not _uuid(claim):
            return "UNKNOWN"
    elif claim is not None:
        return "UNKNOWN"
    if op in {"begin", "seal"}:
        expected = request["expected_revision"] + (outcome != "ALREADY_CLOSED")
        if response["control_revision"] != expected:
            return "UNKNOWN"
    if outcome == "CLAIMED_UNKNOWN":
        return "UNKNOWN"
    return "ADMISSION_REFUSED" if outcome == "DENIED_CLOSED" else "RECEIPT_ONLY"
