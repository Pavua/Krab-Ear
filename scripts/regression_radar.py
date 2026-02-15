"""Regression radar по локальным логам Krab Ear (S53)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
import re


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []


def top_patterns(lines: list[str]) -> tuple[Counter[str], Counter[str], Counter[str]]:
    error_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
    method_counter: Counter[str] = Counter()

    for line in lines:
        if "ERROR" in line or "Exception" in line or "ошибка" in line.lower():
            compact = re.sub(r"\s+", " ", line.strip())
            error_counter[compact[:160]] += 1

        reason_match = re.search(r"reason=([a-zA-Z0-9_]+)", line)
        if reason_match:
            reason_counter[reason_match.group(1)] += 1

        method_match = re.search(r"Ошибка метода ([a-zA-Z0-9_]+)", line)
        if method_match:
            method_counter[method_match.group(1)] += 1

    return error_counter, reason_counter, method_counter


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    report_dir = root / "docs" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"regression_radar_{ts}.md"

    app_support = Path.home() / "Library" / "Application Support" / "KrabEar"
    agent_log = app_support / "agent.log"
    backend_log = app_support / "backend.log"

    lines = read_lines(agent_log) + read_lines(backend_log)
    errors, reasons, methods = top_patterns(lines)

    output = [
        f"# Regression Radar Report — {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- scanned_lines: {len(lines)}",
        f"- agent_log: {agent_log if agent_log.exists() else 'missing'}",
        f"- backend_log: {backend_log if backend_log.exists() else 'missing'}",
        "",
        "## Top Reasons",
        "",
    ]
    if reasons:
        for reason, count in reasons.most_common(12):
            output.append(f"- {reason}: {count}")
    else:
        output.append("- (нет reason-событий)")

    output += ["", "## Top Backend Methods With Errors", ""]
    if methods:
        for method, count in methods.most_common(12):
            output.append(f"- {method}: {count}")
    else:
        output.append("- (ошибки методов не найдены)")

    output += ["", "## Top Error Lines", ""]
    if errors:
        for row, count in errors.most_common(15):
            output.append(f"- x{count}: `{row}`")
    else:
        output.append("- (ошибок не найдено)")

    report_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    print(f"✅ Regression radar report OK\nОтчёт: {report_path}")


if __name__ == "__main__":
    main()
