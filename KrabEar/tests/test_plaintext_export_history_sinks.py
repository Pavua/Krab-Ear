"""A5.3 B: реальные history writers допускаются только свежим разрешением."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.history_service import HistoryService
from backend.plaintext_export_authorization import PlaintextExportAuthorizer
from backend.state_store import StateStore
from _history_crypto_test_helpers import save_settings_with_synthetic_key

ROUTES = (
    "history", "srt", "selected-md", "selected-srt", "json", "csv",
    "html", "obsidian", "batch-md", "batch-srt", "batch-csv", "batch-obsidian",
)


@pytest.fixture
def profile(tmp_path):
    store = StateStore(tmp_path / "profile")
    store.save_settings({"history_encryption_enabled": False, "privacy_mode_enabled": False})
    item = store.add_history_item(text="Синтетический текст A53", paste_status="ok")
    auth = PlaintextExportAuthorizer(
        read_snapshot=store.read_plaintext_policy_snapshot,
        profile_identity=store.read_plaintext_policy_snapshot().fingerprint.profile_identity,
    )
    svc = HistoryService(store, cached_settings=lambda: {"privacy_mode_enabled": False},
                         plaintext_export_authorizer=auth)
    yield store, svc, auth, item, tmp_path / "outputs"
    auth.close()


def policy(store, *, encryption=False, privacy=False):
    save_settings_with_synthetic_key(store, {"history_encryption_enabled": encryption, "privacy_mode_enabled": privacy})


def grant(auth):
    session = str(uuid.uuid4())
    result = auth.issue_grant(session)
    assert result.ok
    return {"app_session_id": session, "epoch": result.epoch.hex(),
            "capability": result.capability, "expected_policy_generation": result.policy_generation}


def call_route(profile, route, extra=None):
    store, svc, auth, item, output = profile
    params = {"save_to_file": True, "copy_to_clipboard": False, "confirm": True, "force": True}
    params.update(extra or {})
    if route.startswith("batch-"):
        fmt = {"md": "markdown"}.get(route[6:], route[6:])
        return svc.handle_batch_export({**params, "formats": [fmt], "output_dir": str(output)})
    if route.startswith("selected-"):
        fmt = "markdown" if route == "selected-md" else "srt"
        return svc.handle_export_selected_items({**params, "item_ids": [item.id], "format": fmt})
    handlers = {
        "history": svc.handle_export_history, "srt": svc.handle_export_history_srt,
        "json": svc.handle_export_history_json, "csv": svc.handle_export_history_csv,
        "html": svc.handle_export_html_report, "obsidian": svc.handle_export_obsidian,
    }
    return handlers[route]({**params, "id": item.id, "output_dir": str(output)})


def filesystem_snapshot(profile):
    root = profile[4].parent
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("mode,reason", [
    ("on", "plaintext_confirmation_required"),
    ("unknown", "plaintext_policy_unavailable"),
    ("privacy", "privacy_mode_active"),
    ("missing-authorizer", "plaintext_policy_unavailable"),
])
def test_denial_never_mutates_files(profile, route, mode, reason, monkeypatch):
    store, svc, auth, item, output = profile
    if mode == "on":
        policy(store, encryption=True)
    elif mode == "privacy":
        policy(store, privacy=True)
    elif mode == "unknown":
        store.settings_path.write_text("{broken", encoding="utf-8")
    else:
        svc._plaintext_export_authorizer = None
    before = filesystem_snapshot(profile)
    mutations = []
    for name in ("mkdir", "write_text"):
        original = getattr(Path, name)

        def observe(path, *args, _name=name, _original=original, **kwargs):
            mutations.append((_name, path))
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, observe)
    result = call_route(profile, route)
    assert mutations == []
    assert result.get("reason") == reason
    assert result.get("ok") is not True
    assert filesystem_snapshot(profile) == before


@pytest.mark.parametrize("route", ROUTES)
def test_off_and_valid_grant_preserve_successful_export(profile, route):
    result = call_route(profile, route)
    path = result.get("path") or result.get("file") or next(iter(result.get("files", {}).values()), None)
    assert path and Path(path).is_file()
    policy(profile[0], encryption=True)
    result = call_route(profile, route, {"plaintext_export": grant(profile[2])})
    assert "reason" not in result
    assert not result.get("errors")


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("context", [None, {}, {"operation_seq": 1}])
def test_off_rejects_supplied_malformed_namespace(profile, route, context):
    before = filesystem_snapshot(profile)
    result = call_route(profile, route, {"plaintext_export": context})
    assert result.get("reason") == "plaintext_session_expired"
    assert filesystem_snapshot(profile) == before


def test_batch_stops_after_revocation_without_rollback(profile, monkeypatch):
    store, svc, auth, item, output = profile
    policy(store, encryption=True)
    context = grant(auth)
    import os

    original = os.replace
    committed = []

    def replace_then_revoke(src, dst, *args, **kwargs):
        # G3-MEM-04 (M04-02): запись плоского файла — temp → fsync → os.replace через
        # dir_fd. Отзыв сессии делаем сразу после ПЕРВОЙ зафиксированной записи.
        result = original(src, dst, *args, **kwargs)
        if kwargs.get("dst_dir_fd") is not None and not str(dst).endswith(".tmp"):
            committed.append(str(dst))
            if len(committed) == 1:
                auth.revoke(context["app_session_id"], bytes.fromhex(context["epoch"]), context["capability"])
        return result

    monkeypatch.setattr(os, "replace", replace_then_revoke)
    result = svc.handle_batch_export({"formats": ["markdown", "srt", "csv", "obsidian"],
                                      "output_dir": str(output), "plaintext_export": context})
    assert result["partial"] is True
    assert result["reason"] == "plaintext_session_expired"
    assert list(result["files"]) == ["markdown"]
    assert list(result["errors"]) == ["srt"]
    written = list(output.rglob("*.*"))
    assert len(committed) == 1 and len(written) == 1 and written[0].is_file()
    assert written[0].name == committed[0]  # без отката: первый файл остался


def test_direct_srt_leaf_denies_without_touching_disk(profile):
    policy(profile[0], encryption=True)
    before = filesystem_snapshot(profile)
    result = profile[1]._finalize_srt_export({"save_to_file": True}, "synthetic", "item", 1, 1)
    assert result["reason"] == "plaintext_confirmation_required"
    assert filesystem_snapshot(profile) == before


def test_direct_csv_leaf_denies_without_touching_disk(profile):
    from backend.plaintext_export_sinks import PlaintextExportDenied
    policy(profile[0], encryption=True)
    before = filesystem_snapshot(profile)
    with pytest.raises(PlaintextExportDenied):
        profile[1]._export_csv_to_dir({}, profile[4], "synthetic")
    assert filesystem_snapshot(profile) == before


@pytest.mark.parametrize("route", ["history", "srt", "selected-md", "selected-srt", "json", "csv", "html"])
def test_render_only_needs_no_file_consent(profile, route):
    policy(profile[0], encryption=True)
    before = filesystem_snapshot(profile)
    result = call_route(profile, route, {"save_to_file": False})
    assert "reason" not in result
    assert filesystem_snapshot(profile) == before


@pytest.mark.parametrize("route", ROUTES)
def test_authorizer_exception_is_fail_closed(profile, route):
    class BrokenAuthorizer:
        def precheck_backend_export(self, *args):
            raise RuntimeError("synthetic auth failure")

        def run_backend_write(self, *args):
            raise RuntimeError("synthetic auth failure")

    profile[1]._plaintext_export_authorizer = BrokenAuthorizer()
    before = filesystem_snapshot(profile)
    result = call_route(profile, route)
    assert result.get("reason") == "plaintext_policy_unavailable"
    assert filesystem_snapshot(profile) == before


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("change", ["session", "epoch", "capability", "generation", "privacy"])
def test_valid_grant_cannot_bypass_fresh_context_and_policy_checks(profile, route, change):
    policy(profile[0], encryption=True)
    context = grant(profile[2])
    expected = "plaintext_session_expired"
    if change == "session":
        context["app_session_id"] = str(uuid.uuid4())
    elif change == "epoch":
        context["epoch"] = "12" * 32
    elif change == "capability":
        context["capability"] = "wrong-token"
    elif change == "generation":
        context["expected_policy_generation"] += 1
    else:
        policy(profile[0], encryption=True, privacy=True)
        expected = "privacy_mode_active"
    before = filesystem_snapshot(profile)
    result = call_route(profile, route, {"plaintext_export": context})
    assert result.get("reason") == expected
    assert filesystem_snapshot(profile) == before


@pytest.mark.parametrize("route", ["history", "selected-md", "selected-srt"])
@pytest.mark.parametrize("on", [False, True])
def test_empty_file_request_still_rejects_unusable_authority(profile, route, on):
    store, svc, auth, item, output = profile
    store.delete_history_item(item.id)
    policy(store, encryption=on)
    before = filesystem_snapshot(profile)
    result = call_route(profile, route, {} if on else {"plaintext_export": {}})
    assert result.get("reason") == ("plaintext_confirmation_required" if on else "plaintext_session_expired")
    assert filesystem_snapshot(profile) == before


# --- ревью #2229: безопасность записи экспорта (M04-02) и каталог-симлинк (F5) ---------------

def test_export_into_symlinked_output_directory_works(profile, tmp_path):
    """/tmp, Dropbox, iCloud, внешние диски — симлинк-каталоги: экспорт туда не должен падать.

    Симлинком должен быть САМ каталог назначения (path.parent): именно на нём срабатывал
    O_NOFOLLOW (batch-маршрут пишет в реальный подкаталог и этого не ловит).
    """
    svc = profile[1]
    real = tmp_path / "real_out"
    real.mkdir()
    link = tmp_path / "link_out"
    link.symlink_to(real, target_is_directory=True)
    svc._write_plaintext_file({"confirm": True, "force": True}, link / "export.md", "содержимое")
    assert (real / "export.md").read_text(encoding="utf-8") == "содержимое"


def test_symlink_at_destination_file_is_replaced_not_followed(profile, tmp_path):
    """M04-02: ссылка на САМ файл назначения не даёт перезаписать чужой файл-цель."""
    svc = profile[1]
    out = tmp_path / "outputs"
    out.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("НЕ ТРОГАТЬ", encoding="utf-8")
    dest = out / "export.md"
    dest.symlink_to(victim)
    svc._write_plaintext_file({"confirm": True, "force": True}, dest, "новое содержимое")
    assert victim.read_text(encoding="utf-8") == "НЕ ТРОГАТЬ"      # цель цела
    assert not dest.is_symlink() and dest.read_text(encoding="utf-8") == "новое содержимое"
    assert [p.name for p in out.iterdir()] == ["export.md"]       # temp не остался


def test_failed_write_leaves_no_partial_or_temp_file(profile, tmp_path, monkeypatch):
    svc = profile[1]
    out = tmp_path / "outputs"
    out.mkdir()
    dest = out / "export.md"
    dest.write_text("прежнее", encoding="utf-8")
    import os as _os

    def boom(*a, **k):
        raise OSError("диск полон")

    monkeypatch.setattr(_os, "fsync", boom)
    with pytest.raises(OSError):
        svc._write_plaintext_file({"confirm": True, "force": True}, dest, "новое")
    assert dest.read_text(encoding="utf-8") == "прежнее"           # не затёрт частично
    assert [p.name for p in out.iterdir()] == ["export.md"]       # temp убран
