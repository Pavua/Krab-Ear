"""Генератор self-update заметок для roadmap (S52).

Скрипт не переписывает ROADMAP.md автоматически, а готовит отчёт с
рекомендованными строками для Progress Snapshot на основе свежих отчётов.
"""

from __future__ import annotations

from pathlib import Path
import re
from datetime import datetime


def latest(pattern: str, report_dir: Path) -> str:
    items = sorted(report_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(items[0]) if items else "-"


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    report_dir = root / "docs" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    roadmap_path = root / "docs" / "ROADMAP.md"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = report_dir / f"roadmap_self_update_{ts}.md"

    roadmap_text = roadmap_path.read_text(encoding="utf-8")
    snapshot_lines = re.findall(r"^\d+\.\s+S\d{2}.*$", roadmap_text, flags=re.M)

    latest_smoke = latest("smoke_release_*.md", report_dir)
    latest_checklist = latest("release_checklist_*.md", report_dir)
    latest_daily = latest("daily_driver_validation_*.md", report_dir)
    latest_autonomous = latest("autonomous_hour_*.md", report_dir)

    lines = [
        f"# Roadmap Self-Update Report — {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Source Reports",
        "",
        f"- smoke: {latest_smoke}",
        f"- release_checklist: {latest_checklist}",
        f"- daily_driver: {latest_daily}",
        f"- autonomous_hour: {latest_autonomous}",
        "",
        "## Snapshot Stats",
        "",
        f"- snapshot_lines_count: {len(snapshot_lines)}",
        "",
        "## Suggested Snapshot Additions",
        "",
        "1. S24 (частично): добавлен daily-driver validation отчёт с residual risks.",
        "2. S49/S50 (частично): hour-runner получил checkpoints и stop-conditions.",
        "3. S51 (частично): добавлен автоматический sprint prioritizer report.",
        "4. S52 (частично): внедрён self-update репорт для roadmap snapshot.",
        "5. S53 (частично): regression radar по agent/backend логам.",
        "",
        "## Note",
        "",
        "Этот отчёт не меняет ROADMAP.md автоматически: он формирует безопасные предложения для обновления snapshot.",
    ]

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✅ Roadmap self-update report OK\nОтчёт: {out_path}")


if __name__ == "__main__":
    main()
