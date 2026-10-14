"""Настоящий Unix IPC → BackendService → Store, tmp profile и fake Engine."""
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from test_history_key_safety_20261014 import Provider, raw_policy, snapshot
from backend.history_crypto import HistoryCrypto
from backend.ipc_server import IPCServer
from backend.service import BackendService
from backend.socket_ownership import SocketOwnershipClaim, SocketOwnershipState
from backend.startup_diagnostics import StartupDiagnostics
from backend.state_store import StateStore


@contextmanager
def running_backend(tmp_path):
    provider = Provider()
    with patch("backend.crypto_keystore._run_security", side_effect=provider):
        store = StateStore(tmp_path / "profile")
        store.initialize_startup_plaintext_policy(new_profile=True)
        engine = SimpleNamespace(quality_profile="balanced", current_model="fixture-model",
                                 _llm_rewriter=None, _settings_get=None,
                                 _resolve_diarization_device=lambda: "cpu", warmup=lambda: None)
        report = SimpleNamespace(status="ready", errors=[], warnings=[], startup_time_ms=0, checks=[])
        with patch.object(threading.Thread, "start", return_value=None), \
                patch("backend.service.PurgeScheduler.start", return_value=None), \
                patch.object(BackendService, "_check_binary_drift_on_startup", return_value=None), \
                patch.object(StartupDiagnostics, "run_all_checks", return_value=report), \
                patch.object(StartupDiagnostics, "_start_lm_studio_background_check", return_value=None):
            service = BackendService(
                store=store, recorder=SimpleNamespace(is_recording=False, start=lambda: None, stop=lambda: b""),
                transcriber=SimpleNamespace(engine=engine, _error_bus=None), translator=Mock(),
            )
        try:
            service._settings_svc._backup = Mock()
            service._settings_svc._reload_and_fire_hooks = Mock()
            service._settings_svc._maybe_disable_sentry_for_privacy = Mock()
            # Короткий уникальный root работает и без runner; runner перенесёт
            # mkdtemp внутрь собственного изолированного TMP.
            with tempfile.TemporaryDirectory(prefix="eks-", dir="/tmp") as socket_root:
                claim = SocketOwnershipClaim(Path(socket_root) / "s")
                path = claim.socket_path
                server = IPCServer(path, service, ownership=claim)
                thread = threading.Thread(target=server.serve_forever, name="key-safety-test-ipc")

                def call(method, **params):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                        peer.settimeout(5)
                        peer.connect(str(path))
                        peer.sendall(json.dumps({"id": "synthetic", "method": method, "params": params}).encode() + b"\n")
                        with peer.makefile("rb") as reader:
                            return json.loads(reader.readline())

                call.socket_path = path
                try:
                    claim.acquire()
                    thread.start()
                    deadline = time.monotonic() + 5
                    while thread.is_alive() and time.monotonic() < deadline:
                        if claim.snapshot().state is SocketOwnershipState.LISTENING:
                            break
                        time.sleep(0.01)
                    assert thread.is_alive(), "owned listener thread stopped"
                    assert claim.snapshot().state is SocketOwnershipState.LISTENING
                    yield store, provider, service, call
                finally:
                    try:
                        stopped = server.stop(timeout_sec=3)
                    finally:
                        try:
                            if thread.ident is not None:
                                thread.join(timeout=3)
                        finally:
                            claim.release()
                    assert stopped
                    assert not thread.is_alive()
        finally:
            assert service.close() is not False


@pytest.fixture
def backend(tmp_path):
    with running_backend(tmp_path) as value:
        yield value


def test_two_live_fixture_profiles_have_isolated_owned_endpoints(tmp_path):
    with running_backend(tmp_path / "first") as first, running_backend(tmp_path / "second") as second:
        first_call, second_call = first[3], second[3]
        written = first_call("add_history_item", text="synthetic first profile only")
        assert written["ok"] is True
        first_page = first_call("get_history_page", limit=10)
        second_page = second_call("get_history_page", limit=10)
        assert first_page["ok"] and second_page["ok"]
        assert (len(first_page["result"]["items"]), len(second_page["result"]["items"])) == (1, 0)
        assert first_call.socket_path != second_call.socket_path
    assert not first_call.socket_path.parent.exists()
    assert not second_call.socket_path.parent.exists()


