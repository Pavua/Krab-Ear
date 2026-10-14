"""Защита ключа и activation: реальные AES/Store, только CLI provider — stub."""
import base64
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from backend.history_crypto import HistoryCrypto
from backend.history_service import HistoryService
from backend.plaintext_export_authorization import MAX_POLICY_BYTES
from backend.settings_service import SettingsService
from backend.state_store import (
    HISTORY_JOURNAL_FILENAMES, HistoryEncryptionUnavailable, StateStore,
    StateStoreSettingsConflictError, StateStoreSettingsCorruptError,
)


class Provider:
    """Missing44 и УСПЕШНЫЙ add позволяют поймать ошибочную замену ключа."""
    def __init__(self):
        self.key = None
        self.calls = []
        self.read_failure = False
        self.add_failure = False
        self.after_copy = False

    def __call__(self, args):
        self.calls.append(args[0])
        if args[0] == "find-generic-password":
            if self.read_failure or self.after_copy:
                return subprocess.CompletedProcess(args, 36, "", "synthetic denied")
            if self.key is None:
                return subprocess.CompletedProcess(args, 44, "", "synthetic missing")
            return subprocess.CompletedProcess(args, 0, base64.b64encode(self.key).decode(), "")
        if args[0] == "add-generic-password":
            if self.add_failure:
                return subprocess.CompletedProcess(args, 36, "", "synthetic denied")
            self.key = base64.b64decode(args[args.index("-w") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError("unexpected provider operation")


@pytest.fixture
def profile(tmp_path):
    provider = Provider()
    with patch("backend.crypto_keystore._run_security", side_effect=provider):
        store = StateStore(tmp_path / "profile")
        store.initialize_startup_plaintext_policy(new_profile=True)
        yield store, provider


def raw_policy(store, enabled):
    settings = store.load_settings()
    settings["history_encryption_enabled"] = enabled
    store.settings_path.write_text(json.dumps(settings))


def snapshot(store):
    return {path.name: path.read_bytes() for path in store.data_dir.iterdir()
            if path.is_file() and (path.name.startswith("history") or path.name == "settings.json")}


def settings_service(store):
    service = SettingsService(store, backup=Mock())
    service._reload_and_fire_hooks = Mock()
    service._maybe_disable_sentry_for_privacy = Mock()
    return service


def backend_object(store):
    from backend.service import BackendService
    service = object.__new__(BackendService)
    service.store = store
    service._settings_svc = settings_service(store)
    return service


@pytest.mark.parametrize("journal", HISTORY_JOURNAL_FILENAMES)
def test_append_never_creates_replacement_beside_any_enc1(profile, journal):
    store, provider = profile
    old_key = b"O" * 32
    raw_policy(store, True)
    (store.data_dir / journal).write_text(HistoryCrypto(old_key).encrypt_line('{"id":"old"}') + "\n")
    before = snapshot(store)
    with pytest.raises(HistoryEncryptionUnavailable):
        store.add_history_item(text="synthetic new")
    assert "add-generic-password" not in provider.calls
    assert snapshot(store) == before
    provider.key = old_key
    item = store.add_history_item(text="synthetic new")
    assert item.id
    assert "add-generic-password" not in provider.calls
    lines = store.history_path.read_text().splitlines()
    assert json.loads(HistoryCrypto(old_key).decrypt_line(lines[-1]))["id"] == item.id


@pytest.mark.parametrize("kind", ["invalid_utf8", "directory", "unreadable"])
def test_unproved_journal_readability_forbids_creation(profile, kind):
    store, provider = profile
    raw_policy(store, True)
    target = store.tags_path
    if kind == "invalid_utf8":
        target.write_bytes(b"\xff")
    elif kind == "directory":
        target.unlink()
        target.mkdir()
    original = Path.open

    def fail_read(path, *args, **kwargs):
        if kind == "unreadable" and path == target:
            raise PermissionError("synthetic")
        return original(path, *args, **kwargs)

    with patch.object(Path, "open", fail_read), pytest.raises(HistoryEncryptionUnavailable):
        store.add_history_item(text="synthetic")
    assert "add-generic-password" not in provider.calls


@pytest.mark.parametrize("route", ["dedicated", "migration", "direct_migration"])
def test_dedicated_and_migration_do_not_create_beside_enc1(profile, route):
    store, provider = profile
    raw_policy(store, route != "dedicated")
    store.history_path.write_text(HistoryCrypto(b"O" * 32).encrypt_line('{"id":"old"}') + "\n")
    before = snapshot(store)
    if route == "direct_migration":
        result = store.migrate_history_encryption()
    else:
        obj = backend_object(store)
        with patch("threading.Thread", return_value=SimpleNamespace(start=lambda: None)):
            result = (obj._handle_set_history_encryption({"enabled": True}) if route == "dedicated"
                      else obj._handle_migrate_history_encryption({}))
    assert result["ok"] is False
    assert "add-generic-password" not in provider.calls
    assert snapshot(store) == before


@pytest.mark.parametrize("route", ["direct", "set", "import", "restore"])
@pytest.mark.parametrize("failure", ["read", "add"])
def test_activation_failure_preserves_settings_revision_and_hooks(profile, tmp_path, route, failure):
    store, provider = profile
    service = settings_service(store)
    provider.read_failure = failure == "read"
    provider.add_failure = failure == "add"
    before = store.settings_path.read_bytes()
    incoming = {"history_encryption_enabled": True, "privacy_mode_enabled": False}
    with pytest.raises(HistoryEncryptionUnavailable):
        if route == "direct":
            store.save_settings(incoming)
        elif route == "set":
            service.handle_set_settings(incoming)
        elif route == "import":
            source = tmp_path / "settings-import.json"
            source.write_text(json.dumps(incoming))
            service.handle_import_settings({"file": str(source)})
        else:
            service._backup.restore_backup.return_value = incoming
            service.handle_restore_settings_backup({"backup_id": "synthetic"})
    assert store.settings_path.read_bytes() == before
    service._reload_and_fire_hooks.assert_not_called()
    service._maybe_disable_sentry_for_privacy.assert_not_called()


def test_warm_crypto_does_not_mask_activation_provider_failure(profile):
    store, provider = profile
    store._history_crypto_initialized = True
    store._history_crypto_instance = HistoryCrypto(b"C" * 32)
    provider.read_failure = True
    before = store.settings_path.read_bytes()
    with pytest.raises(HistoryEncryptionUnavailable):
        store.save_settings({"history_encryption_enabled": True})
    assert provider.calls == ["find-generic-password"]
    assert store.settings_path.read_bytes() == before


@pytest.mark.parametrize("old_policy", [None, "{broken"])
def test_failed_repair_write_does_not_publish_fresh_crypto(profile, old_policy):
    store, provider = profile
    if old_policy is None:
        store.settings_path.unlink()
    else:
        store.settings_path.write_text(old_policy)
    previous = (store._history_crypto_initialized, store._history_crypto_instance)
    with patch("core.atomic_io.atomic_write_text", side_effect=OSError("synthetic before replace")):
        with pytest.raises(OSError, match="before replace"):
            store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False},
                                validated_repair=True)
    assert provider.calls == ["find-generic-password", "add-generic-password"]
    assert (store._history_crypto_initialized, store._history_crypto_instance) == previous
    if old_policy is None:
        assert not store.settings_path.exists()
        store.add_history_item(text="synthetic after failed activation")
        assert not store.history_path.read_text().startswith("ENC1:")
        store.save_settings({"history_encryption_enabled": False, "privacy_mode_enabled": False},
                            validated_repair=True)
    else:
        assert store.settings_path.read_text() == old_policy


