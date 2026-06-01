"""Регрессионные тесты для трёх исправлений wave1760 в SemanticSearcher.

Тест A — reset_model_error (wave911 / HIGH):
  Метод был удалён body-revert'ом (wave1148/1699 cherry-pick train).
  Живой потребитель: SearchAndAnalysisService.handle_semantic_search_reset
  → self._semantic_searcher.reset_model_error().
  Без метода при любой ошибке загрузки модели semantic_search навсегда мёртв +
  вызов reset path падает с AttributeError.

Тест B — несоответствие размеров при загрузке с диска (wave901 / MED):
  Защита от рассогласования embeddings.npy / embeddings_index.json при краше
  между двумя сохранениями.  Без guard'а — IndexError, проглоченный except,
  поиск молча ломается.

Оба теста используют реальный временный каталог + мок SentenceTransformer.
Реальная модель НЕ загружается.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.semantic_search import SemanticSearcher


# ---------------------------------------------------------------------------
# Вспомогательная фабрика
# ---------------------------------------------------------------------------

def _make_fake_model(dim: int = 4) -> MagicMock:
    """Возвращает MagicMock совместимый с SentenceTransformer API."""
    model = MagicMock()

    def _encode(text, normalize_embeddings=True):
        seed = sum(ord(c) for c in str(text)) % 1000
        rng = np.random.RandomState(seed)
        v = rng.rand(dim).astype(np.float32)
        if normalize_embeddings:
            v /= np.linalg.norm(v) + 1e-10
        return v

    def _encode_batch(texts, normalize_embeddings=True):
        return np.stack([_encode(t, normalize_embeddings) for t in texts])

    model.encode.side_effect = lambda text, **kw: (
        _encode_batch(text, **kw) if isinstance(text, list) else _encode(text, **kw)
    )
    return model


def _make_searcher(tmpdir: str, enabled: bool = True, dim: int = 4) -> SemanticSearcher:
    """Создаёт SemanticSearcher с инжектированным fake-моделью."""
    s = SemanticSearcher(data_dir=Path(tmpdir), model_name="test-model", enabled=enabled)
    s._model = _make_fake_model(dim=dim)
    s._model_loaded = True
    return s


# ---------------------------------------------------------------------------
# Тест A: reset_model_error (wave911 regression)
# ---------------------------------------------------------------------------

class TestResetModelError(unittest.TestCase):
    """A: reset_model_error() сбрасывает _model_error и возвращает previous_error."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )

    def test_method_exists(self):
        """SemanticSearcher должен иметь метод reset_model_error."""
        self.assertTrue(
            hasattr(self.searcher, "reset_model_error"),
            "reset_model_error() отсутствует — wave911 регрессия",
        )
        self.assertTrue(callable(self.searcher.reset_model_error))

    def test_reset_clears_model_error(self):
        """После reset_model_error() поле _model_error становится None."""
        self.searcher._model_error = "connection_timeout"
        self.searcher._model = None
        self.searcher._model_loaded = False

        result = self.searcher.reset_model_error()

        self.assertIsNone(self.searcher._model_error)
        self.assertFalse(self.searcher._model_loaded)
        self.assertIsNone(self.searcher._model)

    def test_reset_returns_previous_error(self):
        """reset_model_error() возвращает {"reset": True, "previous_error": str}."""
        self.searcher._model_error = "sentence_transformers_not_installed"

        result = self.searcher.reset_model_error()

        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("reset"))
        self.assertEqual(result.get("previous_error"), "sentence_transformers_not_installed")

    def test_reset_when_no_error_previous_is_none(self):
        """reset_model_error() при отсутствии ошибки — previous_error=None."""
        self.assertIsNone(self.searcher._model_error)

        result = self.searcher.reset_model_error()

        self.assertTrue(result.get("reset"))
        self.assertIsNone(result.get("previous_error"))

    def test_reset_allows_retry(self):
        """После сброса _get_model() может снова попытаться загрузить модель.

        Симулируем: ошибка → reset → fake-модель инжектируется → index_item работает.
        """
        # 1. Устанавливаем ошибку — модель «не загружена»
        self.searcher._model_error = "transient_oom"
        self.searcher._model = None
        self.searcher._model_loaded = False

        # Без reset поиск вернёт False (модель недоступна)
        ok_before = self.searcher.index_item("id1", "Тест восстановления")
        self.assertFalse(ok_before, "index_item должен возвращать False при model_error")

        # 2. Сброс ошибки
        self.searcher.reset_model_error()

        # 3. Инжектируем fake-модель для следующей попытки
        fake_model = _make_fake_model(dim=4)
        with patch(
            "backend.semantic_search.SemanticSearcher._get_model",
            return_value=fake_model,
        ):
            ok_after = self.searcher.index_item("id1", "Тест восстановления")

        self.assertTrue(ok_after, "index_item должен работать после reset + retry")

    def test_reset_is_thread_safe(self):
        """reset_model_error() использует _model_lock — не падает при конкурентных вызовах."""
        import threading
        self.searcher._model_error = "some_error"
        errors = []

        def _reset():
            try:
                self.searcher.reset_model_error()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_reset) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Ошибки при конкурентном reset: {errors}")
        self.assertIsNone(self.searcher._model_error)

    def test_live_consumer_search_and_analysis_service(self):
        """SearchAndAnalysisService.handle_semantic_search_reset вызывает reset_model_error().

        Проверяет, что живой потребитель (search_and_analysis_service.py:166)
        не упадёт с AttributeError после восстановления метода.
        """
        from backend.state_store import StateStore
        from backend.search_and_analysis_service import SearchAndAnalysisService

        store = StateStore(data_dir=Path(self.tmpdir))
        svc = SearchAndAnalysisService(
            store=store,
            semantic_searcher=self.searcher,
            action_items_extractor=None,
            topic_tracker=None,
            recording_insights=None,
            recording_comparison=None,
            stats_report=None,
        )

        self.searcher._model_error = "network_error"
        result = svc.handle_semantic_search_reset({})

        self.assertTrue(result.get("reset"))
        self.assertEqual(result.get("previous_error"), "network_error")
        self.assertIsNone(self.searcher._model_error)


