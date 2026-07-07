#!/usr/bin/env python3
"""e2e_recommended_setup_smoke.py — живой e2e-смок для A1 «Рекомендованная настройка».

План: docs/superpowers/plans/2026-07-07-recommended-setup.md, Задача 7.
Метод: reference_live_e2e_smoke_method (память проекта) — socket-E2E с реальным
бэкендом ловит классы багов (краши/мусор в ответе), невидимые unit-тестам на
пустой/мок-истории и статическим контракт-аудитам.

Поднимает ТОЛЬКО СВОЙ throwaway dev-backend на temp data-dir (никогда не трогает
прод/реальную историю владельца) — паттерн scripts/run_e2e_smokes.command,
переписанный на чистый Python, т.к. этот скрипт самодостаточен (без .command
обвязки). Teardown — в finally, даже при падении любого assert.

Сценарий (§10 п.7 черновика):
  1. get_hardware_profile {} → ok=True, tier ∈ {low, mid, high}.
  2. apply_recommended_setup {dry_run: true} → dry_run=True, snapshot_id=None,
     applied ⊇ подмножество 10 безусловных ключей, skipped содержит GigaAM-пару
     с фиксированной причиной.
  3. apply_recommended_setup {dry_run: false} → snapshot_id is not None.
  4. get_settings {} → безусловные ключи из applied шага 3 реально True в
     settings.json (не только в IPC-ответе).
  5. restore_settings_backup {backup_id: <snapshot_id>} → настройки вернулись
     к состоянию до шага 3.

Run:
    python3 scripts/e2e_recommended_setup_smoke.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Override for git-worktree checkouts that don't carry their own .venv_krab_ear
# (worktrees share .git but not local venvs) — mirrors the KRAB_EAR_* env-override
# convention used throughout the project (core/config.py).
VENV_PY = Path(os.environ.get("KRAB_EAR_E2E_VENV_PYTHON", "")) if os.environ.get(
    "KRAB_EAR_E2E_VENV_PYTHON"
) else REPO_ROOT / ".venv_krab_ear" / "bin" / "python"

_UNCONDITIONAL_KEYS = {
    "smart_silence_skip_enabled", "realtime_silence_filter_enabled",
    "auto_dedup_enabled", "auto_save_transcripts", "phonetic_vocab_enabled",
    "text_snippets_enabled", "auto_learn_corrections_enabled",
    "quick_edit_enabled", "paste_undo_enabled", "calendar_link_enabled",
}
_GIGAAM_KEYS = {"stt_gigaam_enabled", "stt_language_routing_enabled"}
_GIGAAM_REASON = "настройте GigaAM вручную в Настройках"


def call(sock_path: str, method: str, params: dict, timeout: int = 30) -> dict:
    """Отправляет одиночный JSON-RPC запрос через Unix-сокет, возвращает распарсенный ответ."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    req = json.dumps({"id": f"smoke-{method}", "method": method, "params": params}) + "\n"
    s.sendall(req.encode("utf-8"))
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    line = buf.split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


def need(cond: bool, label: str) -> bool:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    return cond


