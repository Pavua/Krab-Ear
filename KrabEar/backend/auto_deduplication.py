"""AutoDeduplicator — автоматическое обнаружение и обработка дубликатов записей Krab Ear.

Использует DuplicateDetector (текстовое сходство via SequenceMatcher) для выявления
дублирующихся транскрипций в истории. Поддерживает три действия:
  - "kept"    — новая запись, дубликатов не найдено
  - "skipped" — дубликат обнаружен, запись пропускается
  - "merged"  — запись достаточно похожа, чтобы быть объединённой (reserved)

Настройка AUTO_DEDUP_ENABLED (в DEFAULT_SETTINGS) управляет режимом по умолчанию.

Privacy gate (W1243 F4 MED):
  Когда privacy_mode_enabled=True все методы обнаружения дубликатов возвращают
  «не дубликат» без загрузки транскрипций из store в память. Это исключает хранение
  пользовательских текстов в оперативной памяти при работе в режиме приватности.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from core.duplicate_detector import DuplicateDetector

logger = logging.getLogger("KrabEar.Backend.AutoDedup")

# Порог выше которого запись считается дубликатом
DEFAULT_DEDUP_THRESHOLD: float = 0.9
# Порог выше которого схожие записи считаются кандидатами на слияние
MERGE_THRESHOLD: float = 0.95
# Настройка-флаг для DEFAULT_SETTINGS / runtime settings
AUTO_DEDUP_ENABLED: bool = False


@dataclass
class DedupResult:
    """Результат проверки одной записи на дублирование."""
    is_duplicate: bool
    duplicate_of: str | None  # ID оригинальной записи (или None)
    similarity: float         # коэффициент сходства [0.0, 1.0]
    action_taken: str         # "kept" | "skipped" | "merged" | "privacy_skipped"


# Sentinel возвращаемый при включённом privacy_mode — no-op, ничего не загружает.
_PRIVACY_SKIPPED = DedupResult(
    is_duplicate=False,
    duplicate_of=None,
    similarity=0.0,
    action_taken="privacy_skipped",
)


class AutoDeduplicator:
    """Автоматическое обнаружение и обработка дубликатов транскрипций.

    Потокобезопасен: все счётчики защищены RLock.

    Args:
        settings_provider: необязательный callable(key, default) → Any для чтения
            runtime-настроек (передаётся как BackendService._get_runtime_setting).
            Если не передан, privacy gate отключён (обратная совместимость).
    """

    def __init__(
        self,
        settings_provider: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        self._detector = DuplicateDetector()
        self._lock = threading.RLock()
        self._settings_provider = settings_provider

        # Статистика работы дедупликатора
        self._total_checked: int = 0
        self._duplicates_found: int = 0
        # Суммарная длина (chars) отклонённых дубликатов — для оценки сэкономленного места
        self._chars_saved: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _privacy_mode_enabled(self) -> bool:
        """Возвращает True если privacy_mode_enabled включён в runtime settings."""
        if self._settings_provider is None:
            return False
        try:
            return bool(self._settings_provider("privacy_mode_enabled", False))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def check_duplicate(
        self,
        text: str,
        timestamp: str,
        store: Any,
        threshold: float = DEFAULT_DEDUP_THRESHOLD,
    ) -> DedupResult:
        """Проверяет, является ли текст дубликатом существующей записи в store.

        Сравнивает text с последними активными записями истории в 60-секундном окне.
        При threshold >= MERGE_THRESHOLD действие помечается как "merged" (не "skipped").

        Privacy gate: если privacy_mode_enabled=True возвращает «не дубликат» без
        загрузки транскрипций из store (W1243 F4 MED fix).

        Args:
            text: текст проверяемой транскрипции.
            timestamp: ISO-8601 строка временной метки новой записи.
            store: StateStore-совместимый объект с методом get_history_page().
            threshold: порог сходства [0..1], по умолчанию DEFAULT_DEDUP_THRESHOLD.

        Returns:
            DedupResult с полями is_duplicate, duplicate_of, similarity, action_taken.
        """
        # Privacy gate — не загружаем тексты из store в режиме приватности
        if self._privacy_mode_enabled():
            logger.debug("check_duplicate: пропуск — privacy_mode включён")
            return _PRIVACY_SKIPPED

        text = (text or "").strip()
        if not text:
            return DedupResult(
                is_duplicate=False,
                duplicate_of=None,
                similarity=0.0,
                action_taken="kept",
            )

        with self._lock:
            self._total_checked += 1

        # Загружаем последние записи для сравнения (достаточно последних 50)
        try:
            items, _ = store.get_history_page(cursor=None, limit=50)
        except Exception:
            logger.exception("Ошибка загрузки истории для дедупликации")
            return DedupResult(
                is_duplicate=False,
                duplicate_of=None,
                similarity=0.0,
                action_taken="kept",
            )

        # Добавляем новую запись как временный элемент для find_duplicates
        new_item: dict[str, Any] = {
            "id": "__new__",
            "text": text,
            "ts": timestamp,
        }
        candidates = list(items) + [new_item]

        groups = self._detector.find_duplicates(candidates, similarity_threshold=threshold)

        best_similarity = 0.0
        duplicate_of: str | None = None

        for group in groups:
            # Проверяем, входит ли новая запись в эту группу дубликатов
            ids_in_group = {item.get("id") for item in group.items}
            if "__new__" not in ids_in_group:
                continue

            # Ищем существующую запись-оригинал (не нашу временную)
            existing = [
                item for item in group.items if item.get("id") != "__new__"
            ]
            if not existing:
                continue

            # Берём первый оригинал (самый ранний в списке = самый свежий из истории)
            original = existing[0]
            best_similarity = group.similarity
            duplicate_of = original.get("id")
            break

        if duplicate_of is not None:
            with self._lock:
                self._duplicates_found += 1
                self._chars_saved += len(text)

            action = "merged" if best_similarity >= MERGE_THRESHOLD else "skipped"
            logger.debug(
                "Дубликат обнаружен: similarity=%.3f, original_id=%s, action=%s",
                best_similarity,
                duplicate_of,
                action,
            )
            return DedupResult(
                is_duplicate=True,
                duplicate_of=duplicate_of,
                similarity=best_similarity,
                action_taken=action,
            )

        return DedupResult(
            is_duplicate=False,
            duplicate_of=None,
            similarity=best_similarity,
            action_taken="kept",
        )

    def run_deduplication(
        self,
        store: Any,
        threshold: float = DEFAULT_DEDUP_THRESHOLD,
    ) -> dict[str, Any]:
        """Сканирует всю историю и возвращает отчёт о дубликатах.

        Не удаляет записи автоматически — только возвращает список групп
        дубликатов для принятия решения пользователем/системой.

        Privacy gate: если privacy_mode_enabled=True возвращает пустой отчёт без
        загрузки транскрипций из store (W1243 F4 MED fix).

        Args:
            store: StateStore-совместимый объект с методом get_history_page().
            threshold: порог текстового сходства [0..1].

        Returns:
            dict с полями:
              - total_scanned: int
              - duplicate_groups: int
              - duplicates: list[dict]  — каждый элемент: {original_id, duplicate_ids, similarity}
              - skipped_reason: str (только если пропущено из-за privacy_mode)
        """
        # Privacy gate — не загружаем тексты из store в режиме приватности
        if self._privacy_mode_enabled():
            logger.debug("run_deduplication: пропуск — privacy_mode включён")
            return {
                "total_scanned": 0,
                "duplicate_groups": 0,
                "duplicates": [],
                "skipped_reason": "privacy_mode",
            }

        all_items: list[dict] = []
        cursor: str | None = None

        # Загружаем всю историю постранично
        while True:
            try:
                page, next_cursor = store.get_history_page(cursor=cursor, limit=200)
            except Exception:
                logger.exception("Ошибка загрузки истории для run_deduplication")
                break

            if not page:
                break

            all_items.extend(page)
            cursor = next_cursor
            if cursor is None:
                break

        groups = self._detector.find_duplicates(all_items, similarity_threshold=threshold)

        duplicates_list: list[dict] = []
        for group in groups:
            if not group.items:
                continue
            # Первый элемент считаем оригиналом (самый старый по позиции в странице)
            original_id = group.items[0].get("id", "")
            duplicate_ids = [item.get("id", "") for item in group.items[1:]]
            duplicates_list.append({
                "original_id": original_id,
                "duplicate_ids": duplicate_ids,
                "similarity": group.similarity,
            })

        logger.info(
            "run_deduplication: проверено %d записей, найдено %d групп дубликатов",
            len(all_items),
            len(duplicates_list),
        )

        return {
            "total_scanned": len(all_items),
            "duplicate_groups": len(duplicates_list),
            "duplicates": duplicates_list,
        }

    def get_dedup_stats(self) -> dict[str, Any]:
        """Возвращает статистику работы дедупликатора за текущую сессию.

        Returns:
            dict с полями:
              - total_checked: int   — всего проверено записей через check_duplicate
              - duplicates_found: int — обнаружено дубликатов
              - chars_saved: int     — суммарное число символов сохранённых от дублирования
              - dedup_rate: float    — доля дубликатов [0..1]
        """
        with self._lock:
            total = self._total_checked
            found = self._duplicates_found
            chars = self._chars_saved

        rate = found / total if total > 0 else 0.0
        return {
            "total_checked": total,
            "duplicates_found": found,
            "chars_saved": chars,
            "dedup_rate": round(rate, 4),
        }

    def reset_stats(self) -> None:
        """Сбрасывает накопленную статистику (для тестов и диагностики)."""
        with self._lock:
            self._total_checked = 0
            self._duplicates_found = 0
            self._chars_saved = 0

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def handle_check_duplicate(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик метода check_duplicate.

        Params:
            text (str): текст для проверки.
            timestamp (str): ISO-8601 метка времени (если пустая — берётся сейчас).
            threshold (float, optional): порог сходства, по умолчанию 0.9.
            store: передаётся из BackendService.

        Returns:
            DedupResult как dict.
        """
        text = str(params.get("text", "")).strip()
        timestamp = str(params.get("timestamp", "")).strip()
        if not timestamp:
            timestamp = datetime.now(tz=timezone.utc).isoformat()
        threshold = float(params.get("threshold", DEFAULT_DEDUP_THRESHOLD))
        store = params.get("_store")
        if store is None:
            raise ValueError("store не передан в handle_check_duplicate")

        result = self.check_duplicate(text=text, timestamp=timestamp, store=store, threshold=threshold)
        return {
            "is_duplicate": result.is_duplicate,
            "duplicate_of": result.duplicate_of,
            "similarity": result.similarity,
            "action_taken": result.action_taken,
        }

    def handle_run_deduplication(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик метода run_deduplication.

        Params:
            threshold (float, optional): порог сходства, по умолчанию 0.9.
            store: передаётся из BackendService.
            _semantic_searcher (optional): SemanticSearcher-совместимый объект с методом
                remove_item(item_id). Если передан, дублирующиеся записи удаляются из
                семантического индекса (W1247 — предотвращение stale embeddings).
        """
        threshold = float(params.get("threshold", DEFAULT_DEDUP_THRESHOLD))
        store = params.get("_store")
        if store is None:
            raise ValueError("store не передан в handle_run_deduplication")

        semantic_searcher = params.get("_semantic_searcher")
        result = self.run_deduplication(store=store, threshold=threshold)

        # W1247 semantic search stale embeddings fix:
        # Удаляем дублирующиеся записи из семантического индекса после дедупликации.
        if semantic_searcher is not None and result.get("duplicate_groups", 0) > 0:
            for entry in result.get("duplicates", []):
                for dup_id in entry.get("duplicate_ids", []):
                    try:
                        semantic_searcher.remove_item(dup_id)
                    except Exception:
                        logger.debug(
                            "handle_run_deduplication: не удалось удалить %s из семантического индекса",
                            dup_id,
                        )

        return result

    def handle_get_dedup_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик метода get_dedup_stats."""
        return self.get_dedup_stats()
