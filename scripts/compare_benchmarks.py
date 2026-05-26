#!/usr/bin/env python3
"""Compare benchmark history and detect performance regressions.

Reads .benchmarks/history.jsonl, computes per-benchmark stats (last 10 runs,
mean, p50, p90), compares last run vs previous, exits 1 on >20% regression.

Usage:
    python scripts/compare_benchmarks.py [--history PATH] [--threshold 0.20] [--json]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import NamedTuple

_DEFAULT_HISTORY = (
    Path(__file__).resolve().parent.parent / ".benchmarks" / "history.jsonl"
)
_WINDOW = 10
_DEFAULT_THRESHOLD = 0.20


class BenchStats(NamedTuple):
    name: str
    last_elapsed: float
    prev_elapsed: float | None
    mean: float
    p50: float
    p90: float
    count: int
    regression_pct: float | None  # positive = slower


def _load_history(path: Path) -> dict[str, list[dict]]:
    """Return {bench_name: [entries sorted oldest→newest]}."""
    if not path.exists():
        return {}
    groups: dict[str, list[dict]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = entry.get("bench_name", "")
            groups.setdefault(name, []).append(entry)
    # Sort each group by ts ascending
    for name in groups:
        groups[name].sort(key=lambda e: e.get("ts", ""))
    return groups


def _percentile(data: list[float], pct: float) -> float:
    """Simple percentile (linear interpolation, 0-1 scale)."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = pct * (len(sorted_data) - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= len(sorted_data):
        return sorted_data[-1]
    frac = idx - lo
    return sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo])


def _compute_stats(name: str, entries: list[dict]) -> BenchStats:
    window = entries[-_WINDOW:]
    times = [e["elapsed_sec"] for e in window]
    last = times[-1]
    prev = times[-2] if len(times) >= 2 else None
    mean = statistics.mean(times)
    p50 = _percentile(times, 0.50)
    p90 = _percentile(times, 0.90)
    if prev is not None and prev > 0:
        regression_pct = (last - prev) / prev
    else:
        regression_pct = None
    return BenchStats(
        name=name,
        last_elapsed=last,
        prev_elapsed=prev,
        mean=mean,
        p50=p50,
        p90=p90,
        count=len(entries),
        regression_pct=regression_pct,
    )


def _format_table(stats_list: list[BenchStats]) -> str:
    if not stats_list:
        return "No benchmark data found.\n"
    rows = []
    header = (
        f"{'Benchmark':<55} {'Last':>7} {'Prev':>7} "
        f"{'Mean':>7} {'p50':>7} {'p90':>7} {'N':>4} {'Δ':>8}"
    )
    sep = "-" * len(header)
    rows.append(header)
    rows.append(sep)
    for s in sorted(stats_list, key=lambda x: x.name):
        prev_str = f"{s.prev_elapsed:.3f}s" if s.prev_elapsed is not None else "  n/a "
        delta_str = (
            f"{s.regression_pct:+.1%}" if s.regression_pct is not None else "  n/a"
        )
        rows.append(
            f"{s.name:<55} {s.last_elapsed:>6.3f}s {prev_str:>7} "
            f"{s.mean:>6.3f}s {s.p50:>6.3f}s {s.p90:>6.3f}s {s.count:>4} {delta_str:>8}"
        )
    rows.append(sep)
    return "\n".join(rows) + "\n"


def _check_regressions(
    stats_list: list[BenchStats], threshold: float
) -> list[BenchStats]:
    return [
        s
        for s in stats_list
        if s.regression_pct is not None and s.regression_pct > threshold
    ]


def _format_json(stats_list: list[BenchStats], regressions: list[BenchStats], threshold: float) -> str:
    """Return JSON representation of the comparison result."""
    benchmarks = [
        {
            "name": s.name,
            "last_elapsed": s.last_elapsed,
            "prev_elapsed": s.prev_elapsed,
            "mean": s.mean,
            "p50": s.p50,
            "p90": s.p90,
            "count": s.count,
            "regression_pct": s.regression_pct,
        }
        for s in sorted(stats_list, key=lambda x: x.name)
    ]
    regression_names = [s.name for s in regressions]
    result = {
        "threshold": threshold,
        "regressions": regression_names,
        "benchmarks": benchmarks,
    }
    return json.dumps(result, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        type=Path,
        default=_DEFAULT_HISTORY,
        help="Path to history.jsonl (default: .benchmarks/history.jsonl)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=_DEFAULT_THRESHOLD,
        help="Regression threshold fraction (default: 0.20 = 20%%)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit JSON instead of human-readable table (useful for CI dashboards)",
    )
    args = parser.parse_args(argv)

    groups = _load_history(args.history)
    if not groups:
        print("No benchmark history found. Run benchmarks first.", file=sys.stderr)
        return 0

    stats_list = [_compute_stats(name, entries) for name, entries in groups.items()]
    regressions = _check_regressions(stats_list, args.threshold)

    if args.json_output:
        print(_format_json(stats_list, regressions, args.threshold))
    else:
        print("\n## Benchmark Comparison Report\n")
        print(_format_table(stats_list))

    if regressions:
        if not args.json_output:
            print(
                f"## REGRESSION DETECTED (threshold: {args.threshold:.0%})\n",
                file=sys.stderr,
            )
        for s in regressions:
            delta = s.regression_pct or 0.0
            msg = (
                f"  ::error title=Benchmark regression::{s.name}: "
                f"{s.prev_elapsed:.3f}s -> {s.last_elapsed:.3f}s "
                f"(+{delta:.1%})"
            )
            print(msg, file=sys.stderr)
        return 1

    if not args.json_output:
        print("No regressions detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
