"""GlossaryService — обработчики IPC-методов глоссария CSV (export/import).

Извлечено из service.py (BackendService, W772) для уменьшения монолита.
Методы: export_glossary_csv, import_glossary_csv.

Зависимости: SettingsService (cached_settings, handle_set_settings).
"""

from __future__ import annotations

import csv
import io
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.settings_service import SettingsService


class GlossaryService:
    """Обработчики IPC-команд экспорта/импорта глоссария (CSV)."""

    def __init__(self, settings_svc: "SettingsService") -> None:
        self._settings_svc = settings_svc

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def handle_export_glossary_csv(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует translation_glossary в CSV-строку.

        Returns: {"ok": True, "csv": "source,target\\n...", "row_count": N}
        """
        settings = self._settings_svc.cached_settings()
        glossary: dict = settings.get("translation_glossary", {}) or {}

        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["source", "target"])
        for source, target in sorted(glossary.items()):
            writer.writerow([source, target])

        return {"ok": True, "csv": buf.getvalue(), "row_count": len(glossary)}

    def handle_import_glossary_csv(self, params: dict[str, Any]) -> dict[str, Any]:
        """Импортирует CSV в translation_glossary.

        params:
          csv: str — CSV-строка с заголовком source,target
          mode: "merge" | "replace" — merge добавляет/обновляет, replace полностью заменяет
          on_conflict: "skip" | "overwrite" | "error" — поведение при конфликте в merge-режиме
            "skip"      — (по умолчанию) существующий термин сохраняется, конфликт записывается
            "overwrite" — существующий термин перезаписывается
            "error"     — импорт прерывается на первом конфликте

        Returns:
          {ok, imported_count, skipped_count, conflict_count,
           conflicts: [{source, existing_target, new_target}], total}
        """
        csv_str = params.get("csv", "")
        mode = params.get("mode", "merge").lower()
        on_conflict = params.get("on_conflict", "skip").lower()

        if mode not in ("merge", "replace"):
            return {"ok": False, "error": f"invalid mode: {mode}"}
        if on_conflict not in ("skip", "overwrite", "error"):
            return {"ok": False, "error": f"invalid on_conflict: {on_conflict}"}

        settings = self._settings_svc.cached_settings()
        current: dict = dict(settings.get("translation_glossary", {}) or {})
        new_entries: dict = {} if mode == "replace" else dict(current)
        skipped = 0
        conflicts: list = []
        # Track sources seen in this CSV file for within-CSV deduplication
        seen_in_csv: dict = {}

        try:
            reader = csv.reader(io.StringIO(csv_str))
            header = next(reader, None)
            if not header or [h.strip().lower() for h in header] != ["source", "target"]:
                return {"ok": False, "error": "header must be: source,target"}
            for row in reader:
                if len(row) != 2:
                    skipped += 1
                    continue
                src, tgt = row[0].strip(), row[1].strip()
                if not src or not tgt:
                    skipped += 1
                    continue
                # Skip rows where source == target (no-op entries)
                if src == tgt:
                    skipped += 1
                    continue
                # Within-CSV deduplication: skip duplicate source rows, keep first
                if src in seen_in_csv:
                    skipped += 1
                    continue
                seen_in_csv[src] = tgt

                # Conflict detection in merge mode
                if mode == "merge" and src in current and current[src] != tgt:
                    conflicts.append({
                        "source": src,
                        "existing_target": current[src],
                        "new_target": tgt,
                    })
                    if on_conflict == "error":
                        return {
                            "ok": False,
                            "error": f"conflict on source '{src}': existing='{current[src]}' new='{tgt}'",
                            "imported_count": 0,
                            "skipped_count": skipped,
                            "conflict_count": len(conflicts),
                            "conflicts": conflicts,
                        }
                    elif on_conflict == "skip":
                        # Keep existing — don't overwrite
                        continue
                    # on_conflict == "overwrite": fall through to set new value

                new_entries[src] = tgt
        except Exception as exc:
            return {"ok": False, "error": f"parse error: {exc}"}

        self._settings_svc.handle_set_settings({"translation_glossary": new_entries})

        prev_count = len(current)
        imported = len(new_entries) - (prev_count if mode == "merge" else 0)
        return {
            "ok": True,
            "imported_count": max(imported, 0),
            "skipped_count": skipped,
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
            "total": len(new_entries),
        }
