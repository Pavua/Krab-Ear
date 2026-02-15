"""Проверка зон ответственности Codex/Antigravity по файловым изменениям.

Зачем нужен скрипт:
1) не допускать случайных правок в чужих зонах;
2) фиксировать нарушения до merge/release;
3) поддерживать безопасную параллельную работу двух агентов.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import fnmatch
import json
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "docs" / "agent_ownership.json"
SNAPSHOT_DIR = ROOT_DIR / ".coordination" / "boundary_snapshots"
REPORT_DIR = ROOT_DIR / "docs" / "reports"


@dataclass(slots=True)
class Zone:
    name: str
    owner: str
    prefix: Path
    monitor: bool = True


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("zones", [])
    payload.setdefault("shared_prefixes", [])
    payload.setdefault("exclude_globs", [])
    return payload


def normalize_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def is_excluded(path: Path, patterns: list[str]) -> bool:
    as_posix = path.as_posix()
    for pattern in patterns:
        if fnmatch.fnmatch(as_posix, pattern):
            return True
    return False


def scan_files(prefixes: list[Path], exclude_globs: list[str]) -> dict[str, int]:
    snapshot: dict[str, int] = {}
    seen_roots: set[str] = set()
    for prefix in prefixes:
        if not prefix.exists():
            continue
        key = prefix.as_posix()
        if key in seen_roots:
            continue
        seen_roots.add(key)

        if prefix.is_file():
            if not is_excluded(prefix, exclude_globs):
                try:
                    snapshot[prefix.as_posix()] = int(prefix.stat().st_mtime_ns)
                except FileNotFoundError:
                    pass
            continue

        for item in prefix.rglob("*"):
            if not item.is_file():
                continue
            if is_excluded(item, exclude_globs):
                continue
            try:
                snapshot[item.as_posix()] = int(item.stat().st_mtime_ns)
            except FileNotFoundError:
                continue
    return snapshot


def load_snapshot(path: Path) -> dict[str, int] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    files = payload.get("files", {})
    if not isinstance(files, dict):
        return None
    result: dict[str, int] = {}
    for file_path, mtime_ns in files.items():
        try:
            result[str(file_path)] = int(mtime_ns)
        except (TypeError, ValueError):
            continue
    return result


def save_snapshot(path: Path, files: dict[str, int], owner: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "owner": owner,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "files": files,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def classify_owner(path: Path, zones: list[Zone], shared_prefixes: list[Path]) -> tuple[str, str]:
    normalized = path.as_posix()
    for prefix in sorted(shared_prefixes, key=lambda p: len(p.as_posix()), reverse=True):
        p = prefix.as_posix()
        if normalized == p or normalized.startswith(p + "/"):
            return "shared", f"shared:{p}"

    best_zone: Zone | None = None
    for zone in zones:
        p = zone.prefix.as_posix()
        if normalized == p or normalized.startswith(p + "/"):
            if best_zone is None or len(p) > len(best_zone.prefix.as_posix()):
                best_zone = zone

    if best_zone is None:
        return "unassigned", "unassigned"
    return best_zone.owner, best_zone.name


def collect_changed_files(current: dict[str, int], previous: dict[str, int] | None) -> list[Path]:
    if previous is None:
        return []
    changed: set[str] = set()
    for path, mtime in current.items():
        if previous.get(path) != mtime:
            changed.add(path)
    for path in previous:
        if path not in current:
            changed.add(path)
    return [Path(path) for path in sorted(changed)]


def build_report(
    owner: str,
    changed_files: list[Path],
    allowed_count: int,
    violations: list[tuple[Path, str, str]],
    baseline_created: bool,
) -> str:
    now = datetime.now().isoformat(timespec="seconds")
    lines = [
        f"# Agent Boundary Report — {now}",
        "",
        f"- owner: `{owner}`",
        f"- baseline_created: `{str(baseline_created).lower()}`",
        f"- changed_files: `{len(changed_files)}`",
        f"- allowed_files: `{allowed_count}`",
        f"- violations: `{len(violations)}`",
        "",
    ]

    if violations:
        lines.extend(["## Violations", ""])
        for file_path, file_owner, reason in violations:
            lines.append(f"- `{file_path}` -> owner `{file_owner}` ({reason})")
        lines.append("")

    lines.extend(["## Changed Files", ""])
    if changed_files:
        for file_path in changed_files[:300]:
            lines.append(f"- `{file_path}`")
        if len(changed_files) > 300:
            lines.append(f"- ... ещё {len(changed_files) - 300}")
    else:
        lines.append("- (нет изменений относительно прошлого снимка)")
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Проверка границ зон ответственности агентов")
    parser.add_argument("owner", choices=["codex", "antigravity"], help="Кто проверяется")
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        help="Явно переданный изменённый файл (можно указывать несколько раз)",
    )
    parser.add_argument(
        "--no-update-snapshot",
        action="store_true",
        help="Не обновлять снимок после проверки",
    )
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Не завершать ошибкой при нарушениях",
    )
    parser.add_argument(
        "--reset-baseline",
        action="store_true",
        help="Сбросить прошлый снимок и создать новый baseline",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(CONFIG_PATH)

    zones = [
        Zone(
            name=str(item.get("name", "")),
            owner=str(item.get("owner", "")).strip().lower(),
            prefix=normalize_path(str(item.get("prefix", ""))),
            monitor=bool(item.get("monitor", True)),
        )
        for item in config.get("zones", [])
        if str(item.get("name", "")).strip() and str(item.get("prefix", "")).strip()
    ]
    shared_prefixes = [normalize_path(value) for value in config.get("shared_prefixes", [])]
    exclude_globs = [str(value) for value in config.get("exclude_globs", [])]

    monitored_prefixes = [zone.prefix for zone in zones if zone.monitor] + shared_prefixes
    current_snapshot = scan_files(monitored_prefixes, exclude_globs)

    snapshot_path = SNAPSHOT_DIR / f"{args.owner}.json"
    old_snapshot = None if args.reset_baseline else load_snapshot(snapshot_path)

    baseline_created = False
    if args.file:
        changed_files = [normalize_path(value) for value in args.file]
    else:
        if old_snapshot is None:
            baseline_created = True
            changed_files = []
        else:
            changed_files = collect_changed_files(current_snapshot, old_snapshot)

    allowed_owners = {args.owner, "shared"}
    violations: list[tuple[Path, str, str]] = []
    allowed_count = 0

    for file_path in changed_files:
        owner, reason = classify_owner(file_path, zones, shared_prefixes)
        if owner in allowed_owners:
            allowed_count += 1
            continue
        violations.append((file_path, owner, reason))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"agent_boundary_{args.owner}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    report_body = build_report(
        owner=args.owner,
        changed_files=changed_files,
        allowed_count=allowed_count,
        violations=violations,
        baseline_created=baseline_created,
    )
    report_path.write_text(report_body + "\n", encoding="utf-8")

    if not args.no_update_snapshot:
        save_snapshot(snapshot_path, current_snapshot, owner=args.owner)

    if baseline_created:
        print(f"✅ baseline создан для owner={args.owner}")
        print(f"Отчёт: {report_path}")
        print(f"Снимок: {snapshot_path}")
        return 0

    if violations:
        print(f"❌ boundary-check: найдено нарушений {len(violations)}")
        print(f"Отчёт: {report_path}")
        if args.no_fail:
            return 0
        return 1

    print("✅ boundary-check: нарушений нет")
    print(f"Отчёт: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
