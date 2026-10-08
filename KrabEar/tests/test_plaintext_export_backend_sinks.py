"""Card B: настоящие временные policy-файлы, файловые callback и граница revoke."""

import json
import threading
import uuid
from pathlib import Path

import pytest

from backend.plaintext_export_authorization import PlaintextExportAuthorizer
from backend.state_store import StateStore


def seed_policy(directory, encryption=False, privacy=False):
    (directory / "settings.json").write_text(json.dumps({
        "history_encryption_enabled": encryption,
        "privacy_mode_enabled": privacy,
        "_plaintext_export_policy_revision": uuid.uuid4().hex,
    }), encoding="utf-8")


def test_backend_write_runs_one_file_without_swift_sequence(tmp_path):
    seed_policy(tmp_path)
    store = StateStore(tmp_path)
    auth = PlaintextExportAuthorizer(
        read_snapshot=store.read_plaintext_policy_snapshot,
        profile_identity=str(tmp_path),
    )
    from backend.plaintext_export_sinks import BackendSink, run_export_write
    target = tmp_path / "export.txt"
    result = run_export_write(auth, None, BackendSink.HISTORY, lambda: target.write_text("synthetic"))
    assert result == len("synthetic")
    assert target.read_text() == "synthetic"
    assert auth.active_receipt_count == 0


@pytest.fixture
def setup_auth(tmp_path):
    seed_policy(tmp_path, encryption=True)
    store = StateStore(tmp_path)
    auth = PlaintextExportAuthorizer(
        read_snapshot=store.read_plaintext_policy_snapshot,
        profile_identity=str(tmp_path),
    )
    session = str(uuid.uuid4())
    grant = auth.issue_grant(session)
    assert grant.ok
    context = {
        "app_session_id": session, "epoch": grant.epoch.hex(),
        "capability": grant.capability,
        "expected_policy_generation": grant.policy_generation,
    }
    yield auth, context, store, tmp_path
    auth.close()


def test_backend_writes_leave_swift_high_water_and_receipt_unchanged(setup_auth):
    from backend.plaintext_export_sinks import BackendSink, run_export_write
    auth, context, store, path = setup_auth
    session, epoch, capability, generation = (
        context["app_session_id"], bytes.fromhex(context["epoch"]),
        context["capability"], context["expected_policy_generation"],
    )
    before = auth.validate(session, epoch, capability, generation, 10, "history-md")
    assert before.ok
    for index in range(3):
        result = run_export_write(auth, context, BackendSink.HISTORY,
                                  lambda: (path / f"{index}.txt").write_text("test"))
        assert result == 4
    assert auth.active_receipt_count == 1
    assert auth.consume_receipt(before.receipt)
    assert auth.validate_for_write(session, epoch, capability, generation, 11, "history-md").ok
    replay = auth.validate_for_write(session, epoch, capability, generation, 11, "history-md")
    assert not replay.ok
    assert replay.reason == "plaintext_session_expired"


@pytest.mark.parametrize("change,want", [
    ("revoke", "plaintext_session_expired"),
    ("privacy", "privacy_mode_active"),
    ("unknown", "plaintext_policy_unavailable"),
    ("close", "plaintext_policy_unavailable"),
    ("revision", "plaintext_session_expired"),
])
def test_precheck_does_not_authorize_a_later_write(setup_auth, change, want):
    from backend.plaintext_export_sinks import BackendSink, PlaintextExportDenied, precheck_export, run_export_write
    auth, context, store, path = setup_auth
    assert precheck_export(auth, context, BackendSink.HISTORY) is None
    if change == "revoke":
        auth.revoke(context["app_session_id"], auth.epoch, context["capability"])
    elif change == "privacy":
        seed_policy(path, True, True)
    elif change == "unknown":
        (path / "settings.json").write_text("broken")
    elif change == "close":
        auth.close()
    else:
        seed_policy(path, True)
    target = path / "forbidden" / "result.txt"
    with pytest.raises(PlaintextExportDenied) as caught:
        run_export_write(auth, context, BackendSink.HISTORY,
                         lambda: target.parent.mkdir())
    assert caught.value.reason == want
    assert not target.parent.exists()


