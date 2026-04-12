"""Проверка и восстановление целостности данных Krab Ear.

Выполняет набор проверок NDJSON-файлов истории и settings.json,
сообщает о проблемах и автоматически исправляет то, что исправимо.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.IntegrityChecker")

# ISO 8601 basic check: YYYY-MM-DDTHH:MM:SS or with timezone
_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
)

REQUIRED_ITEM_FIELDS = ("id", "ts", "text")


@dataclass
class CheckResult:
    """Результат одной проверки."""
    name: str
    status: str          # "ok" | "warning" | "error"
    message: str
    auto_fixable: bool = False


@dataclass
class IntegrityReport:
    """Итоговый отчёт о состоянии данных."""
    status: str                         # "ok" | "warnings" | "errors"
    checks: list[CheckResult] = field(default_factory=list)
    total_items: int = 0
    orphaned_tombstones: int = 0
    invalid_json_lines: int = 0


@dataclass
class RepairResult:
    """Результат автоматического восстановления."""
    fixed: int = 0
    skipped: int = 0
    details: list[str] = field(default_factory=list)


class IntegrityChecker:
    """Проверяет и восстанавливает целостность данных Krab Ear."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_integrity(self, data_dir: Path) -> IntegrityReport:
        """Запускает все проверки и возвращает сводный отчёт."""
        checks: list[CheckResult] = []
        total_items = 0
        orphaned_tombstones = 0
        invalid_json_lines = 0

        history_path = data_dir / "history.ndjson"
        tombstones_path = data_dir / "history_tombstones.ndjson"
        settings_path = data_dir / "settings.json"

        # 1. Valid NDJSON format
        valid_lines, bad_lines, parsed_items = self._load_ndjson(history_path)
        invalid_json_lines = bad_lines
        if bad_lines > 0:
            checks.append(CheckResult(
                name="valid_ndjson",
                status="error",
                message=f"{bad_lines} строк(а) в history.ndjson содержат невалидный JSON",
                auto_fixable=True,
            ))
        else:
            checks.append(CheckResult(
                name="valid_ndjson",
                status="ok",
                message="Все строки history.ndjson корректный JSON",
            ))

        # 2. Required fields
        missing_fields_count = 0
        for item in parsed_items:
            for f in REQUIRED_ITEM_FIELDS:
                if f not in item or item[f] is None or item[f] == "":
                    missing_fields_count += 1
                    break
        if missing_fields_count > 0:
            checks.append(CheckResult(
                name="required_fields",
                status="error",
                message=f"{missing_fields_count} записей не имеют обязательных полей (id, ts, text)",
                auto_fixable=False,
            ))
        else:
            checks.append(CheckResult(
                name="required_fields",
                status="ok",
                message="Все записи содержат обязательные поля",
            ))

        # 3. Duplicate IDs
        ids_seen: dict[str, int] = {}
        for item in parsed_items:
            item_id = item.get("id", "")
            if item_id:
                ids_seen[item_id] = ids_seen.get(item_id, 0) + 1
        duplicates = {k: v for k, v in ids_seen.items() if v > 1}
        if duplicates:
            checks.append(CheckResult(
                name="duplicate_ids",
                status="warning",
                message=f"{len(duplicates)} дублирующихся ID ({', '.join(list(duplicates.keys())[:3])}…)",
                auto_fixable=False,
            ))
        else:
            checks.append(CheckResult(
                name="duplicate_ids",
                status="ok",
                message="Дублирующихся ID не обнаружено",
            ))

        # 4. Timestamp format
        bad_ts_count = 0
        for item in parsed_items:
            ts = item.get("ts", "")
            if ts and not _TS_RE.match(str(ts)):
                bad_ts_count += 1
        if bad_ts_count > 0:
            checks.append(CheckResult(
                name="timestamp_format",
                status="warning",
                message=f"{bad_ts_count} записей с некорректным форматом timestamp",
                auto_fixable=False,
            ))
        else:
            checks.append(CheckResult(
                name="timestamp_format",
                status="ok",
                message="Все timestamp в корректном ISO-формате",
            ))

        # 5. Orphaned tombstones
        active_ids = {item["id"] for item in parsed_items if "id" in item}
        _, _, tombstone_items = self._load_ndjson(tombstones_path)
        tombstone_ids = {t.get("id", "") for t in tombstone_items if t.get("id")}
        orphaned = tombstone_ids - active_ids
        orphaned_tombstones = len(orphaned)
        if orphaned_tombstones > 0:
            checks.append(CheckResult(
                name="orphaned_tombstones",
                status="warning",
                message=f"{orphaned_tombstones} tombstone(s) указывают на несуществующие записи",
                auto_fixable=True,
            ))
        else:
            checks.append(CheckResult(
                name="orphaned_tombstones",
                status="ok",
                message="Все tombstone-записи указывают на существующие элементы",
            ))

        # 6. Settings file valid JSON
        if settings_path.exists():
            try:
                settings_text = settings_path.read_text(encoding="utf-8")
                parsed_settings = json.loads(settings_text)
                if not isinstance(parsed_settings, dict):
                    raise ValueError("не является объектом")
                checks.append(CheckResult(
                    name="settings_json",
                    status="ok",
                    message="settings.json корректный JSON-объект",
                ))
            except Exception as exc:
                checks.append(CheckResult(
                    name="settings_json",
                    status="error",
                    message=f"settings.json повреждён: {exc}",
                    auto_fixable=True,
                ))
        else:
            checks.append(CheckResult(
                name="settings_json",
                status="ok",
                message="settings.json отсутствует (будет создан при первом запуске)",
            ))

        total_items = len(parsed_items)

        # Aggregate status
        statuses = {c.status for c in checks}
        if "error" in statuses:
            overall = "errors"
        elif "warning" in statuses:
            overall = "warnings"
        else:
            overall = "ok"

        return IntegrityReport(
            status=overall,
            checks=checks,
            total_items=total_items,
            orphaned_tombstones=orphaned_tombstones,
            invalid_json_lines=invalid_json_lines,
        )

    def repair(self, data_dir: Path, report: IntegrityReport) -> RepairResult:
        """Автоматически исправляет то, что помечено auto_fixable=True."""
        result = RepairResult()
        fixable = [c for c in report.checks if c.auto_fixable and c.status != "ok"]

        for check in fixable:
            if check.name == "valid_ndjson":
                fixed = self._repair_ndjson(data_dir / "history.ndjson")
                result.fixed += fixed
                result.details.append(f"valid_ndjson: удалено {fixed} невалидных строк")

            elif check.name == "orphaned_tombstones":
                fixed = self._repair_orphaned_tombstones(data_dir)
                result.fixed += fixed
                result.details.append(f"orphaned_tombstones: удалено {fixed} устаревших tombstone")

            elif check.name == "settings_json":
                self._repair_settings(data_dir / "settings.json")
                result.fixed += 1
                result.details.append("settings_json: повреждённый файл сброшен до {}}")

            else:
                result.skipped += 1

        non_fixable = [c for c in report.checks if not c.auto_fixable and c.status != "ok"]
        result.skipped += len(non_fixable)
        return result

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def handle_check_integrity(self, params: dict[str, Any]) -> dict[str, Any]:
        data_dir_str = params.get("data_dir")
        if not data_dir_str:
            raise ValueError("Параметр data_dir обязателен")
        data_dir = Path(data_dir_str)
        report = self.check_integrity(data_dir)
        return {
            "status": report.status,
            "total_items": report.total_items,
            "orphaned_tombstones": report.orphaned_tombstones,
            "invalid_json_lines": report.invalid_json_lines,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "auto_fixable": c.auto_fixable,
                }
                for c in report.checks
            ],
        }

    def handle_repair_data(self, params: dict[str, Any]) -> dict[str, Any]:
        data_dir_str = params.get("data_dir")
        if not data_dir_str:
            raise ValueError("Параметр data_dir обязателен")
        data_dir = Path(data_dir_str)
        report = self.check_integrity(data_dir)
        repair_result = self.repair(data_dir, report)
        return {
            "fixed": repair_result.fixed,
            "skipped": repair_result.skipped,
            "details": repair_result.details,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_ndjson(self, path: Path) -> tuple[int, int, list[dict]]:
        """Читает NDJSON-файл. Возвращает (valid_lines, bad_lines, parsed_items)."""
        if not path.exists():
            return 0, 0, []
        valid = 0
        bad = 0
        items: list[dict] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    items.append(obj)
                    valid += 1
                else:
                    bad += 1
            except json.JSONDecodeError:
                bad += 1
        return valid, bad, items

    def _repair_ndjson(self, path: Path) -> int:
        """Удаляет невалидные строки из NDJSON-файла. Возвращает число удалённых строк."""
        if not path.exists():
            return 0
        removed = 0
        good_lines: list[str] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    good_lines.append(stripped)
                else:
                    removed += 1
            except json.JSONDecodeError:
                removed += 1
        tmp = path.with_suffix(".ndjson.tmp")
        tmp.write_text("\n".join(good_lines) + ("\n" if good_lines else ""), encoding="utf-8")
        tmp.replace(path)
        return removed

    def _repair_orphaned_tombstones(self, data_dir: Path) -> int:
        """Удаляет tombstone-записи для несуществующих элементов."""
        history_path = data_dir / "history.ndjson"
        tombstones_path = data_dir / "history_tombstones.ndjson"
        if not tombstones_path.exists():
            return 0
        _, _, history_items = self._load_ndjson(history_path)
        active_ids = {item["id"] for item in history_items if "id" in item}
        _, _, tombstones = self._load_ndjson(tombstones_path)
        kept: list[dict] = []
        removed = 0
        for t in tombstones:
            tid = t.get("id", "")
            if tid in active_ids:
                kept.append(t)
            else:
                removed += 1
        tmp = tombstones_path.with_suffix(".ndjson.tmp")
        tmp.write_text(
            "\n".join(json.dumps(t, ensure_ascii=False) for t in kept) + ("\n" if kept else ""),
            encoding="utf-8",
        )
        tmp.replace(tombstones_path)
        return removed

    def _repair_settings(self, settings_path: Path) -> None:
        """Сбрасывает повреждённый settings.json до пустого объекта."""
        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text("{}", encoding="utf-8")
        tmp.replace(settings_path)