def main() -> int:
    if not VENV_PY.exists():
        print(f"ERROR: venv python не найден: {VENV_PY} (сначала запустите настройку окружения)")
        return 1

    data_dir = Path(tempfile.mkdtemp(prefix="krab_ear_e2e_recsetup_"))
    sock_path = str(data_dir / "krabear.sock")
    log_path = data_dir / "backend.log"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "KrabEar")

    proc: subprocess.Popen | None = None
    rc = 0
    try:
        print(f"==> Запуск throwaway dev-backend (data-dir: {data_dir})")
        with open(log_path, "wb") as log_fh:
            proc = subprocess.Popen(
                [str(VENV_PY), "KrabEar/main.py", "--data-dir", str(data_dir)],
                cwd=str(REPO_ROOT), env=env, stdout=log_fh, stderr=subprocess.STDOUT,
            )

        # Ждём появления сокета до ~20с.
        for _ in range(40):
            if os.path.exists(sock_path):
                break
            if proc.poll() is not None:
                print("ERROR: backend завершился при старте. Последние строки лога:")
                print(_tail(log_path, 20))
                return 1
            time.sleep(0.5)
        else:
            print("ERROR: сокет так и не появился. Лог:")
            print(_tail(log_path, 20))
            return 1

        time.sleep(2)  # даём warmup'ам устояться

        ok_all = True

        # Шаг 1: get_hardware_profile
        print("\n=== Шаг 1: get_hardware_profile ===")
        r = call(sock_path, "get_hardware_profile", {})
        res = r.get("result", {})
        ok_all &= need(r.get("ok") is True, "get_hardware_profile: ok=True")
        tier = res.get("tier")
        ok_all &= need(tier in ("low", "mid", "high"), f"get_hardware_profile: tier валиден (got {tier!r})")

        # Шаг 2: apply_recommended_setup dry_run=true
        print("\n=== Шаг 2: apply_recommended_setup {dry_run: true} ===")
        r = call(sock_path, "apply_recommended_setup", {"dry_run": True})
        res = r.get("result", {})
        ok_all &= need(r.get("ok") is True, "apply_recommended_setup dry_run=true: ok=True")
        ok_all &= need(res.get("dry_run") is True, "apply_recommended_setup: dry_run=True в ответе")
        ok_all &= need(res.get("snapshot_id") is None, "apply_recommended_setup dry_run=true: snapshot_id=None")
        applied_keys = {a["key"] for a in res.get("applied", [])}
        ok_all &= need(
            bool(applied_keys & _UNCONDITIONAL_KEYS),
            f"apply_recommended_setup: applied содержит подмножество безусловных ключей (got {sorted(applied_keys)})",
        )
        skipped_map = {s["key"]: s["reason"] for s in res.get("skipped", [])}
        for gigaam_key in _GIGAAM_KEYS:
            ok_all &= need(
                skipped_map.get(gigaam_key) == _GIGAAM_REASON,
                f"apply_recommended_setup: {gigaam_key} skipped с причиной GigaAM (got {skipped_map.get(gigaam_key)!r})",
            )
            ok_all &= need(gigaam_key not in applied_keys, f"apply_recommended_setup: {gigaam_key} НЕ в applied")

        # Шаг 3: apply_recommended_setup dry_run=false
        print("\n=== Шаг 3: apply_recommended_setup {dry_run: false} ===")
        r = call(sock_path, "apply_recommended_setup", {"dry_run": False})
        res3 = r.get("result", {})
        ok_all &= need(r.get("ok") is True, "apply_recommended_setup dry_run=false: ok=True")
        ok_all &= need(res3.get("dry_run") is False, "apply_recommended_setup: dry_run=False в ответе")
        snapshot_id = res3.get("snapshot_id")
        ok_all &= need(snapshot_id is not None, "apply_recommended_setup dry_run=false: snapshot_id заполнен")
        applied_keys_3 = {a["key"] for a in res3.get("applied", [])}

        # Шаг 4: get_settings — реально записано в settings.json, не только в IPC-ответе
        print("\n=== Шаг 4: get_settings (проверка реальной записи) ===")
        r = call(sock_path, "get_settings", {})
        settings_after = r.get("result", {})
        ok_all &= need(r.get("ok") is True, "get_settings: ok=True")
        for key in applied_keys_3 & _UNCONDITIONAL_KEYS:
            ok_all &= need(
                bool(settings_after.get(key)) is True,
                f"get_settings: {key} реально True в settings.json после apply",
            )

        # Шаг 5: restore_settings_backup — откат к состоянию до Шага 3
        print("\n=== Шаг 5: restore_settings_backup (откат) ===")
        r = call(sock_path, "restore_settings_backup", {"backup_id": snapshot_id})
        res5 = r.get("result", {})
        ok_all &= need(r.get("ok") is True, "restore_settings_backup: ok=True")
        restored = res5.get("restored_settings", {})
        ok_all &= need(isinstance(restored, dict), "restore_settings_backup: restored_settings — dict")
        # До Шага 3 (свежий backend, без предыдущих запусков) все безусловные ключи
        # были False/отсутствовали — после отката они не должны быть True.
        for key in applied_keys_3 & _UNCONDITIONAL_KEYS:
            ok_all &= need(
                not bool(restored.get(key, False)),
                f"restore_settings_backup: {key} возвращён к False/отсутствует после отката",
            )

        print("\n" + "=" * 60)
        if ok_all:
            print("  ALL GREEN — apply_recommended_setup e2e-смок пройден")
            print("=" * 60)
            rc = 0
        else:
            print("  FAILURE — см. FAIL-строки выше")
            print("=" * 60)
            rc = 1
        return rc
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: исключение во время смока: {exc}")
        print(_tail(log_path, 30))
        return 1
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        _rmtree(data_dir)


def _tail(path: Path, n: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return "(лог недоступен)"


def _rmtree(path: Path) -> None:
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
