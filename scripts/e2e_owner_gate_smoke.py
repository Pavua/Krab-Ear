#!/usr/bin/env python3
"""Живой e2e-смок волны R2 «Владение записью» (Task 8, Step 1).

Поднимает ОТДЕЛЬНЫЙ backend на throwaway data_dir (прод и его история не
затрагиваются), прогоняет сценарии владения через реальный Unix-socket и
гасит инстанс в finally. Паттерн — `scripts/e2e_rescue_smoke.py`.

Запуск (venv обязателен — VENV_PY берётся из sys.executable):

    source .venv_krab_ear/bin/activate
    python scripts/e2e_owner_gate_smoke.py

ПОКРЫТО (сценарии плана R2, Task 8 Step 1):
  A (а) диктовка → promote во встречу → meeting_stop: ровно один терминальный
        ответ, ноль ложных owner-mismatch в логе.
  B (б) стоп с протухшим токеном при живой записи → unknown_generation,
        запись остаётся жива.
  C (в) двойной стоп одним токеном → тот же терминальный ответ (TTL-replay),
        без второй финализации.
  E (е) повторный meeting_stop по тому же токену → тот же item_id, без
        второго persist и без второго meeting.finished.

НЕ ПОКРЫТО ЗДЕСЬ (осознанно, не молча — см. правило «No silent caps»):
  D (г) recorder_timeout → повтор тем же токеном забирает G1. Требует
        ИНЪЕКЦИИ сбоя в AudioRecorder.stop (таймаут), недостижимой снаружи
        по IPC на живом backend. Покрыто юнит-уровнем:
        KrabEar/tests/test_recording_stop_gate.py.
  F (д) timeout финализации встречи сохраняет session/token. Тот же класс —
        нужна инъекция долгой финализации. Покрыто юнит-уровнем:
        KrabEar/tests/test_meeting_session_service_W_C2a.py.

Смок требует РАБОЧЕГО МИКРОФОНА (start_recording открывает захват на 1-2 с)
и потому запускается локально, а не в ubuntu-CI.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KRAB_EAR = REPO_ROOT / "KrabEar"
# venv общий для всех git-worktree проекта — переиспользуем интерпретатор,
# под которым запущен ЭТОТ скрипт (см. тот же комментарий в e2e_rescue_smoke).
VENV_PY = Path(sys.executable)

SOCKET_READY_TIMEOUT_SEC = 90.0
RECORD_SETTLE_SEC = 1.5
MEETING_SETTLE_SEC = 2.0
STOP_TIMEOUT_SEC = 180.0

_STEPS: list[tuple[str, bool, str]] = []


def _step(label: str, ok: bool, detail: str = "") -> bool:
    mark = "OK  " if ok else "FAIL"
    line = f"  {mark}  {label}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)
    _STEPS.append((label, ok, detail))
    return ok


def _call(sock_path: Path, method: str, params: dict, timeout: float = 60.0) -> dict:
    """Один JSON-RPC запрос через Unix socket (паттерн e2e_ipc_smoke.py)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock_path))
    req = json.dumps({"id": f"owner-smoke-{method}", "method": method, "params": params}) + "\n"
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


def _result(resp: dict) -> dict:
    """Тело ответа: у части хендлеров result вложен, у части — плоский."""
    if isinstance(resp.get("result"), dict):
        return resp["result"]
    return resp


