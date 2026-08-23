#!/usr/bin/env python3
"""e2e_rescue_smoke.py — живой e2e-смок R1 «Надёжность записи» (Task 8).

Проверяет ВЕСЬ стек Фазы 1 живьём против THROWAWAY dev-backend (одноразовый
data_dir в /tmp — прод НЕ трогается):

  1. Continuous spill (recording_spill.py / Task 1-3): во время активной
     записи сырые PCM-фреймы дублируются на диск в rescue/*.f32.part и
     переживают kill -9.
  2. Восстановление на старте (recording_rescue.py / Task 4): следующий
     запуск backend на том же data_dir находит .part-файл, финализирует его
     в WAV, транскрибирует (или оставляет WAV при privacy/пустом STT) и
     пушит KrabError audio.recording_rescued.
  3. Форензика некорректного завершения (shutdown_forensics.py / Task 6):
     SIGKILL = UNCLEAN → следующий старт собирает forensics/<ts>/*.

Запуск:
    python scripts/e2e_rescue_smoke.py

Паттерн процесса — по образцу scripts/run_e2e_smokes.command +
scripts/e2e_ipc_smoke.py (throwaway data_dir, teardown в finally). Это
e2e-инструмент, НЕ unit-тест: поведенческая правда уже покрыта тестами
Task 1-7; здесь проверяется, что весь стек реально работает СОБРАННЫМ
ВМЕСТЕ на живом процессе, а не в изоляции с фейками.

Любой FAIL здесь = реальный баг Фазы 1 (или дрейф между планом и кодом) —
чинить в соответствующем модуле, НЕ ослаблять смок.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KRAB_EAR = REPO_ROOT / "KrabEar"
# venv общий для всех git-worktree проекта (живёт в корне ГЛАВНОГО checkout,
# не в каждом worktree) — поэтому вместо REPO_ROOT/.venv_krab_ear/bin/python
# (который отсутствует внутри worktree-копии) переиспользуем интерпретатор,
# под которым уже запущен ЭТОТ скрипт: пользователь обязан активировать venv
# перед запуском (см. докстринг), тогда sys.executable указывает точно на
# него независимо от того, из main checkout или из worktree мы работаем.
VENV_PY = Path(sys.executable)

# Щедрые таймауты (см. CLAUDE.md "Щедрые таймауты для агентных задач") —
# тёплый старт mlx-whisper/GigaAM/pyannote может занять 10-40с; второй старт
# backend делает ТРИ вещи последовательно в фоновом треде (форензика +
# rescue-скан с транскрипцией), поэтому даём широкий бюджет.
SOCKET_READY_TIMEOUT_SEC = 60.0
SPILL_GROWTH_WAIT_SEC = 3.0
KILL_WAIT_TIMEOUT_SEC = 15.0
RESCUE_POLL_TIMEOUT_SEC = 90.0
FORENSICS_POLL_TIMEOUT_SEC = 30.0
SHUTDOWN_INFO_WAIT_SEC = 15.0

_STEPS: list[tuple[str, bool, str]] = []  # (label, ok, detail)


def _step(label: str, ok: bool, detail: str = "") -> bool:
    mark = "OK  " if ok else "FAIL"
    line = f"  {mark}  {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    _STEPS.append((label, ok, detail))
    return ok


def _call(sock_path: Path, method: str, params: dict, timeout: float = 30.0) -> dict:
    """Один JSON-RPC запрос через Unix socket (паттерн e2e_ipc_smoke.py)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock_path))
    req = json.dumps({"id": f"rescue-smoke-{method}", "method": method, "params": params}) + "\n"
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


def _wait_for_socket(sock_path: Path, proc: subprocess.Popen, timeout_sec: float, log_path: Path) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if sock_path.exists():
            # Сокет-файл появляется до готовности слушателя — короткая пауза
            # + пробный ping закрывают гонку без опоры на фиксированный sleep.
            try:
                resp = _call(sock_path, "ping", {}, timeout=5.0)
                if resp.get("ok"):
                    return True
            except (OSError, socket.timeout, ConnectionRefusedError):
                pass
        if proc.poll() is not None:
            print(f"  ERROR: backend процесс завершился во время старта (exit={proc.returncode}). "
                  f"Хвост лога {log_path}:")
            _print_log_tail(log_path)
            return False
        time.sleep(0.5)
    return False


def _print_log_tail(log_path: Path, lines: int = 40) -> None:
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in content[-lines:]:
            print(f"    {line}")
    except Exception as exc:
        print(f"    (не удалось прочитать лог: {exc!r})")


