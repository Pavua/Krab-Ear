"""Скоринг roadmap-спринтов по impact/risk/effort (S51).

Скрипт читает docs/ROADMAP.md, извлекает Sxx-блоки и строит приоритетную
очередь на основе простых эвристик. Ничего не изменяет в кодовой базе.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable


@dataclass(slots=True)
class SprintScore:
    sprint_id: str
    title: str
    impact: int
    risk: int
    effort: int

    @property
    def total(self) -> int:
        # Чем выше impact, ниже risk/effort — тем выше приоритет.
        return self.impact * 4 - self.risk * 2 - self.effort


def _score_by_keywords(title: str) -> tuple[int, int, int]:
    t = title.lower()
    impact = 5
    risk = 3
    effort = 3

    high_impact_keys = [
        "reliability",
        "permissions",
        "realtime",
        "history",
        "release",
        "security",
        "regression",
    ]
    if any(k in t for k in high_impact_keys):
        impact += 2

    low_risk_keys = ["ux", "checklist", "docs", "backup", "validation", "report"]
    if any(k in t for k in low_risk_keys):
        risk -= 1
        effort -= 1

    high_risk_keys = ["research", "experimental", "phone", "embedding", "notarization", "ci"]
    if any(k in t for k in high_risk_keys):
        risk += 2
        effort += 1

    impact = max(1, min(10, impact))
    risk = max(1, min(10, risk))
    effort = max(1, min(10, effort))
    return impact, risk, effort


def parse_sprints(text: str) -> Iterable[SprintScore]:
    for match in re.finditer(r"^####\s+(S\d{2})\.\s+(.+)$", text, flags=re.M):
        sprint_id, title = match.group(1), match.group(2).strip()
        impact, risk, effort = _score_by_keywords(title)
        yield SprintScore(
            sprint_id=sprint_id,
            title=title,
            impact=impact,
            risk=risk,
            effort=effort,
        )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    roadmap_path = root / "docs" / "ROADMAP.md"
    report_dir = root / "docs" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = __import__("time").strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"sprint_prioritizer_{ts}.md"

    text = roadmap_path.read_text(encoding="utf-8")
    scores = sorted(parse_sprints(text), key=lambda x: (x.total, x.impact), reverse=True)

    lines = [
        f"# Sprint Prioritizer Report — {__import__('datetime').datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- total_sprints: {len(scores)}",
        "",
        "## Top 20",
        "",
    ]
    for idx, item in enumerate(scores[:20], start=1):
        lines.append(
            f"{idx}. {item.sprint_id} — {item.title} "
            f"(impact={item.impact}, risk={item.risk}, effort={item.effort}, total={item.total})"
        )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✅ Sprint prioritizer OK\nОтчёт: {report_path}")


if __name__ == "__main__":
    main()
