"""Soak-тест IPC backend Krab Ear.

Сценарий проверяет длительную стабильность локального backend без участия UI:
1) поднимает service.py в отдельном процессе;
2) прогоняет тысячи IPC-запросов add/set_status/search/page/delete/compact;
3) собирает метрики и формирует JSON + Markdown отчёты.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import random
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any


@dataclass
class Metrics:
    add_ms: list[float]
    status_ms: list[float]
    page_ms: list[float]
    search_ms: list[float]
    delete_ms: list[float]
    compact_ms: list[float]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    idx = int(round((p / 100.0) * (len(sorted_values) - 1)))
    return sorted_values[idx]


def latency_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "avg": 0.0}
    return {
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "avg": round(statistics.fmean(values), 3),
    }


def ipc_call(socket_path: Path, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "id": f"soak-{time.time_ns()}",
        "method": method,
        "params": params or {},
    }

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(socket_path))
        conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            raw = conn.recv(8192)
            if not raw:
                break
            chunks.append(raw)
            if b"\n" in raw:
                break

    merged = b"".join(chunks).decode("utf-8", errors="replace")
    line = merged.split("\n", 1)[0].strip()
    if not line:
        raise RuntimeError("Пустой ответ от backend")
    response = json.loads(line)
    if not response.get("ok", False):
        err = response.get("error", {})
        raise RuntimeError(f"{err.get('code')}: {err.get('message')}")
    return response


def wait_backend(socket_path: Path, timeout_sec: float = 12.0) -> None:
    start = time.perf_counter()
    while time.perf_counter() - start < timeout_sec:
        if socket_path.exists():
            try:
                ipc_call(socket_path, "ping")
                return
            except Exception:
                pass
        time.sleep(0.2)
    raise RuntimeError("Backend не поднялся вовремя")


def write_reports(summary: dict[str, Any], report_path: Path) -> tuple[Path, Path]:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = report_path.with_suffix(".md")
    md = []
    md.append(f"# Soak Backend Report — {summary['created_at']}")
    md.append("")
    md.append("## Summary")
    md.append(f"- status: **{summary['status']}**")
    md.append(f"- cycles: `{summary['cycles']}`")
    md.append(f"- seed: `{summary['seed']}`")
    md.append(f"- crash_count: `{summary['crash_count']}`")
    md.append(f"- paste_success_rate: `{summary['paste_success_rate']:.3f}`")
    md.append("")
    md.append("## Latency (ms)")
    md.append("| operation | p50 | p95 | avg |")
    md.append("|---|---:|---:|---:|")
    for op, stats in summary["latency_ms"].items():
        md.append(f"| {op} | {stats['p50']} | {stats['p95']} | {stats['avg']} |")
    md.append("")

    if summary.get("errors"):
        md.append("## Errors")
        for err in summary["errors"]:
            md.append(f"- {err}")
        md.append("")

    md.append("## Files")
    md.append(f"- json: `{report_path.name}`")
    md.append(f"- md: `{md_path.name}`")
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    index_path = report_path.parent / "SOAK_BACKEND_INDEX.md"
    if not index_path.exists():
        index_path.write_text(
            "# Soak Backend Index\n\n| created_at | status | cycles | paste_success_rate | crash_count | report |\n|---|---|---:|---:|---:|---|\n",
            encoding="utf-8",
        )

    rel_report = report_path.name
    line = (
        f"| {summary['created_at']} | {summary['status']} | {summary['cycles']} | "
        f"{summary['paste_success_rate']:.3f} | {summary['crash_count']} | {rel_report} |\n"
    )
    with index_path.open("a", encoding="utf-8") as fh:
        fh.write(line)

    return report_path, md_path


def run_soak(cycles: int, seed: int, report_path: Path | None) -> dict[str, Any]:
    random.seed(seed)

    with tempfile.TemporaryDirectory(prefix="krabear_soak_") as tmp:
        data_dir = Path(tmp) / "data"
        socket_path = data_dir / "krabear.sock"

        service_script = Path(__file__).resolve().parents[1] / "backend" / "service.py"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(service_script),
                "--data-dir",
                str(data_dir),
                "--socket-path",
                str(socket_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        metrics = Metrics([], [], [], [], [], [])
        ids: list[str] = []
        errors: list[str] = []
        crash_count = 0
        status = "ok"

        status_updates_total = 0
        status_updates_ok = 0

        try:
            wait_backend(socket_path)

            for idx in range(cycles):
                if proc.poll() is not None:
                    crash_count += 1
                    status = "failed"
                    errors.append(f"Backend завершился раньше времени, code={proc.returncode}, idx={idx}")
                    break

                try:
                    t0 = time.perf_counter()
                    add_result = ipc_call(
                        socket_path,
                        "add_history_item",
                        {
                            "text": f"soak entry {idx} random {random.randint(1, 999_999)}",
                            "paste_status": "failed",
                        },
                    )
                    metrics.add_ms.append((time.perf_counter() - t0) * 1000)

                    item_id = ((add_result.get("result") or {}).get("id"))
                    if isinstance(item_id, str):
                        ids.append(item_id)

                        # Эмулируем реальную жизнь: часть вставок "ok", часть "failed".
                        simulated_paste_ok = random.random() < 0.82
                        new_status = "ok" if simulated_paste_ok else "failed"
                        t0 = time.perf_counter()
                        ipc_call(socket_path, "set_paste_status", {"id": item_id, "paste_status": new_status})
                        metrics.status_ms.append((time.perf_counter() - t0) * 1000)
                        status_updates_total += 1
                        if simulated_paste_ok:
                            status_updates_ok += 1

                    if idx % 20 == 0:
                        t0 = time.perf_counter()
                        ipc_call(socket_path, "get_history_page", {"cursor": None, "limit": 50})
                        metrics.page_ms.append((time.perf_counter() - t0) * 1000)

                    if idx % 30 == 0:
                        t0 = time.perf_counter()
                        ipc_call(socket_path, "search_history", {"query": "soak entry", "cursor": None, "limit": 50})
                        metrics.search_ms.append((time.perf_counter() - t0) * 1000)

                    if idx % 50 == 0 and ids:
                        t0 = time.perf_counter()
                        delete_id = random.choice(ids)
                        ipc_call(socket_path, "delete_history_item", {"id": delete_id})
                        metrics.delete_ms.append((time.perf_counter() - t0) * 1000)

                    if idx > 0 and idx % 300 == 0:
                        t0 = time.perf_counter()
                        ipc_call(socket_path, "compact_history", {})
                        metrics.compact_ms.append((time.perf_counter() - t0) * 1000)
                except Exception as exc:
                    status = "failed"
                    errors.append(f"idx={idx}: {exc}")
                    break

            final_page_count = 0
            try:
                final_page = ipc_call(socket_path, "get_history_page", {"cursor": None, "limit": 50})
                final_page_count = len(((final_page.get("result") or {}).get("items") or []))
            except Exception as exc:
                status = "failed"
                errors.append(f"final_page_error: {exc}")

            paste_success_rate = (
                float(status_updates_ok) / float(status_updates_total)
                if status_updates_total > 0
                else 0.0
            )

            summary = {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "cycles": cycles,
                "seed": seed,
                "status": status,
                "final_page_count": final_page_count,
                "crash_count": crash_count,
                "paste_success_rate": round(paste_success_rate, 6),
                "latency_ms": {
                    "add": latency_stats(metrics.add_ms),
                    "set_paste_status": latency_stats(metrics.status_ms),
                    "page": latency_stats(metrics.page_ms),
                    "search": latency_stats(metrics.search_ms),
                    "delete": latency_stats(metrics.delete_ms),
                    "compact": latency_stats(metrics.compact_ms),
                },
                "ops_count": {
                    "add": len(metrics.add_ms),
                    "set_paste_status": len(metrics.status_ms),
                    "page": len(metrics.page_ms),
                    "search": len(metrics.search_ms),
                    "delete": len(metrics.delete_ms),
                    "compact": len(metrics.compact_ms),
                },
                "errors": errors,
            }

            if report_path:
                write_reports(summary, report_path)

            return summary
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description="Soak-тест IPC backend Krab Ear")
    parser.add_argument("--cycles", type=int, default=1000, help="Число циклов IPC")
    parser.add_argument("--seed", type=int, default=42, help="Seed для random")
    parser.add_argument("--report", default=None, help="Путь к JSON-отчёту")
    args = parser.parse_args()

    report_path = Path(args.report).expanduser() if args.report else None
    summary = run_soak(cycles=max(1, args.cycles), seed=args.seed, report_path=report_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if summary.get("status") != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()
