#!/usr/bin/env python3
"""backend_log_digest.py — расширенный анализатор лога бэкенда Krab Ear.

Категории (Wave 440):
  1. ERROR / CRITICAL / Traceback  (оригинал)
  2. WARNING с ключевыми словами   (оригинал)
  3. MLX subprocess crashes        (НОВОЕ)
  4. Audio device disconnect        (НОВОЕ)
  5. Settings backup key anomaly   (НОВОЕ)
  6. REST server 5xx errors        (НОВОЕ)
  7. Disk space trajectory         (НОВОЕ)
  8. History.ndjson growth rate    (НОВОЕ)

Запуск:
  python3 scripts/backend_log_digest.py [--log PATH] [--lines N] [--out PATH]

Exit code:
  0 — healthy (no alerts)
  1 — ≥1 ALERT triggered
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_PATTERNS: dict[str, list[str]] = {
    "error": [
        r"ERROR",
        r"CRITICAL",
        r"Traceback \(most recent call last\)",
    ],
    "warning_keywords": [
        r"WARNING.*(?:warmup|rewriter|circuit|timeout|reconnect)",
        r"(?:warmup|rewriter|circuit|timeout|reconnect).*WARNING",
    ],
    "mlx_crash": [
        r"[Mm][Ll][Xx].*subprocess.*kill",
        r"[Mm][Ll][Xx][Ww]atch.*process.*exited.*-9",
        r"mlx_subprocess.*returncode\s*=?\s*-[0-9]",
        r"subprocess_timeout.*mlx",
        r"MLX.*SIGKILL",
        r"MLX.*SIGABRT",
        r"killed after timeout",
    ],
    "audio_device": [
        r"PortAudioError.*[Ii]nput overflowed",
        r"[Dd]evice[Uu]navailable",
        r"[Aa]udio[Rr]ecorder.*device.*disappear",
        r"recording interrupted.*device",
        r"InputOverflowError",
        r"sounddevice.*Error",
    ],
    "settings_keys": [
        r"[Ss]ettings[Bb]ackup.*keys\s*=\s*([0-9]+)",
        r"settings.*keys.*?([0-9]{3,})",
        r"[Ss]ettings[Vv]alidat.*unknown key",
        r"unexpected.*settings.*key.*count",
    ],
    "rest_5xx": [
        r'"(?:GET|POST|PUT|DELETE|PATCH) [^ ]+ HTTP/[0-9.]+" 5[0-9]{2}',
        r"rest_server.*5[0-9]{2}",
        r"rest_server.*[Ii]nternal [Ss]erver [Ee]rror",
        r"flask.*500",
    ],
    "disk_space": [
        r"[Dd]isk[Ss]pace[Mm]onitor.*free[_]?gb\s*=\s*([0-9.]+)",
        r"free space.*?([0-9]+\.[0-9]+)\s*GB",
        r"disk.*space.*below.*threshold",
        r"free.*?([0-9]+\.[0-9]+)\s*GB.*warning",
    ],
    "history_growth": [
        r"[Ss]tate[Ss]tore.*items\s*=\s*([0-9]+)",
        r"[Ss]tate[Ss]tore.*loaded.*?([0-9]+)\s*items",
        r"history\.ndjson.*size\s*=\s*([0-9]+)",
        r"compaction.*skipped",
        r"history.*?([0-9]{4,})\s*entries",
    ],
}

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

THRESH = {
    "error_count": 10,
    "warning_count": 20,
    "warning_keyword_count": 10,
    "mlx_crash_count": 1,
    "audio_device_count": 1,
    "settings_keys_max": 190,
    "rest_5xx_count": 3,
    "disk_free_gb_min": 0.5,
    "history_item_max": 5000,
    "compaction_skip_max": 2,
    "history_size_bytes_max": 50 * 1024 * 1024,  # 50 MB
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

class DigestResult(NamedTuple):
    alerts: list[str]
    sections: dict[str, str]
    error_count: int
    warning_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p) for p in patterns]


_COMPILED: dict[str, list[re.Pattern]] = {
    k: _compile(v) for k, v in _PATTERNS.items()
}


def _matches_any(line: str, category: str) -> bool:
    return any(pat.search(line) for pat in _COMPILED[category])


def _first_capture(line: str, category: str) -> str | None:
    """Return first capturing group match across all patterns in category."""
    for pat in _COMPILED[category]:
        m = pat.search(line)
        if m and m.lastindex:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyse(lines: list[str]) -> DigestResult:
    alerts: list[str] = []
    sections: dict[str, str] = {}

    # ---- 1. Errors ---------------------------------------------------------
    error_lines: list[str] = [ln for ln in lines if _matches_any(ln, "error")]
    error_count = len(error_lines)

    # Group by first distinctive token after ERROR/CRITICAL
    error_categories: dict[str, int] = defaultdict(int)
    for ln in error_lines:
        m = re.search(r"(?:ERROR|CRITICAL)\s+(.+?)(?:\s*$)", ln)
        if m:
            key = m.group(1).strip()[:60]
        else:
            key = ln.strip()[:60]
        error_categories[key] += 1

    top_errors = sorted(error_categories.items(), key=lambda x: -x[1])[:5]
    err_lines_out = [f"- ({cnt}×) `{k}`" for k, cnt in top_errors] or ["- None"]
    sections["errors"] = (
        f"## ERROR Summary\n\nTotal: {error_count}\n\n" + "\n".join(err_lines_out)
    )
    if error_count > THRESH["error_count"]:
        alerts.append(f"error_count={error_count} > {THRESH['error_count']}")

    # ---- 2. Warnings -------------------------------------------------------
    warn_lines: list[str] = [ln for ln in lines if "WARNING" in ln]
    warn_keyword_lines = [ln for ln in warn_lines if _matches_any(ln, "warning_keywords")]
    warning_count = len(warn_lines)

    sections["warnings"] = (
        f"## WARNING Summary\n\nTotal WARNINGs: {warning_count}  "
        f"| Keyword WARNINGs: {len(warn_keyword_lines)}"
    )
    if len(warn_keyword_lines) > THRESH["warning_keyword_count"] or warning_count > THRESH["warning_count"]:
        alerts.append(f"warning_keyword_count={len(warn_keyword_lines)}")

    # ---- 3. MLX crashes ----------------------------------------------------
    mlx_lines = [ln for ln in lines if _matches_any(ln, "mlx_crash")]
    mlx_count = len(mlx_lines)
    mlx_status = f"{mlx_count} crash(es)"
    if mlx_count >= THRESH["mlx_crash_count"]:
        mlx_status += "  ⚠️ ALERT"
        alerts.append(f"mlx_crash_count={mlx_count}")
    else:
        mlx_status += "  ✅ healthy"

    sections["mlx_crash"] = (
        f"## MLX Subprocess Crashes\n\n- {mlx_status}"
    )

    # ---- 4. Audio device disconnect ----------------------------------------
    audio_lines = [ln for ln in lines if _matches_any(ln, "audio_device")]
    audio_count = len(audio_lines)
    audio_status = f"{audio_count} event(s)"
    if audio_count >= THRESH["audio_device_count"]:
        audio_status += "  ⚠️ ALERT"
        alerts.append(f"audio_device_count={audio_count}")
    else:
        audio_status += "  ✅ healthy"

    sections["audio_device"] = (
        f"## Audio Device Interruptions\n\n- {audio_status}"
    )

    # ---- 5. Settings key count anomaly -------------------------------------
    key_counts: list[int] = []
    for ln in lines:
        v = _first_capture(ln, "settings_keys")
        if v:
            try:
                key_counts.append(int(v))
            except ValueError:
                pass

    max_keys = max(key_counts) if key_counts else 0
    keys_status = f"Max keys seen: {max_keys}"
    if max_keys > THRESH["settings_keys_max"]:
        keys_status += f"  ⚠️ ALERT (threshold={THRESH['settings_keys_max']})"
        alerts.append(f"settings_keys_max={max_keys}")
    elif max_keys > 0:
        keys_status += "  ✅ healthy"
    else:
        keys_status += " (no backup log entries found)"

    sections["settings_keys"] = (
        f"## Settings Schema Anomaly\n\n- {keys_status}"
    )

    # ---- 6. REST 5xx -------------------------------------------------------
    rest_lines = [ln for ln in lines if _matches_any(ln, "rest_5xx")]
    rest_count = len(rest_lines)
    rest_status = f"{rest_count} 5xx response(s)"
    if rest_count >= THRESH["rest_5xx_count"]:
        rest_status += "  ⚠️ ALERT"
        alerts.append(f"rest_5xx_count={rest_count}")
    else:
        rest_status += "  ✅ healthy"

    sections["rest_5xx"] = (
        f"## REST 5xx Errors\n\n- {rest_status}"
    )

    # ---- 7. Disk space trajectory ------------------------------------------
    disk_values: list[tuple[int, float]] = []  # (line_index, free_gb)
    for idx, ln in enumerate(lines):
        v = _first_capture(ln, "disk_space")
        if v:
            try:
                disk_values.append((idx, float(v)))
            except ValueError:
                pass

    disk_alert_lines = [ln for ln in lines if "disk.*space.*below" in ln.lower() or
                        re.search(r"disk.*below.*threshold", ln, re.IGNORECASE)]
    disk_status_parts: list[str] = []

    if disk_values:
        latest_free = disk_values[-1][1]
        disk_status_parts.append(f"Latest free: {latest_free:.2f} GB")
        if latest_free < THRESH["disk_free_gb_min"]:
            disk_status_parts.append(f"⚠️ BELOW {THRESH['disk_free_gb_min']} GB THRESHOLD")
            alerts.append(f"disk_free_gb={latest_free:.2f}")

        if len(disk_values) >= 2:
            delta = disk_values[-1][1] - disk_values[0][1]
            disk_status_parts.append(f"24h delta: {delta:+.2f} GB")
            if delta < -1.0:
                disk_status_parts.append("⚠️ RAPID DROP")
                alerts.append(f"disk_delta_gb={delta:.2f}")
    elif disk_alert_lines:
        disk_status_parts.append("⚠️ threshold warning found in log")
        alerts.append("disk_space_warning")
    else:
        disk_status_parts.append("no disk monitor entries found")

    sections["disk_space"] = (
        "## Disk Space Trajectory\n\n" +
        "\n".join(f"- {p}" for p in disk_status_parts)
    )

    # ---- 8. History.ndjson growth ------------------------------------------
    item_counts: list[int] = []
    compaction_skips = 0
    history_size_bytes: int = 0

    for ln in lines:
        # Item count
        v = _first_capture(ln, "history_growth")
        if v:
            try:
                item_counts.append(int(v))
            except ValueError:
                pass

        if re.search(r"compaction.*skipped", ln, re.IGNORECASE):
            compaction_skips += 1

        m = re.search(r"history\.ndjson.*size\s*=\s*([0-9]+)", ln)
        if m:
            try:
                history_size_bytes = max(history_size_bytes, int(m.group(1)))
            except ValueError:
                pass

    history_status_parts: list[str] = []
    max_items = max(item_counts) if item_counts else 0

    if max_items:
        history_status_parts.append(f"Max items loaded: {max_items}")
        if max_items > THRESH["history_item_max"]:
            history_status_parts.append(f"⚠️ ALERT (threshold={THRESH['history_item_max']})")
            alerts.append(f"history_item_count={max_items}")
        else:
            history_status_parts.append("✅ healthy")
    else:
        history_status_parts.append("No item count entries found")

    history_status_parts.append(f"Compaction skips: {compaction_skips}")
    if compaction_skips >= THRESH["compaction_skip_max"]:
        history_status_parts.append("⚠️ ALERT: frequent compaction skip")
        alerts.append(f"compaction_skips={compaction_skips}")

    if history_size_bytes > THRESH["history_size_bytes_max"]:
        size_mb = history_size_bytes / (1024 * 1024)
        history_status_parts.append(f"⚠️ File size {size_mb:.1f} MB > 50 MB limit")
        alerts.append(f"history_ndjson_size_mb={size_mb:.1f}")

    sections["history_growth"] = (
        "## History Store Growth\n\n" +
        "\n".join(f"- {p}" for p in history_status_parts)
    )

    return DigestResult(
        alerts=alerts,
        sections=sections,
        error_count=error_count,
        warning_count=warning_count,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Krab Ear backend log digest analyser")
    parser.add_argument(
        "--log",
        default=os.path.expanduser(
            "~/Library/Application Support/KrabEar/backend.log"
        ),
        help="Path to backend.log",
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=3000,
        help="Number of tail lines to analyse (default: 3000)",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Write digest Markdown to this path (optional; print to stdout if omitted)",
    )
    args = parser.parse_args()

    # Read log
    if not os.path.exists(args.log):
        print("healthy (log file not found)")
        return 0

    with open(args.log, encoding="utf-8", errors="replace") as fh:
        raw_lines = fh.readlines()

    tail_lines = [ln.rstrip() for ln in raw_lines[-args.lines:]]

    result = analyse(tail_lines)

    # Build summary line
    if result.alerts:
        summary = (
            f"DIGEST STATUS: {len(result.alerts)} ALERT(s) — "
            + " | ".join(result.alerts)
            + f" | {result.error_count} ERRORs | {result.warning_count} WARNINGs"
        )
    else:
        summary = (
            f"DIGEST STATUS: healthy "
            f"({result.error_count} ERRORs, {result.warning_count} WARNINGs)"
        )

    # Build full markdown
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    md_parts = [
        f"# Krab Ear Backend Log Digest — {now_str}",
        "",
        f"**{summary}**",
        "",
        f"_Analysed last {args.lines} lines of `{args.log}`_",
        "",
    ]
    for section_md in result.sections.values():
        md_parts.append(section_md)
        md_parts.append("")

    full_md = "\n".join(md_parts)

    if args.out:
        os.makedirs(os.path.dirname(args.out) if os.path.dirname(args.out) else ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(full_md)
        print(summary)
        print(f"Written: {args.out}")
    else:
        print(full_md)

    return 1 if result.alerts else 0


if __name__ == "__main__":
    sys.exit(main())
