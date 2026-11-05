"""Синтетическая классификация, без auth, сети и разрешений на выполнение."""
import copy
import hashlib
import json
import uuid

import pytest

from response_v1 import classify_response
import response_v1


def fingerprint(request):
    payload = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def classify(request, reply, *, binding=True, pinned_digest=None, owner="owner-1", controller="controller-1"):
    return classify_response(request, reply, lab_binding=binding, expected_request_sha256=pinned_digest,
                             expected_owner_epoch=owner, expected_controller_epoch=controller)


@pytest.fixture
def example():
    request = {"v": 1, "op": "reserve", "request_id": "req-1", "client_incarnation": "client-1",
               "owner_epoch": "owner-1", "controller_epoch": "controller-1", "executor": "exec-1", "kind": "stt"}
    reply = {key: request[key] for key in ("v", "op", "request_id", "client_incarnation", "owner_epoch",
                                            "controller_epoch")}
    digest = fingerprint(request)
    reply.update(outcome="DENIED_CLOSED", grant_id=None, claim_id=None, control_revision=1, request_sha256=digest)
    return request, reply, digest


def test_lab_bound_correlated_refusal_is_distinct_from_unknown(example):
    request, reply, digest = example
    assert classify(request, reply, pinned_digest=digest) == "ADMISSION_REFUSED"
    assert classify(request, reply, binding=False, pinned_digest=digest) == "UNKNOWN"


@pytest.mark.parametrize("change", ["wrong_epoch", "wrong_id", "extra", "missing", "bool_revision", "wrong_op"])
def test_malformed_or_uncorrelated_reply_is_unknown(example, change):
    request, reply, digest = copy.deepcopy(example)
    if change == "wrong_epoch":
        reply["owner_epoch"] = "other"
    elif change == "wrong_id":
        reply["request_id"] = "other"
    elif change == "extra":
        reply["surprise"] = 1
    elif change == "missing":
        del reply["claim_id"]
    elif change == "bool_revision":
        reply["control_revision"] = True
    else:
        reply["op"] = "claim"
    assert classify(request, reply, pinned_digest=digest) == "UNKNOWN"


def test_reserved_is_only_receipt_and_conflict_is_unknown(example):
    request, reply, digest = example
    reply.update(outcome="RESERVED", grant_id=str(uuid.uuid4()))
    assert classify(request, reply, pinned_digest=digest) == "RECEIPT_ONLY"
    reply.update(outcome="CONFLICT", grant_id=None)
    assert classify(request, reply, pinned_digest=digest) == "UNKNOWN"


def test_claimed_unknown_and_contradictory_status_remain_unknown(example):
    request, reply, _ = example
    grant = str(uuid.uuid4())
    request = {**{key: request[key] for key in ("v", "request_id", "client_incarnation", "owner_epoch",
                                                 "controller_epoch")}, "op": "revoke", "grant_id": grant}
    reply.update(op="revoke", outcome="CLAIMED_UNKNOWN", grant_id=grant, request_sha256=fingerprint(request))
    assert classify(request, reply, pinned_digest=fingerprint(request)) == "UNKNOWN"
    request.pop("grant_id")
    request["op"] = "status"
    reply = {key: request[key] for key in ("v", "op", "request_id", "client_incarnation", "owner_epoch",
                                            "controller_epoch")}
    reply.update(outcome="OBSERVED", request_sha256=fingerprint(request), control_revision=1, outstanding_count=1, claimed_count=1,
                 unknown=False, drain_authority=False)
    assert classify(request, reply, pinned_digest=fingerprint(request)) == "UNKNOWN"


def test_seal_wrong_revision_is_unknown(example):
    request, reply, _ = example
    request.pop("executor")
    request.pop("kind")
    request.update(op="seal", expected_revision=1)
    reply.update(op="seal", outcome="SEALED", control_revision=1, request_sha256=fingerprint(request))
    assert classify(request, reply, pinned_digest=fingerprint(request)) == "UNKNOWN"
    reply["control_revision"] = 2
    assert classify(request, reply, pinned_digest=fingerprint(request)) == "RECEIPT_ONLY"


@pytest.mark.parametrize("outcome,revision", [("CLOSED", 8), ("ALREADY_CLOSED", 7)])
def test_begin_receipt_revision_rules(example, outcome, revision):
    request, reply, _ = example
    request.pop("executor")
    request.pop("kind")
    request.update(op="begin", expected_revision=7)
    digest = fingerprint(request)
    reply.update(op="begin", outcome=outcome, control_revision=revision, request_sha256=digest)
    assert classify(request, reply, pinned_digest=digest) == "RECEIPT_ONLY"
    reply["control_revision"] = 7 if revision == 8 else 8
    assert classify(request, reply, pinned_digest=digest) == "UNKNOWN"


@pytest.mark.parametrize("canonical,digest,outcome,revision", [
    (b'{"client_incarnation":"inc-1","controller_epoch":"ctrl_1","executor":"worker-1",'
     b'"kind":"stt","op":"reserve","owner_epoch":"owner.1","request_id":"req:1","v":1}',
     "970c9e15da0e357bd28b78caac9096db7d430e23812944c6188a8152e862bb8a", "DENIED_CLOSED", 0),
    (b'{"client_incarnation":"inc-1","controller_epoch":"ctrl_1",'
     b'"expected_revision":9223372036854775806,"op":"seal","owner_epoch":"owner.1",'
     b'"request_id":"seal:1","v":1}',
     "46314f58ba9e1c4cce8434c20edd89cbfa8e2c9ebb2c4ea370cf3b36a773c8db", "SEALED", 9223372036854775807),
])
def test_canonical_vectors_bind_classifier_to_expected_bytes(canonical, digest, outcome, revision):
    request = dict(reversed(list(json.loads(canonical).items())))
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    assert encoded == canonical
    assert hashlib.sha256(canonical).hexdigest() == digest
    reply = {key: request[key] for key in ("v", "op", "request_id", "client_incarnation", "owner_epoch",
                                            "controller_epoch")}
    reply.update(outcome=outcome, grant_id=None, claim_id=None, control_revision=revision, request_sha256=digest)
    expected = "ADMISSION_REFUSED" if outcome == "DENIED_CLOSED" else "RECEIPT_ONLY"
    assert classify(request, reply, pinned_digest=digest, owner="owner.1", controller="ctrl_1") == expected


