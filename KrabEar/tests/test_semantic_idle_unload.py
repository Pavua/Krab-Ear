"""F5: ленивая выгрузка модели семантического поиска в простое (D5).

D5 (владелец): выгрузка ~1.9 ГБ при простое + ленивый ре-подъём.
Индекс (RAM/диск) при unload НЕ трогается — прецедент reset_model_error.
"""
from __future__ import annotations

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.semantic_search import SemanticSearcher  # noqa: E402
from backend.memory_conductor import MemoryConductor  # noqa: E402

import numpy as np  # noqa: E402


def _make_fake_model(dim: int = 4):
    class _FakeModel:
        def encode(self, texts, **kwargs):
            return np.ones((len(texts), dim), dtype="float32")

    return _FakeModel()


class SearcherUnloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.searcher = SemanticSearcher(data_dir=Path(self.tmp.name), enabled=True)
        self.searcher._model = _make_fake_model()
        self.searcher._model_loaded = True

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unload_drops_model_keeps_index_and_allows_reload(self) -> None:
        marker = np.ones((1, 4), dtype="float32")
        self.searcher._embeddings = marker
        self.searcher._index = ["id1"]
        self.assertTrue(self.searcher.unload_model())
        self.assertIsNone(self.searcher._model)
        self.assertFalse(self.searcher.model_loaded)
        self.assertIsNone(self.searcher._model_error)  # ре-подъём разрешён
        self.assertIs(self.searcher._embeddings, marker)  # индекс не тронут
        self.assertEqual(self.searcher._index, ["id1"])

    def test_unload_idempotent(self) -> None:
        self.assertTrue(self.searcher.unload_model())
        self.assertFalse(self.searcher.unload_model())

    def test_unload_if_idle_keeps_fresh_model(self) -> None:
        self.searcher._last_used_ts = time.monotonic()
        self.assertFalse(self.searcher.unload_if_idle(600.0))
        self.assertTrue(self.searcher.model_loaded)

    def test_unload_if_idle_drops_stale_model(self) -> None:
        self.searcher._last_used_ts = time.monotonic() - 3600.0
        self.assertTrue(self.searcher.unload_if_idle(600.0))
        self.assertFalse(self.searcher.model_loaded)

    def test_get_model_marks_last_used(self) -> None:
        self.searcher._last_used_ts = 0.0
        self.searcher._get_model()
        self.assertGreater(self.searcher._last_used_ts, 0.0)

    def test_search_after_unload_relazy_loads(self) -> None:
        self.searcher._encode = lambda model, text: np.ones(4, dtype="float32")
        self.searcher._encode_batch = lambda model, texts: np.ones((len(texts), 4), dtype="float32")
        self.searcher.index_item("id1", "текст один")
        self.assertTrue(self.searcher._model_loaded)
        self.assertTrue(self.searcher.unload_model())
        fake_st = types.ModuleType("sentence_transformers")
        fake_st.SentenceTransformer = lambda name: _make_fake_model()
        with mock.patch.dict(sys.modules, {"sentence_transformers": fake_st}):
            results = self.searcher.search("текст")
        self.assertEqual([r["id"] for r in results], ["id1"])


class SemanticStepTests(unittest.TestCase):
    """F5: `_semantic_step` кондуктора — всегда включён (без enforce_for).

    Отличие от gigaam-шага: семантика — CPU-модель, не GPU-резидент, поэтому
    `memory_conductor_enforce*` на неё НЕ влияет; порог <= 0 = выключено.
    Фикстура повторяет `test_memory_conductor_2026_08_19.py` (мок settings-сервиса).
    """

    def _settings(self, **over):
        base = {
            "memory_conductor_enabled": True,
            "memory_conductor_enforce": False,
            "memory_conductor_enforce_gigaam": False,
            "memory_conductor_enforce_rewriter": False,
            "memory_conductor_enforce_brain": False,
            "semantic_search_idle_unload_sec": 1800.0,
        }
        base.update(over)
        svc = mock.MagicMock()
        svc.cached_settings.return_value = base
        return svc

    def _mk(self, *, settings=None, idle_fn=None, unload=None):
        return MemoryConductor(
            settings_service=settings or self._settings(),
            ledger=mock.MagicMock(),
            is_recording=lambda: False,
            is_meeting_active=lambda: False,
            pressure_fn=lambda: 0,
            host_stats_fn=lambda: None,
            semantic_unload_if_idle=(
                unload if unload is not None else mock.MagicMock(return_value=True)
            ),
            semantic_idle_sec_fn=idle_fn,
            tick_sec=0.05,
        )

    def test_idle_over_threshold_unloads_with_threshold(self):
        unload = mock.MagicMock(return_value=True)
        c = self._mk(idle_fn=lambda: 99999.0, unload=unload)
        c._semantic_step()
        unload.assert_called_once_with(1800.0)

    def test_idle_provider_error_is_silent(self):
        unload = mock.MagicMock(return_value=True)

        def _boom():
            raise RuntimeError("idle unavailable")

        c = self._mk(idle_fn=_boom, unload=unload)
        c._semantic_step()
        unload.assert_not_called()

    def test_idle_below_threshold_no_unload(self):
        unload = mock.MagicMock(return_value=True)
        c = self._mk(idle_fn=lambda: 10.0, unload=unload)
        c._semantic_step()
        unload.assert_not_called()

    def test_enforce_false_does_not_gate_semantic_unload(self):
        # 🔴 Ключевое отличие от gigaam/rewriter: все memory_conductor_enforce*
        # выключены, а semantic-выгрузка всё равно обязана вызваться.
        unload = mock.MagicMock(return_value=True)
        c = self._mk(idle_fn=lambda: 99999.0, unload=unload)
        for resident in ("gigaam", "rewriter", "brain", "recording_sequence"):
            self.assertFalse(c.enforce_for(resident))
        c._semantic_step()
        unload.assert_called_once_with(1800.0)

    def test_threshold_zero_disables_step(self):
        # Реализация проверяет threshold <= 0 → return (0 = выключено);
        # idle-провайдер при этом не должен вызываться вовсе.
        unload = mock.MagicMock(return_value=True)
        idle_fn = mock.MagicMock(side_effect=AssertionError("idle_fn must not be called"))
        c = self._mk(
            settings=self._settings(semantic_search_idle_unload_sec=0.0),
            idle_fn=idle_fn,
            unload=unload,
        )
        c._semantic_step()
        unload.assert_not_called()
        idle_fn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
