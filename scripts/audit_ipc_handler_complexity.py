#!/usr/bin/env python3
"""audit_ipc_handler_complexity.py — static analysis of IPC handler complexity.

Parses KrabEar/backend/service.py with stdlib `ast` and, for every
`_handle_*` method, computes:
  - LOC (lines in the method body)
  - cyclomatic complexity (if/elif/while/for/try/except each count +1; base = 1)
  - risky calls: subprocess.run, requests., socket., time.sleep
  - lock usage: `with self._lock`, `with mlx_lock()`
  - dispatch type: "delegated" if the body is a single `return self._<svc>.*`
    call; otherwise "inline"

Emits JSON report to stdout.  Without --json also prints a human-readable
markdown summary.

Usage:
    python scripts/audit_ipc_handler_complexity.py [--json] [path/to/service.py]
"""
from __future__ import annotations

import ast
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_SERVICE_PATH = Path(__file__).parent.parent / "KrabEar" / "backend" / "service.py"

# AST node types that add to cyclomatic complexity (each counts +1)
_COMPLEXITY_NODES = (
    ast.If,
    ast.While,
    ast.For,
    ast.ExceptHandler,
    ast.Try,
    ast.With,         # conservative: context managers can hide blocking ops
    ast.AsyncFor,
    ast.AsyncWith,
    ast.IfExp,        # ternary expression
    ast.comprehension,  # list/set/dict comprehensions
)

# Patterns for risky identifiers/attributes
_RISKY_PATTERNS = {
    "subprocess.run": ("subprocess", "run"),
    "subprocess.call": ("subprocess", "call"),
    "subprocess.Popen": ("subprocess", "Popen"),
    "requests.get": ("requests", "get"),
    "requests.post": ("requests", "post"),
    "requests.put": ("requests", "put"),
    "requests.request": ("requests", "request"),
    "socket.": ("socket",),
    "time.sleep": ("time", "sleep"),
    "urllib": ("urllib",),
}

# Patterns for lock usage (text-based in node source — simpler than full AST walk)
_LOCK_PATTERNS = [
    "self._lock",
    "mlx_lock()",
    "_ipc_throttle",
]


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _count_complexity(node: ast.AST) -> int:
    """Cyclomatic complexity: base 1 + one per branching node."""
    count = 1
    for child in ast.walk(node):
        if isinstance(child, _COMPLEXITY_NODES):
            count += 1
    return count


def _risky_calls(node: ast.AST) -> list[str]:
    """Return list of risky call patterns found anywhere in the subtree."""
    found: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Attribute):
            continue
        # Build dotted chain: e.g. subprocess.run → ["subprocess", "run"]
        parts: list[str] = []
        cur: ast.expr = child
        while isinstance(cur, ast.Attribute):
            parts.insert(0, cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.insert(0, cur.id)
        for label, pat in _RISKY_PATTERNS.items():
            if len(parts) >= len(pat) and tuple(parts[: len(pat)]) == pat:
                found.add(label)
    return sorted(found)


def _lock_usage(source_lines: list[str]) -> list[str]:
    """Check raw source lines for lock patterns (text search, fast)."""
    used: list[str] = []
    text = "\n".join(source_lines)
    for pat in _LOCK_PATTERNS:
        if pat in text:
            used.append(pat)
    return used


def _is_delegated(func: ast.FunctionDef, source_lines: list[str]) -> bool:
    """True if the entire body is a single `return self._<svc>.handle_*` call.

    A delegated stub has exactly one statement (or docstring + one statement)
    and the return expression is `self.<something>.handle_<something>(...)`.
    """
    stmts = func.body
    # Strip leading docstring
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant):
        stmts = stmts[1:]
    if len(stmts) != 1:
        return False
    stmt = stmts[0]
    if not isinstance(stmt, ast.Return):
        return False
    val = stmt.value
    if not isinstance(val, ast.Call):
        return False
    func_attr = val.func
    if not isinstance(func_attr, ast.Attribute):
        return False
    # func_attr.value should be `self.<svc>` (another Attribute or Name)
    inner = func_attr.value
    if not isinstance(inner, ast.Attribute):
        return False
    if not isinstance(inner.value, ast.Name) or inner.value.id != "self":
        return False
    # The called method should start with "handle_"
    return func_attr.attr.startswith("handle_")


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