def test_revoke_after_authorization_allows_only_current_file(setup_auth):
    from backend.plaintext_export_sinks import BackendSink, PlaintextExportDenied, run_export_write
    auth, context, store, path = setup_auth

    def first_write():
        auth.revoke(context["app_session_id"], auth.epoch, context["capability"])
        (path / "first.txt").write_text("allowed")
        return "first-written"

    assert run_export_write(auth, context, BackendSink.HISTORY, first_write) == "first-written"
    with pytest.raises(PlaintextExportDenied):
        run_export_write(auth, context, BackendSink.HISTORY,
                         lambda: (path / "second.txt").write_text("denied"))
    assert (path / "first.txt").read_text() == "allowed"
    assert not (path / "second.txt").exists()


def test_callback_executes_outside_both_locks(setup_auth):
    from backend.plaintext_export_sinks import BackendSink, run_export_write
    auth, context, store, path = setup_auth
    acquired = threading.Event()

    def obtain_locks():
        with store._lock():
            auth.get_policy()
            acquired.set()

    def writer():
        worker = threading.Thread(target=obtain_locks, daemon=True)
        worker.start()
        worker.join(2)
        assert acquired.is_set(), "writer удерживает store или authorizer lock"
        (path / "unlocked.txt").write_text("done")

    run_export_write(auth, context, BackendSink.HISTORY, writer)
    assert (path / "unlocked.txt").read_text() == "done"


def test_callback_exception_is_original_and_never_retried(setup_auth):
    from backend.plaintext_export_sinks import BackendSink, run_export_write
    auth, context, store, path = setup_auth
    calls = []
    original = OSError("synthetic disk full")

    def writer():
        calls.append(1)
        raise original

    with pytest.raises(OSError) as caught:
        run_export_write(auth, context, BackendSink.HISTORY, writer)
    assert caught.value is original
    assert calls == [1]


@pytest.mark.parametrize("namespace", [None, {}, [], True, "token", {
    "app_session_id": str(uuid.uuid4()), "epoch": "0" * 64,
    "expected_policy_generation": 0, "capability": None,
}])
def test_off_absent_allowed_but_supplied_bad_namespace_denied(tmp_path, namespace):
    from backend.plaintext_export_sinks import BackendSink, PlaintextExportDenied, export_context, run_export_write
    seed_policy(tmp_path)
    store = StateStore(tmp_path)
    auth = PlaintextExportAuthorizer(read_snapshot=store.read_plaintext_policy_snapshot,
                                     profile_identity=str(tmp_path))
    run_export_write(auth, export_context({}), BackendSink.HISTORY,
                     lambda: (tmp_path / "allowed").write_text("ok"))
    with pytest.raises(PlaintextExportDenied) as caught:
        run_export_write(auth, export_context({"plaintext_export": namespace}), BackendSink.HISTORY,
                         lambda: (tmp_path / "denied").write_text("wrong"))
    assert caught.value.reason == "plaintext_session_expired"
    assert not (tmp_path / "denied").exists()


def test_off_explicit_canonical_context_is_allowed(tmp_path):
    from backend.plaintext_export_sinks import BackendSink, run_export_write
    seed_policy(tmp_path)
    store = StateStore(tmp_path)
    auth = PlaintextExportAuthorizer(read_snapshot=store.read_plaintext_policy_snapshot,
                                     profile_identity=str(tmp_path))
    status = auth.get_policy()
    context = {"app_session_id": str(uuid.uuid4()), "epoch": status["epoch"],
               "capability": None, "expected_policy_generation": status["policy_generation"]}
    run_export_write(auth, context, BackendSink.HISTORY, lambda: (tmp_path / "ok").write_text("ok"))
    assert (tmp_path / "ok").read_text() == "ok"


