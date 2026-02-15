"""Проверка performance budget на базе последнего UX telemetry отчёта (S55)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re


def parse_metric(text: str, key: str, default: float = 0.0) -> float:
    m = re.search(rf"{re.escape(key)}:\s*([0-9]+(?:\.[0-9]+)?)", text)
    if not m:
        return default
    try:
        return float(m.group(1))
    except ValueError:
        return default


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    report_dir = root / "docs" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = report_dir / f"performance_budget_{ts}.md"

    telemetry_reports = sorted(report_dir.glob("ux_telemetry_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not telemetry_reports:
        out_path.write_text(
            f"# Performance Budget Report — {datetime.now().isoformat(timespec='seconds')}\n\n"
            "- status: **FAILED**\n"
            "- reason: ux_telemetry_report_missing\n",
            encoding="utf-8",
        )
        print(f"❌ Performance budget FAILED\nОтчёт: {out_path}")
        raise SystemExit(1)

    telemetry = telemetry_reports[0]
    text = telemetry.read_text(encoding="utf-8")
    paste_success = parse_metric(text, "paste_success_rate_percent", 0.0)
    avg_duration = parse_metric(text, "avg_recording_duration_sec", 0.0)

    budget_paste_success = 95.0
    budget_avg_duration = 90.0
    ok = paste_success >= budget_paste_success and avg_duration <= budget_avg_duration

    lines = [
        f"# Performance Budget Report — {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- status: **{'OK' if ok else 'FAILED'}**",
        f"- source_telemetry: {telemetry}",
        "",
        "## Budgets",
        "",
        f"- paste_success_rate_percent >= {budget_paste_success}",
        f"- avg_recording_duration_sec <= {budget_avg_duration}",
        "",
        "## Actual",
        "",
        f"- paste_success_rate_percent: {paste_success:.2f}",
        f"- avg_recording_duration_sec: {avg_duration:.2f}",
    ]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if ok:
        print(f"✅ Performance budget OK\nОтчёт: {out_path}")
    else:
        print(f"❌ Performance budget FAILED\nОтчёт: {out_path}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
