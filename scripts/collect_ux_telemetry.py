"""Локальная UX-телеметрия Krab Ear (S54)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
import re


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    report_dir = root / "docs" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"ux_telemetry_{ts}.md"

    log_dir = Path.home() / "Library" / "Application Support" / "KrabEar"
    agent_log = log_dir / "agent.log"
    backend_log = log_dir / "backend.log"

    lines = read_lines(agent_log) + read_lines(backend_log)

    reasons = Counter()
    statuses = Counter()
    durations = []
    for line in lines:
        reason = re.search(r"reason=([a-zA-Z0-9_]+)", line)
        if reason:
            reasons[reason.group(1)] += 1
        if "Ответ stop_recording" in line:
            status_match = re.search(r"status=([a-zA-Z0-9_]+)", line)
            if status_match:
                statuses[status_match.group(1)] += 1
            dur_match = re.search(r"duration[_a-z]*=([0-9]+(?:\\.[0-9]+)?)", line)
            if dur_match:
                try:
                    durations.append(float(dur_match.group(1)))
                except ValueError:
                    pass

    total_stop = sum(statuses.values())
    ok_stop = statuses.get("ok", 0)
    paste_success_rate = (ok_stop / total_stop * 100.0) if total_stop else 0.0
    avg_duration = (sum(durations) / len(durations)) if durations else 0.0

    lines_out = [
        f"# UX Telemetry Report — {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- scanned_lines: {len(lines)}",
        f"- stop_events: {total_stop}",
        f"- stop_ok: {ok_stop}",
        f"- paste_success_rate_percent: {paste_success_rate:.2f}",
        f"- avg_recording_duration_sec: {avg_duration:.2f}",
        "",
        "## Top Stop Status",
        "",
    ]
    if statuses:
        for key, count in statuses.most_common(10):
            lines_out.append(f"- {key}: {count}")
    else:
        lines_out.append("- (нет stop событий)")

    lines_out += ["", "## Top Failure Reasons", ""]
    if reasons:
        for key, count in reasons.most_common(15):
            lines_out.append(f"- {key}: {count}")
    else:
        lines_out.append("- (нет reason событий)")

    report_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    print(f"✅ UX telemetry report OK\nОтчёт: {report_path}")


if __name__ == "__main__":
    main()
