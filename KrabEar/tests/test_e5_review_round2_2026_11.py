"""Находки внешнего ревью ChatGPT по ветке E4/E5 (2026-11-16, раунд 2), подтверждённые по коду.

F1 unlink после ошибки удалял чужой файл; F4 явный null включал авто-экспорт; F5 сравнение env-пина
до нормализации; F6 два экспорта за секунду; F7 privacy-гейт пропускал None; F8 неудачный старт потока
блокировал миграцию навсегда; F9 фактический data_dir не был исключён; F10 очистка HF-env сносила
чужое значение; F11 права зависели от umask.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_service import SettingsService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402

_POLICY = {"privacy_mode_enabled": False, "history_encryption_enabled": False}


@pytest.fixture()
def svc(tmp_path):
    service = SettingsService(StateStore(tmp_path / "data"))
    service.handle_set_settings(dict(_POLICY))
    return service


@pytest.fixture()
def fake_home(tmp_path):
    """HOME вне allowlist (/var/tmp), чтобы тест не писал в реальный домашний каталог."""
    with tempfile.TemporaryDirectory(dir="/var/tmp") as raw:
        home = Path(raw).resolve()
        with patch.dict(os.environ, {"HOME": str(home)}):
            yield home


# --- F4 -------------------------------------------------------------------------------------------------------

def _auto_export(params):
    from backend.service import BackendService

    seen: dict = {}
    fake = SimpleNamespace(_export_scheduler=SimpleNamespace(configure=lambda **kw: seen.update(kw) or {}))
    BackendService._handle_configure_auto_export(fake, {"format": "json", **params})
    return seen["enabled"]


@pytest.mark.parametrize("value", [None, "maybe", float("nan"), float("inf"), []])
def test_configure_auto_export_invalid_enabled_fails_closed(value):
    assert _auto_export({"enabled": value}) is False


def test_configure_auto_export_absent_enabled_keeps_documented_default():
    assert _auto_export({}) is True


def test_backend_coerce_bool_non_finite_uses_default():
    from backend.service import BackendService

    assert BackendService._coerce_bool(float("inf"), False) is False
    assert BackendService._coerce_bool(float("nan"), True) is True


# --- F7 -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, 1, "true", "false"])
def test_sentry_diagnostics_denied_unless_privacy_explicitly_false(monkeypatch, value):
    from backend.service import BackendService

    calls: list = []
    monkeypatch.setitem(sys.modules, "sentry_sdk", SimpleNamespace(
        add_breadcrumb=lambda **kw: calls.append(kw), capture_message=lambda *a, **kw: calls.append(kw),
        flush=lambda **kw: None))
    err = SimpleNamespace(component="c", code="x", severity="error", context={})
    fake = SimpleNamespace(
        _error_bus=SimpleNamespace(list_recent=lambda limit: [err]),
        _get_runtime_setting=lambda key, default: value if key == "privacy_mode_enabled" else default)
    assert BackendService._handle_send_diagnostics_to_sentry(fake, {}).get("ok") is False
    assert calls == []


# --- F8 -------------------------------------------------------------------------------------------------------

def test_migration_flag_is_cleared_when_worker_thread_cannot_start(monkeypatch):
    from backend.service import BackendService

    class FakeStore:
        @contextlib.contextmanager
        def _lock(self):
            yield

        def _read_encryption_flag_unlocked(self):
            return True

        def _acquire_history_crypto_unlocked(self):
            return object()

        def migrate_history_encryption(self, progress_cb=None):
            return {"ok": True, "total": 0, "encrypted": 0}

    fake = SimpleNamespace(store=FakeStore())
    real_start = threading.Thread.start

    def boom(self):
        if self.name == "history-enc-migration":
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", boom)
    with pytest.raises(RuntimeError):
        BackendService._handle_migrate_history_encryption(fake, {})
    assert fake._history_migration_running is False


# --- F10 ------------------------------------------------------------------------------------------------------

def test_clearing_hf_token_keeps_env_value_replaced_by_someone_else(monkeypatch):
    from backend.service import BackendService

    for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    values = {"hf_token": "SYNTHETIC_A", "stt_gigaam_hf_token": ""}
    fake = SimpleNamespace(_get_runtime_setting=lambda key, default: values.get(key, default), transcriber=None)
    BackendService._propagate_hf_token_to_env(fake, overwrite=True)
    os.environ["HF_TOKEN"] = "SYNTHETIC_B_EXTERNAL"
    values["hf_token"] = ""
    BackendService._propagate_hf_token_to_env(fake, overwrite=True)
    assert os.environ.get("HF_TOKEN") == "SYNTHETIC_B_EXTERNAL"
    assert os.environ.get("HUGGING_FACE_HUB_TOKEN") in (None, "")
    assert os.environ.get("HUGGINGFACE_TOKEN") in (None, "")


# --- F1, F6, F9, F11 ------------------------------------------------------------------------------------------

def test_export_cleanup_after_write_error_never_deletes_someone_elses_file(svc, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    target = out / "race.json"
    real_fdopen = os.fdopen

    def hostile_fdopen(fd, *args, **kwargs):
        os.unlink(target)                       # другой процесс подменил запись каталога
        target.write_text("KEEP-ME", encoding="utf-8")
        real_fdopen(fd).close()
        raise OSError("disk full")

    with patch("backend.settings_service.os.fdopen", hostile_fdopen):
        with pytest.raises(OSError):
            svc.handle_export_settings({"file": str(target)})
    assert target.read_text(encoding="utf-8") == "KEEP-ME"


def test_two_default_exports_in_one_second_both_succeed(svc, fake_home):
    first = svc.handle_export_settings({})["file"]
    second = svc.handle_export_settings({})["file"]
    assert first != second
    assert Path(first).exists() and Path(second).exists()


def test_export_refuses_new_file_inside_configured_data_dir(svc, tmp_path):
    for name in ("brand_new_history.ndjson", "settings_export.json"):
        target = tmp_path / "data" / name
        with pytest.raises(RuntimeError):
            svc.handle_export_settings({"file": str(target)})
        assert not target.exists()


def test_export_is_0600_even_under_a_hostile_umask(svc, tmp_path):
    target = tmp_path / "umask.json"
    old = os.umask(0o777)
    try:
        svc.handle_export_settings({"file": str(target)})
    finally:
        os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))


# --- F5 -------------------------------------------------------------------------------------------------------

def test_import_unchanged_pinned_value_passes_even_if_spelled_as_string(svc, tmp_path, monkeypatch):
    svc.handle_set_settings({"ipc_signing_enabled": False})
    svc.invalidate_cache()
    monkeypatch.setenv("KRAB_EAR_IPC_SIGNING_ENABLED", "true")
    cur = svc.cached_settings()
    src = tmp_path / "same.json"
    src.write_text(json.dumps({
        "privacy_mode_enabled": cur["privacy_mode_enabled"],
        "history_encryption_enabled": cur["history_encryption_enabled"],
        "ipc_signing_enabled": "false",
    }))
    svc.handle_import_settings({"file": str(src)})  # не должно бросать: значение по смыслу то же


def test_import_changed_pinned_value_true_is_rejected(svc, tmp_path, monkeypatch):
    svc.handle_set_settings({"ipc_signing_enabled": False})
    svc.invalidate_cache()
    monkeypatch.setenv("KRAB_EAR_IPC_SIGNING_ENABLED", "true")
    cur = svc.cached_settings()
    src = tmp_path / "chg.json"
    src.write_text(json.dumps({
        "privacy_mode_enabled": cur["privacy_mode_enabled"],
        "history_encryption_enabled": cur["history_encryption_enabled"],
        "ipc_signing_enabled": True,
    }))
    with pytest.raises(ValueError):
        svc.handle_import_settings({"file": str(src)})
