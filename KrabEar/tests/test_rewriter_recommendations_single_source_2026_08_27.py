"""Волна «честные имена моделей» (2026-08-27): список рекомендованных моделей
рерайтера обязан жить в ОДНОМ месте.

`handle_list_llm_models` собирал его дважды — на успешном пути и в except-ветке.
Дублированный литерал расходится молча: правят один, второй остаётся с
протухшим именем и всплывает ровно тогда, когда LM Studio недоступна, то есть
в момент, когда рекомендации нужнее всего. `qwen3-8b-abliterated` в обоих
списках отсутствует в каталоге владельца (живая сверка 2026-08-27).
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.llm_ops_service import LLMOpsService  # noqa: E402


def _svc():
    settings = MagicMock()
    settings.cached_settings.return_value = {"llm_base_url": "http://127.0.0.1:1234/v1"}
    return LLMOpsService(store=MagicMock(), settings_svc=settings, transcriber=MagicMock())


class RecommendationsSingleSourceTests(unittest.TestCase):
    def test_success_and_failure_paths_recommend_the_same_models(self):
        svc = _svc()

        with patch("requests.get", side_effect=OSError("LM Studio недоступна")):
            unreachable = svc.handle_list_llm_models({})

        # Ветка внешнего except: падение ещё до HTTP-цикла.
        broken = _svc()
        broken._settings_svc.cached_settings.side_effect = RuntimeError("настройки недоступны")
        crashed = broken.handle_list_llm_models({})

        self.assertEqual(
            unreachable["recommended_models"],
            crashed["recommended_models"],
            "списки рекомендаций разошлись между путями — литерал продублирован",
        )

    def test_recommendations_are_a_single_literal_in_source(self):
        import ast

        src = os.path.join(PROJECT_ROOT, "backend", "llm_ops_service.py")
        with open(src, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        # Списковые литералы, целиком состоящие из строк-имён моделей.
        model_lists = [
            [el.value for el in node.elts]
            for node in ast.walk(tree)
            if isinstance(node, ast.List)
            and node.elts
            and all(isinstance(el, ast.Constant) and isinstance(el.value, str) for el in node.elts)
            and any("-" in el.value for el in node.elts)
        ]
        self.assertLessEqual(
            len(model_lists), 1,
            f"имена моделей перечислены в {len(model_lists)} литералах: {model_lists}",
        )

    def test_no_recommendation_is_a_known_dead_name(self):
        """Имена, удалённые из каталога владельца, не должны возвращаться."""
        dead = {"qwen3-8b-abliterated", "qwen3-4b-abliterated", "qwen3-4b-instruct"}
        svc = _svc()
        with patch("requests.get", side_effect=OSError("недоступна")):
            recs = svc.handle_list_llm_models({})["recommended_models"]
        self.assertEqual(dead & set(recs), set(), f"рекомендуются мёртвые модели: {dead & set(recs)}")


if __name__ == "__main__":
    unittest.main()