# ---------------------------------------------------------------------------
# Тест B: shape consistency guard в _load_from_disk (wave901 regression)
# ---------------------------------------------------------------------------

class TestLoadFromDiskShapeGuard(unittest.TestCase):
    """B: рассогласованная пара на диске → _load_from_disk оставляет индекс пустым."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_desynced_pair(self, tmpdir: str, n_embeddings: int, n_ids: int, dim: int = 4):
        """Записывает намеренно рассогласованный .npy + .json."""
        embeddings = np.random.rand(n_embeddings, dim).astype(np.float32)
        index = [f"id{i}" for i in range(n_ids)]

        emb_path = Path(tmpdir) / "embeddings.npy"
        idx_path = Path(tmpdir) / "embeddings_index.json"

        np.save(str(emb_path), embeddings)
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(index, f)

    def test_desynced_pair_leaves_index_empty(self):
        """N embeddings + N+1 ids → _load_from_disk не должна заполнять индекс."""
        self._write_desynced_pair(self.tmpdir, n_embeddings=3, n_ids=4, dim=4)

        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        # Инжектируем fake-модель (чтобы _get_model не упал),
        # затем вызываем _load_from_disk напрямую
        searcher._model = _make_fake_model(dim=4)
        searcher._model_loaded = True

        # Вызываем защищённый метод напрямую
        searcher._load_from_disk()

        with searcher._index_lock:
            self.assertEqual(
                len(searcher._index),
                0,
                "Индекс должен остаться пустым при рассогласованной паре",
            )
            self.assertIsNone(
                searcher._embeddings,
                "_embeddings должен остаться None при рассогласованной паре",
            )

    def test_desynced_pair_more_embeddings_than_ids(self):
        """N+1 embeddings + N ids — тоже считается рассогласованием."""
        self._write_desynced_pair(self.tmpdir, n_embeddings=5, n_ids=3, dim=4)

        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        searcher._model = _make_fake_model(dim=4)
        searcher._model_loaded = True
        searcher._load_from_disk()

        with searcher._index_lock:
            self.assertEqual(len(searcher._index), 0)
            self.assertIsNone(searcher._embeddings)

    def test_desynced_pair_no_indexerror(self):
        """Рассогласованная пара не должна вызывать IndexError при последующем поиске."""
        self._write_desynced_pair(self.tmpdir, n_embeddings=3, n_ids=4, dim=4)

        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        searcher._model = _make_fake_model(dim=4)
        searcher._model_loaded = True
        searcher._load_from_disk()

        # Поиск на пустом индексе должен вернуть [], не упасть
        with patch(
            "backend.semantic_search.SemanticSearcher._get_model",
            return_value=searcher._model,
        ):
            results = searcher.search("запрос", top_k=5)

        self.assertEqual(results, [], "Поиск на пустом индексе должен вернуть []")

    def test_consistent_pair_loads_correctly(self):
        """Согласованная пара (N embeddings + N ids) загружается нормально."""
        n = 3
        dim = 4
        embeddings = np.random.rand(n, dim).astype(np.float32)
        index = [f"id{i}" for i in range(n)]

        emb_path = Path(self.tmpdir) / "embeddings.npy"
        idx_path = Path(self.tmpdir) / "embeddings_index.json"
        np.save(str(emb_path), embeddings)
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(index, f)

        searcher = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        searcher._model = _make_fake_model(dim=dim)
        searcher._model_loaded = True
        searcher._load_from_disk()

        with searcher._index_lock:
            self.assertEqual(len(searcher._index), n)
            self.assertIsNotNone(searcher._embeddings)
            self.assertEqual(searcher._embeddings.shape[0], n)

    def test_save_locked_atomic_roundtrip(self):
        """_save_locked записывает атомарно: пара остаётся согласованной после сохранения."""
        searcher = _make_searcher(self.tmpdir, dim=4)

        # Индексируем несколько элементов через публичный API
        for i in range(3):
            searcher.index_item(f"item{i}", f"Текст номер {i}")

        # Создаём новый экземпляр и загружаем с диска
        searcher2 = SemanticSearcher(
            data_dir=Path(self.tmpdir),
            model_name="test-model",
            enabled=True,
        )
        searcher2._model = _make_fake_model(dim=4)
        searcher2._model_loaded = True
        searcher2._load_from_disk()

        with searcher2._index_lock:
            n_ids = len(searcher2._index)
            n_emb = searcher2._embeddings.shape[0] if searcher2._embeddings is not None else 0

        self.assertEqual(n_ids, 3)
        self.assertEqual(n_emb, 3, "После атомарного сохранения индексы должны совпадать")


if __name__ == "__main__":
    unittest.main()
