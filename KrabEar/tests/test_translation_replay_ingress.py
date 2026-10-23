"""Узкий write RPC: auth независимо от общего signing, без audit-копий."""
from types import SimpleNamespace
from unittest.mock import Mock

from backend.event_replay import EventReplayManager


def make_ingress(tmp_path):
    from backend.translation_replay_ingress import TranslationReplayIngress, read_ingress_token
    manager = EventReplayManager()
    ingress = TranslationReplayIngress(manager, tmp_path)
    return manager, ingress, read_ingress_token(tmp_path)


def test_token_private_and_wrong_or_missing_token_cannot_mint_ticket(tmp_path):
    manager, ingress, token = make_ingress(tmp_path)
    assert token and len(token) == 64
    p = tmp_path / "translation_replay_token"
    assert p.stat().st_mode & 0o777 == 0o600
    assert ingress.begin({}) == {"status": "rejected"}
    assert ingress.begin({"translation_token": "bad"}) == {"status": "rejected"}
    assert len(manager._translation_tickets) == 0


def test_epoch_anchor_and_commit_are_purpose_bound(tmp_path):
    manager, ingress, token = make_ingress(tmp_path)
    auth = {"translation_token": token}
    opened = ingress.begin(auth)
    assert opened["status"] == "ready" and "ticket" not in opened
    window = ingress.begin({**auth, "epoch": opened["epoch"]})
    data = {"text": "фраза", "translation": None, "start_ts": 0, "end_ts": 3, "language_detected": "ru"}
    assert ingress.commit({"ticket": window["ticket"], "payload": data})["status"] == "rejected"
    assert ingress.commit({**auth, "ticket": window["ticket"], "payload": data})["status"] == "accepted"
    manager.clear()
    assert ingress.begin({**auth, "epoch": opened["epoch"]})["status"] == "stale"


def test_token_read_refuses_symlink_or_broad_permissions(tmp_path):
    from backend.translation_replay_ingress import read_ingress_token
    target = tmp_path / "target"
    target.write_text("a" * 64)
    p = tmp_path / "translation_replay_token"
    p.symlink_to(target)
    assert read_ingress_token(tmp_path) is None
    p.unlink()
    p.write_text("a" * 64)
    p.chmod(0o644)
    assert read_ingress_token(tmp_path) is None


def test_actual_dispatcher_never_audits_ingress_secret_or_phrase(tmp_path):
    from backend.service import BackendService
    from backend.ipc_throttle import IPCThrottle
    manager, ingress, token = make_ingress(tmp_path)
    service = BackendService.__new__(BackendService)
    service._settings_svc = SimpleNamespace(cached_settings=lambda: {})
    service._request_signer = None
    service._ipc_throttle = IPCThrottle()
    service._audit_logger = Mock()
    service._dispatch_table = {"translation_replay_begin": ingress.begin,
                               "translation_replay_commit": ingress.commit}
    def rpc(method, params):
        return service.handle_request({"id": "synthetic", "method": method, "params": params})
    auth = {"translation_token": token}
    assert rpc("translation_replay_begin", {})["result"]["status"] == "rejected"
    epoch = rpc("translation_replay_begin", auth)["result"]["epoch"]
    ticket = rpc("translation_replay_begin", {**auth, "epoch": epoch})["result"]["ticket"]
    phrase = {"text": "private phrase", "translation": None, "start_ts": 0, "end_ts": 3, "language_detected": None}
    assert rpc("translation_replay_commit", {**auth, "ticket": ticket, "payload": phrase})["result"]["status"] == "accepted"
    service._audit_logger.log_request.assert_not_called()


def test_token_read_refuses_directory_and_invalid_content(tmp_path):
    from backend.translation_replay_ingress import read_ingress_token
    p = tmp_path / "translation_replay_token"
    p.mkdir(mode=0o700)
    assert read_ingress_token(tmp_path) is None
    p.rmdir()
    p.write_text("not a token")
    p.chmod(0o600)
    assert read_ingress_token(tmp_path) is None


def test_malformed_auth_and_handler_failure_never_reflect_values(tmp_path, caplog):
    manager, ingress, token = make_ingress(tmp_path)
    auth = {"translation_token": token}
    for malformed in (None, [], "sensitive-marker", {"translation_token": []}):
        assert ingress.begin(malformed)["status"] == "rejected"
    manager.translation_epoch = Mock(side_effect=RuntimeError("synthetic-private-phrase " + token))
    assert ingress.begin(auth)["status"] == "error"
    assert token not in caplog.text and "synthetic-private-phrase" not in caplog.text


def test_translation_secret_redacted_in_service_and_disconnect_error_context():
    from backend.plaintext_export_redaction import safe_error, safe_request_id
    token = "synthetic-token-marker"
    request = {"id": token, "method": "translation_replay_commit", "params": {"translation_token": token}}
    message, trace = safe_error(RuntimeError(token), context=request, with_traceback=True)
    assert token not in message + trace
    assert safe_request_id(request) != token