@pytest.mark.parametrize("key,value", [
    ("app_session_id", "not-uuid"), ("app_session_id", "A" * 36),
    ("epoch", "A" * 64), ("epoch", b"bad"),
    ("capability", True), ("capability", "wrong"),
    ("expected_policy_generation", True), ("expected_policy_generation", -1),
    ("operation_seq", 10),
])
def test_backend_context_strictness(setup_auth, key, value):
    from backend.plaintext_export_sinks import BackendSink, PlaintextExportDenied, run_export_write
    auth, context, store, path = setup_auth
    context = {**context, key: value}
    with pytest.raises(PlaintextExportDenied) as caught:
        run_export_write(auth, context, BackendSink.HISTORY, lambda: (path / "denied").write_text("bad"))
    assert caught.value.reason == "plaintext_session_expired"
    assert not (path / "denied").exists()


@pytest.mark.parametrize("privacy,want", [(True, "privacy_mode_active"), (False, "plaintext_policy_unavailable")])
def test_bad_context_still_revokes_when_policy_denies(setup_auth, privacy, want):
    from backend.plaintext_export_sinks import BackendSink, PlaintextExportDenied, precheck_export
    auth, context, store, path = setup_auth
    if privacy:
        seed_policy(path, True, True)
    else:
        (path / "settings.json").write_text("broken")
    with pytest.raises(PlaintextExportDenied) as caught:
        precheck_export(auth, {}, BackendSink.HISTORY)
    assert caught.value.reason == want
    assert auth.active_grant_count == 0
    seed_policy(path, True)
    auth.get_policy()
    with pytest.raises(PlaintextExportDenied):
        precheck_export(auth, context, BackendSink.HISTORY)


def test_provider_failure_revokes_and_hides_exception(setup_auth):
    from backend.plaintext_export_sinks import BackendSink, PlaintextExportDenied, run_export_write
    auth, context, store, path = setup_auth

    def broken():
        raise RuntimeError("sensitive synthetic token")

    auth._read_snapshot = broken
    with pytest.raises(PlaintextExportDenied) as caught:
        run_export_write(auth, context, BackendSink.HISTORY, lambda: (path / "bad").write_text("bad"))
    assert str(caught.value) == "plaintext_policy_unavailable"
    assert auth.active_grant_count == 0
    assert not (path / "bad").exists()


@pytest.mark.parametrize("bad_auth", [None, object()])
def test_missing_authorizer_never_defaults_to_off(tmp_path, bad_auth):
    from backend.plaintext_export_sinks import BackendSink, PlaintextExportDenied, run_export_write
    with pytest.raises(PlaintextExportDenied) as caught:
        run_export_write(bad_auth, None, BackendSink.HISTORY, lambda: (tmp_path / "bad").write_text("bad"))
    assert caught.value.reason == "plaintext_policy_unavailable"
    assert not (tmp_path / "bad").exists()


def test_raising_authorizer_is_safe_but_writer_exception_is_not_swallowed(tmp_path):
    from backend.plaintext_export_sinks import BackendSink, PlaintextExportDenied, run_export_write

    class BrokenAuthorizer:
        def precheck_backend_export(self, context, sink):
            raise ValueError("sensitive synthetic token")

    with pytest.raises(PlaintextExportDenied) as caught:
        run_export_write(BrokenAuthorizer(), None, BackendSink.HISTORY,
                         lambda: (tmp_path / "bad").write_text("bad"))
    assert str(caught.value) == "plaintext_policy_unavailable"
    assert not (tmp_path / "bad").exists()


def test_scheduler_never_borrows_a_grant(setup_auth):
    from backend.plaintext_export_sinks import BackendSink, PlaintextExportDenied, run_export_write
    auth, context, store, path = setup_auth
    with pytest.raises(PlaintextExportDenied) as caught:
        run_export_write(auth, context, BackendSink.SCHEDULER, lambda: (path / "bad").write_text("bad"))
    assert caught.value.reason == "plaintext_confirmation_required"
    assert auth.active_grant_count == 1
    assert not (path / "bad").exists()


def test_precheck_result_cannot_be_reused_as_authority(setup_auth):
    from backend.plaintext_export_sinks import BackendSink, PlaintextExportDenied, precheck_export, run_export_write
    auth, context, store, path = setup_auth
    decision = precheck_export(auth, context, BackendSink.HISTORY)
    with pytest.raises(PlaintextExportDenied) as caught:
        run_export_write(auth, decision, BackendSink.HISTORY, lambda: (path / "bad").write_text("bad"))
    assert caught.value.reason == "plaintext_confirmation_required"
    assert not (path / "bad").exists()


