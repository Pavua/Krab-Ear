"""Семантический поиск по истории транскрипций через sentence embeddings.

Использует multilingual-e5-base или mxbai-embed-large (через sentence-transformers).
Хранит embeddings в embeddings.npy + индекс id→row в embeddings_index.json.
Fallback на keyword search если модель не загружена.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


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
        }

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
            embedding = self._encode(model, text)  # shape (D,)

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
                self._save_locked()
            return True
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
                        self._save_locked()
                    indexed = len(ids_to_encode)
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

            q_emb = self._encode(model, query)  # shape (D,)
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
        logger.info("semantic_search: индекс и файлы embeddings очищены (purge_all)")

    def remove_item(self, item_id: str) -> bool:
        """Remove item from index. Returns True if removed, False if not found.

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
            self._save_locked()
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_model(self) -> Any:
        """Lazy load sentence-transformers model. Thread-safe."""
        with self._model_lock:
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

    def _encode(self, model: Any, text: str) -> Any:
        """Кодирует один текст. Возвращает np.ndarray shape (D,)."""
        # multilingual-e5 рекомендует prefix "query: " / "passage: "
        # mxbai-embed-large рекомендует "Represent this sentence: "
        # Для единообразия используем "query: " (работает с обоими)
        prefix = "query: "
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
        """Загружает embeddings и индекс с диска (вызывается после загрузки модели)."""
        try:
            import numpy as np
            if self._embeddings_path.exists() and self._index_path.exists():
                embeddings = np.load(str(self._embeddings_path))
                with open(self._index_path, encoding="utf-8") as f:
                    index = json.load(f)
                # W884 E2: защита от частичной записи — нарушение согласованности
                # между .npy и .json (краш между двумя сохранениями).
                if embeddings.shape[0] != len(index):
                    logger.error(
                        "semantic_search: несоответствие размеров при загрузке с диска "
                        "(embeddings=%d, index=%d) — пропускаем загрузку, сброс к пустому индексу",
                        embeddings.shape[0],
                        len(index),
                    )
                    return
                with self._index_lock:
                    self._embeddings = embeddings
                    self._index = index
                logger.info(
                    "semantic_search: загружено %d embeddings с диска",
                    len(index),
                )
        except Exception as exc:
            logger.warning("semantic_search: не удалось загрузить embeddings с диска: %s", exc)

    def _save_locked(self) -> None:
        """Сохраняет embeddings и индекс на диск атомарно. Должен вызываться под _index_lock.

        Использует запись во временные файлы + ``os.replace`` чтобы избежать
        ситуации, при которой краш между двумя сохранениями оставляет .npy и
        .json в рассогласованном состоянии (W884 E2 / wave901 fix).

        Важно: ``np.save`` автоматически добавляет расширение ``.npy`` если
        его нет, поэтому временный файл называется ``*.tmp.npy`` — numpy сохранит
        в него, а мы делаем ``os.replace(tmp + ".npy", dst)``.
        """
        try:
            import numpy as np
            dir_str = str(self._data_dir)
            # Атомарная запись .npy — временный файл в той же директории,
            # затем os.replace (атомарная замена на POSIX).
            # np.save добавляет ".npy" к пути если его нет → tmp_npy_base + ".npy"
            # является реальным путём куда numpy запишет данные.
            if self._embeddings is not None:
                fd, tmp_npy_base = tempfile.mkstemp(dir=dir_str, suffix=".tmp")
                actual_tmp_npy = tmp_npy_base + ".npy"
                try:
                    os.close(fd)
                    np.save(tmp_npy_base, self._embeddings)  # → tmp_npy_base + ".npy"
                    os.replace(actual_tmp_npy, str(self._embeddings_path))
                except Exception:
                    for p in (tmp_npy_base, actual_tmp_npy):
                        try:
                            os.unlink(p)
                        except OSError:
                            pass
                    raise
                finally:
                    # mkstemp создаёт tmp_npy_base без содержимого; np.save пишет
                    # в tmp_npy_base + ".npy". Удаляем пустой tmp_npy_base если остался.
                    try:
                        if os.path.exists(tmp_npy_base):
                            os.unlink(tmp_npy_base)
                    except OSError:
                        pass
            # Атомарная запись .json
            fd, tmp_json = tempfile.mkstemp(dir=dir_str, suffix=".json.tmp")
            try:
                os.close(fd)
                with open(tmp_json, "w", encoding="utf-8") as f:
                    json.dump(self._index, f, ensure_ascii=False)
                os.replace(tmp_json, str(self._index_path))
            except Exception:
                try:
                    os.unlink(tmp_json)
                except OSError:
                    pass
                raise
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
