"""Card B: прямые файловые менеджеры и частичная авторизация на tmp-профиле."""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.obsidian_sync import ObsidianSyncManager
from backend.plaintext_export_authorization import PlaintextExportAuthorizer
from backend.sharing_manager import SharingManager, SharePackage
from backend.state_store import StateStore


def policy(tmp_path, encryption=False, privacy=False):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "settings.json").write_text(json.dumps({
        "history_encryption_enabled": encryption, "privacy_mode_enabled": privacy,
        "_plaintext_export_policy_revision": uuid.uuid4().hex,
    }))
    store = StateStore(profile)
    store.read_plaintext_policy_snapshot()  # Создать lock-файл fixture до снимка filesystem.
    auth = PlaintextExportAuthorizer(read_snapshot=store.read_plaintext_policy_snapshot,
                                     profile_identity=str(profile))
    return store, auth


def context(auth):
    sid = str(uuid.uuid4())
    grant = auth.issue_grant(sid)
    assert grant.ok
    return {"app_session_id": sid, "epoch": auth.epoch.hex(),
            "capability": grant.capability,
            "expected_policy_generation": grant.policy_generation}


def items():
    return [{"id": "first", "ts": "2026-01-01T01:00:00+00:00", "text": "synthetic first"},
            {"id": "second", "ts": "2026-01-01T02:00:00+00:00", "text": "synthetic second"}]


def obsidian(tmp_path, auth=None):
    manager = ObsidianSyncManager(plaintext_export_authorizer=auth)
    manager._vault_path = tmp_path / "vault"
    return manager


def sharing(tmp_path, auth=None):
    store = SimpleNamespace(data_dir=tmp_path / "sharing", get_history_item_by_id=lambda _id: items()[0])
    return SharingManager(store, plaintext_export_authorizer=auth)