def _spawn_backend(data_dir: Path, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KRAB_EAR)
    # Privacy-журнал тоже уводим в throwaway: логгер home-rooted по умолчанию и
    # иначе пишет в боевой compliance-файл вопреки обещанию шапки скрипта.
    env["KRAB_EAR_PRIVACY_AUDIT_DIR"] = str(data_dir)
    log_fh = log_path.open("wb")
    proc = subprocess.Popen(
        [str(VENV_PY), str(KRAB_EAR / "main.py"), "--data-dir", str(data_dir)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    return proc


def main() -> int:
    if not VENV_PY.exists():
        print(f"ERROR: интерпретатор {VENV_PY} не найден — активируй venv перед запуском "
              f"(source .venv_krab_ear/bin/activate из корня репо)")
        return 1

    tmp_dir = Path(tempfile.mkdtemp(prefix="krab_ear_rescue_smoke_"))
    sock_path = tmp_dir / "krabear.sock"
    log1 = tmp_dir / "backend_life1.log"
    log2 = tmp_dir / "backend_life2.log"
    proc1: "subprocess.Popen | None" = None
    proc2: "subprocess.Popen | None" = None
    overall_ok = True

    try:
        print("=" * 70)
        print("R1 rescue e2e smoke — data_dir:", tmp_dir)
        print("=" * 70)

        # --- Шаг 1: первая жизнь backend ---
        print("\n==> Шаг 1: запуск первой жизни backend")
        proc1 = _spawn_backend(tmp_dir, log1)
        ok = _wait_for_socket(sock_path, proc1, SOCKET_READY_TIMEOUT_SEC, log1)
        overall_ok &= _step("backend #1 поднялся и отвечает на ping", ok)
        if not ok:
            return _finish(overall_ok, tmp_dir)

        # --- Шаг 2: start_recording ---
        print("\n==> Шаг 2: start_recording")
        try:
            resp = _call(sock_path, "start_recording", {}, timeout=15.0)
            ok = bool(resp.get("ok"))
        except Exception as exc:
            resp = {}
            ok = False
            print(f"  EXCEPTION: {exc!r}")
        overall_ok &= _step("start_recording вернул ok=true", ok, str(resp.get("result", resp)))
        if not ok:
            return _finish(overall_ok, tmp_dir)

        # --- Шаг 3: spill растёт на диске ---
        print("\n==> Шаг 3: rescue/*.f32.part существует и растёт")
        rescue_dir = tmp_dir / "rescue"
        part_path = _find_first_part(rescue_dir, timeout_sec=SPILL_GROWTH_WAIT_SEC + 2.0)
        ok = _step(
            "rescue/*.f32.part появился",
            part_path is not None,
            str(part_path) if part_path else "не найден за отведённое время",
        )
        overall_ok &= ok
        if not ok:
            return _finish(overall_ok, tmp_dir)

        size_a = part_path.stat().st_size
        time.sleep(SPILL_GROWTH_WAIT_SEC)
        size_b = part_path.stat().st_size
        ok = _step(".part растёт без close()", size_b > size_a, f"{size_a} → {size_b} байт")
        overall_ok &= ok

        # --- Шаг 4: SIGKILL посреди записи ---
        print("\n==> Шаг 4: SIGKILL посреди записи (жёсткая смерть)")
        pid1 = proc1.pid
        os.kill(pid1, signal.SIGKILL)
        # subprocess.Popen.wait() — а НЕ os.kill(pid, 0) в цикле: наш собственный
        # процесс — родитель proc1, поэтому мёртвый ребёнок висит зомби (kill(pid,0)
        # видит зомби как «живой»), пока МЫ его не reap-нем через wait(). Опрос через
        # os.kill(pid, 0) до вызова wait() был бы гонкой сам с собой — никогда не
        # увидит смерть, пока сам её не подтвердит.
        try:
            proc1.wait(timeout=KILL_WAIT_TIMEOUT_SEC)
            died = True
        except subprocess.TimeoutExpired:
            died = False
        overall_ok &= _step("backend #1 умер по SIGKILL", died)

        part_survived = part_path.exists()
        overall_ok &= _step(".part пережил kill -9 (данные на диске)", part_survived)

        # --- Шаг 5: вторая жизнь backend на ТОМ ЖЕ data_dir ---
        print("\n==> Шаг 5: запуск второй жизни backend (тот же data_dir)")
        proc2 = _spawn_backend(tmp_dir, log2)
        ok = _wait_for_socket(sock_path, proc2, SOCKET_READY_TIMEOUT_SEC, log2)
        overall_ok &= _step("backend #2 поднялся и отвечает на ping", ok)
        if not ok:
            return _finish(overall_ok, tmp_dir)

        # --- Шаг 6: восстановление — .rescued.wav и/или транскрипция + KrabError ---
        print("\n==> Шаг 6: восстановление записи (rescue scan) + KrabError")
        rescue_outcome = _poll_rescue_outcome(part_path, rescue_dir, RESCUE_POLL_TIMEOUT_SEC)
        overall_ok &= _step(
            ".part финализирован (транскрибирован ИЛИ WAV оставлен как есть)",
            rescue_outcome["finalized"],
            rescue_outcome["detail"],
        )

        error_found = _poll_recording_rescued_error(sock_path, RESCUE_POLL_TIMEOUT_SEC)
        overall_ok &= _step("list_recent_errors содержит audio.recording_rescued", error_found)

        # --- Шаг 7: форензика UNCLEAN-жизни собрана ---
        print("\n==> Шаг 7: forensics/ непуст (SIGKILL = UNCLEAN)")
        forensics_dir = tmp_dir / "forensics"
        forensics_ok = _poll_forensics_nonempty(forensics_dir, FORENSICS_POLL_TIMEOUT_SEC)
        if forensics_dir.is_dir():
            forensics_detail = str(sorted(p.name for p in forensics_dir.iterdir()))
        else:
            forensics_detail = "каталог отсутствует"
        overall_ok &= _step("forensics/ содержит собранный снимок", forensics_ok, forensics_detail)

        # --- Шаг 8: штатное завершение SIGTERM + shutdown_info.json ---
        print("\n==> Шаг 8: штатный SIGTERM второй жизни + shutdown_info.json")
        pid2 = proc2.pid
        os.kill(pid2, signal.SIGTERM)
        proc2.wait(timeout=SHUTDOWN_INFO_WAIT_SEC)
        died2 = not _pid_alive(pid2)
        overall_ok &= _step("backend #2 завершился по SIGTERM", died2)

        info_path = tmp_dir / "shutdown_info.json"
        info_ok, info_detail = _check_shutdown_info(info_path)
        overall_ok &= _step("shutdown_info.json: signal=SIGTERM, recording_active=false", info_ok, info_detail)

        return _finish(overall_ok, tmp_dir)
    finally:
        for proc, log_path in ((proc1, log1), (proc2, log2)):
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=5.0)
                except Exception:
                    pass
        if not overall_ok:
            # Сохранить логи для диагностики ДО удаления tmp_dir.
            _preserve_failure_logs(tmp_dir, log1, log2)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _find_first_part(rescue_dir: Path, timeout_sec: float) -> "Path | None":
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if rescue_dir.is_dir():
            parts = sorted(rescue_dir.glob("*.f32.part"))
            if parts:
                return parts[0]
        time.sleep(0.3)
    return None


def _poll_rescue_outcome(part_path: Path, rescue_dir: Path, timeout_sec: float) -> dict:
    """Ждём, пока rescue-скан обработает .part: либо он исчез и появился
    .rescued.wav (потом тоже может исчезнуть после транскрипции), либо
    .part исчез без следа (успешно транскрибирован и WAV подчищен), либо
    (на пустой тишине STT ничего не вернул) WAV остался лежать как есть —
    оба исхода валидны (см. Task 8 сценарий)."""
    stem = part_path.name[: -len(".f32.part")]
    wav_path = rescue_dir / f"{stem}.rescued.wav"
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        part_gone = not part_path.exists()
        if part_gone:
            if wav_path.exists():
                return {"finalized": True, "detail": f".part финализирован, WAV лежит: {wav_path.name}"}
            return {"finalized": True, "detail": ".part исчез (транскрибирован, WAV подчищен)"}
        time.sleep(1.0)
    return {"finalized": False, "detail": f".part всё ещё существует спустя {timeout_sec:.0f}с: {part_path}"}


def _poll_recording_rescued_error(sock_path: Path, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            resp = _call(sock_path, "list_recent_errors", {"since_seq": 0}, timeout=10.0)
            errors = resp.get("result", {}).get("errors", [])
            if any(e.get("code") == "audio.recording_rescued" for e in errors):
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def _poll_forensics_nonempty(forensics_dir: Path, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if forensics_dir.is_dir() and any(forensics_dir.iterdir()):
            return True
        time.sleep(1.0)
    return False


def _check_shutdown_info(info_path: Path) -> tuple[bool, str]:
    deadline = time.monotonic() + SHUTDOWN_INFO_WAIT_SEC
    while time.monotonic() < deadline:
        if info_path.exists():
            try:
                data = json.loads(info_path.read_text(encoding="utf-8"))
                sig = data.get("signal")
                rec_active = data.get("recording_active")
                if sig == "SIGTERM" and rec_active is False:
                    return True, f"signal={sig} recording_active={rec_active} uptime_sec={data.get('uptime_sec')}"
                return False, f"неожиданное содержимое: signal={sig} recording_active={rec_active}"
            except Exception as exc:
                return False, f"не удалось прочитать/распарсить {info_path}: {exc!r}"
        time.sleep(0.5)
    return False, f"{info_path} не появился за {SHUTDOWN_INFO_WAIT_SEC:.0f}с"


def _preserve_failure_logs(tmp_dir: Path, log1: Path, log2: Path) -> None:
    dest = Path("/tmp/krab_ear_rescue_smoke_last_failure")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for src, name in ((log1, "backend_life1.log"), (log2, "backend_life2.log")):
            if src.exists():
                shutil.copyfile(src, dest / name)
        print(f"\n  (логи неудачного прогона скопированы в {dest})")
    except Exception as exc:
        print(f"\n  (не удалось сохранить логи неудачного прогона: {exc!r})")


def _finish(overall_ok: bool, tmp_dir: Path) -> int:
    print("\n" + "=" * 70)
    if overall_ok:
        print("  ALL GREEN — R1 rescue e2e smoke")
    else:
        print("  FAILED — R1 rescue e2e smoke (см. FAIL выше)")
        failed = [label for label, ok, _ in _STEPS if not ok]
        for label in failed:
            print(f"    - {label}")
    print("=" * 70)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