@pytest.mark.parametrize("mutation", ["payload", "both_epochs", "owner_epoch", "missing_context", "reply_digest",
                                      "nonascii_digest", "bool_version", "null_executor"])
def test_request_binding_and_independently_pinned_epochs_are_required(example, mutation):
    request, reply, digest = copy.deepcopy(example)
    owner, controller = "owner-1", "controller-1"
    if mutation == "payload":
        request["executor"] = "other-executor"
        reply["request_sha256"] = fingerprint(request)
    elif mutation == "both_epochs":
        request["controller_epoch"] = reply["controller_epoch"] = "stale-epoch"
        digest = reply["request_sha256"] = fingerprint(request)
    elif mutation == "owner_epoch":
        request["owner_epoch"] = reply["owner_epoch"] = "stale-owner"
        digest = reply["request_sha256"] = fingerprint(request)
    elif mutation == "missing_context":
        digest = None
    elif mutation == "reply_digest":
        reply["request_sha256"] = "0" * 64
    elif mutation == "nonascii_digest":
        reply["request_sha256"] = "я" * 64
    elif mutation == "bool_version":
        request["v"] = reply["v"] = True
        digest = reply["request_sha256"] = fingerprint(request)
    else:
        request["executor"] = None
        digest = reply["request_sha256"] = fingerprint(request)
    assert classify(request, reply, pinned_digest=digest, owner=owner, controller=controller) == "UNKNOWN"


@pytest.mark.parametrize("op,outcome", [("claim", "CLAIMED"), ("revoke", "REVOKED")])
def test_claim_and_revoke_receipts_bind_grant_without_permit(example, op, outcome):
    request, reply, _ = example
    request.pop("kind")
    grant = str(uuid.uuid4())
    request.update(op=op, grant_id=grant)
    if op == "revoke":
        request.pop("executor")
    reply.update(op=op, outcome=outcome, grant_id=grant,
                 claim_id=str(uuid.uuid4()) if op == "claim" else None, request_sha256=fingerprint(request))
    assert classify(request, reply, pinned_digest=fingerprint(request)) == "RECEIPT_ONLY"
    reply["grant_id"] = str(uuid.uuid4())
    assert classify(request, reply, pinned_digest=fingerprint(request)) == "UNKNOWN"


def test_status_unknown_is_not_a_receipt_and_never_claims_drain(example):
    request, _, _ = example
    request.pop("executor")
    request.pop("kind")
    request["op"] = "status"
    reply = {key: request[key] for key in ("v", "op", "request_id", "client_incarnation", "owner_epoch",
                                            "controller_epoch")}
    reply.update(outcome="OBSERVED", request_sha256=fingerprint(request), control_revision=1, outstanding_count=0,
                 claimed_count=0, unknown=False, drain_authority=False)
    assert classify(request, reply, pinned_digest=fingerprint(request)) == "RECEIPT_ONLY"
    reply["unknown"] = True
    assert classify(request, reply, pinned_digest=fingerprint(request)) == "UNKNOWN"
    reply.update(unknown=False, drain_authority=True)
    assert classify(request, reply, pinned_digest=fingerprint(request)) == "UNKNOWN"


def test_unknown_transport_and_invalid_binding_never_accept_refusal(example):
    request, reply, digest = example
    assert classify(request, None, pinned_digest=digest) == "UNKNOWN"
    assert classify(request, reply, binding=1, pinned_digest=digest) == "UNKNOWN"
    assert classify(request, reply, pinned_digest=digest, owner=None) == "UNKNOWN"
    assert classify(request, reply, pinned_digest=digest, controller="stale") == "UNKNOWN"


@pytest.mark.parametrize("side", ["request", "response"])
def test_non_string_keys_that_compare_like_real_fields_are_unknown(example, side):
    request, reply, digest = example

    class Alias:
        def __init__(self, key):
            self.key = key

        def __hash__(self):
            return hash(self.key)

        def __eq__(self, other):
            return self.key == other

    target, key = (request, "executor") if side == "request" else (reply, "outcome")
    value = target.pop(key)
    target[Alias(key)] = value
    assert classify(request, reply, pinned_digest=digest) == "UNKNOWN"


def test_mutating_request_after_hash_cannot_change_correlation(example, monkeypatch):
    request, reply, digest = example
    reply["controller_epoch"] = "stale-epoch"
    original_sha256 = hashlib.sha256

    def delayed_hash(data):
        result = original_sha256(data)
        request["controller_epoch"] = "stale-epoch"
        return result

    monkeypatch.setattr(response_v1.hashlib, "sha256", delayed_hash)
    assert classify(request, reply, pinned_digest=digest) == "UNKNOWN"


def test_mutating_response_after_hash_cannot_change_correlation(example, monkeypatch):
    request, reply, digest = example
    reply["controller_epoch"] = "stale-epoch"
    original_sha256 = hashlib.sha256

    def delayed_hash(data):
        result = original_sha256(data)
        reply["controller_epoch"] = request["controller_epoch"]
        return result

    monkeypatch.setattr(response_v1.hashlib, "sha256", delayed_hash)
    assert classify(request, reply, pinned_digest=digest) == "UNKNOWN"