@pytest.mark.parametrize("kind", ["obsidian", "sharing", "persist"])
@pytest.mark.parametrize("mode", ["missing", "on", "privacy", "unknown", "raising"])
def test_direct_manager_denies_before_any_write(tmp_path, kind, mode):
    store, auth = policy(tmp_path, mode == "on", mode == "privacy")
    if mode == "missing":
        auth = None
    elif mode == "unknown":
        (store.data_dir / "settings.json").write_text("bad")
    elif mode == "raising":
        auth = SimpleNamespace(precheck_backend_export=lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic")))
    manager = obsidian(tmp_path, auth) if kind == "obsidian" else sharing(tmp_path, auth)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    with pytest.raises(Exception, match="plaintext_|privacy_mode"):
        if kind == "obsidian":
            manager.sync(items(), force=True)
        elif kind == "sharing":
            manager.prepare_share(["first"])
        else:
            manager._persist_package(SharePackage("id", "synthetic", "file.md", 9,
                                                  datetime.now(timezone.utc).isoformat()))
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before
    assert not (tmp_path / "vault").exists()
    assert not (tmp_path / "sharing").exists()


def test_obsidian_revoke_between_files_preserves_retry_cursor(tmp_path):
    _, auth = policy(tmp_path, True)
    ctx = context(auth)
    manager = obsidian(tmp_path, auth)
    original = Path.write_text

    def revoke_after_first(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.suffix == ".md":
            auth.revoke(ctx["app_session_id"], auth.epoch, ctx["capability"])
        return result

    with patch.object(Path, "write_text", revoke_after_first):
        result = manager.sync(items(), plaintext_export=ctx)
    assert result.synced_count == 1
    assert result.errors
    assert result.to_dict()["partial"] is True
    assert len(list((tmp_path / "vault").rglob("*.md"))) == 1
    assert manager._last_sync_ts is None
    retry = manager.sync(items(), plaintext_export=context(auth))
    assert retry.synced_count == 2
    assert len(list((tmp_path / "vault").rglob("*.md"))) == 2


def test_share_revoke_after_package_does_not_write_index_or_rollback(tmp_path):
    _, auth = policy(tmp_path, True)
    ctx = context(auth)
    manager = sharing(tmp_path, auth)
    original = manager._save_index

    def revoke_before_index(*args, **kwargs):
        auth.revoke(ctx["app_session_id"], auth.epoch, ctx["capability"])
        return original(*args, **kwargs)

    with patch.object(manager, "_save_index", revoke_before_index):
        with pytest.raises(Exception) as error:
            manager.prepare_share(["first"], plaintext_export=ctx)
    assert getattr(error.value, "partial", False)
    assert len(list(manager._shares_dir.glob("*.md"))) == 1
    assert not manager._index_path.exists()
    assert not manager._index_path.with_suffix(".tmp").exists()


def test_off_managers_export_without_context(tmp_path):
    _, auth = policy(tmp_path)
    sync = obsidian(tmp_path, auth).sync(items())
    assert sync.synced_count == 2
    manager = sharing(tmp_path, auth)
    package = manager.prepare_share(["first"])
    assert (manager._shares_dir / package.filename).read_text() == package.content
    assert json.loads(manager._index_path.read_text())[package.share_id]["content"] == package.content


def test_sharing_constructor_and_list_do_not_prune_under_on(tmp_path):
    _, auth = policy(tmp_path, True)
    directory = tmp_path / "sharing" / "shares"
    directory.mkdir(parents=True)
    package = SharePackage("expired", "synthetic", "old.md", 9, "2026", expires_at=1)
    payload = directory / package.filename
    payload.write_text(package.content)
    index = directory / "shares_index.json"
    raw = json.dumps({package.share_id: package.to_dict()})
    index.write_text(raw)
    manager = sharing(tmp_path, auth)
    assert manager.list_shared() == []
    assert payload.read_text() == package.content
    assert index.read_text() == raw


def test_sharing_revoke_on_without_context_preserves_files(tmp_path):
    _, auth = policy(tmp_path, True)
    manager = sharing(tmp_path, auth)
    package = manager.prepare_share(["first"], plaintext_export=context(auth))
    raw = manager._index_path.read_bytes()
    with pytest.raises(Exception, match="plaintext_confirmation_required"):
        manager.revoke_share(package.share_id)
    assert manager._index_path.read_bytes() == raw
    assert (manager._shares_dir / package.filename).exists()


def test_off_share_index_write_failure_is_not_success(tmp_path):
    _, auth = policy(tmp_path)
    manager = sharing(tmp_path, auth)
    with patch.object(Path, "replace", side_effect=OSError("synthetic disk failure")):
        with pytest.raises(Exception) as error:
            manager.prepare_share(["first"])
    assert getattr(error.value, "partial", False)
    assert len(list(manager._shares_dir.glob("*.md"))) == 1


@pytest.mark.parametrize("kind", ["obsidian", "sharing"])
def test_off_supplied_null_namespace_is_denied(tmp_path, kind):
    _, auth = policy(tmp_path)
    manager = obsidian(tmp_path, auth) if kind == "obsidian" else sharing(tmp_path, auth)
    params = {"plaintext_export": None, "items": items(), "item_ids": ["first"]}
    response = manager.handle_sync(params) if kind == "obsidian" else manager.handle_prepare_share(params)
    assert response["reason"] == "plaintext_session_expired"
    assert not (tmp_path / "vault").exists()
    assert not (tmp_path / "sharing").exists()


@pytest.mark.parametrize("kind", ["obsidian", "sharing"])
@pytest.mark.parametrize("invalid", ["missing", "capability", "session", "epoch", "generation", "sink_kind"])
def test_manager_confirmation_and_force_do_not_bypass_bad_context(tmp_path, kind, invalid):
    _, auth = policy(tmp_path, True)
    ctx = context(auth)
    if invalid == "missing":
        ctx = {}
    elif invalid == "capability":
        ctx["capability"] = "invalid"
    elif invalid == "session":
        ctx["app_session_id"] = str(uuid.uuid4())
    elif invalid == "epoch":
        ctx["epoch"] = "00" * 32
    elif invalid == "generation":
        ctx["expected_policy_generation"] += 1
    else:
        ctx["sink_kind"] = "pretend_allowed"
    manager = obsidian(tmp_path, auth) if kind == "obsidian" else sharing(tmp_path, auth)
    params = {"plaintext_export": ctx, "items": items(), "item_ids": ["first"],
              "force": True, "confirm": True, "save_to_file": True}
    response = manager.handle_sync(params) if kind == "obsidian" else manager.handle_prepare_share(params)
    assert response["ok"] is False
    assert response["reason"] == "plaintext_session_expired"
    assert not (tmp_path / "vault").exists()
    assert not (tmp_path / "sharing").exists()