def test_successful_activation_replaces_stale_warm_crypto(profile):
    store, provider = profile
    old_crypto = HistoryCrypto(b"O" * 32)
    store._history_crypto_initialized = True
    store._history_crypto_instance = old_crypto
    provider.key = b"N" * 32
    store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False})
    assert store._history_crypto_instance is not old_crypto
    item = store.add_history_item(text="synthetic fresh key")
    assert json.loads(HistoryCrypto(provider.key).decrypt_line(
        store.history_path.read_text().splitlines()[-1]))["id"] == item.id
    assert provider.calls == ["find-generic-password"]


def test_failed_missing_policy_activation_cannot_encrypt_or_block_off_repair(profile):
    store, _provider = profile
    store.settings_path.unlink()
    with patch("core.atomic_io.atomic_write_text", side_effect=OSError("synthetic before replace")):
        with pytest.raises(OSError):
            store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False},
                                validated_repair=True)
    store.add_history_item(text="synthetic after failed activation")
    encrypted = store.history_path.read_text().startswith("ENC1:")
    repair_error = None
    try:
        store.save_settings({"history_encryption_enabled": False, "privacy_mode_enabled": False},
                            validated_repair=True)
    except StateStoreSettingsCorruptError as exc:
        repair_error = str(exc)
    assert (encrypted, repair_error) == (False, None)


