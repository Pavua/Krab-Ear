"""Семантический поиск по истории транскрипций через sentence embeddings.

Использует multilingual-e5-base или mxbai-embed-large (через sentence-transformers).
Хранит embeddings в embeddings.npy + индекс id→row в embeddings_index.json.
Fallback на keyword search если модель не загружена.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# G3-MEM-05 (M05-04): версия формата единого снимка embeddings_snapshot.npz.
_SNAPSHOT_VERSION = 1


class SnapshotNotPersistedError(RuntimeError):
    """Запись удалена из RAM-индекса, но снимок на диске обновить не удалось.

    Эмбеддинг удалённой записи остаётся в embeddings_snapshot.npz и вернётся после
    рестарта — вызывающий обязан считать удаление неполным (ревью #2229, F6).
    """


#: M04-05: durable флаг «индекс не соответствует истории» (после restore истории).
REINDEX_FLAG_NAME = "embeddings_reindex_required.flag"


def invalidate_index_files(data_dir: Path) -> bool:
    """M04-05: обнулить индекс на ДИСКЕ и поставить флаг перестройки (без экземпляра).

    Для докатки restore мимо HistoryService (auto_backup на старте процесса):
    in-memory индекса там ещё нет, а снимок эмбеддингов прежней истории иначе
    поднялся бы при первой загрузке модели. True — флаг на диске, снимков нет.
    """
    base = Path(data_dir)
    ok = True
    for name in ("embeddings_snapshot.npz", "embeddings.npy", "embeddings_index.json"):
        try:
            (base / name).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("semantic_search: не удалось удалить %s: %s", name, exc)
            ok = False
    try:
        fd = os.open(str(base / REINDEX_FLAG_NAME), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning("semantic_search: флаг перестройки не записан: %s", exc)
        ok = False
    return ok


class SemanticSearcher:
    """Индексирует и ищет историю транскрипций через sentence embeddings.

    Lazy load модели: модель загружается только при первом вызове index/search.
    Embeddings сохраняются в <data_dir>/embeddings.npy.
    Индекс id→row сохраняется в <data_dir>/embeddings_index.json.
    """

    def __init__(
        self,
        data_dir: Path,
        model_name: str = "intfloat/multilingual-e5-base",
        enabled: bool = False,
        max_items: int = 0,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._model_name = model_name
        self._enabled = enabled
        # wave-22 LOW: cap on indexed rows (FIFO eviction). <=0 → unbounded.
        try:
            self._max_items = int(max_items)
        except (TypeError, ValueError):
            self._max_items = 0

        self._model: Any = None
        self._model_lock = threading.Lock()
        self._model_loaded = False
        self._model_error: Optional[str] = None
        # F5/D5: monotonic-отметка последнего использования модели (ставится в
        # _get_model — единственной точке входа search/index). Нужна для
        # idle-выгрузки кондуктором.
        self._last_used_ts: float = 0.0

        # numpy arrays / index — loaded lazily
        self._embeddings: Any = None  # np.ndarray shape (N, D)
        self._index: list[str] = []  # id at position i → row i
        self._index_lock = threading.Lock()

        # wave-22 MED: monotonic purge counter. Incremented under _index_lock in
        # purge_all(). index_item/index_all capture this BEFORE the (slow) encode;
        # after re-acquiring the lock they ABORT the re-add if it changed — a purge
        # that straddled the encode becomes a hard barrier so a cleartext-derived
        # embedding of a just-purged transcript can never be re-persisted.
        self._purge_epoch = 0

        # wave-22 LOW: optional ErrorBus, late-injected by BackendService (same
        # pattern as HistoryService._error_bus). When None, error surfacing is a
        # silent no-op (warning still logged).
        self._error_bus: Any = None

        self._embeddings_path = self._data_dir / "embeddings.npy"
        self._index_path = self._data_dir / "embeddings_index.json"
        # G3-MEM-05 (M05-04): единый файл-снимок — две части (npy + json) заменяется
        # одним NPZ (с версией и согласованностью строк).
        self._snapshot_path = self._data_dir / "embeddings_snapshot.npz"
        # M04-05: durable флаг «индекс не соответствует истории» (после restore
        # истории). Снимается только полной перестройкой (index_all force=True).
        self._reindex_flag_path = self._data_dir / REINDEX_FLAG_NAME

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def model_loaded(self) -> bool:
        return self._model_loaded

    @property
    def model_error(self) -> Optional[str]:
        return self._model_error

    def status(self) -> dict:
        """Возвращает статус индексатора."""
        with self._index_lock:
            indexed_count = len(self._index)
        return {
            "enabled": self._enabled,
            "model_loaded": self._model_loaded,
            "model_name": self._model_name,
            "model_error": self._model_error,
            "indexed_count": indexed_count,
            # Остались файлы старого формата (embeddings.npy/json): они не читаются (префикс
            # query:→passage: делает векторы несовместимыми, M05-12), индекс содержит лишь новые
            # записи до полной перестройки (semantic_search_reindex force=True).
            "needs_reindex": self._legacy_files_present() or self._reindex_flag_present(),
        }

    def _reindex_flag_present(self) -> bool:
        try:
            return self._reindex_flag_path.exists()
        except OSError:
            return True

    def mark_reindex_required(self) -> bool:
        """M04-05: пометить индекс как требующий полной перестройки (durable).

        Вызывается после restore истории: эмбеддинги прежней истории уже
        удалены purge_all(), а флаг переживает рестарт, пока пользователь не
        перестроит индекс. True — флаг на диске.
        """
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._reindex_flag_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return True
        except OSError as exc:
            logger.warning("semantic_search: флаг перестройки не записан: %s", exc)
            return False

    def _legacy_files_present(self) -> bool:
        try:
            return self._embeddings_path.exists() or self._index_path.exists()
        except OSError:
            return False

    def index_item(self, item_id: str, text: str) -> bool:
        """Добавляет/обновляет embedding для одного элемента.

        Returns True при успехе, False если модель недоступна.
        """
        if not self._enabled:
            return False
        if not text or not text.strip():
            return False

        model = self._get_model()
        if model is None:
            return False

        try:
            import numpy as np
            # wave-22 MED: snapshot purge epoch BEFORE the slow encode. If a
            # purge_all() runs while we are encoding, the epoch will differ and we
            # must NOT re-add this (now-purged) cleartext-derived embedding.
            with self._index_lock:
                epoch_before = self._purge_epoch
            embedding = self._encode_passage(model, text)  # shape (D,)

            with self._index_lock:
                if self._purge_epoch != epoch_before:
                    # A purge happened during the encode → hard barrier. Do NOT
                    # mutate _embeddings/_index and do NOT call _save_locked()
                    # (otherwise embeddings.npy/embeddings_index.json would be
                    # re-created with data the user just purged).
                    logger.info(
                        "semantic_search: индексация %s отменена — "
                        "во время encode произошла очистка (purge barrier)",
                        item_id,
                    )
                    return False
                if item_id in self._index:
                    row = self._index.index(item_id)
                    self._embeddings[row] = embedding
                else:
                    if self._embeddings is None:
                        self._embeddings = embedding[np.newaxis, :]
                    else:
                        self._embeddings = np.vstack([self._embeddings, embedding[np.newaxis, :]])
                    self._index.append(item_id)
                self._evict_over_cap_locked()
                persisted = self._save_locked()
            # G3-P03-01: запись в память уже выполнена, но «успех» — только при
            # подтверждённой записи на диск; иначе False (+ error bus из _save_locked).
            return bool(persisted)
        except Exception as exc:
            logger.warning("semantic_search: ошибка индексации %s: %s", item_id, exc)
            return False

    def index_all(self, items: list[dict], force: bool = False) -> dict:
        """Переиндексирует все переданные items (список dicts с 'id' и 'text').

        При force=True перестраивает индекс с нуля.
        Returns {'indexed': N, 'skipped': M, 'errors': K}.
        """
        if not self._enabled:
            return {"indexed": 0, "skipped": len(items), "errors": 0, "reason": "disabled"}

        model = self._get_model()
        if model is None:
            return {"indexed": 0, "skipped": len(items), "errors": 0, "reason": self._model_error or "model_unavailable"}

        with self._index_lock:
            if force:
                self._embeddings = None
                self._index = []
            # wave-22 MED: snapshot purge epoch BEFORE the slow batch encode so a
            # concurrent purge_all() acts as a hard barrier (see index_item).
            epoch_before = self._purge_epoch

        indexed = 0
        skipped = 0
        errors = 0
        persisted = True

        try:
            import numpy as np
            texts_to_encode = []
            ids_to_encode = []

            for item in items:
                item_id = item.get("id", "")
                text = (item.get("text") or "").strip()
                if not item_id or not text:
                    skipped += 1
                    continue
                with self._index_lock:
                    already = item_id in self._index
                if already and not force:
                    skipped += 1
                    continue
                texts_to_encode.append(text)
                ids_to_encode.append(item_id)

            if texts_to_encode:
                try:
                    batch_embeddings = self._encode_batch(model, texts_to_encode)
                    with self._index_lock:
                        if self._purge_epoch != epoch_before:
                            # Purge straddled the batch encode → abort, persist
                            # nothing (the purge already cleared the files).
                            logger.info(
                                "semantic_search: batch-индексация отменена — "
                                "во время encode произошла очистка (purge barrier)"
                            )
                            return {"indexed": 0, "skipped": skipped,
                                    "errors": 0, "reason": "purged_during_encode"}
                        for _i, (eid, emb) in enumerate(zip(ids_to_encode, batch_embeddings)):
                            if eid in self._index:
                                row = self._index.index(eid)
                                self._embeddings[row] = emb
                            else:
                                if self._embeddings is None:
                                    self._embeddings = emb[np.newaxis, :]
                                else:
                                    self._embeddings = np.vstack([self._embeddings, emb[np.newaxis, :]])
                                self._index.append(eid)
                        self._evict_over_cap_locked()
                        # F9 (ревью #2229): результат записи раньше терялся — indexed=N при отказе
                        # диска. Полная перестройка (force) заодно снимает legacy-файлы.
                        persisted = bool(self._save_locked(drop_legacy=force))
                    if persisted:
                        indexed = len(ids_to_encode)
                    else:
                        errors = len(ids_to_encode)
                        logger.warning("semantic_search: снимок индекса не записан — indexed не засчитан")
                except Exception as exc:
                    logger.warning("semantic_search: ошибка batch индексации: %s", exc)
                    errors = len(ids_to_encode)
        except ImportError:
            return {"indexed": 0, "skipped": len(items), "errors": 0,
                    "reason": "numpy_unavailable"}

        return {"indexed": indexed, "skipped": skipped, "errors": errors}

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """Ищет top_k ближайших к query.

        Returns список {'id': str, 'score': float} отсортированных по убыванию score.
        Fallback: возвращает [] если модель недоступна.
        """
        if not self._enabled or not query.strip():
            return []

        model = self._get_model()
        if model is None:
            return []

        try:
            with self._index_lock:
                if self._embeddings is None or len(self._index) == 0:
                    return []
                embeddings = self._embeddings.copy()
                index = list(self._index)

            q_emb = self._encode_query(model, query)  # shape (D,)
            scores = self._cosine_similarity_batch(q_emb, embeddings)  # shape (N,)

            top_k = min(top_k, len(index))
            top_indices = scores.argsort()[::-1][:top_k]
            results = []
            for i in top_indices:
                results.append({"id": index[i], "score": float(scores[i])})
            return results
        except Exception as exc:
            logger.warning("semantic_search: ошибка поиска: %s", exc)
            return []

    def reset_model_error(self) -> dict:
        """Сбрасывает зафиксированную ошибку загрузки модели, позволяя повторную попытку.

        Очищает ``_model_error`` и ``_model``/``_model_loaded``, так что следующий
        вызов ``_get_model()`` попытается загрузить модель заново.  Полезно после
        временных сбоев (сеть, HuggingFace недоступен, недостаточно RAM и т.п.).

        NOTE (D3 — wave-31): этот метод сбрасывает ТОЛЬКО ошибку загрузки модели и
        состояние модели.  Он НЕ очищает in-memory индекс embeddings (_embeddings /
        _index) и НЕ удаляет файлы с диска.  Это намеренное поведение: пользователь
        хочет повторить загрузку модели, не теряя уже индексированные данные.
        Для полной очистки индекса используйте purge_all().

        Returns:
            {"reset": True, "previous_error": str|None}
        """
        with self._model_lock:
            previous = self._model_error
            self._model_error = None
            self._model = None
            self._model_loaded = False
        logger.info(
            "semantic_search: сброс ошибки модели, предыдущая: %s", previous or "—"
        )
        return {"reset": True, "previous_error": previous}

    def unload_model(self) -> bool:
        """Гасит ТОЛЬКО модель (память); индекс в RAM/на диске не трогает.

        Идемпотентен. `_model_error` НЕ выставляется — следующий запрос
        лениво поднимет модель заново (прецедент reset_model_error:261).
        """
        with self._model_lock:
            if self._model is None and not self._model_loaded:
                return False
            self._model = None
            self._model_loaded = False
        logger.info("semantic_search: модель выгружена (idle unload)")
        return True

    def unload_if_idle(self, idle_sec: float) -> bool:
        """Выгружает модель, если она простаивает >= idle_sec секунд."""
        try:
            idle = time.monotonic() - float(self._last_used_ts)
            threshold = float(idle_sec)
        except (TypeError, ValueError):
            return False
        if threshold <= 0 or idle < threshold:
            return False
        return self.unload_model()

    def purge_all(self) -> None:
        """Полностью очищает in-memory индекс и удаляет файлы embeddings с диска.

        Вызывается при purge_history для защиты PII — гарантирует, что
        embeddings.npy и embeddings_index.json не содержат личных данных
        после общей очистки истории.

        NOTE (D1 — wave-31): epoch bump выполняется ПЕРВЫМ под локом, до очистки
        памяти и удаления файлов.  Это устраняет resurrection race:
        _load_from_disk() вызывается lazily внутри _get_model() — если загрузка
        модели начинается ПОСЛЕ удаления файлов (step 2) но ДО прежнего bump
        (step 3), _load_from_disk читала бы пустые/отсутствующие файлы под
        СТАРЫМ epoch и могла бы переписать пустой индекс.  Теперь bump происходит
        на step 0: любая _load_from_disk или _save_locked, захватывающая _index_lock
        после этой точки, видит увеличенный epoch и обязана прекратить персистирование
        (паттерн из index_item / index_all).
        """
        with self._index_lock:
            # wave-31 D1: bump epoch FIRST — acts as a hard barrier for any
            # concurrent _load_from_disk / index_item / index_all that is still
            # in progress.  Must happen before the memory clear and before the
            # disk deletion so the epoch is already visible under lock when any
            # racing writer next acquires _index_lock.
            self._purge_epoch += 1
            self._embeddings = None
            self._index = []
        # Явные unlink на каждый файл (а не цикл по кортежу): статический гейт
        # полноты purge (scripts/audit_purge_coverage.py) читает AST и видит
        # только прямые вызовы с атрибутом пути.
        try:
            if self._embeddings_path.exists():
                self._embeddings_path.unlink()
        except Exception as exc:
            logger.warning("semantic_search: не удалось удалить embeddings.npy: %s", exc)
        try:
            if self._index_path.exists():
                self._index_path.unlink()
        except Exception as exc:
            logger.warning("semantic_search: не удалось удалить embeddings_index.json: %s", exc)
        try:
            if self._snapshot_path.exists():  # G3-MEM-05 (M05-04): единый NPZ-снимок
                self._snapshot_path.unlink()
        except Exception as exc:
            logger.warning("semantic_search: не удалось удалить embeddings_snapshot.npz: %s", exc)
        try:
            if self._reindex_flag_path.exists():  # M04-05: история пуста — перестраивать нечего
                self._reindex_flag_path.unlink()
        except Exception as exc:
            logger.warning("semantic_search: не удалось удалить флаг перестройки: %s", exc)
        logger.info("semantic_search: индекс и файлы embeddings очищены (purge_all)")

    def remove_item(self, item_id: str) -> bool:
        """Remove item from index.

        Returns True если удалено, False если записи в индексе не было (контракт прежний).
        Raises SnapshotNotPersistedError, если запись убрана из RAM, но снимок на диске
        обновить не удалось: эмбеддинг остался в embeddings_snapshot.npz и вернётся после
        рестарта — вызывающий обязан показать неполное удаление (ревью #2229, F6).

        Thread-safe. Shifts row indices for all items after the removed one.
        Also persists the updated index to disk.
        """
        try:
            import numpy as np
        except ImportError:
            return False

        with self._index_lock:
            if item_id not in self._index:
                return False
            idx = self._index.index(item_id)
            # Remove the item_id from the list
            self._index.pop(idx)
            # Remove the corresponding row from the embeddings matrix
            if self._embeddings is not None:
                if self._embeddings.shape[0] == 1:
                    self._embeddings = None
                else:
                    self._embeddings = np.delete(self._embeddings, idx, axis=0)
            # G3-MEM-05 (M05-04): вернуть статус подтверждения записи на диск.
            if not self._save_locked():
                raise SnapshotNotPersistedError(
                    f"снимок индекса не записан после удаления {item_id}"
                )
            return True
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_model(self) -> Any:
        """Lazy load sentence-transformers model. Thread-safe."""
        with self._model_lock:
            # F5/D5: отметка использования ДО раннего return — иначе загруженная
            # модель, к которой обратились через _get_model, осталась бы «свежей»
            # с точки зрения unload_if_idle.
            self._last_used_ts = time.monotonic()
            if self._model_loaded:
                return self._model
            if self._model_error:
                return None
            try:
                from sentence_transformers import SentenceTransformer
                logger.info("semantic_search: загружаем модель %s …", self._model_name)
                self._model = SentenceTransformer(self._model_name)
                self._model_loaded = True
                logger.info("semantic_search: модель загружена")
                self._load_from_disk()
                return self._model
            except ImportError:
                self._model_error = "sentence_transformers_not_installed"
                logger.warning(
                    "semantic_search: sentence-transformers не установлен. "
                    "Установите: pip install sentence-transformers"
                )
                return None
            except Exception as exc:
                self._model_error = str(exc)
                logger.warning("semantic_search: не удалось загрузить модель: %s", exc)
                return None

    def _encode_query(self, model: Any, text: str) -> Any:
        """Кодирует запрос (search). Возвращает np.ndarray shape (D,)."""
        return self._encode(model, text, prefix="query: ")

    def _encode_passage(self, model: Any, text: str) -> Any:
        """Кодирует документ (index_item/index_all). Возвращает np.ndarray shape (D,).

        G3-MEM-05 (M05-12): документы индексируются passage:, запросы query: —
        один и тот же документ при разных точках входа даёт одинаковый вектор.
        """
        return self._encode(model, text, prefix="passage: ")

    def _encode(self, model: Any, text: str, prefix: str = "query: ") -> Any:
        """Кодирует один текст. Возвращает np.ndarray shape (D,)."""
        # multilingual-e5 рекомендует prefix "query: " / "passage: "
        # mxbai-embed-large рекомендует "Represent this sentence: "
        # Для единообразия используем "query: " (работает с обоими)
        result = model.encode(prefix + text, normalize_embeddings=True)
        return result

    def _encode_batch(self, model: Any, texts: list[str]) -> Any:
        """Кодирует список текстов. Возвращает np.ndarray shape (N, D)."""
        prefix = "passage: "
        prefixed = [prefix + t for t in texts]
        return model.encode(prefixed, normalize_embeddings=True)

    @staticmethod
    def _cosine_similarity_batch(query_emb: Any, matrix: Any) -> Any:
        """Косинусное сходство query_emb (D,) со всеми строками matrix (N, D).

        Оба вектора нормализованы → dot product = cosine similarity.
        Returns np.ndarray shape (N,).
        """
        import numpy as np
        q = query_emb / (np.linalg.norm(query_emb) + 1e-10)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10
        normalized = matrix / norms
        return normalized @ q

    def _evict_over_cap_locked(self) -> None:
        """Drop oldest rows so the index never exceeds ``self._max_items``.

        Must be called under ``_index_lock``. FIFO / most-recent-N eviction:
        ``_index`` preserves insertion order, so the first rows are the oldest.
        No-op when ``_max_items <= 0`` (unbounded) or the index is within cap.
        """
        cap = self._max_items
        if cap <= 0:
            return
        n = len(self._index)
        if n <= cap:
            return
        drop = n - cap
        # Keep the most recent `cap` rows (drop the oldest `drop`). Pure slicing —
        # no numpy symbol needed; _embeddings is already an ndarray (or None).
        self._index = self._index[drop:]
        if self._embeddings is not None:
            if self._embeddings.shape[0] <= drop:
                self._embeddings = None
                self._index = []
            else:
                self._embeddings = self._embeddings[drop:]
        logger.info(
            "semantic_search: вытеснено %d старых строк (cap=%d, было=%d)",
            drop, cap, n,
        )

    def _push_error(self, code: str, message_debug: str) -> None:
        """Surface a persistence error via the attached ErrorBus, if wired.

        ``_error_bus`` is late-injected by ``BackendService.__init__`` (same
        pattern as ``HistoryService._error_bus``).  No-op when not wired so that
        unit tests without a full BackendService still work.  The caller is
        expected to also log a warning — this only adds the loud-error surface.
        """
        error_bus = getattr(self, "_error_bus", None)
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone

            entry = ERROR_REGISTRY.get(code, {})
            err = KrabError(
                severity=entry.get("severity", "error"),
                component="history",
                code=code,
                message_user=entry.get("user_msg_ru", "Ошибка истории"),
                message_debug=message_debug,
                timestamp=datetime.now(timezone.utc),
                context={"data_dir": str(self._data_dir)},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception:  # noqa: BLE001
            logger.exception("semantic_search: _push_error failed for code=%s", code)

    def _load_from_disk(self) -> None:
        """Загружает embeddings и индекс с диска (вызывается после загрузки модели).

        G3-MEM-05 (M05-03): resurrection race — если purge_all() выполнилась
        пока мы читали с диска, старые embeddings не могут вернуться в RAM.
        """
        try:
            import numpy as np
            with self._index_lock:
                epoch_before = self._purge_epoch

            if self._snapshot_path.exists():
                # G3-MEM-05 (M05-04): единый NPZ — версия формата должна совпадать
                # с _SNAPSHOT_VERSION; иначе — полная перестройка.
                with np.load(str(self._snapshot_path), allow_pickle=False) as data:
                    snap_version = data.get("version", _SNAPSHOT_VERSION)
                    snap_vectors = data["vectors"] if "vectors" in data else None
                    snap_ids = data["ids"] if "ids" in data else None
                version = snap_version
                if version != _SNAPSHOT_VERSION:
                    logger.warning(
                        "semantic_search: неверная версия снимка (%s) — полная перестройка",
                        version,
                    )
                    return
                if snap_vectors is None or snap_ids is None:
                    logger.error("semantic_search: в снимке нет vectors/ids — полная перестройка")
                    return
                vectors = snap_vectors
                ids = [str(x) for x in snap_ids]
                if vectors.shape[0] != len(ids):
                    logger.error(
                        "semantic_search: несоответствие размеров снимка "
                        "(vectors=%d, ids=%d) — полная перестройка",
                        vectors.shape[0],
                        len(ids),
                    )
                    return
                with self._index_lock:
                    if self._purge_epoch != epoch_before:
                        logger.info(
                            "semantic_search: загрузка с диска отменена — purge_all() после чтения"
                        )
                        return
                    # Пустой снимок (удалён последний элемент) хранится как (0, 0) —
                    # возвращаем в «неинициализированное» состояние, иначе следующий
                    # index_item не сможет стекнуть D-мерный вектор на 0-колоночную матрицу.
                    self._embeddings = vectors if len(ids) > 0 else None
                    self._index = ids
                logger.info("semantic_search: загружено %d embeddings с диска", len(ids))
            else:
                logger.info(
                    "semantic_search: снимок NPZ не найден — полная перестройка из истории"
                )
        except Exception as exc:
            logger.warning("semantic_search: не удалось загрузить embeddings с диска: %s", exc)

    def _save_locked(self, drop_legacy: bool = False) -> bool:
        """Сохраняет embeddings и индекс на диск атомарно. Должен вызываться под _index_lock.

        G3-MEM-05 (M05-04): единый файл-снимок ``embeddings_snapshot.npz`` —
        ``np.savez(vectors, ids, version)`` во временный файл + fsync +
        ``os.replace`` (нет состояния, где .npy новее .json или наоборот).
        Устаревшие ``embeddings.npy``/``embeddings_index.json`` удаляются.

        Возвращает True только если снимок подтвердён на диск; False — отказ
        (ошибка уже передана в error bus).
        """
        try:
            import numpy as np
            vectors = (
                self._embeddings
                if self._embeddings is not None
                else np.empty((0, 0), dtype=np.float32)
            )
            ids = np.asarray(self._index, dtype=np.str_)
            fd, tmp = tempfile.mkstemp(dir=str(self._data_dir), suffix=".npz")
            try:
                os.close(fd)
                with os.fdopen(os.open(tmp, os.O_WRONLY), "wb") as f:
                    np.savez(f, vectors=vectors, ids=ids, version=_SNAPSHOT_VERSION)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, str(self._snapshot_path))
                dir_fd = os.open(str(self._data_dir), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            # G3-MEM-05 (M05-04): устаревшие embeddings.npy/json удаляются ТОЛЬКО после полной
            # перестройки (drop_legacy=True из index_all(force=True)). Раньше их сносила первая же
            # единичная индексация — флаг needs_reindex пропадал, хотя индекс содержал лишь новые
            # записи (ревью #2229, F7). Purge чистит эти файлы так же, как и снимок.
            if drop_legacy:
                for p in (self._embeddings_path, self._index_path, self._reindex_flag_path):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception as exc:
            logger.warning("semantic_search: не удалось сохранить embeddings: %s", exc)
            # wave-22 LOW: persistence failure was previously swallowed (warning
            # only) while index_item still reported success — the on-disk index
            # silently diverged from memory. Surface it as a loud disk/history
            # error so the user knows the embeddings index is not being persisted.
            self._push_error(
                "history.write_fail",
                f"semantic_search _save_locked failed: {exc}",
            )
            return False
        return True

    # W1172: alias for backward compat
    remove = remove_item


def keyword_fallback_search(
    query: str,
    items: List[dict],
    top_k: int = 10,
) -> List[dict]:
    """Простой keyword fallback поиск если semantic модель недоступна.

    Returns список {'id': str, 'score': float} — score = кол-во совпавших слов / всего слов в query.
    """
    if not query.strip():
        return []
    query_words = set(query.lower().split())
    results = []
    for item in items:
        item_id = item.get("id", "")
        text = (item.get("text") or "").lower()
        if not item_id or not text:
            continue
        matched = sum(1 for w in query_words if w in text)
        if matched > 0:
            score = matched / max(len(query_words), 1)
            results.append({"id": item_id, "score": score})
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]
