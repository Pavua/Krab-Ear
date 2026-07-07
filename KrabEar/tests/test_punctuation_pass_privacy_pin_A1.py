"""test_punctuation_pass_privacy_pin_A1.py — Задача №0 плана A1 recommended-setup
(docs/superpowers/plans/2026-07-07-recommended-setup.md).

Пиннинг-тест: core/engine.py::AudioEngine._punctuation_pass_allowed() уже гейтит на
privacy_mode_enabled (W1755 defense-in-depth: "mirrors _llm_rewrite_allowed"), но
test_engine_unit.py::LLMToggleTestCase не проверял именно этот угол (privacy_mode_enabled=True).
Данный тест фиксирует существующее корректное поведение как регрессионный барьер —
это НЕ фикс кода, код уже правильный.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_punctuation_pass_privacy_pin_A1.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class PunctuationPassPrivacyGateTestCase(unittest.TestCase):
    """Зеркалит test_engine_unit.py::LLMToggleTestCase._make_engine — тот же
    лёгкий конструктор AudioEngine(llm_rewriter=..., settings_get=...) без тяжёлого
    __init__ (модели STT не загружаются, settings_get инжектируется напрямую)."""

    def _make_engine(self, rewriter=None, settings_override: dict | None = None):
        from core.engine import AudioEngine
        settings_map = settings_override or {}
        engine = AudioEngine(
            llm_rewriter=rewriter,
            settings_get=lambda k, d: settings_map.get(k, d),
        )
        return engine

    def test_punctuation_pass_blocked_when_privacy_mode_enabled(self) -> None:
        """privacy_mode_enabled=True блокирует punctuation-pass, даже если
        stt_punctuation_llm_pass_enabled=True и rewriter присутствует."""
        fake_rw = MagicMock()
        engine = self._make_engine(
            rewriter=fake_rw,
            settings_override={
                "privacy_mode_enabled": True,
                "stt_punctuation_llm_pass_enabled": True,
            },
        )
        self.assertFalse(
            engine._punctuation_pass_allowed(),
            "_punctuation_pass_allowed должен вернуть False при privacy_mode_enabled=True, "
            "даже если stt_punctuation_llm_pass_enabled=True",
        )

    def test_punctuation_pass_allowed_when_privacy_mode_disabled(self) -> None:
        """Контроль: privacy_mode_enabled=False + toggle=True → True (доказывает, что
        предыдущий тест не проходит тривиально из-за отсутствия rewriter'а)."""
        fake_rw = MagicMock()
        engine = self._make_engine(
            rewriter=fake_rw,
            settings_override={
                "privacy_mode_enabled": False,
                "stt_punctuation_llm_pass_enabled": True,
            },
        )
        self.assertTrue(engine._punctuation_pass_allowed())


if __name__ == "__main__":
    unittest.main(verbosity=2)
