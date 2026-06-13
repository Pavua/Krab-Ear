"""AutoDeduplicator — автоматическое обнаружение и обработка дубликатов записей Krab Ear.

Использует DuplicateDetector (текстовое сходство via SequenceMatcher) для выявления
дублирующихся транскрипций в истории. Поддерживает три действия:
  - "kept"    — новая запись, дубликатов не найдено
  - "skipped" — дубликат обнаружен, запись пропускается
  - "merged"  — запись достаточно похожа, чтобы быть объединённой (reserved)

Настройка AUTO_DEDUP_ENABLED (в DEFAULT_SETTINGS) управляет режимом по умолчанию.

W1243 F2 HIGH fix:
  - _MAX_DEDUP_SCAN = 1000: ограничение полного сканирования (run_deduplication)
    берёт только последние 1000 записей, чтобы избежать O(n²) на 10k+ истории.
  - Записи без поля ts трактуются как самые старые (timestamp=0) — вне 60-сек окна.
  - run_deduplication выполняется в фоновом потоке (daemon=True); IPC-обработчик
    возвращает немедленно {"ok": True, "job_id": "..."}.
  - dedup_progress IPC-метод для опроса статуса фоновой задачи.
"""

from __future__ import annotations

import logging
import math
import threading
import uuid
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Callable, Optional

from core.duplicate_detector import DuplicateDetector

logger = logging.getLogger("KrabEar.Backend.AutoDedup")

# Порог выше которого запись считается дубликатом
DEFAULT_DEDUP_THRESHOLD: float = 0.9
# Порог выше которого схожие записи считаются кандидатами на слияние
MERGE_THRESHOLD: float = 0.95
# Настройка-флаг для DEFAULT_SETTINGS / runtime settings
AUTO_DEDUP_ENABLED: bool = False


