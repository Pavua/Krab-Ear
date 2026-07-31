#!/usr/bin/env python3
"""Krab Ear memory baseline tracker.

Polls backend get_diagnostics + psutil RSS/VSZ for KrabEarAgent + backend
(service.py or main.py — see S3/Р9) + gigaam_worker.py + legacy rest_server.py
processes every N seconds, writes to CSV.

Usage:
    python3 scripts/memory_baseline.py --interval 60 --duration 3600 --output mem-baseline.csv
    python3 scripts/memory_baseline.py --once  # single snapshot (продовый сокет по умолчанию)

    # S3/Task10: throwaway-замеры канарейки — ОБЯЗАТЕЛЬНО одно из двух, никогда прод:
    python3 scripts/memory_baseline.py --once --data-dir /tmp/krab_ear_smoke_datadir
    python3 scripts/memory_baseline.py --once --socket /tmp/krab_ear_smoke_datadir/krabear.sock

    # S3/Task10: канарейка снимается НА рабочей машине владельца, где почти
    # всегда жив прод-агент — БЕЗ --pid throwaway-замер рискует поймать ЧУЖОЙ
    # (продовый) процесс с совпадающим cmdline-маркером (живой прогон это
    # подтвердил: throwaway in-process-конфигурация без единого своего
    # rest_server.py получила rest_rss_mb>0 от продового standalone-процесса
    # владельца). --pid <PID своего subprocess.Popen> ограничивает скоуп
    # ИЗВЕСТНЫМ деревом throwaway-процессов; повторяем для нескольких корней:
    python3 scripts/memory_baseline.py --once --data-dir /tmp/krab_ear_after --pid 12345
    python3 scripts/memory_baseline.py --once --data-dir /tmp/krab_ear_before --pid 12345 --pid 12399

Designed to be memory-safe — script itself should be <50MB RSS.
"""
import argparse
import csv
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import psutil
except ImportError:
    print("psutil required: pip install psutil", file=sys.stderr)
    sys.exit(2)


# S3/Task10: легаси стендэлон REST (rest_server.py) не ловился ДО этой правки —
# замер «до» слияния (два процесса: service.py/main.py + rest_server.py) видел
# только первый, поэтому «минус сотни МБ дубля» было нечем подтвердить числом.
_PROCESS_CMDLINE_MARKERS = (
    "KrabEarAgent",
    "KrabEar/backend/service.py",
    "KrabEar/main.py",
    "KrabEar/backend/rest_server.py",
    "gigaam_worker",
)


def _matches_krab_process(cmdline: str) -> bool:
    """True, если cmdline процесса относится к экосистеме Krab Ear."""
    return any(marker in cmdline for marker in _PROCESS_CMDLINE_MARKERS)


def _proc_entry(proc: "psutil.Process") -> dict:
    cmdline = " ".join(proc.cmdline() or [])
    mem = proc.memory_info()
    return {
        "pid": proc.pid,
        "name": proc.name(),
        "rss_mb": mem.rss / 1024 / 1024,
        "vsz_mb": mem.vms / 1024 / 1024,
        "cmd_short": cmdline.split("/")[-1][:50],
        "cmdline": cmdline,
    }


def _get_processes_scoped(pid_roots: list[int]) -> list[dict]:
    """S3/Task10 (живой прогон вскрыл): system-wide cmdline-скан ловит ЛЮБОЙ
    процесс на машине с совпадающей подстрокой — включая живой ПРОД-агент
    владельца, если throwaway-канарейка снимается на ЕГО ЖЕ рабочей машине
    (а это и есть основной сценарий: двухнедельная канарейка идёт НЕ на
    изолированном CI, а рядом с прод-инстансом владельца). Живой прогон на
    этой самой машине подтвердил утечку: throwaway in-process-конфигурация
    (без единого своего rest_server.py процесса) получила rest_rss_mb>0 —
    это оказался ЧУЖОЙ продовый standalone rest_server.py, матчнутый тем же
    маркером. Scoping по PID-дереву (вызывающая сторона передаёт PID
    СОБСТВЕННОГО throwaway-спавна, известный из subprocess.Popen.pid) —
    единственный способ отличить "мой процесс" от "процесс с похожим именем
    у кого-то ещё на этой же машине".
    """
    seen: set[int] = set()
    for root_pid in pid_roots:
        try:
            root = psutil.Process(root_pid)
        except psutil.NoSuchProcess:
            continue
        seen.add(root_pid)
        try:
            seen.update(child.pid for child in root.children(recursive=True))
        except psutil.NoSuchProcess:
            pass

    matches = []
    for pid in seen:
        try:
            matches.append(_proc_entry(psutil.Process(pid)))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


