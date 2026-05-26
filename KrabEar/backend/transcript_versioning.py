"""TranscriptVersionManager — версионирование текста транскрипций Krab Ear.

Позволяет отслеживать историю редактирования текста для каждой записи.
Данные сохраняются в {data_dir}/transcript_versions.ndjson в append-only формате.
"""

from __future__ import annotations

import difflib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.TranscriptVersioning")

# Допустимые источники версии
VALID_SOURCES = frozenset({"stt_raw", "stt_cleaned", "llm_rewrite", "manual", "import"})
_VERSIONS_FILE = "transcript_versions.ndjson"


class TranscriptVersionManager:
    """Версионирование текста транскрипций.

    Каждая версия — строка NDJSON:
    {
        "item_id": str,
        "version_num": int,       # начиная с 1, монотонно растёт по item_id
        "text": str,
        "source": str,            # stt_raw | stt_cleaned | llm_rewrite | manual | import
        "created_at": ISO8601,
    }
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        self._versions_path = self._data_dir / _VERSIONS_FILE
        self._lock = threading.Lock()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._versions_path.touch(exist_ok=True)

    # ------------------------------------------------------------------
    # Внутренние хелперы
    # ------------------------------------------------------------------

    def _read_all(self) -> list[dict[str, Any]]:
        """Читает все версии из NDJSON."""
        records: list[dict[str, Any]] = []
        try:
            for line in self._versions_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Пропущена повреждённая строка в transcript_versions.ndjson")
        except Exception as exc:
            logger.error("Не удалось прочитать transcript_versions.ndjson: %s", exc)
        return records

    def _append(self, record: dict[str, Any]) -> None:
        """Добавляет запись в конец NDJSON."""
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._versions_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _next_version_num(self, item_id: str, all_records: list[dict[str, Any]]) -> int:
        """Возвращает следующий номер версии для item_id."""
        existing = [r["version_num"] for r in all_records if r.get("item_id") == item_id]
        return max(existing, default=0) + 1

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def save_version(self, item_id: str, text: str, source: str = "manual") -> dict[str, Any]:
        """Сохраняет новую версию текста транскрипции.

        Args:
            item_id: ID записи истории.
            text: Текст транскрипции.
            source: Источник версии (stt_raw, stt_cleaned, llm_rewrite, manual, import).

        Returns:
            Словарь с полями version_num, item_id, text, source, created_at.

        Raises:
            ValueError: если item_id пуст, text пуст, или source не поддерживается.
        """
        item_id = str(item_id).strip()
        if not item_id:
            raise ValueError("item_id не может быть пустым")
        if not isinstance(text, str):
            raise ValueError("text должен быть строкой")
        source = str(source).strip()
        if source not in VALID_SOURCES:
            raise ValueError(f"Недопустимый source {source!r}. Допустимые: {sorted(VALID_SOURCES)}")

        with self._lock:
            all_records = self._read_all()
            version_num = self._next_version_num(item_id, all_records)
            record: dict[str, Any] = {
                "item_id": item_id,
                "version_num": version_num,
                "text": text,
                "source": source,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._append(record)
            return dict(record)

    def get_versions(self, item_id: str) -> list[dict[str, Any]]:
        """Возвращает все версии для item_id, от новейшей к старейшей.

        Args:
            item_id: ID записи истории.

        Returns:
            Список версий, отсортированных по version_num убыванию.
        """
        item_id = str(item_id).strip()
        with self._lock:
            all_records = self._read_all()
        versions = [r for r in all_records if r.get("item_id") == item_id]
        versions.sort(key=lambda r: r.get("version_num", 0), reverse=True)
        return versions

    def get_version(self, item_id: str, version_num: int) -> dict[str, Any]:
        """Возвращает конкретную версию транскрипции.

        Args:
            item_id: ID записи истории.
            version_num: Номер версии (начиная с 1).

        Returns:
            Словарь версии.

        Raises:
            KeyError: если версия не найдена.
        """
        item_id = str(item_id).strip()
        version_num = int(version_num)
        with self._lock:
            all_records = self._read_all()
        for r in all_records:
            if r.get("item_id") == item_id and r.get("version_num") == version_num:
                return dict(r)
        raise KeyError(f"Версия {version_num} для item_id={item_id!r} не найдена")

    def revert_to_version(self, item_id: str, version_num: int) -> dict[str, Any]:
        """Создаёт новую версию с текстом из указанной версии (откат).

        Откат не удаляет более новые версии — создаётся новая запись с
        source='manual' и комментарием о revert.

        Args:
            item_id: ID записи истории.
            version_num: Номер версии для отката.

        Returns:
            Новая версия (результат отката).

        Raises:
            KeyError: если указанная версия не найдена.
        """
        target = self.get_version(item_id, version_num)
        new_version = self.save_version(
            item_id=item_id,
            text=target["text"],
            source="manual",
        )
        new_version["reverted_from"] = version_num
        return new_version

    def diff_versions(self, item_id: str, v1: int, v2: int) -> dict[str, Any]:
        """Возвращает текстовый diff между двумя версиями.

        Args:
            item_id: ID записи истории.
            v1: Номер первой версии (база).
            v2: Номер второй версии (новая).

        Returns:
            Словарь с полями:
                - item_id, v1, v2
                - text_v1, text_v2
                - unified_diff: список строк unified diff
                - added_lines: кол-во добавленных строк
                - removed_lines: кол-во удалённых строк

        Raises:
            KeyError: если одна из версий не найдена.
        """
        rec1 = self.get_version(item_id, v1)
        rec2 = self.get_version(item_id, v2)

        text1 = rec1["text"]
        text2 = rec2["text"]

        lines1 = text1.splitlines(keepends=True)
        lines2 = text2.splitlines(keepends=True)

        diff_lines = list(difflib.unified_diff(
            lines1,
            lines2,
            fromfile=f"v{v1}",
            tofile=f"v{v2}",
        ))

        added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))

        return {
            "item_id": item_id,
            "v1": v1,
            "v2": v2,
            "text_v1": text1,
            "text_v2": text2,
            "unified_diff": diff_lines,
            "added_lines": added,
            "removed_lines": removed,
        }

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_save_transcript_version(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: save_transcript_version.

        Параметры: item_id (str), text (str), source (str, опционально).
        """
        item_id = str(params.get("item_id", "")).strip()
        if not item_id:
            raise ValueError("Параметр item_id обязателен")
        text = params.get("text")
        if text is None:
            raise ValueError("Параметр text обязателен")
        text = str(text)
        source = str(params.get("source", "manual")).strip() or "manual"
        return self.save_version(item_id=item_id, text=text, source=source)

    def handle_get_transcript_versions(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_transcript_versions.

        Параметры: item_id (str).
        Ответ: {item_id, versions: [...], total: N}
        """
        item_id = str(params.get("item_id", "")).strip()
        if not item_id:
            raise ValueError("Параметр item_id обязателен")
        versions = self.get_versions(item_id)
        return {"item_id": item_id, "versions": versions, "total": len(versions)}

    def handle_revert_transcript_version(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: revert_transcript_version.

        Параметры: item_id (str), version_num (int).
        """
        item_id = str(params.get("item_id", "")).strip()
        if not item_id:
            raise ValueError("Параметр item_id обязателен")
        version_num = params.get("version_num")
        if version_num is None:
            raise ValueError("Параметр version_num обязателен")
        version_num = int(version_num)
        return self.revert_to_version(item_id=item_id, version_num=version_num)

    def purge_versions_for_item(self, item_id: str) -> int:
        """Удаляет все версии транскрипции для указанного item_id.

        Используется при удалении, архивировании или слиянии записей, чтобы
        избежать «висячих» версий в хранилище.

        Args:
            item_id: ID записи истории.

        Returns:
            Количество удалённых версий.
        """
        clean_id = str(item_id).strip()
        if not clean_id:
            return 0
        with self._lock:
            all_records = self._read_all()
            remaining = [r for r in all_records if r.get("item_id") != clean_id]
            purged = len(all_records) - len(remaining)
            if purged > 0:
                tmp = self._versions_path.with_suffix(".ndjson.tmp")
                try:
                    with tmp.open("w", encoding="utf-8") as fh:
                        for record in remaining:
                            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    tmp.replace(self._versions_path)
                except Exception:
                    tmp.unlink(missing_ok=True)
                    raise
            return purged
