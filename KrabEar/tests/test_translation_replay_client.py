"""Неопределённый ACK, фиксированный epoch и repair без второго outbox."""
from unittest.mock import Mock


def client_with_rpc(tmp_path, rpc):
    from backend.translation_replay_client import TranslationReplayClient
    token = tmp_path / "translation_replay_token"
    token.write_text("a" * 64)
    token.chmod(0o600)
    return TranslationReplayClient(tmp_path / "unused.sock", tmp_path, rpc=rpc)


def test_open_anchor_does_not_mint_and_is_never_refreshed(tmp_path):
    rpc = Mock(side_effect=[{"status": "ready", "epoch": "old"}, {"status": "stale"}, {"status": "stale"}])
    c = client_with_rpc(tmp_path, rpc)
    assert c.open()["status"] == "ready"
    assert c.begin_window()["status"] == "stale"
    assert c.begin_window()["status"] == "stale"
    assert [call.args[1].get("epoch") for call in rpc.call_args_list] == [None, "old", "old"]


def test_commit_lost_ack_retries_same_ticket_and_payload(tmp_path):
    rpc = Mock(side_effect=[{"status": "ready", "epoch": "e"},
                            {"status": "ready", "ticket": "b" * 32},
                            TimeoutError(), {"status": "duplicate", "seq": 7}])
    c = client_with_rpc(tmp_path, rpc)
    c.open()
    ticket = c.begin_window()
    data = {"text": "phrase"}
    assert c.commit_window(ticket, data)["status"] == "duplicate"
    assert rpc.call_args_list[-1].args[1] == rpc.call_args_list[-2].args[1]


def test_known_epoch_failure_is_unknown_and_initial_unavailable_stays_legacy(tmp_path):
    rpc = Mock(side_effect=[{"status": "ready", "epoch": "e"}, TimeoutError()])
    c = client_with_rpc(tmp_path, rpc)
    c.open()
    assert c.begin_window()["status"] == "unknown"
    rpc = Mock(side_effect=OSError())
    c = client_with_rpc(tmp_path, rpc)
    assert c.open()["status"] == "unavailable"
    assert c.begin_window()["status"] == "unavailable"
    assert rpc.call_count == 1


def test_commit_unknown_after_both_attempts_never_falls_back(tmp_path):
    rpc = Mock(side_effect=[{"status": "ready", "epoch": "e"},
                            {"status": "ready", "ticket": "b" * 32},
                            TimeoutError(), TimeoutError()])
    c = client_with_rpc(tmp_path, rpc)
    c.open()
    t = c.begin_window()
    assert c.commit_window(t, {"text": "phrase"})["status"] == "unknown"
    assert rpc.call_count == 4


def test_missing_token_before_any_anchor_is_explicit_unavailable(tmp_path):
    from backend.translation_replay_client import TranslationReplayClient
    c = TranslationReplayClient(tmp_path / "unused.sock", tmp_path)
    assert c.open()["status"] == "unavailable"
    assert c.begin_window()["status"] == "unavailable"