def analyse_service(path: Path) -> list[dict[str, Any]]:
    """Parse service.py and return list of handler metric dicts."""
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))

    handlers: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_handle_"):
            continue

        start = node.lineno
        end = node.end_lineno or start
        loc = end - start + 1

        # Lines of the method body (for text-search of lock patterns)
        body_lines = source_lines[start - 1 : end]

        complexity = _count_complexity(node)
        risky = _risky_calls(node)
        locks = _lock_usage(body_lines)
        delegated = _is_delegated(node, body_lines)

        handlers.append(
            {
                "name": node.name,
                "start_line": start,
                "end_line": end,
                "loc": loc,
                "complexity": complexity,
                "risky_calls": risky,
                "locks": locks,
                "dispatch": "delegated" if delegated else "inline",
            }
        )

    return sorted(handlers, key=lambda h: h["name"])


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _top_n_by(handlers: list[dict], key: str, n: int = 10) -> list[dict]:
    """Return top-n handlers sorted descending by key, excluding delegated."""
    inline = [h for h in handlers if h["dispatch"] == "inline"]
    return sorted(inline, key=lambda h: h[key], reverse=True)[:n]


def build_json_report(handlers: list[dict]) -> dict:
    """Build full JSON report structure."""
    inline = [h for h in handlers if h["dispatch"] == "inline"]
    delegated = [h for h in handlers if h["dispatch"] == "delegated"]

    risky = [h for h in inline if h["risky_calls"]]
    locking = [h for h in inline if h["locks"]]

    return {
        "summary": {
            "total_handlers": len(handlers),
            "inline": len(inline),
            "delegated": len(delegated),
            "with_risky_calls": len(risky),
            "with_locks": len(locking),
        },
        "top10_by_loc": _top_n_by(handlers, "loc"),
        "top10_by_complexity": _top_n_by(handlers, "complexity"),
        "risky_handlers": sorted(
            risky, key=lambda h: (len(h["risky_calls"]), h["complexity"]), reverse=True
        ),
        "all_handlers": handlers,
    }


def _table_row(h: dict, rank: int | None = None) -> str:
    prefix = f"{rank}." if rank is not None else " "
    risky = ", ".join(h["risky_calls"]) if h["risky_calls"] else "-"
    locks = ", ".join(h["locks"]) if h["locks"] else "-"
    return (
        f"| {prefix:<4} | `{h['name']}`{' ' * max(0, 60 - len(h['name']))} "
        f"| {h['loc']:>4} | {h['complexity']:>5} | {risky:<32} | {locks:<20} |"
    )


