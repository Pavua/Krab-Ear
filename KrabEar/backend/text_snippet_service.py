"""Сервис IPC для управления текстовыми сниппетами (голосовые расширения текста).

Хранит пары trigger→expansion в text_snippets.json (в data_dir),
предоставляет три IPC-метода:
    add_text_snippet    — добавить/обновить пару
    list_text_snippets  — получить все пары
    remove_text_snippet — удалить по триггеру

Формат файла: {"snippets": [{"trigger": str, "expansion": str}], "updated_at": "ISO8601"}

Атомарная запись (tmp → replace), потокобезопасность через threading.Lock.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

from core.parsing_utils import safe_json_loads

logger = logging.getLogger("KrabEar.Backend.TextSnippetService")

_SNIPPETS_FILENAME = "text_snippets.json"
_MAX_SNIPPETS = 200
_MAX_TRIGGER_LEN = 200
_MAX_EXPANSION_LEN = 2000


class TextSnippetService:
    """IPC-сервис для хранения и управления пользовательскими текстовыми сниппетами.

    Args:
        data_dir: директория для text_snippets.json.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / _SNIPPETS_FILENAME
        data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ── Storage ──────────────────────────────────────────────────────────────

    def _load(self) -> List[dict]:
        """Загружает список сниппетов с диска. Пустой список при ошибке."""
        if not self._path.exists():
            return []
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("text_snippets.json: не удалось прочитать: %s", exc)
            return []
        payload = safe_json_loads(raw, default=None, context="text_snippets.json")
        if not isinstance(payload, dict):
            logger.warning("text_snippets.json: неожиданный формат, возвращаем пустой список")
            return []
        snippets = payload.get("snippets", [])
        if not isinstance(snippets, list):
            logger.warning("text_snippets.json: поле 'snippets' не список")
            return []
        return [
            s for s in snippets
            if isinstance(s, dict)
            and isinstance(s.get("trigger"), str) and s["trigger"].strip()
            and isinstance(s.get("expansion"), str)
        ]

    def _save(self, snippets: List[dict]) -> None:
        """Атомарная запись (tmp → replace)."""
        payload = {
            "snippets": snippets,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            logger.error("text_snippets.json: ошибка сохранения: %s", exc)
            raise

    # ── Public read API (called by engine.py via provider callback) ──────────

    def get_snippets(self) -> List[dict]:
        """Возвращает текущий список сниппетов (thread-safe)."""
        with self._lock:
            return self._load()

    def clear_all(self) -> None:
        """Удаляет text_snippets.json с диска (для privacy-purge).

        Идемпотентен — не бросает исключений если файл уже отсутствует.
        """
        with self._lock:
            try:
                self._path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "TextSnippetService.clear_all: не удалось удалить файл: %s", exc
                )

    # ── IPC handlers ─────────────────────────────────────────────────────────

    def handle_add_text_snippet(self, params: dict[str, Any]) -> dict:
        """IPC: add_text_snippet — добавить/обновить сниппет.

        Params:
            trigger   (str, обязательный): фраза-триггер.
            expansion (str, обязательный): текст подстановки.

        Returns:
            {"ok": True, "trigger": trigger, "expansion": expansion}
        """
        trigger = params.get("trigger", "")
        expansion = params.get("expansion")

        if not isinstance(trigger, str) or not trigger.strip():
            raise ValueError("Параметр 'trigger' обязателен и не может быть пустым")
        trigger = trigger.strip()
        if len(trigger) > _MAX_TRIGGER_LEN:
            raise ValueError(
                f"Триггер слишком длинный: {len(trigger)} символов (максимум {_MAX_TRIGGER_LEN})"
            )
        if not isinstance(expansion, str):
            raise ValueError("Параметр 'expansion' обязателен")
        if len(expansion) > _MAX_EXPANSION_LEN:
            raise ValueError(
                f"Расширение слишком длинное: {len(expansion)} символов (максимум {_MAX_EXPANSION_LEN})"
            )

        with self._lock:
            snippets = self._load()
            if len(snippets) >= _MAX_SNIPPETS and not any(
                s["trigger"].lower() == trigger.lower() for s in snippets
            ):
                raise RuntimeError(
                    f"Достигнут лимит сниппетов ({_MAX_SNIPPETS}). "
                    "Удалите ненужные перед добавлением новых."
                )
            # Дедупликация по триггеру (case-insensitive): обновляем существующий
            updated = False
            for s in snippets:
                if s["trigger"].lower() == trigger.lower():
                    s["trigger"] = trigger
                    s["expansion"] = expansion
                    updated = True
                    break
            if not updated:
                snippets.append({"trigger": trigger, "expansion": expansion})
            self._save(snippets)

        logger.info(
            "TextSnippet: %s trigger=%r expansion_len=%d",
            "обновлён" if updated else "добавлен",
            trigger,
            len(expansion),
        )
        return {"ok": True, "trigger": trigger, "expansion": expansion}

    def handle_list_text_snippets(self, params: dict[str, Any]) -> dict:
        """IPC: list_text_snippets — получить все сниппеты.

        Returns:
            {"ok": True, "snippets": [{"trigger": str, "expansion": str}, ...]}
        """
        snippets = self.get_snippets()
        return {"ok": True, "snippets": snippets}

    def handle_remove_text_snippet(self, params: dict[str, Any]) -> dict:
        """IPC: remove_text_snippet — удалить сниппет по триггеру.

        Params:
            trigger (str, обязательный): фраза-триггер для удаления.

        Returns:
            {"ok": True, "trigger": trigger, "removed": bool}
        """
        trigger = params.get("trigger", "")
        if not isinstance(trigger, str) or not trigger.strip():
            raise ValueError("Параметр 'trigger' обязателен")
        trigger = trigger.strip()

        with self._lock:
            snippets = self._load()
            before = len(snippets)
            snippets = [s for s in snippets if s["trigger"].lower() != trigger.lower()]
            removed = len(snippets) < before
            if removed:
                self._save(snippets)

        if not removed:
            raise RuntimeError(f"Сниппет с триггером {trigger!r} не найден")

        logger.info("TextSnippet: удалён trigger=%r", trigger)
        return {"ok": True, "trigger": trigger, "removed": True}
