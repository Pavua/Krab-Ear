"""Семантический поиск по истории транскрипций через sentence embeddings.

Использует multilingual-e5-base или mxbai-embed-large (через sentence-transformers).
Хранит embeddings в embeddings.npy + индекс id→row в embeddings_index.json.
Fallback на keyword search если модель не загружена.
"""

from __future__ import annotations

import json
import logging
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
    ) -> None:
        self._data_dir = Path(data_dir)
        self._model_name = model_name
        self._enabled = enabled

        self._model: Any = None
        self._model_lock = threading.Lock()
        self._model_loaded = False
        self._model_error: Optional[str] = None

        # numpy arrays / index — loaded lazily
        self._embeddings: Any = None  # np.ndarray shape (N, D)
        self._index: list[str] = []  # id at position i → row i
        self._index_lock = threading.Lock()

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
            embedding = self._encode(model, text)  # shape (D,)

            with self._index_lock:
                if item_id in self._index:
                    row = self._index.index(item_id)
                    self._embeddings[row] = embedding
                else:
                    if self._embeddings is None:
                        self._embeddings = embedding[np.newaxis, :]
                    else:
                        self._embeddings = np.vstack([self._embeddings, embedding[np.newaxis, :]])
                    self._index.append(item_id)
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

        if force:
            with self._index_lock:
                self._embeddings = None
                self._index = []

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

    def _load_from_disk(self) -> None:
        """Загружает embeddings и индекс с диска (вызывается после загрузки модели)."""
        try:
            import numpy as np
            if self._embeddings_path.exists() and self._index_path.exists():
                embeddings = np.load(str(self._embeddings_path))
                with open(self._index_path, encoding="utf-8") as f:
                    index = json.load(f)
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
        """Сохраняет embeddings и индекс на диск. Должен вызываться под _index_lock."""
        try:
            import numpy as np
            if self._embeddings is not None:
                np.save(str(self._embeddings_path), self._embeddings)
            with open(self._index_path, "w", encoding="utf-8") as f:
                json.dump(self._index, f, ensure_ascii=False)
        except Exception as exc:
            logger.warning("semantic_search: не удалось сохранить embeddings: %s", exc)


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
