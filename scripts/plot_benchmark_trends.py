#!/usr/bin/env python3
"""Plot benchmark trends as PNG line charts.

Reads .benchmarks/history.jsonl and writes one PNG per benchmark into
docs/benchmarks/. Requires matplotlib.

Usage:
    python scripts/plot_benchmark_trends.py [--history PATH] [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_DEFAULT_HISTORY = (
    Path(__file__).resolve().parent.parent / ".benchmarks" / "history.jsonl"
)
_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "docs" / "benchmarks"


def _load_history(path: Path) -> dict[str, list[dict]]:
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
    for name in groups:
        groups[name].sort(key=lambda e: e.get("ts", ""))
    return groups


def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def _plot_one(
    name: str,
    entries: list[dict],
    output_dir: Path,
) -> Path:
    try:
        import matplotlib  # noqa: PLC0415
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415
    except ImportError:
        raise ImportError("matplotlib is required: pip install matplotlib")

    times = [e["elapsed_sec"] for e in entries]
    labels = [e.get("commit", "?") for e in entries]

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(range(len(times)), times, marker="o", linewidth=1.5, markersize=4)
    ax.set_title(name, fontsize=10)
    ax.set_xlabel("Run #")
    ax.set_ylabel("Elapsed (s)")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{_safe_filename(name)}.png"
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        type=Path,
        default=_DEFAULT_HISTORY,
        help="Path to history.jsonl (default: .benchmarks/history.jsonl)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_DEFAULT_OUT,
        help="Output directory for PNG files (default: docs/benchmarks/)",
    )
    args = parser.parse_args(argv)

    groups = _load_history(args.history)
    if not groups:
        print("No benchmark history found.", file=sys.stderr)
        return 1

    for name, entries in sorted(groups.items()):
        if len(entries) < 2:
            print(f"  Skipping '{name}' (only {len(entries)} run(s))")
            continue
        try:
            out = _plot_one(name, entries, args.output_dir)
            print(f"  Wrote {out}")
        except ImportError as exc:
            print(f"  {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"  Error plotting '{name}': {exc}", file=sys.stderr)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