def test_sink_requires_server_enum_not_untrusted_string(setup_auth):
    from backend.plaintext_export_sinks import BackendSink, PlaintextExportDenied, run_export_write
    auth, context, store, path = setup_auth
    with pytest.raises(PlaintextExportDenied):
        run_export_write(auth, context, BackendSink.HISTORY.value, lambda: (path / "bad").write_text("bad"))
    assert not (path / "bad").exists()


@pytest.mark.parametrize("previous_on", [False, True])
def test_fresh_off_absent_context_survives_supported_revision_change(tmp_path, previous_on):
    from backend.plaintext_export_sinks import BackendSink, run_export_write
    seed_policy(tmp_path, encryption=previous_on)
    store = StateStore(tmp_path)
    auth = PlaintextExportAuthorizer(
        read_snapshot=store.read_plaintext_policy_snapshot, profile_identity=str(tmp_path),
    )
    try:
        auth.get_policy()
        if previous_on:
            assert auth.issue_grant(str(uuid.uuid4())).ok
        store.save_settings({"history_encryption_enabled": False, "privacy_mode_enabled": False})
        target = tmp_path / "allowed-off.txt"
        run_export_write(auth, None, BackendSink.HISTORY, lambda: target.write_text("fresh OFF"))
        assert target.read_text() == "fresh OFF"
        assert auth.active_grant_count == 0
        assert auth.active_receipt_count == 0
    finally:
        auth.close()


def test_manual_grant_does_not_open_legacy_backup_archive_or_version_writers(setup_auth):
    from backend.archive_manager import ArchiveManager
    from backend.history_service import HistoryService
    from backend.history_encryption_policy import HistoryEncryptionOperationUnavailable
    from backend.transcript_versioning import TranscriptVersionManager
    auth, context, store, path = setup_auth
    assert auth.active_grant_count == 1
    result = HistoryService(store=store, plaintext_export_authorizer=auth).handle_backup_history(
        {"plaintext_export": context, "confirm": True, "force": True},
    )
    assert result["ok"] is False
    assert not (path / "backups").exists()
    archive = ArchiveManager(store=store)
    assert archive.handle_archive_items({"item_ids": ["synthetic"], "plaintext_export": context})["ok"] is False
    assert not (path / "archive").exists()
    with pytest.raises(HistoryEncryptionOperationUnavailable):
        TranscriptVersionManager(data_dir=path).save_version("synthetic", "text")
    assert not (path / "transcript_versions.ndjson").exists()
    assert auth.active_grant_count == 1


@pytest.mark.parametrize("require_auto_save", [False, True])
def test_manual_grant_does_not_open_recording_or_import_markdown_gate(setup_auth, require_auto_save):
    from backend.recording_core_service import RecordingCoreService
    auth, context, store, path = setup_auth
    settings = {**store.load_settings(), "auto_save_transcripts": True, "plaintext_export": context}
    assert not RecordingCoreService._should_write_plaintext_md(settings, False, require_auto_save=require_auto_save)
    assert auth.active_grant_count == 1


def test_manual_grant_keeps_auto_backup_encrypted(setup_auth):
    import os
    from backend.auto_backup import AutoBackupManager
    from backend.history_crypto import HistoryCrypto
    auth, context, store, path = setup_auth
    crypto = HistoryCrypto(os.urandom(32))
    store._get_history_crypto = lambda: crypto
    store.history_path.write_text(crypto.encrypt_line('{"id":"synthetic","text":"PRIVATE_TEST_MARKER"}') + "\n")
    original = store.history_path.read_bytes()
    result = AutoBackupManager(store=store, interval_hours=0).check_and_backup()
    assert result["backed_up"] is True
    assert Path(result["backup_path"]).name.startswith("auto_snapshot_")
    assert not [p for p in (path / "backups").glob("auto_backup_*") if p.is_dir()]
    assert all(b"PRIVATE_TEST_MARKER" not in p.read_bytes()
               for p in Path(result["backup_path"]).rglob("*") if p.is_file())
    assert store.history_path.read_bytes() == original
    assert auth.active_grant_count == 1
