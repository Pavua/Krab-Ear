"""Lightweight unit tests for AudioEngine helpers that don't require STT models.

Targets:
- _raw_confidence_from_result  — confidence calculation from segments dict
- _short_model_name            — module-level utility (path-based name shortening)
- _llm_rewrite_allowed         — runtime toggle check (no rewriter → False)
- _punctuation_pass_allowed    — runtime toggle check (mirrors _llm_rewrite_allowed)
- set_quality_profile          — mlx.clear_cache called on profile switch
- normalize_audio              — returns original path for missing files (legacy contract)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class RawConfidenceFromResultTestCase(unittest.TestCase):
    """Unit-тесты _raw_confidence_from_result: вычисление confidence из segments."""

    def _confidence(self, result: dict) -> float:
        from core.engine import AudioEngine
        return AudioEngine._raw_confidence_from_result(result)

    def test_no_segments_returns_zero(self) -> None:
        """Пустой segments список → 0.0."""
        self.assertAlmostEqual(self._confidence({"segments": []}), 0.0)

    def test_missing_segments_key_returns_zero(self) -> None:
        """Отсутствие ключа segments → 0.0."""
        self.assertAlmostEqual(self._confidence({}), 0.0)

    def test_single_segment_avg_logprob_zero(self) -> None:
        """avg_logprob=0.0 → exp(0)=1.0."""
        result = {"segments": [{"avg_logprob": 0.0}]}
        self.assertAlmostEqual(self._confidence(result), 1.0, places=5)

    def test_single_segment_typical_logprob(self) -> None:
        """avg_logprob=-0.5 → exp(-0.5) ≈ 0.6065."""
        import math
        result = {"segments": [{"avg_logprob": -0.5}]}
        expected = math.exp(-0.5)
        self.assertAlmostEqual(self._confidence(result), expected, places=5)

    def test_multiple_segments_averaged(self) -> None:
        """Несколько сегментов — среднее по exp(avg_logprob)."""
        import math
        # avg_logprob для двух сегментов: -1.0 и 0.0 → (exp(-1)+exp(0))/2
        result = {"segments": [{"avg_logprob": -1.0}, {"avg_logprob": 0.0}]}
        expected = (math.exp(-1.0) + math.exp(0.0)) / 2
        self.assertAlmostEqual(self._confidence(result), expected, places=5)

    def test_missing_avg_logprob_uses_default(self) -> None:
        """Сегмент без avg_logprob использует default -1.0."""
        import math
        result = {"segments": [{}]}
        expected = math.exp(-1.0)
        self.assertAlmostEqual(self._confidence(result), expected, places=5)


class ShortModelNameTestCase(unittest.TestCase):
    """Unit-тесты _short_model_name: укорочение пути модели."""

    def _short(self, model: str) -> str:
        from core.engine import _short_model_name
        return _short_model_name(model)

    def test_empty_string_returns_unknown(self) -> None:
        self.assertEqual(self._short(""), "unknown")

    def test_none_returns_unknown(self) -> None:
        # None is cast to str("None") by str() then rsplit — should still work
        from core.engine import _short_model_name
        self.assertEqual(_short_model_name(None), "unknown")  # type: ignore[arg-type]

    def test_plain_name_unchanged(self) -> None:
        self.assertEqual(self._short("whisper-large-v3"), "whisper-large-v3")

    def test_hf_style_path_returns_last_segment(self) -> None:
        self.assertEqual(self._short("mlx-community/whisper-large-v3-mlx"), "whisper-large-v3-mlx")

    def test_deep_path_returns_last_segment(self) -> None:
        self.assertEqual(self._short("a/b/c/model-name"), "model-name")


class LLMToggleTestCase(unittest.TestCase):
    """Unit-тесты _llm_rewrite_allowed и _punctuation_pass_allowed."""

    def _make_engine(self, rewriter=None, settings_override: dict | None = None):
        from core.engine import AudioEngine
        settings_map = settings_override or {}
        engine = AudioEngine(
            llm_rewriter=rewriter,
            settings_get=lambda k, d: settings_map.get(k, d),
        )
        return engine

    def test_no_rewriter_llm_not_allowed(self) -> None:
        """Без rewriter'а _llm_rewrite_allowed всегда False."""
        engine = self._make_engine(rewriter=None, settings_override={"llm_rewrite_enabled": True})
        self.assertFalse(engine._llm_rewrite_allowed())

    def test_rewriter_present_but_toggle_off(self) -> None:
        """rewriter есть, но toggle off → False."""
        fake_rw = MagicMock()
        engine = self._make_engine(rewriter=fake_rw, settings_override={"llm_rewrite_enabled": False})
        self.assertFalse(engine._llm_rewrite_allowed())

    def test_rewriter_present_toggle_on(self) -> None:
        """rewriter есть + toggle on → True."""
        fake_rw = MagicMock()
        engine = self._make_engine(rewriter=fake_rw, settings_override={"llm_rewrite_enabled": True})
        self.assertTrue(engine._llm_rewrite_allowed())

    def test_no_rewriter_punctuation_not_allowed(self) -> None:
        """Без rewriter'а _punctuation_pass_allowed всегда False."""
        engine = self._make_engine(
            rewriter=None,
            settings_override={"stt_punctuation_llm_pass_enabled": True},
        )
        self.assertFalse(engine._punctuation_pass_allowed())

    def test_punctuation_pass_rewriter_toggle_on(self) -> None:
        """rewriter есть + toggle on → True."""
        fake_rw = MagicMock()
        engine = self._make_engine(
            rewriter=fake_rw,
            settings_override={"stt_punctuation_llm_pass_enabled": True},
        )
        self.assertTrue(engine._punctuation_pass_allowed())