def test_directory_fsync_failure_after_replace_keeps_committed_crypto(profile):
    import os
    import stat
    store, provider = profile
    store._history_crypto_initialized = True
    store._history_crypto_instance = HistoryCrypto(b"O" * 32)
    provider.key = b"N" * 32
    original = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic directory fsync")
        return original(fd)

    with patch("os.fsync", side_effect=fail_directory), pytest.raises(OSError, match="directory fsync"):
        store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False})
    assert json.loads(store.settings_path.read_text())["history_encryption_enabled"] is True
    item = store.add_history_item(text="synthetic committed key")
    assert json.loads(HistoryCrypto(provider.key).decrypt_line(
        store.history_path.read_text().splitlines()[-1]))["id"] == item.id
    assert provider.calls == ["find-generic-password"]


@pytest.mark.parametrize("raw", ['{broken', '{}', '{"history_encryption_enabled":"true"}'])
def test_explicit_repair_to_on_also_requires_fresh_provider(profile, raw):
    store, provider = profile
    store.settings_path.write_text(raw)
    provider.read_failure = True
    with pytest.raises(HistoryEncryptionUnavailable):
        store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False},
                            validated_repair=True)
    assert store.settings_path.read_text() == raw
    assert provider.calls == ["find-generic-password"]


def test_stale_cas_and_invalid_payload_do_not_touch_provider(profile):
    store, provider = profile
    with pytest.raises(StateStoreSettingsConflictError):
        store.save_settings({"history_encryption_enabled": True}, expected_revision="stale")
    with pytest.raises(StateStoreSettingsCorruptError):
        store.save_settings({"history_encryption_enabled": "false"})
    with pytest.raises(StateStoreSettingsCorruptError):
        store.save_settings({"history_encryption_enabled": True, "oversize": "x" * (MAX_POLICY_BYTES + 1)})
    assert provider.calls == []


def test_unrelated_already_on_save_needs_no_provider(profile):
    store, provider = profile
    raw_policy(store, True)
    provider.read_failure = True
    store.save_settings({"overlay_opacity_percent": 50})
    assert store.load_settings()["history_encryption_enabled"] is True
    assert provider.calls == []


def test_repeated_dedicated_on_rechecks_provider(profile):
    store, provider = profile
    raw_policy(store, True)
    store._history_crypto_initialized = True
    store._history_crypto_instance = HistoryCrypto(b"C" * 32)
    provider.read_failure = True
    before = store.settings_path.read_bytes()
    result = backend_object(store)._handle_set_history_encryption({"enabled": True})
    assert result["ok"] is False
    assert provider.calls == ["find-generic-password"]
    assert store.settings_path.read_bytes() == before


