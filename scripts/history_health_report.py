"""Локальный health-отчёт истории Krab Ear.

Зачем нужен:
1) быстро проверить целостность и объём истории;
2) увидеть качество вставки/перевода без запуска UI;
3) получить markdown-отчёт в docs/reports для диагностики.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = ROOT_DIR / "KrabEar"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from backend.state_store import StateStore  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Генерация health-отчёта истории Krab Ear")
    parser.add_argument(
        "--data-dir",
        default=str(Path.home() / "Library" / "Application Support" / "KrabEar"),
        help="Каталог данных KrabEar (по умолчанию ~/Library/Application Support/KrabEar)",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser().resolve()
    report_dir = ROOT_DIR / "docs" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    store = StateStore(data_dir=data_dir)
    stats = store.get_history_stats()
    overview = store.get_history_overview()
    settings = store.load_settings()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"history_health_{ts}.md"

    top_modes = overview.get("top_modes", [])
    if isinstance(top_modes, list) and top_modes:
        top_modes_lines = "\n".join(
            f"- {entry.get('mode', 'unknown')}: {entry.get('count', 0)}"
            for entry in top_modes
            if isinstance(entry, dict)
        )
    else:
        top_modes_lines = "- (нет данных)"

    lines = [
        f"# History Health Report — {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- data_dir: `{data_dir}`",
        f"- active_count: {stats.get('active_count', 0)}",
        f"- history_lines: {stats.get('history_lines', 0)}",
        f"- tombstones_lines: {stats.get('tombstones_lines', 0)}",
        f"- status_lines: {stats.get('status_lines', 0)}",
        f"- total_bytes: {stats.get('total_bytes', 0)}",
        "",
        "## Quality Overview",
        "",
        f"- today_count: {overview.get('today_count', 0)}",
        f"- last_24h_count: {overview.get('last_24h_count', 0)}",
        f"- paste_ok: {overview.get('paste_ok', 0)}",
        f"- paste_failed: {overview.get('paste_failed', 0)}",
        f"- translated_ok: {overview.get('translated_ok', 0)}",
        f"- translated_error: {overview.get('translated_error', 0)}",
        f"- no_translation: {overview.get('no_translation', 0)}",
        "",
        "## Top Translation Modes",
        "",
        top_modes_lines,
        "",
        "## Settings Snapshot",
        "",
        f"- history_page_size: {settings.get('history_page_size', 50)}",
        f"- history_focus_mode: {settings.get('history_focus_mode', True)}",
        f"- history_text_density: {settings.get('history_text_density', 'normal')}",
        f"- ui_last_tab: {settings.get('ui_last_tab', 'history')}",
        "",
        "## Notes",
        "",
        "- Если `tombstones_lines` и `status_lines` быстро растут, запустите `Оптимизировать историю`.",
        "- Если `paste_failed` заметно выше `paste_ok`, проверьте доступность активного поля и права Accessibility.",
    ]

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✅ History health report OK\nОтчёт: {report_path}")


if __name__ == "__main__":
    main()
