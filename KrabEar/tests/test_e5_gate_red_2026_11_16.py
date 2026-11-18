"""Красные тесты по разбору ревью E5 (backend/service.py, settings_service.py, settings_validator.py).

Каждый тест ДОЛЖЕН падать на коммите ea993112 (origin/codex/krab-ear-v2) и проходить после
минимальной правки, указанной в докстринге. Тесты работают только на temp-каталогах,
без сети; сервис целиком не поднимают (где можно — вызывают метод на SimpleNamespace).

Запуск (из KrabEar/): python -m pytest tests/test_e5_gate_red_2026_11_16.py
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_service import SettingsService  # noqa: E402
from backend.settings_validator import SettingsValidator  # noqa: E402
from backend.state_store import StateStore  # noqa: E402

_POLICY = {"privacy_mode_enabled": False, "history_encryption_enabled": False}


@pytest.fixture()
def svc(tmp_path):
    store = StateStore(tmp_path / "data")
    service = SettingsService(store)
    service.handle_set_settings(dict(_POLICY))  # новый профиль требует явную пару policy-флагов
    return service


# --- R1 (S2): null в hf_token ронял BackendService.__init__ (crash-loop после set_settings) -------------------

@pytest.mark.parametrize("bad", [None, 12345, ["x"]])
def test_hf_token_propagation_tolerates_non_string_runtime_values(bad):
    """Минимальная правка: `str(x or "").strip()` в _propagate_hf_token_to_env (service.py:2036-2037)
    И/ИЛИ валидатор: hf_token/stt_gigaam_hf_token добавить в _STRING_FIELDS (settings_validator.py:28)."""
    from backend.service import BackendService

    fake = SimpleNamespace(_get_runtime_setting=lambda key, default: bad, transcriber=None)
    BackendService._propagate_hf_token_to_env(fake, overwrite=True)  # не должен бросать


@pytest.mark.parametrize("bad", [None, 12345, ["x"]])
def test_set_settings_rejects_non_string_hf_token(svc, bad):
    with pytest.raises(ValueError):
        svc.handle_set_settings({"hf_token": bad})


# --- R2 (S3): import/restore обходят env-pin (set_settings его соблюдает) ------------------------------------

def test_import_settings_respects_env_pin(svc, tmp_path, monkeypatch):
    """Минимальная правка: вызвать self._check_env_pinned(incoming) в handle_import_settings
    (и для restored в handle_restore_settings_backup) до записи."""
    monkeypatch.setenv("KRAB_EAR_IPC_SIGNING_ENABLED", "true")
    src = tmp_path / "imp.json"
    cur = svc.cached_settings()
    src.write_text(json.dumps({
        "privacy_mode_enabled": cur["privacy_mode_enabled"],
        "history_encryption_enabled": cur["history_encryption_enabled"],
        "ipc_signing_enabled": False,
    }))
    with pytest.raises(ValueError):
        svc.handle_import_settings({"file": str(src)})
    svc.invalidate_cache()
    assert svc.cached_settings().get("ipc_signing_enabled") is not False


# --- R3 (S4): двойной запуск миграции шифрования истории (check-then-act вне лока) -----------------------------

def test_migrate_history_encryption_is_single_flight():
    """Минимальная правка: под отдельным threading.Lock атомарно проверить и выставить
    _history_migration_running ДО старта потока (service.py:5989-6032)."""
    from backend.service import BackendService

    started: list[float] = []

    class FakeStore:
        @contextlib.contextmanager
        def _lock(self):
            time.sleep(0.2)  # окно гонки: флаг ещё не выставлен
            yield

        def _read_encryption_flag_unlocked(self):
            return True

        def _acquire_history_crypto_unlocked(self):
            return object()

        def migrate_history_encryption(self, progress_cb=None):
            started.append(time.monotonic())
            time.sleep(0.2)
            return {"ok": True, "total": 0, "encrypted": 0}

    fake = SimpleNamespace(store=FakeStore())
    results: list[dict] = []
    threads = [
        threading.Thread(target=lambda: results.append(BackendService._handle_migrate_history_encryption(fake, {})))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(0.6)
    assert len(started) == 1, f"workers started: {len(started)}; results={results}"


# --- R4 (S4): очистка токена в GUI не очищает env процесса -------------------------------------------------------

def test_clearing_hf_token_removes_env_that_settings_injected(monkeypatch):
    """Минимальная правка: помнить, что env выставили настройки (self._hf_env_from_settings),
    и при пустых обоих токенах удалить именно эти ключи (+ инвалидировать pipeline);
    явный внешний env (KRAB_EAR_HF_TOKEN / plist) не трогать."""
    from backend.service import BackendService

    for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    values = {"hf_token": "SYNTHETIC_TOKEN", "stt_gigaam_hf_token": ""}
    fake = SimpleNamespace(_get_runtime_setting=lambda key, default: values.get(key, default), transcriber=None)
    BackendService._propagate_hf_token_to_env(fake, overwrite=True)
    assert os.environ.get("HF_TOKEN") == "SYNTHETIC_TOKEN"
    values["hf_token"] = ""
    BackendService._propagate_hf_token_to_env(fake, overwrite=True)
    assert os.environ.get("HF_TOKEN") in (None, "")


# --- R5 (S3/S4): configure_auto_export: строка "false" включает экспорт -----------------------------------------

def test_configure_auto_export_string_false_is_not_enabled():
    """Минимальная правка: service.py:3575 использовать строгий bool-парсер (как SettingsService._coerce_bool)
    или отклонять не-bool через ValueError."""
    from backend.service import BackendService

    seen: dict = {}
    fake = SimpleNamespace(
        _export_scheduler=SimpleNamespace(configure=lambda **kw: seen.update(kw) or {}),
    )
    BackendService._handle_configure_auto_export(fake, {"format": "json", "enabled": "false"})
    assert seen["enabled"] is False


# --- R6 (S3): export_settings перезаписывает любой существующий файл в allowlist и пишет 0644 ----------------

def test_export_settings_does_not_overwrite_foreign_file_and_is_private(svc, tmp_path):
    """Минимальная правка: открывать через os.open(O_CREAT|O_EXCL, 0o600) либо atomic_write_text(...)
    с mode 0600; отказывать, если файл уже существует и не является ранее созданным экспортом."""
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    with pytest.raises((ValueError, RuntimeError, FileExistsError)):
        svc.handle_export_settings({"file": str(victim)})
    assert victim.read_text() == "precious"

    out = tmp_path / "exp.json"
    svc.handle_export_settings({"file": str(out)})
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_export_settings_refuses_to_target_data_dir_internals(svc, tmp_path):
    """Allowlist содержит ~/.krab_ear_data и ~/Library/Application Support/KrabEar (рабочие данные).
    Минимальная правка: исключить data_dir/settings-каталог (и .secrets) из целей экспорта."""
    target = svc.store.data_dir / "history.ndjson"
    target.write_text("history-line\n")
    with pytest.raises((ValueError, RuntimeError, FileExistsError)):
        svc.handle_export_settings({"file": str(target)})
    assert target.read_text() == "history-line\n"


# --- R7 (S4): NaN включает cloud_rewriter_enabled ------------------------------------------------------------------

def test_validator_nan_does_not_enable_cloud_rewriter():
    """Минимальная правка: SettingsValidator._coerce_bool (settings_validator.py:535) — для float
    требовать math.isfinite, иначе None (-> default False)."""
    result = SettingsValidator().validate({"cloud_rewriter_enabled": float("nan")})
    assert result.fixed["cloud_rewriter_enabled"] is False


# --- R8 (S4): send_diagnostics_to_sentry без privacy-гейта (сестринский get_audit_log его имеет) --------------------

def test_send_diagnostics_to_sentry_denied_in_privacy_mode(monkeypatch):
    """Минимальная правка: в начале _handle_send_diagnostics_to_sentry вернуть
    {"ok": False, "reason": "privacy_mode_active"}, если privacy_mode_enabled (через _get_runtime_setting)."""
    from backend.service import BackendService

    calls: list = []
    fake_sdk = SimpleNamespace(
        add_breadcrumb=lambda **kw: calls.append(("bc", kw)),
        capture_message=lambda *a, **kw: calls.append(("msg", kw)),
        flush=lambda **kw: None,
    )
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
    err = SimpleNamespace(component="c", code="x", severity="error", context={"k": "v"})
    fake = SimpleNamespace(
        _error_bus=SimpleNamespace(list_recent=lambda limit: [err]),
        _get_runtime_setting=lambda key, default: True if key == "privacy_mode_enabled" else default,
        _cached_settings=lambda: {"privacy_mode_enabled": True},
    )
    result = BackendService._handle_send_diagnostics_to_sentry(fake, {})
    assert result.get("ok") is False
    assert calls == []