def get_processes(pid_roots: list[int] | None = None) -> list[dict]:
    """Find Krab Ear-related processes.

    pid_roots (S3/Task10) — когда задан, скоуп ограничивается ЭТИМ деревом
    процессов (root + psutil-потомки), а не всей машиной; см. докстринг
    _get_processes_scoped про живую находку утечки.
    """
    if pid_roots:
        return _get_processes_scoped(pid_roots)
    matches = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if _matches_krab_process(cmdline):
                matches.append({
                    "pid": proc.info["pid"],
                    "name": proc.info["name"],
                    "rss_mb": proc.info["memory_info"].rss / 1024 / 1024,
                    "vsz_mb": proc.info["memory_info"].vms / 1024 / 1024,
                    "cmd_short": cmdline.split("/")[-1][:50],
                    "cmdline": cmdline,
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


# Продовый сокет — единственное поведение по умолчанию, когда оператор не
# указал ни --socket, ни --data-dir. Это осознанный дефолт для исходного
# use-case скрипта (долгоживущий RAM-канарейка ПРОТИВ РЕАЛЬНОГО прод-агента
# владельца), а не промах — throwaway-замеры обязаны передавать один из двух
# флагов явно (см. main()).
_PROD_SOCKET_PATH = os.path.expanduser("~/Library/Application Support/KrabEar/krabear.sock")


def get_backend_diagnostics(sock_path: str | None) -> dict | None:
    """Query backend IPC get_diagnostics по ЯВНО переданному сокету.

    S3/Task10: раньше путь был жёстко закодирован на продовый сокет — throwaway
    замер (временный --data-dir) либо молча получал None, либо (что хуже) сам
    того не желая опрашивал живой backend владельца. Теперь вызывающая сторона
    обязана передать путь явно; None означает «источник не задан» — тихий
    возврат None, а не скрытый фоллбэк на прод.
    """
    if not sock_path or not os.path.exists(sock_path):
        return None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(sock_path)
        sock.sendall((json.dumps({"id": "baseline", "method": "get_diagnostics", "params": {}}) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode()).get("result", {})
    except Exception:
        return None


def take_snapshot(sock_path: str | None, pid_roots: list[int] | None = None) -> dict:
    """Single point-in-time snapshot.

    sock_path — куда идти за get_diagnostics; None допустим (снимок процессов
    без диагностики backend'а, например когда сокет ещё не поднялся).
    pid_roots — см. get_processes(): ограничивает скоуп ИЗВЕСТНЫМ деревом
    throwaway-процессов вместо всей машины (S3/Task10).
    """
    procs = get_processes(pid_roots)
    diag = get_backend_diagnostics(sock_path) or {}

    agent_rss = next((p["rss_mb"] for p in procs if "KrabEarAgent" in p["cmdline"]), 0)
    # S3/Task10: два непересекающихся паттерна — backend (service.py ИЛИ
    # main.py, ОДИН процесс в любой конфигурации) и легаси rest_server.py
    # (второй процесс ТОЛЬКО в standalone-режиме, исчезает после слияния).
    backend_rss = next(
        (p["rss_mb"] for p in procs
         if "KrabEar/backend/service.py" in p["cmdline"] or "KrabEar/main.py" in p["cmdline"]),
        0,
    )
    rest_rss = next((p["rss_mb"] for p in procs if "KrabEar/backend/rest_server.py" in p["cmdline"]), 0)
    worker_rss_total = sum(p["rss_mb"] for p in procs if "gigaam_worker" in p["cmdline"])

    return {
        "timestamp": datetime.now().isoformat(),
        "uptime_sec": diag.get("system", {}).get("uptime_sec", 0),
        "agent_rss_mb": agent_rss,
        "backend_rss_mb": backend_rss,
        "rest_rss_mb": rest_rss,
        "worker_rss_mb_total": worker_rss_total,
        # Сумма — прямое число для сравнения «до/после» слияния (обещание
        # серии M «минус сотни МБ дубля» подтверждается или опровергается
        # разницей total_rss_mb между двумя прогонами, а не на глаз).
        "total_rss_mb": agent_rss + backend_rss + rest_rss + worker_rss_total,
        "history_total_items": diag.get("history", {}).get("total_items", 0),
        "llm_circuit": diag.get("llm", {}).get("circuit_state", "unknown"),
    }


def _resolve_socket_path(args: argparse.Namespace) -> str:
    """--socket и --data-dir — явные throwaway-оверрайды; без них — продовый путь."""
    if args.socket_path and args.data_dir:
        raise SystemExit("--socket и --data-dir взаимоисключающие — источник сокета неоднозначен")
    if args.socket_path:
        return os.path.expanduser(args.socket_path)
    if args.data_dir:
        return str(Path(os.path.expanduser(args.data_dir)) / "krabear.sock")
    return _PROD_SOCKET_PATH


def main() -> int:
    ap = argparse.ArgumentParser(description="Krab Ear memory baseline tracker")
    ap.add_argument("--interval", type=int, default=60, help="seconds between snapshots")
    ap.add_argument("--duration", type=int, default=3600, help="total duration seconds (0=infinite)")
    ap.add_argument("--once", action="store_true", help="single snapshot then exit")
    ap.add_argument("--output", default="mem-baseline.csv", help="CSV path")
    ap.add_argument(
        "--socket", dest="socket_path", default=None,
        help="путь к Unix-сокету throwaway-инстанса (взаимоисключим с --data-dir)",
    )
    ap.add_argument(
        "--data-dir", dest="data_dir", default=None,
        help="каталог данных throwaway-инстанса — сокет выводится как <data-dir>/krabear.sock",
    )
    ap.add_argument(
        "--pid", dest="pid_roots", type=int, action="append", default=None,
        help=(
            "PID throwaway-процесса (свой subprocess.Popen.pid); повторяем для "
            "нескольких корней (напр. main.py + отдельный legacy rest_server.py "
            "в конфигурации 'до'). Скоуп замера ограничивается ЭТИМ деревом "
            "процессов, а не всей машиной — без него канарейка, снятая на "
            "рабочей машине владельца рядом с ЖИВЫМ прод-агентом, ловит ЧУЖОЙ "
            "процесс с совпадающим именем (живой прогон это подтвердил)."
        ),
    )
    args = ap.parse_args()

    sock_path = _resolve_socket_path(args)

    output_path = Path(args.output)
    fieldnames = [
        "timestamp", "uptime_sec", "agent_rss_mb", "backend_rss_mb", "rest_rss_mb",
        "worker_rss_mb_total", "total_rss_mb", "history_total_items", "llm_circuit",
    ]

    write_header = not output_path.exists()
    with output_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()

        if args.once:
            snap = take_snapshot(sock_path, pid_roots=args.pid_roots)
            writer.writerow(snap)
            print(f"snapshot written: {snap}")
            return 0

        start = time.monotonic()
        while True:
            snap = take_snapshot(sock_path, pid_roots=args.pid_roots)
            writer.writerow(snap)
            f.flush()
            print(
                f"[{snap['timestamp']}] "
                f"agent={snap['agent_rss_mb']:.0f}MB "
                f"backend={snap['backend_rss_mb']:.0f}MB "
                f"rest={snap['rest_rss_mb']:.0f}MB "
                f"worker={snap['worker_rss_mb_total']:.0f}MB "
                f"total={snap['total_rss_mb']:.0f}MB",
                flush=True,
            )

            if args.duration > 0 and (time.monotonic() - start) >= args.duration:
                break
            time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