@pytest.mark.parametrize("enabled", ["false", 1, None, []])
def test_dedicated_non_bool_refused_before_provider(profile, enabled):
    store, provider = profile
    before = store.settings_path.read_bytes()
    with pytest.raises(ValueError):
        backend_object(store)._handle_set_history_encryption({"enabled": enabled})
    assert provider.calls == []
    assert store.settings_path.read_bytes() == before


def legacy_backup(store):
    backup = store.data_dir / "backups" / "backup_synthetic"
    backup.mkdir(parents=True)
    (backup / "history.ndjson").write_text('{"id":"restored","ts":"2026-01-01T00:00:00Z","text":"synthetic"}\n')
    (backup / "settings.json").write_text(json.dumps({
        "history_encryption_enabled": True, "privacy_mode_enabled": False,
    }))
    return backup


def test_legacy_restore_refuses_before_copy_on_provider_failure(profile):
    store, provider = profile
    backup = legacy_backup(store)
    provider.read_failure = True
    before = snapshot(store)
    with patch("shutil.copy2", wraps=__import__("shutil").copy2) as copy:
        with pytest.raises(HistoryEncryptionUnavailable):
            HistoryService(store).handle_restore_history({"backup_path": str(backup), "restore_settings": True})
        copy.assert_not_called()
    assert snapshot(store) == before


def test_legacy_restore_uses_one_prepared_commit_before_copy(profile):
    store, provider = profile
    provider.key = b"K" * 32
    backup = legacy_backup(store)
    import shutil
    original = shutil.copy2

    def copy_once(*args, **kwargs):
        provider.after_copy = True
        return original(*args, **kwargs)

    with patch.object(shutil, "copy2", copy_once):
        result = HistoryService(store).handle_restore_history({"backup_path": str(backup), "restore_settings": True})
    assert result["restored_entries"] == 1
    assert provider.calls == ["find-generic-password"]
    assert store.load_settings()["history_encryption_enabled"] is True


@pytest.mark.parametrize("where", ["live", "prospective"])
def test_legacy_restore_key_guard_uses_union_of_live_and_future_journals(profile, where):
    store, provider = profile
    backup = legacy_backup(store)
    target = store.history_path if where == "live" else backup / "history.ndjson"
    target.write_text(HistoryCrypto(b"O" * 32).encrypt_line('{"id":"old"}') + "\n")
    before = snapshot(store)
    with pytest.raises(HistoryEncryptionUnavailable):
        HistoryService(store).handle_restore_history({"backup_path": str(backup), "restore_settings": True})
    assert "add-generic-password" not in provider.calls
    assert snapshot(store) == before


def test_fresh_activation_and_plaintext_migration_keep_creation(profile):
    store, provider = profile
    store.add_history_item(text="synthetic")
    store.save_settings({"history_encryption_enabled": True})
    assert provider.calls.count("add-generic-password") == 1
    assert store.migrate_history_encryption()["ok"] is True
    assert store.history_path.read_text().startswith("ENC1:")


@pytest.mark.parametrize("route", ["activation", "append", "migration"])
def test_creating_provider_is_inside_the_existing_exclusive_lock(profile, route):
    import threading
    store, provider = profile
    modes = []

    def observe(args):
        modes.append(store._lock_mode.get(threading.get_ident()))
        return provider(args)

    if route != "activation":
        raw_policy(store, True)
    if route == "migration":
        store.history_path.write_text('{"id":"synthetic"}\n')
    with patch("backend.crypto_keystore._run_security", side_effect=observe):
        if route == "activation":
            store.save_settings({"history_encryption_enabled": True})
        elif route == "append":
            store.add_history_item(text="synthetic")
        else:
            assert store.migrate_history_encryption()["ok"] is True
    assert modes and all(mode is False for mode in modes)
