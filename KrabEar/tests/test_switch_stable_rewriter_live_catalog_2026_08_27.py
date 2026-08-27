"""Волна «честные имена моделей» (2026-08-27): лечебное действие обязано
сверяться с ЖИВЫМ каталогом LM Studio, а не с именем, зашитым в код.

Корень: `_switch_to_stable_rewriter` — кнопка «починить» в тосте
`rewriter.channel_error`. Она писала в настройки `qwen3-4b-abliterated`,
которой в каталоге владельца НЕТ (живая сверка 2026-08-27: 105 моделей,
этого имени среди них нет). То есть нажатие «починить» превращало
эпизодический сбой рерайтера в постоянный — фикс строго хуже бага.

Направление отказа fail-safe: не нашли живого кандидата — НЕ трогаем
рабочую настройку и честно говорим почему.
"""
from __future__ import annotations

import os
import sys
import unittest
import io
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.error_actions import _switch_to_stable_rewriter, handle_action  # noqa: E402


def _catalog(models, error=None):
    """Заглушка LLMOpsService с ответом handle_list_llm_models по контракту."""
    svc = MagicMock()
    svc.handle_list_llm_models.return_value = {
        "models": list(models),
        "model_details": [{"id": m} for m in models],
        "recommended_models": [],
        "error": error,
    }
    return svc


def _settings(current="gemma-4-e4b-it-mlx"):
    svc = MagicMock()
    svc.cached_settings.return_value = {"llm_model": current}
    return svc


class SwitchStableRewriterUsesLiveCatalogTests(unittest.TestCase):
    def test_picks_a_model_that_exists_in_the_catalog(self):
        settings = _settings()
        ops = _catalog(["huihui-qwen3-14b-abl-v2", "some-other-model"])

        result = _switch_to_stable_rewriter(settings_service=settings, llm_ops_svc=ops)

        self.assertTrue(result["executed"])
        settings.handle_set_settings.assert_called_once()
        written = settings.handle_set_settings.call_args[0][0]["llm_model"]
        self.assertIn(written, ("huihui-qwen3-14b-abl-v2", "some-other-model"))

    def test_never_writes_a_model_absent_from_the_catalog(self):
        """Ядро волны: зашитое имя не должно попадать в настройки вслепую."""
        settings = _settings()
        catalog = ["gemma-4-26b-a4b-it@4bit", "huihui-qwen3-14b-abl-v2"]
        ops = _catalog(catalog)

        _switch_to_stable_rewriter(settings_service=settings, llm_ops_svc=ops)

        written = settings.handle_set_settings.call_args[0][0]["llm_model"]
        self.assertIn(written, catalog)

    def test_does_not_switch_to_the_currently_broken_model(self):
        """Действие вызывают, когда текущая модель сбоит — на неё же и переключать нельзя."""
        settings = _settings(current="gemma-4-e4b-it-mlx")
        ops = _catalog(["gemma-4-e4b-it-mlx", "huihui-qwen3-14b-abl-v2"])

        _switch_to_stable_rewriter(settings_service=settings, llm_ops_svc=ops)

        written = settings.handle_set_settings.call_args[0][0]["llm_model"]
        self.assertNotEqual(written, "gemma-4-e4b-it-mlx")

    def test_empty_catalog_leaves_settings_untouched(self):
        settings = _settings()
        ops = _catalog([], error="no_catalog_endpoint")

        result = _switch_to_stable_rewriter(settings_service=settings, llm_ops_svc=ops)

        self.assertFalse(result["executed"])
        settings.handle_set_settings.assert_not_called()
        self.assertIn("каталог", (result["reason"] or "").lower())

    def test_catalog_with_only_the_broken_model_leaves_settings_untouched(self):
        settings = _settings(current="gemma-4-e4b-it-mlx")
        ops = _catalog(["gemma-4-e4b-it-mlx"])

        result = _switch_to_stable_rewriter(settings_service=settings, llm_ops_svc=ops)

        self.assertFalse(result["executed"])
        settings.handle_set_settings.assert_not_called()

    def test_missing_ops_service_leaves_settings_untouched(self):
        """Без доступа к каталогу проверить кандидата нечем — молчим, а не гадаем."""
        settings = _settings()

        result = _switch_to_stable_rewriter(settings_service=settings, llm_ops_svc=None)

        self.assertFalse(result["executed"])
        settings.handle_set_settings.assert_not_called()

    def test_catalog_failure_does_not_raise(self):
        settings = _settings()
        ops = MagicMock()
        ops.handle_list_llm_models.side_effect = RuntimeError("LM Studio недоступна")

        result = handle_action(
            "switch_to_stable_rewriter", settings_service=settings, llm_ops_svc=ops
        )

        self.assertFalse(result["executed"])
        settings.handle_set_settings.assert_not_called()


class ErrorActionCatalogWiringTests(unittest.TestCase):
    """Гейт декоративной проводки: без каталога новое поведение — вечный отказ.

    Хендлер сверяет кандидата с живым каталогом, но каталог приходит только
    из service.py. Если этот kwarg потеряется при рефакторинге, действие
    останется «зелёным» в юнит-тестах и перестанет работать в проде —
    ровно тот класс, что ловят audit_decorative_wiring и MainErrorsWiringTests.
    """

    def test_service_passes_the_catalog_into_handle_action(self):
        import ast

        service_py = os.path.join(PROJECT_ROOT, "backend", "service.py")
        tree = ast.parse(io.open(service_py, encoding="utf-8").read())

        target = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "_handle_handle_error_action"),
            None,
        )
        self.assertIsNotNone(target, "_handle_handle_error_action не найден в service.py")

        calls = [
            n for n in ast.walk(target)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "handle_action"
        ]
        self.assertTrue(calls, "вызов handle_action не найден")
        self.assertTrue(
            any(kw.arg == "llm_ops_svc" for call in calls for kw in call.keywords),
            "handle_action вызывается без llm_ops_svc — сверять кандидата с каталогом нечем",
        )


if __name__ == "__main__":
    unittest.main()
