#!/usr/bin/env python3
"""Krab Ear memory baseline tracker.

Polls backend get_diagnostics + psutil RSS/VSZ for KrabEarAgent + backend
(service.py or main.py — see S3/Р9) + gigaam_worker.py processes every N
seconds, writes to CSV.

Usage:
    python3 scripts/memory_baseline.py --interval 60 --duration 3600 --output mem-baseline.csv
    python3 scripts/memory_baseline.py --once  # single snapshot

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


def get_processes() -> list[dict]:
    """Find Krab Ear-related processes."""
    matches = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            # S3/Р9: плист/BackendSupervisor теперь спавнят KrabEar/main.py, а
            # не backend/service.py напрямую — старое имя оставлено для
            # процессов, поднятых до переустановки юнита (sibling-сайт
            # той же проверки в backend/service.py::_handle_get_memory_stats).
            if any(s in cmdline for s in (
                "KrabEarAgent", "KrabEar/backend/service.py", "KrabEar/main.py", "gigaam_worker",
            )):
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


def get_backend_diagnostics() -> dict | None:
    """Query backend IPC get_diagnostics. Returns None on failure."""
    sock_path = os.path.expanduser("~/Library/Application Support/KrabEar/krabear.sock")
    if not os.path.exists(sock_path):
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


def take_snapshot() -> dict:
    """Single point-in-time snapshot."""
    procs = get_processes()
    diag = get_backend_diagnostics() or {}

    return {
        "timestamp": datetime.now().isoformat(),
        "uptime_sec": diag.get("system", {}).get("uptime_sec", 0),
        "agent_rss_mb": next((p["rss_mb"] for p in procs if "KrabEarAgent" in p["cmdline"]), 0),
        "backend_rss_mb": next((p["rss_mb"] for p in procs if "KrabEar/backend/service.py" in p["cmdline"]), 0),
        "worker_rss_mb_total": sum(p["rss_mb"] for p in procs if "gigaam_worker" in p["cmdline"]),
        "history_total_items": diag.get("history", {}).get("total_items", 0),
        "llm_circuit": diag.get("llm", {}).get("circuit_state", "unknown"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Krab Ear memory baseline tracker")
    ap.add_argument("--interval", type=int, default=60, help="seconds between snapshots")
    ap.add_argument("--duration", type=int, default=3600, help="total duration seconds (0=infinite)")
    ap.add_argument("--once", action="store_true", help="single snapshot then exit")
    ap.add_argument("--output", default="mem-baseline.csv", help="CSV path")
    args = ap.parse_args()

    output_path = Path(args.output)
    fieldnames = [
        "timestamp", "uptime_sec", "agent_rss_mb", "backend_rss_mb",
        "worker_rss_mb_total", "history_total_items", "llm_circuit",
    ]

    write_header = not output_path.exists()
    with output_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()

        if args.once:
            snap = take_snapshot()
            writer.writerow(snap)
            print(f"snapshot written: {snap}")
            return 0

        start = time.monotonic()
        while True:
            snap = take_snapshot()
            writer.writerow(snap)
            f.flush()
            print(
                f"[{snap['timestamp']}] "
                f"agent={snap['agent_rss_mb']:.0f}MB "
                f"backend={snap['backend_rss_mb']:.0f}MB "
                f"worker={snap['worker_rss_mb_total']:.0f}MB",
                flush=True,
            )

            if args.duration > 0 and (time.monotonic() - start) >= args.duration:
                break
            time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