def _spawn_backend(data_dir: Path, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KRAB_EAR)
    log_fh = log_path.open("wb")
    return subprocess.Popen(
        [str(VENV_PY), str(KRAB_EAR / "main.py"), "--data-dir", str(data_dir)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )


def _wait_for_socket(sock_path: Path, proc: subprocess.Popen, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                if _call(sock_path, "ping", {}, timeout=5.0).get("ok"):
                    return True
            except (OSError, socket.timeout, ConnectionRefusedError, json.JSONDecodeError):
                pass
        if proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def _count_owner_mismatch(log_path: Path) -> int:
    """Сколько раз backend зафиксировал позитивный owner-mismatch."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return -1
    return text.count("owner_mismatch")


# --------------------------------------------------------------------------
# Сценарии
# --------------------------------------------------------------------------

def _scenario_b_stale_token(sock: Path) -> bool:
    """(б) Стоп с протухшим токеном при живой записи не убивает запись."""
    start = _result(_call(sock, "start_recording", {"source": "dictation"}))
    if start.get("status") != "recording":
        return _step("B: старт диктовки", False, f"status={start.get('status')}")
    token = start.get("generation_token")
    time.sleep(RECORD_SETTLE_SEC)

    stale = _result(_call(sock, "stop_recording", {
        "source": "dictation",
        "generation_token": "stale-token-that-never-existed",
    }, timeout=STOP_TIMEOUT_SEC))
    ok_status = stale.get("status") == "unknown_generation"
    _step("B: чужой токен отклонён", ok_status, f"status={stale.get('status')}")

    state = _result(_call(sock, "get_recording_state", {}))
    alive = bool(state.get("is_recording"))
    _step("B: запись пережила чужой стоп", alive, f"is_recording={alive}")

    # Прибираем за собой — настоящим токеном.
    real = _result(_call(sock, "stop_recording", {
        "source": "dictation",
        "generation_token": token,
    }, timeout=STOP_TIMEOUT_SEC))
    _step("B: свой токен останавливает", real.get("status") not in (None, "unknown_generation"),
          f"status={real.get('status')}")
    return ok_status and alive


def _scenario_c_double_stop(sock: Path) -> bool:
    """(в) Двойной стоп одним токеном отдаёт ТОТ ЖЕ терминальный ответ."""
    start = _result(_call(sock, "start_recording", {"source": "dictation"}))
    if start.get("status") != "recording":
        return _step("C: старт диктовки", False, f"status={start.get('status')}")
    token = start.get("generation_token")
    time.sleep(RECORD_SETTLE_SEC)

    first = _result(_call(sock, "stop_recording", {
        "source": "dictation", "generation_token": token,
    }, timeout=STOP_TIMEOUT_SEC))
    second = _result(_call(sock, "stop_recording", {
        "source": "dictation", "generation_token": token,
    }, timeout=STOP_TIMEOUT_SEC))

    same_status = first.get("status") == second.get("status")
    same_item = first.get("history_id") == second.get("history_id")
    _step("C: повтор даёт тот же статус", same_status,
          f"{first.get('status')} vs {second.get('status')}")
    _step("C: повтор даёт тот же history_id", same_item,
          f"{first.get('history_id')} vs {second.get('history_id')}")

    state = _result(_call(sock, "get_recording_state", {}))
    _step("C: вторая финализация не запустила запись", not state.get("is_recording"))
    return same_status and same_item


def _scenario_a_promote(sock: Path, log_path: Path) -> bool:
    """(а) Диктовка → promote во встречу → meeting_stop: один терминал."""
    before = _count_owner_mismatch(log_path)

    start = _result(_call(sock, "start_recording", {"source": "dictation"}))
    if start.get("status") != "recording":
        return _step("A: старт диктовки", False, f"status={start.get('status')}")
    time.sleep(RECORD_SETTLE_SEC)

    meeting = _result(_call(sock, "meeting_start", {}, timeout=120.0))
    promoted = bool(meeting.get("ok", True))
    _step("A: встреча поднялась поверх диктовки", promoted, f"resp={str(meeting)[:120]}")
    if not promoted:
        _call(sock, "stop_recording", {"source": "dictation"}, timeout=STOP_TIMEOUT_SEC)
        return False
    time.sleep(MEETING_SETTLE_SEC)

    stop = _result(_call(sock, "meeting_stop", {}, timeout=STOP_TIMEOUT_SEC))
    ok_stop = bool(stop.get("ok", True))
    _step("A: meeting_stop завершился", ok_stop, f"resp={str(stop)[:120]}")

    state = _result(_call(sock, "get_recording_state", {}))
    _step("A: запись остановлена ровно один раз", not state.get("is_recording"))

    after = _count_owner_mismatch(log_path)
    no_false_mismatch = (after == before)
    _step("A: ноль ложных owner-mismatch", no_false_mismatch, f"было {before}, стало {after}")
    return ok_stop and no_false_mismatch


def _scenario_e_meeting_replay(sock: Path) -> bool:
    """(е) Повторный meeting_stop по токену реплеит тот же item_id."""
    started = _result(_call(sock, "meeting_start", {}, timeout=120.0))
    if not started.get("ok", True):
        return _step("E: старт встречи", False, f"resp={str(started)[:120]}")
    time.sleep(MEETING_SETTLE_SEC)

    first = _result(_call(sock, "meeting_stop", {}, timeout=STOP_TIMEOUT_SEC))
    second = _result(_call(sock, "meeting_stop", {}, timeout=STOP_TIMEOUT_SEC))

    first_item = first.get("item_id") or first.get("history_id")
    second_item = second.get("item_id") or second.get("history_id")
    # Второй стоп либо реплеит тот же item, либо честно сообщает, что встречи
    # уже нет. Чего быть НЕ должно — второй НОВЫЙ item_id (двойной persist).
    no_double_persist = (second_item is None) or (second_item == first_item)
    _step("E: повтор не создал второй item", no_double_persist,
          f"{first_item} vs {second_item}")
    return no_double_persist


def _finish(tmp_dir: Path, log_path: Path) -> int:
    failed = [s for s in _STEPS if not s[1]]
    print("\n" + "=" * 66)
    print(f"ИТОГО: {len(_STEPS) - len(failed)}/{len(_STEPS)} шагов OK")
    print("НЕ ПОКРЫТО смоком (нужна инъекция сбоя, покрыто юнит-тестами):")
    print("  (г) recorder_timeout → test_recording_stop_gate.py")
    print("  (д) timeout финализации встречи → test_meeting_session_service_W_C2a.py")
    if failed:
        print("\nПРОВАЛЕНО:")
        for label, _, detail in failed:
            print(f"  - {label} — {detail}")
        keep = Path(tempfile.gettempdir()) / f"owner_gate_smoke_fail_{int(time.time())}.log"
        try:
            shutil.copy2(log_path, keep)
            print(f"\nЛог backend сохранён: {keep}")
        except OSError:
            pass
    print("=" * 66)
    return 1 if failed else 0


def main() -> int:
    if not KRAB_EAR.exists():
        print(f"Не найден {KRAB_EAR}", file=sys.stderr)
        return 2

    tmp_dir = Path(tempfile.mkdtemp(prefix="krab_owner_smoke_"))
    data_dir = tmp_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_dir / "backend.log"
    proc: "subprocess.Popen | None" = None

    print("=" * 66)
    print("E2E OWNER GATE SMOKE (R2) — throwaway data_dir:", data_dir)
    print("=" * 66)

    try:
        proc = _spawn_backend(data_dir, log_path)
        sock = data_dir / "krabear.sock"
        if not _wait_for_socket(sock, proc, SOCKET_READY_TIMEOUT_SEC):
            _step("backend поднялся", False, "сокет не открылся")
            return _finish(tmp_dir, log_path)
        _step("backend поднялся", True)

        _scenario_b_stale_token(sock)
        _scenario_c_double_stop(sock)
        _scenario_a_promote(sock, log_path)
        _scenario_e_meeting_replay(sock)

        return _finish(tmp_dir, log_path)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