def build_markdown(report: dict, service_path: Path) -> str:
    s = report["summary"]
    lines: list[str] = []

    lines.append("# IPC Handler Complexity Audit — wave763")
    lines.append("")
    lines.append(f"**Source**: `{service_path}`  ")
    lines.append(f"**Date**: 2026-05-26  ")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Count |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total `_handle_*` methods | {s['total_handlers']} |")
    lines.append(f"| Inline (need attention) | {s['inline']} |")
    lines.append(f"| Delegated stubs | {s['delegated']} |")
    lines.append(f"| With risky calls (subprocess/network/sleep) | {s['with_risky_calls']} |")
    lines.append(f"| With lock usage | {s['with_locks']} |")
    lines.append("")

    # -- Top 10 by LOC
    lines.append("## Top 10 Inline Handlers by LOC (excluding delegated stubs)")
    lines.append("")
    lines.append("| Rank | Handler | LOC | CC | Risky calls | Locks |")
    lines.append("|------|---------|-----|-----|-------------|-------|")
    for i, h in enumerate(report["top10_by_loc"], 1):
        lines.append(_table_row(h, i))
    lines.append("")

    # -- Top 10 by complexity
    lines.append("## Top 10 Inline Handlers by Cyclomatic Complexity")
    lines.append("")
    lines.append("| Rank | Handler | LOC | CC | Risky calls | Locks |")
    lines.append("|------|---------|-----|-----|-------------|-------|")
    for i, h in enumerate(report["top10_by_complexity"], 1):
        lines.append(_table_row(h, i))
    lines.append("")

    # -- Risky handlers
    if report["risky_handlers"]:
        lines.append("## High-Risk Handlers (subprocess / network / sleep calls)")
        lines.append("")
        lines.append(
            "> These handlers make blocking or external calls on the IPC thread. "
            "Consider delegating to a background thread or extracted service."
        )
        lines.append("")
        lines.append("| Handler | LOC | CC | Risky calls | Locks |")
        lines.append("|---------|-----|-----|-------------|-------|")
        for h in report["risky_handlers"]:
            risky = ", ".join(h["risky_calls"])
            locks = ", ".join(h["locks"]) if h["locks"] else "-"
            lines.append(
                f"| `{h['name']}` | {h['loc']} | {h['complexity']} | {risky} | {locks} |"
            )
        lines.append("")
    else:
        lines.append("## High-Risk Handlers")
        lines.append("")
        lines.append("No subprocess/network/sleep calls found in inline handlers.")
        lines.append("")

    # -- Extraction candidates
    lines.append("## Extraction Candidates")
    lines.append("")
    lines.append(
        "Handlers with LOC > 30 or CC > 8 that remain inline are the strongest "
        "candidates for extraction into a dedicated service."
    )
    lines.append("")
    candidates = [
        h for h in report["all_handlers"]
        if h["dispatch"] == "inline" and (h["loc"] > 30 or h["complexity"] > 8)
    ]
    candidates.sort(key=lambda h: (h["loc"] + h["complexity"] * 5), reverse=True)
    if candidates:
        lines.append("| Handler | LOC | CC | Risk score |")
        lines.append("|---------|-----|-----|------------|")
        for h in candidates:
            risk = h["loc"] + h["complexity"] * 5
            lines.append(f"| `{h['name']}` | {h['loc']} | {h['complexity']} | {risk} |")
    else:
        lines.append("No candidates exceed the LOC>30 or CC>8 threshold.")
    lines.append("")

    lines.append("## Definitions")
    lines.append("")
    lines.append("- **LOC**: lines of code (entire method, including docstring).")
    lines.append("- **CC**: cyclomatic complexity — base 1 + 1 per `if/elif/while/for/try/except/with/ternary/comprehension`.")
    lines.append("- **Risky calls**: `subprocess.run`, `requests.*`, `socket.*`, `time.sleep`, `urllib` — blocking or network I/O on the IPC thread.")
    lines.append("- **Delegated stub**: body is exactly `return self._<svc>.handle_*(...)` — already extracted, excluded from top-N tables.")
    lines.append("- **Risk score**: LOC + CC × 5 (used for extraction priority ranking).")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    args = sys.argv[1:]
    emit_json_only = "--json" in args
    remaining = [a for a in args if not a.startswith("--")]
    service_path = Path(remaining[0]) if remaining else DEFAULT_SERVICE_PATH

    if not service_path.exists():
        print(f"ERROR: cannot find {service_path}", file=sys.stderr)
        sys.exit(1)

    handlers = analyse_service(service_path)
    report = build_json_report(handlers)

    if emit_json_only:
        print(json.dumps(report, indent=2))
    else:
        # Print JSON first, then markdown summary to stderr so it can be
        # captured independently:  script.py > out.json  or  script.py 2>summary.md
        print(json.dumps(report, indent=2))
        md = build_markdown(report, service_path)
        print("\n" + "=" * 78, file=sys.stderr)
        print(md, file=sys.stderr)


if __name__ == "__main__":
    main()
