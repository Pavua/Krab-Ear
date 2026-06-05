"""TranscriptVersionManager — версионирование текста транскрипций Krab Ear.

Позволяет отслеживать историю редактирования текста для каждой записи.
Данные сохраняются в {data_dir}/transcript_versions.ndjson в append-only формате.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.TranscriptVersioning")

# Допустимые источники версии
VALID_SOURCES = frozenset({"stt_raw", "stt_cleaned", "llm_rewrite", "manual", "import"})
_VERSIONS_FILE = "transcript_versions.ndjson"

# F1: максимальное количество версий на одну запись истории.
# При превышении — oldest versions (с наименьшим version_num) удаляются.
MAX_VERSIONS_PER_ITEM = 50

# W1423/W1563: максимальный размер текста одной версии.
# Предотвращает неограниченный рост NDJSON-файла при сохранении очень длинных текстов.
_MAX_TEXT_BYTES = 256 * 1024  # 256 KB per version — prevents unbounded version blob growth


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

    def __init__(
        self,
        data_dir: Path | str,
        settings_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._versions_path = self._data_dir / _VERSIONS_FILE
        self._lock = threading.Lock()
        # wave-36 (HIGH B2): optional settings provider so read handlers can honour
        # privacy mode. Returns the full settings dict; None → privacy gate is a no-op
        # (preserves backward compatibility for the data-dir-only constructor in tests).
        self._settings_fn = settings_fn
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._versions_path.touch(exist_ok=True)

    # ------------------------------------------------------------------
    # Внутренние хелперы
    # ------------------------------------------------------------------

    def _is_privacy_mode(self) -> bool:
        """True если privacy_mode_enabled активен (через settings_fn, если подключён)."""
        if self._settings_fn is None:
            return False
        try:
            settings = self._settings_fn()
            return bool(settings.get("privacy_mode_enabled", False))
        except Exception:  # noqa: BLE001
            return False

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

    def _rewrite_all(self, records: list[dict[str, Any]]) -> None:
        """Перезаписывает весь файл NDJSON (используется для применения cap/удаления).

        W1770 data-integrity fix: запись идёт АТОМАРНО через tmp-файл + fsync +
        os.replace, как в StateStore._compact_unlocked. Плоский write_text/open('w')
        не атомарен — крах посреди перезаписи усекал/повреждал ВСЮ историю версий.
        Паттерн: пишем в {path}.tmp, flush+fsync (данные гарантированно на диске),
        затем os.replace(tmp, path) (атомарная подмена inode). При любой ошибке —
        удаляем tmp и пробрасываем исключение, оригинальный файл остаётся целым.
        """
        content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        tmp_path = self._versions_path.with_suffix(".ndjson.tmp")
        _replaced = False
        try:
            with tmp_path.open("w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                # fsync до rename — гарантия, что данные на диске при крахе в момент replace
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._versions_path)
            _replaced = True
        finally:
            # Если атомарная подмена не состоялась (ошибка записи/fsync), убираем tmp,
            # чтобы не оставлять мусор. После успешного replace tmp уже исчез — guard на exists.
            if not _replaced:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _enforce_version_cap(self, item_id: str, all_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """F1: если количество версий для item_id превышает MAX_VERSIONS_PER_ITEM,
        удаляет старейшие (с наименьшим version_num) до достижения лимита.

        Возвращает обновлённый список всех записей (уже перезаписанный на диск
        только если был превышен лимит).
        """
        item_versions = [r for r in all_records if r.get("item_id") == item_id]
        excess = len(item_versions) - MAX_VERSIONS_PER_ITEM
        if excess <= 0:
            return all_records
        # Сортируем по version_num (ASC) и берём excess старейших для удаления
        item_versions.sort(key=lambda r: r.get("version_num", 0))
        to_drop = set(id(r) for r in item_versions[:excess])
        trimmed = [r for r in all_records if id(r) not in to_drop]
        self._rewrite_all(trimmed)
        logger.debug(
            "Версии для item_id=%r обрезаны до %d (удалено %d старейших)",
            item_id, MAX_VERSIONS_PER_ITEM, excess,
        )
        return trimmed

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
        # W1410 F1: skip empty text
        if not text or not text.strip():
            return None
        source = str(source).strip()
        if source not in VALID_SOURCES:
            raise ValueError(f"Недопустимый source {source!r}. Допустимые: {sorted(VALID_SOURCES)}")
        # W1563: raise ValueError for oversized text (restored from W1423 truncation approach)
        _text_bytes = len(text.encode("utf-8"))
        if _text_bytes > _MAX_TEXT_BYTES:
            raise ValueError(
                f"version text {_text_bytes} bytes exceeds _MAX_TEXT_BYTES={_MAX_TEXT_BYTES}"
            )

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
            # F1: применяем лимит после добавления (cap = MAX_VERSIONS_PER_ITEM)
            all_records.append(record)
            self._enforce_version_cap(item_id, all_records)
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
        revert_text = target["text"]
        clean_id = str(item_id).strip()
        if not isinstance(revert_text, str) or not revert_text.strip():
            raise ValueError("Текст целевой версии пустой — откат невозможен")
        if len(revert_text.encode("utf-8")) > _MAX_TEXT_BYTES:
            revert_text = revert_text.encode("utf-8")[:_MAX_TEXT_BYTES - 11].decode("utf-8", errors="ignore") + "[TRUNCATED]"
        with self._lock:
            all_records = self._read_all()
            next_num = self._next_version_num(clean_id, all_records)
            record: dict[str, Any] = {
                "item_id": clean_id,
                "version_num": next_num,
                "text": revert_text,
                "source": "manual",
                "reverted_from": version_num,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._append(record)
            all_records.append(record)
            self._enforce_version_cap(clean_id, all_records)
            return dict(record)

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
    # F2: Каскадное удаление (privacy)
    # ------------------------------------------------------------------

    def delete_versions_for(self, item_id: str) -> int:
        """Удаляет все версии для указанного item_id из персистентного хранилища.

        Вызывается при удалении записи истории через delete_history_item,
        чтобы не допустить privacy bypass (версии иначе остаются в файле навсегда).

        Args:
            item_id: ID записи истории.

        Returns:
            Количество удалённых версий.
        """
        item_id = str(item_id).strip()
        if not item_id:
            return 0
        with self._lock:
            all_records = self._read_all()
            kept = [r for r in all_records if r.get("item_id") != item_id]
            deleted = len(all_records) - len(kept)
            if deleted > 0:
                self._rewrite_all(kept)
                logger.debug("Удалено %d версий для item_id=%r", deleted, item_id)
        return deleted

    purge_versions_for_item = delete_versions_for  # W1259

    def cleanup_for_ids(self, item_ids: list[str]) -> int:
        """Каскадно удаляет версии для набора item_ids (bulk cleanup).

        Используется в cleanup_old_history после пакетного удаления записей.

        Args:
            item_ids: Список ID записей, версии которых нужно удалить.

        Returns:
            Общее количество удалённых версий.
        """
        if not item_ids:
            return 0
        id_set = {str(i).strip() for i in item_ids if str(i).strip()}
        if not id_set:
            return 0
        with self._lock:
            all_records = self._read_all()
            kept = [r for r in all_records if r.get("item_id") not in id_set]
            deleted = len(all_records) - len(kept)
            if deleted > 0:
                self._rewrite_all(kept)
                logger.debug(
                    "Bulk cleanup: удалено %d версий для %d item_ids",
                    deleted, len(id_set),
                )
        return deleted

    def clear_all(self) -> int:
        """Полностью очищает хранилище версий (privacy-purge / wipe-all).

        Безусловно усекает transcript_versions.ndjson до пустого файла под
        _lock — НИ ОДНОЙ версии (включая версии уже удалённых orphan-записей)
        не остаётся на диске. Используется ТОЛЬКО из handle_purge_all_data:
        в отличие от cleanup_for_ids(current_ids), который оставляет версии
        записей, чьи item_id уже исчезли из истории, clear_all() — корректный
        безусловный privacy-wipe.

        Запись атомарна (tmp+fsync+replace через _rewrite_all). Идемпотентен.

        Returns:
            Количество удалённых версий (до очистки).
        """
        with self._lock:
            removed = len(self._read_all())
            if removed > 0:
                self._rewrite_all([])
                logger.info(
                    "clear_all: transcript_versions.ndjson очищен (privacy-purge), удалено %d версий",
                    removed,
                )
        return removed

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
        # Privacy mode gate (wave-36, HIGH B2): returns the FULL edit history of a
        # transcript's text (every saved version is cleartext PII). Withhold while
        # privacy mode is active — schema-parity empty response.
        if self._is_privacy_mode():
            return {"item_id": item_id, "versions": [], "total": 0, "reason": "privacy_mode_active"}
        versions = self.get_versions(item_id)
        return {"item_id": item_id, "versions": versions, "total": len(versions)}

    def handle_revert_transcript_version(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: revert_transcript_version.

        Privacy gate (wave-1770 HIGH): reverting reads transcript text from version
        history and creates a new manual version. Without this gate, an attacker could
        extract transcript text by reverting versions even while privacy mode is active,
        bypassing the gate on handle_get_transcript_versions. Gate symmetric with its
        companion handler.

        Параметры: item_id (str), version_num (int).
        """
        if self._is_privacy_mode():
            return {"ok": False, "reason": "privacy_mode_active"}
        item_id = str(params.get("item_id", "")).strip()
        if not item_id:
            raise ValueError("Параметр item_id обязателен")
        version_num = params.get("version_num")
        if version_num is None:
            raise ValueError("Параметр version_num обязателен")
        version_num = int(version_num)
        return self.revert_to_version(item_id=item_id, version_num=version_num)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def purge_orphaned_versions(self, active_item_ids: set[str]) -> int:
        """Удаляет версии для item_id-ов, которых больше нет в активной истории.

        Вызывается после компактирования StateStore, когда tombstone-записи
        окончательно вычеркнуты и соответствующие item_id исчезли из хранилища.

        Args:
            active_item_ids: Множество item_id-ов, которые остаются активными
                             после компактирования.

        Returns:
            Количество удалённых версий (строк).
        """
        with self._lock:
            all_records = self._read_all()
            kept = [r for r in all_records if r.get("item_id") in active_item_ids]
            purged = len(all_records) - len(kept)
            if purged > 0:
                try:
                    # W1770: атомарная перезапись (tmp+fsync+replace) через _rewrite_all,
                    # вместо прежнего не-атомарного open('w') — крах посреди записи
                    # больше не повреждает всю историю версий.
                    self._rewrite_all(kept)
                    logger.info(
                        "purge_orphaned_versions: удалено %d версий для tombstone-записей",
                        purged,
                    )
                except Exception as exc:
                    logger.error("purge_orphaned_versions: ошибка записи: %s", exc)
                    return 0
        return purged