class QualityProfileClearCacheTestCase(unittest.TestCase):
    """set_quality_profile вызывает mlx.core.clear_cache при смене профиля."""

    def test_clear_cache_called_on_switch(self) -> None:
        """При смене balanced→max должен дёрнуть mx.clear_cache()."""
        from core.engine import AudioEngine
        engine = AudioEngine()
        mock_core = MagicMock()
        mock_mlx = MagicMock()
        mock_mlx.core = mock_core
        # `import mlx.core as _mx` requires both "mlx" and "mlx.core" in sys.modules
        with patch.dict("sys.modules", {"mlx": mock_mlx, "mlx.core": mock_core}):
            result = engine.set_quality_profile("max")
        self.assertTrue(result)
        mock_core.clear_cache.assert_called_once()

    def test_clear_cache_not_called_when_no_change(self) -> None:
        """При повторном вызове с тем же профилем — ранний возврат, clear_cache не вызывается."""
        from core.engine import AudioEngine
        engine = AudioEngine()
        # balanced is the initial profile; calling balanced again → early return (False)
        mock_core = MagicMock()
        mock_mlx = MagicMock()
        mock_mlx.core = mock_core
        with patch.dict("sys.modules", {"mlx": mock_mlx, "mlx.core": mock_core}):
            result = engine.set_quality_profile("balanced")
        self.assertFalse(result)
        mock_core.clear_cache.assert_not_called()


class NormalizeAudioMissingFileTestCase(unittest.TestCase):
    """normalize_audio: legacy contract — возвращает путь если файл отсутствует."""

    def test_missing_file_returns_path(self) -> None:
        """Отсутствующий файл → возвращает исходный путь (legacy-контракт)."""
        from core.engine import AudioEngine
        engine = AudioEngine()
        fake_path = "/nonexistent/path/audio.wav"
        result = engine.normalize_audio(fake_path)
        self.assertEqual(result, fake_path)


class ConfidenceSingleSourceTestCase(unittest.TestCase):
    """avg_logprob-расчёт живёт ТОЛЬКО в _raw_confidence_from_result.

    Инлайновые дубли (финальные метрики, chunked-путь) зануляли confidence
    GigaAM-результатов (нет segments, есть явный confidence) даже после фикса
    самого хелпера — sibling-асимметрия. AST-гейт против нового расползания.
    """

    def test_avg_logprob_only_in_helper(self) -> None:
        import ast
        import inspect
        import core.engine as engine_mod

        tree = ast.parse(inspect.getsource(engine_mod))
        offenders: set[str] = set()

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

            def visit_Constant(self, node: ast.Constant) -> None:
                if node.value == "avg_logprob" and self.stack:
                    offenders.add(self.stack[-1])

        Visitor().visit(tree)
        self.assertEqual(
            offenders, {"_raw_confidence_from_result"},
            "avg_logprob-расчёт должен идти только через "
            f"_raw_confidence_from_result, найдено в: {sorted(offenders)}",
        )


if __name__ == "__main__":
    unittest.main()