def _sanitize_threshold(value: object) -> float:
    """Validate and clamp IPC threshold to [0.0, 1.0].

    wave-1770 HIGH: IPC handlers previously accepted NaN/Inf/negative threshold
    values which caused data-loss (negative → every recording silently dropped as
    duplicate) or silent dedup bypass (NaN makes all comparisons falsy).

    Returns *value* clamped to [0.0, 1.0], or DEFAULT_DEDUP_THRESHOLD on error.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_DEDUP_THRESHOLD
    if not math.isfinite(v):
        return DEFAULT_DEDUP_THRESHOLD
    return max(0.0, min(1.0, v))


# W1243 F2: максимальное число записей для полного сканирования run_deduplication
_MAX_DEDUP_SCAN: int = 1000

# Максимальное число одновременно хранимых dedup-задач в реестре _jobs.
# При превышении эвиктируется самый старый terminal-job (done/failed/cancelled).
# Предотвращает unbounded-growth утечку памяти при частых вызовах run_deduplication_async.
_MAX_TRACKED_JOBS: int = 100

# Maximum number of history items allowed for synchronous IPC deduplication.
# Larger histories block the IPC thread due to O(n²) SequenceMatcher comparisons.
MAX_DEDUP_ITEMS: int = 500

# Timestamp-заглушка для записей без поля ts — ставим в эпоху 0 (самые старые)
_MISSING_TS_PLACEHOLDER: str = "1970-01-01T00:00:00+00:00"

# W1245: Jaccard hybrid algorithm boundary constants
_JACCARD_LOW: float = 0.7    # below → use Jaccard only
_JACCARD_HIGH: float = 0.85  # above → use Jaccard only; between → blend with SequenceMatcher


def _text_similarity(a: str, b: str) -> float:
    """W1245: Jaccard similarity on lowercased word sets with SequenceMatcher fallback.

    For Jaccard scores in [_JACCARD_LOW, _JACCARD_HIGH] (indeterminate zone),
    blends with SequenceMatcher ratio to reduce false positives from short texts
    (old pure-SequenceMatcher gave ~0.91 for 'Привет как дела' vs 'Привет как').
    Returns 0.0 if either string is empty.
    """
    if not a or not b:
        # Both empty → identical (1.0) per convention, one empty → 0.0
        if not a and not b:
            return 1.0
        return 0.0
    set_a = set(a.lower().split())
    set_b = set(b.lower().split())
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    jaccard = intersection / union if union > 0 else 0.0
    # Indeterminate zone: blend with SequenceMatcher for higher precision
    if _JACCARD_LOW <= jaccard <= _JACCARD_HIGH:
        sm = SequenceMatcher(None, a.lower(), b.lower()).ratio()
        return (jaccard + sm) / 2.0
    return jaccard


def _parse_ts_for_sort(item: dict) -> float:
    """W1776 MED: parse a history item's timestamp to a sortable Unix float.

    Used by run_deduplication to order a duplicate group chronologically so the
    EARLIEST record can be picked as the original (deterministic by timestamp,
    not by page position).  Missing/empty/unparseable ts → 0.0 (sorts as oldest),
    consistent with the _MISSING_TS_PLACEHOLDER normalization.
    """
    ts = item.get("ts")
    if ts is None or ts == "":
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return 0.0


@dataclass
class DedupResult:
    """Результат проверки одной записи на дублирование."""
    is_duplicate: bool
    duplicate_of: str | None  # ID оригинальной записи (или None)
    similarity: float         # коэффициент сходства [0.0, 1.0]
    action_taken: str         # "kept" | "skipped" | "merged" | "privacy_skipped"


# W1248: sentinel instance returned when privacy_mode_enabled is True — avoids
# creating a new object on every call (identity check in tests: `result is _PRIVACY_SKIPPED`).
_PRIVACY_SKIPPED = DedupResult(
    is_duplicate=False,
    duplicate_of=None,
    similarity=0.0,
    action_taken="privacy_skipped",
)


class AutoDeduplicator:
    """Автоматическое обнаружение и обработка дубликатов транскрипций.

    Потокобезопасен: все счётчики и состояние фоновых задач защищены RLock.

    W1243 F2 HIGH fix:
      - run_deduplication теперь ограничен _MAX_DEDUP_SCAN записями.
      - Записи с отсутствующим ts получают заглушку 1970-01-01 (эпоха 0).
      - Фоновое выполнение: handle_run_deduplication возвращает job_id немедленно.
      - handle_dedup_progress — опрос статуса задачи.

    W1567 F1 HIGH fix (wire _text_similarity):
      - check_duplicate now uses _text_similarity (Jaccard hybrid, W1245) as the
        primary tier before delegating to DuplicateDetector.
      - Tier 1: similarity >= _TIER1_ROUTING_THRESHOLD (0.85) → definite-duplicate
        candidate; declared as duplicate only when sim >= caller's threshold.
      - Tier 2: _JACCARD_LOW (0.7) <= similarity < _TIER1_ROUTING_THRESHOLD → ambiguous,
        fall through to DuplicateDetector for SequenceMatcher confirmation.
      - Below _JACCARD_LOW (0.7) → not a duplicate, DuplicateDetector skipped.
      - 60-second time window enforced in tier-1 scan (mirrors DuplicateDetector behaviour).

    W1711 fix (honor caller threshold):
      - Tier-1 now respects the caller's `threshold` argument.  Previously the hardcoded
        0.85 class constant was used as the duplicate-decision gate, meaning a pair with
        sim=0.89 was reported as duplicate even when threshold=0.99.  Now:
          * sim >= _TIER1_ROUTING_THRESHOLD (0.85) → enters tier-1 candidate pool
          * is_duplicate=True only when sim >= threshold (the caller's value)
          * Raising threshold → less aggressive dedup; lowering → more aggressive.
      - recording_core_service callers now read `auto_dedup_threshold` from runtime
        settings and forward it to check_duplicate(threshold=...).  Previously callers
        omitted the argument, leaving the setting completely unwired (data-loss bug).
    """

    # W1567: routing boundary — above this Jaccard score the record enters tier-1 as a
    # definite-duplicate candidate (versus the ambiguous zone [_JACCARD_LOW, 0.85) that
    # falls through to tier-2 DuplicateDetector).  This constant controls the tier-1/tier-2
    # routing split, NOT the final duplicate decision — that uses the caller's `threshold`.
    # W1711: renamed from _SIMILARITY_THRESHOLD to make the intent unambiguous.
    _TIER1_ROUTING_THRESHOLD: float = 0.85

    # 60-second window for tier-1 scan (same as DuplicateDetector.DEFAULT_TIME_WINDOW_SECONDS).
    _TIME_WINDOW_SECONDS: int = 60

    def __init__(
        self,
        settings_provider: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        self._detector = DuplicateDetector()
        self._lock = threading.RLock()
        # W1245 F5: serializes concurrent check_duplicate calls to prevent race conditions.
        self._check_lock = threading.Lock()
        # Поставщик runtime-настроек: _get_runtime_setting(key, default) из BackendService.
        # Если не задан — проверка режима приватности пропускается (небезопасно, только для тестов).
        self._settings_provider = settings_provider

        # Статистика работы дедупликатора
        self._total_checked: int = 0
        self._duplicates_found: int = 0
        # Суммарная длина (chars) отклонённых дубликатов — для оценки сэкономленного места
        self._chars_saved: int = 0

        # Реестр фоновых dedup-задач: job_id -> state dict
        self._jobs: dict[str, dict[str, Any]] = {}

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

        W1243 F2: записи без поля ts трактуются как самые старые (ts=1970-01-01),
        что автоматически выводит их за пределы 60-секундного окна.

        Args:
            text: текст проверяемой транскрипции.
            timestamp: ISO-8601 строка временной метки новой записи.
            store: StateStore-совместимый объект с методом get_history_page().
            threshold: порог сходства [0..1], по умолчанию DEFAULT_DEDUP_THRESHOLD.

        Returns:
            DedupResult с полями is_duplicate, duplicate_of, similarity, action_taken.
        """
        text = (text or "").strip()
        if not text:
            return DedupResult(
                is_duplicate=False,
                duplicate_of=None,
                similarity=0.0,
                action_taken="kept",
            )

        # W1248: пропускаем дедупликацию в режиме приватности — сравнение текстов запрещено.
        # Returns _PRIVACY_SKIPPED sentinel (same instance, supports `result is _PRIVACY_SKIPPED`).
        if self._privacy_mode_enabled():
            logger.debug("check_duplicate: пропущено — активен режим приватности")
            return _PRIVACY_SKIPPED

        with self._check_lock:
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

            # W1243 F2: нормализуем ts — пустой/отсутствующий → эпоха 0 (вне окна)
            normalized_items = []
            for item in items:
                if not item.get("ts"):
                    item = dict(item)
                    item["ts"] = _MISSING_TS_PLACEHOLDER
                normalized_items.append(item)

            # ------------------------------------------------------------------
            # W1567 F1 HIGH: Tier-1 scan using _text_similarity (Jaccard hybrid).
            # Filter candidates to the 60-second time window first, then score.
            # ------------------------------------------------------------------
            try:
                new_ts = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, AttributeError):
                new_ts = None

            # W1711: best tier-1 candidate (sim >= _TIER1_ROUTING_THRESHOLD).
            # We track the highest-similarity item; duplicate is declared only when
            # sim >= threshold (the caller's value), not the hardcoded routing constant.
            tier1_best_id: str | None = None
            tier1_best_sim: float = 0.0
            tier1_best_text: str = ""
            ambiguous_candidates: list[dict] = []  # _JACCARD_LOW <= sim < _TIER1_ROUTING_THRESHOLD → tier-2

            for candidate in normalized_items:
                cand_text = str(candidate.get("text") or "").strip()
                if not cand_text:
                    continue

                # 60-second time window filter
                if new_ts is not None:
                    cand_ts_raw = candidate.get("ts") or ""
                    try:
                        cand_ts = datetime.fromisoformat(
                            str(cand_ts_raw).replace("Z", "+00:00")
                        ).timestamp()
                        if abs(new_ts - cand_ts) > self._TIME_WINDOW_SECONDS:
                            continue
                    except (ValueError, AttributeError):
                        # Unparseable ts → treated as outside window (safe)
                        continue

                sim = _text_similarity(text, cand_text)

                if sim >= self._TIER1_ROUTING_THRESHOLD:
                    # High-similarity candidate — track best; will declare duplicate
                    # below only if sim also meets the caller's threshold.
                    if tier1_best_id is None or sim > tier1_best_sim:
                        tier1_best_id = candidate.get("id")
                        tier1_best_sim = sim
                        tier1_best_text = cand_text
                elif sim >= _JACCARD_LOW:
                    # Ambiguous zone — collect for tier-2 DuplicateDetector confirmation
                    ambiguous_candidates.append(candidate)

            # W1776 HIGH DATA-LOSS fix: the tier-1 score is Jaccard over lowercased
            # WORD SETS — it is order- AND multiplicity-insensitive.  Two DISTINCT
            # recordings that happen to share the same bag of words score 1.0:
            #   'собака укусила человека' vs 'человека укусила собака' → 1.0
            #   'купи хлеб молоко яйца'   vs 'купи яйца молоко хлеб'   → 1.0
            # Short-circuiting to is_duplicate=True on such a pair caused the recording
            # to be silently dropped (recording_core_service early-returns on
            # is_duplicate=True → never persisted, no .md, no tombstone = irreversible
            # data loss of a DISTINCT recording).
            #
            # Fix: Jaccard now serves ONLY as a cheap candidate PRE-filter.  The final
            # tier-1 duplicate decision must be confirmed by an ORDER-SENSITIVE
            # difflib.SequenceMatcher ratio meeting the caller's threshold.  When the
            # Jaccard pre-filter passes but SequenceMatcher does not, we do NOT declare
            # a duplicate here — the candidate falls through to tier-2 (DuplicateDetector,
            # which itself uses SequenceMatcher) for a final, order-sensitive verdict.
            tier1_confirmed = False
            if tier1_best_id is not None and tier1_best_sim >= threshold:
                # Order-sensitive confirmation: never short-circuit to duplicate when
                # the SequenceMatcher ratio is below the caller's threshold.  This is
                # what blocks pure word-order swaps (Jaccard 1.0, SequenceMatcher low).
                sm_ratio = SequenceMatcher(
                    None, text.lower(), tier1_best_text.lower()
                ).ratio()
                if sm_ratio >= threshold:
                    tier1_confirmed = True

            if tier1_confirmed:
                # Tier-1 confirmed (Jaccard pre-filter + SequenceMatcher ≥ threshold):
                # report without re-running the full DuplicateDetector scan.  We surface
                # the Jaccard score as `similarity` (the established tier-1 contract);
                # only the duplicate DECISION is now order-sensitive.
                with self._lock:
                    self._duplicates_found += 1
                    self._chars_saved += len(text)

                action = "merged" if tier1_best_sim >= MERGE_THRESHOLD else "skipped"
                logger.debug(
                    "Дубликат (tier-1 Jaccard+SequenceMatcher confirm): jaccard=%.3f "
                    "threshold=%.3f original_id=%s action=%s",
                    tier1_best_sim,
                    threshold,
                    tier1_best_id,
                    action,
                )
                return DedupResult(
                    is_duplicate=True,
                    duplicate_of=tier1_best_id,
                    similarity=tier1_best_sim,
                    action_taken=action,
                )

            # If tier-1 found a high-Jaccard candidate but it was NOT confirmed as a
            # duplicate above (either Jaccard < threshold, or the order-sensitive
            # SequenceMatcher ratio fell below threshold — e.g. a pure word-order swap),
            # add it to ambiguous_candidates so tier-2 makes the final SequenceMatcher
            # decision at the caller's threshold.  This guarantees an order-swapped,
            # distinct recording is never declared a duplicate.
            if tier1_best_id is not None:
                # Re-scan to find the actual candidate dict (we only stored the id/sim)
                for candidate in normalized_items:
                    if candidate.get("id") == tier1_best_id:
                        if candidate not in ambiguous_candidates:
                            ambiguous_candidates.append(candidate)
                        break

            # ------------------------------------------------------------------
            # Tier-2: run DuplicateDetector (SequenceMatcher) on ambiguous
            # candidates (0.7 <= Jaccard < 0.85) only, or on ALL normalized items
            # when no new_ts was available (legacy behaviour preserved).
            # ------------------------------------------------------------------
            tier2_candidates = ambiguous_candidates if new_ts is not None else normalized_items

            best_similarity = 0.0
            duplicate_of: str | None = None

            if tier2_candidates:
                # Добавляем новую запись как временный элемент для find_duplicates
                new_item: dict[str, Any] = {
                    "id": "__new__",
                    "text": text,
                    "ts": timestamp,
                }
                candidates_for_detector = tier2_candidates + [new_item]

                groups = self._detector.find_duplicates(
                    candidates_for_detector, similarity_threshold=threshold
                )

                for group in groups:
                    ids_in_group = {item.get("id") for item in group.items}
                    if "__new__" not in ids_in_group:
                        continue

                    existing = [
                        item for item in group.items if item.get("id") != "__new__"
                    ]
                    if not existing:
                        continue

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
                    "Дубликат (tier-2 SequenceMatcher): similarity=%.3f, original_id=%s, action=%s",
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
        """Сканирует историю (с ограничением _MAX_DEDUP_SCAN) и возвращает отчёт о дубликатах.

        W1243 F2 HIGH fix:
          - Загружает не более _MAX_DEDUP_SCAN (1000) записей — берёт самые свежие.
          - Записи с пустым/отсутствующим ts получают заглушку _MISSING_TS_PLACEHOLDER,
            что выводит их за пределы 60-секундного окна check_duplicate.

        Не удаляет записи автоматически — только возвращает список групп
        дубликатов для принятия решения пользователем/системой.

        Args:
            store: StateStore-совместимый объект с методом get_history_page().
            threshold: порог текстового сходства [0..1].

        Returns:
            dict с полями:
              - total_scanned: int
              - total_in_store: int   — реальный размер истории (до ограничения)
              - capped: bool          — True если история обрезана до _MAX_DEDUP_SCAN
              - duplicate_groups: int
              - duplicates: list[dict]  — каждый элемент: {original_id, duplicate_ids, similarity}
        """
        # W1248: пропускаем в режиме приватности — сравнение текстов запрещено.
        if self._privacy_mode_enabled():
            logger.debug("run_deduplication: пропущено — активен режим приватности")
            return {
                "total_scanned": 0,
                "total_in_store": 0,
                "capped": False,
                "duplicate_groups": 0,
                "duplicates": [],
                "skipped_reason": "privacy_mode",
            }

        all_items: list[dict] = []
        cursor: str | None = None
        total_in_store: int = 0

        # Загружаем историю постранично до _MAX_DEDUP_SCAN записей
        while len(all_items) < _MAX_DEDUP_SCAN:
            remaining = _MAX_DEDUP_SCAN - len(all_items)
            page_size = min(200, remaining)
            try:
                page, next_cursor = store.get_history_page(cursor=cursor, limit=page_size)
            except Exception:
                logger.exception("Ошибка загрузки истории для run_deduplication")
                break

            if not page:
                break

            all_items.extend(page)
            total_in_store += len(page)
            cursor = next_cursor
            if cursor is None:
                break

        # Определяем реальный total_in_store (если обрезали — делаем ещё один запрос для подсчёта)
        # Упрощение: если страниц было меньше _MAX_DEDUP_SCAN — total_in_store == len(all_items)
        capped = len(all_items) >= _MAX_DEDUP_SCAN and cursor is not None

        # W1243 F2: нормализуем ts у записей без метки времени
        normalized_items = []
        for item in all_items:
            if not item.get("ts"):
                item = dict(item)
                item["ts"] = _MISSING_TS_PLACEHOLDER
            normalized_items.append(item)

        groups = self._detector.find_duplicates(normalized_items, similarity_threshold=threshold)

        duplicates_list: list[dict] = []
        for group in groups:
            if not group.items:
                continue
            # W1776 MED fix: pick the original DETERMINISTICALLY by timestamp, NOT by
            # page position.  get_history_page returns items newest-first, so the old
            # `group.items[0]` picked the NEWEST record as the "original" and de-indexed
            # the OLDER, true-original record — the inverse of intended behaviour.
            # Sort the group by parsed ts ascending and treat the EARLIEST as the
            # original; everything later is a duplicate.  Records with an unparseable
            # or missing ts sort as oldest (epoch 0) — consistent with the
            # _MISSING_TS_PLACEHOLDER normalization applied above.
            ordered = sorted(group.items, key=_parse_ts_for_sort)
            original_id = ordered[0].get("id", "")
            duplicate_ids = [item.get("id", "") for item in ordered[1:]]
            duplicates_list.append({
                "original_id": original_id,
                "duplicate_ids": duplicate_ids,
                "similarity": group.similarity,
            })

        logger.info(
            "run_deduplication: проверено %d записей (capped=%s), найдено %d групп дубликатов",
            len(normalized_items),
            capped,
            len(duplicates_list),
        )

        return {
            "total_scanned": len(normalized_items),
            "total_in_store": total_in_store,
            "capped": capped,
            "duplicate_groups": len(duplicates_list),
            "duplicates": duplicates_list,
        }

    # ------------------------------------------------------------------
    # Background job management (W1243 F2)
    # ------------------------------------------------------------------

    def _evict_oldest_terminal_job_locked(self) -> None:
        """Эвиктирует самый старый terminal-job (done/failed/cancelled) из реестра.

        Вызывается под self._lock при превышении _MAX_TRACKED_JOBS.
        Итерирует по _jobs в порядке вставки (Python 3.7+ dict) и удаляет первый
        найденный terminal-job. Если terminal-задач нет — ничего не делает
        (running/queued задачи защищены от эвикции).
        """
        _terminal = {"done", "failed", "cancelled"}
        for jid, job in self._jobs.items():
            if job.get("status") in _terminal:
                del self._jobs[jid]
                logger.debug(
                    "AutoDedup: эвиктирован terminal-job %s (реестр превысил %d записей)",
                    jid,
                    _MAX_TRACKED_JOBS,
                )
                return

    def _create_dedup_job(self) -> str:
        """Создаёт новую запись в реестре фоновых dedup-задач.

        Перед добавлением проверяет ёмкость реестра: если len(_jobs) >= _MAX_TRACKED_JOBS,
        эвиктирует самый старый terminal-job, чтобы предотвратить unbounded-growth.
        """
        job_id = f"dedup-{uuid.uuid4().hex[:8]}"
        with self._lock:
            # Эвикция при превышении лимита: удаляем самый старый завершённый job
            if len(self._jobs) >= _MAX_TRACKED_JOBS:
                self._evict_oldest_terminal_job_locked()
            self._jobs[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "started_at": time.monotonic(),
                "finished_at": None,
                "result": None,
                "error": None,
            }
        return job_id

    def _update_dedup_job(self, job_id: str, **fields: Any) -> None:
        """Обновляет поля задачи в реестре."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def get_dedup_job(self, job_id: str) -> dict[str, Any] | None:
        """Возвращает снимок состояния задачи (или None если не существует)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            snapshot = dict(job)
        started_at = snapshot.get("started_at") or 0.0
        finished_at = snapshot.get("finished_at")
        now = finished_at if finished_at is not None else time.monotonic()
        snapshot["elapsed_sec"] = round(max(0.0, now - started_at), 3)
        return snapshot

    def run_deduplication_async(
        self,
        store: Any,
        threshold: float = DEFAULT_DEDUP_THRESHOLD,
    ) -> str:
        """Запускает run_deduplication в фоновом потоке (daemon=True).

        W1243 F2: IPC немедленно возвращает job_id; прогресс доступен через
        get_dedup_job(job_id) или IPC-метод dedup_progress.

        Returns:
            job_id: строка-идентификатор задачи.
        """
        job_id = self._create_dedup_job()

        def _worker() -> None:
            self._update_dedup_job(job_id, status="running")
            try:
                result = self.run_deduplication(store=store, threshold=threshold)
                self._update_dedup_job(
                    job_id,
                    status="done",
                    result=result,
                    finished_at=time.monotonic(),
                )
            except Exception as exc:
                logger.exception("run_deduplication_async failed for job_id=%s", job_id)
                self._update_dedup_job(
                    job_id,
                    status="failed",
                    error=str(exc),
                    finished_at=time.monotonic(),
                )

        t = threading.Thread(target=_worker, daemon=True, name=f"AutoDedup-{job_id}")
        t.start()
        return job_id

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
    # Внутренние вспомогательные методы
    # ------------------------------------------------------------------

    def _privacy_mode_enabled(self) -> bool:
        """Возвращает True если включён режим приватности (запрещено сравнивать тексты).

        Использует settings_provider (инжектированный из BackendService) для чтения
        runtime-настройки 'privacy_mode_enabled'. Без провайдера — всегда False (не блокирует).
        """
        if self._settings_provider is None:
            return False
        try:
            return bool(self._settings_provider("privacy_mode_enabled", False))
        except Exception:
            logger.warning("_privacy_mode_enabled: ошибка чтения настройки, считаем False")
            return False

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
        # wave-1770 HIGH: validate before use — negative/NaN/Inf cause data-loss.
        threshold = _sanitize_threshold(params.get("threshold", DEFAULT_DEDUP_THRESHOLD))
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

        W1248: поддерживает _semantic_searcher — если передан, вызывает remove_item
        для каждого найденного дубликата (исключение логируется и не прокидывается).

        W1243 F2: запускает сканирование синхронно и возвращает полный результат.
        Для асинхронного режима используйте run_deduplication_async.

        Params:
            threshold (float, optional): порог сходства, по умолчанию 0.9.
            store: передаётся из BackendService.
            _semantic_searcher (optional): объект с методом remove_item(id).

        Returns:
            dict с полями из run_deduplication.
        """
        # wave-1770 HIGH: validate threshold before use.
        threshold = _sanitize_threshold(params.get("threshold", DEFAULT_DEDUP_THRESHOLD))
        store = params.get("_store")
        if store is None:
            raise ValueError("store не передан в handle_run_deduplication")
        semantic_searcher = params.get("_semantic_searcher")

        # W1248: privacy mode check must happen before any store access.
        if self._privacy_mode_enabled():
            logger.debug("handle_run_deduplication: пропущено — активен режим приватности")
            return {
                "total_scanned": 0,
                "total_in_store": 0,
                "capped": False,
                "duplicate_groups": 0,
                "duplicates": [],
                "skipped_reason": "privacy_mode",
            }

        # G2: cap synchronous IPC deduplication to avoid blocking the IPC thread
        # with O(n²) SequenceMatcher comparisons on large histories.
        try:
            probe_page, _ = store.get_history_page(cursor=None, limit=MAX_DEDUP_ITEMS + 1)
            history_items = probe_page
        except Exception:
            history_items = []
        if len(history_items) > MAX_DEDUP_ITEMS:
            return {"ok": False, "reason": f"too many items (max {MAX_DEDUP_ITEMS})"}

        result = self.run_deduplication(store=store, threshold=threshold)

        # W1248: notify semantic searcher about removed duplicates
        if semantic_searcher is not None and result.get("duplicates"):
            for group in result["duplicates"]:
                for dup_id in group.get("duplicate_ids", []):
                    try:
                        semantic_searcher.remove_item(dup_id)
                    except Exception:
                        logger.warning(
                            "handle_run_deduplication: semantic_searcher.remove_item(%s) failed",
                            dup_id,
                        )

        return result

    def handle_dedup_progress(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик метода dedup_progress — опрос статуса фоновой dedup-задачи.

        Params:
            job_id (str): идентификатор задачи, полученный из run_deduplication.

        Returns:
            dict с полями: job_id, status, elapsed_sec, result (или None), error (или None).
            Если job_id не найден — возвращает {"found": False}.
        """
        job_id = str(params.get("job_id", "")).strip()
        if not job_id:
            raise ValueError("job_id обязателен для dedup_progress")

        state = self.get_dedup_job(job_id)
        if state is None:
            return {"found": False, "job_id": job_id}

        return {
            "found": True,
            "job_id": state["job_id"],
            "status": state["status"],
            "elapsed_sec": state.get("elapsed_sec", 0.0),
            "result": state.get("result"),
            "error": state.get("error"),
        }

    def handle_get_dedup_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик метода get_dedup_stats."""
        return self.get_dedup_stats()