@pytest.mark.parametrize("route", ["set_settings", "set_history_encryption", "import_settings", "restore_settings_backup"])
@pytest.mark.parametrize("failure", ["read", "add"])
def test_activation_failure_over_real_socket_preserves_revision_and_hooks(backend, tmp_path, route, failure):
    store, provider, service, call = backend
    provider.read_failure = failure == "read"
    provider.add_failure = failure == "add"
    incoming = {"history_encryption_enabled": True, "privacy_mode_enabled": False}
    before = store.settings_path.read_bytes()
    params = incoming
    if route == "set_history_encryption":
        params = {"enabled": True}
    elif route == "import_settings":
        source = tmp_path / "settings-import.json"
        source.write_text(json.dumps(incoming))
        params = {"file": str(source)}
    elif route == "restore_settings_backup":
        service._settings_svc._backup.restore_backup.return_value = incoming
        params = {"backup_id": "synthetic"}
    result = call(route, **params)
    assert not result["ok"] or result["result"].get("ok") is False
    assert store.settings_path.read_bytes() == before
    assert provider.calls == (["find-generic-password"] if failure == "read"
                              else ["find-generic-password", "add-generic-password"])
    service._settings_svc._reload_and_fire_hooks.assert_not_called()
    service._settings_svc._maybe_disable_sentry_for_privacy.assert_not_called()


def test_append_missing_key_and_recovery_over_same_socket_backend(backend):
    store, provider, service, call = backend
    raw_policy(store, True)
    service._settings_svc.invalidate_cache()
    old = HistoryCrypto(b"O" * 32)
    store.history_path.write_text(old.encrypt_line(json.dumps({
        "id": "old", "ts": "2026-01-01T00:00:00Z", "text": "synthetic old",
    })) + "\n")
    before = snapshot(store)
    result = call("add_history_item", text="synthetic new")
    assert result["ok"] is False
    assert "add-generic-password" not in provider.calls
    assert snapshot(store) == before
    provider.key = b"O" * 32
    result = call("add_history_item", text="synthetic recovered")
    assert result["ok"] is True
    loaded = call("get_history_page", limit=10)
    assert loaded["ok"] is True
    assert len(loaded["result"]["items"]) == 2
    assert "add-generic-password" not in provider.calls


def test_migration_preflight_missing_key_over_real_socket(backend):
    store, provider, service, call = backend
    raw_policy(store, True)
    service._settings_svc.invalidate_cache()
    store.history_path.write_text(HistoryCrypto(b"O" * 32).encrypt_line('{"id":"old"}') + "\n")
    before = snapshot(store)
    result = call("migrate_history_encryption")
    assert result["ok"] is True
    assert result["result"] == {"ok": False, "status": "encryption_unavailable"}
    assert "add-generic-password" not in provider.calls
    assert snapshot(store) == before


def test_legacy_restore_failure_over_real_socket_precedes_history_copy(backend):
    from test_history_key_safety_20261014 import legacy_backup
    store, provider, service, call = backend
    backup = legacy_backup(store)
    provider.read_failure = True
    before = snapshot(store)
    result = call("restore_history", backup_path=str(backup), restore_settings=True)
    assert result["ok"] is False
    assert snapshot(store) == before
    assert provider.calls == ["find-generic-password"]


def test_fresh_profile_activation_write_and_read_over_real_socket(backend):
    store, provider, service, call = backend
    result = call("set_history_encryption", enabled=True)
    assert result["ok"] is True and result["result"]["ok"] is True
    item = call("add_history_item", text="synthetic positive")
    assert item["ok"] is True
    loaded = call("get_history_page", limit=10)
    assert loaded["result"]["items"][0]["id"] == item["result"]["id"]
    assert store.history_path.read_text().startswith("ENC1:")
    assert provider.calls.count("add-generic-password") == 1
