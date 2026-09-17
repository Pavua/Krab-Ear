"""engine читает сохранённый quality_profile при конструировании (волна 0.3).

Долг §1.4 Q4-плана: engine.py хардкодил "balanced" до первой записи, а
diagnostics (service.py:4431) читает сохранённое значение — расхождение,
видимое пользователю до первой диктовки.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine


def _make_engine(settings_get=None):
    """Как в test_engine_edge_cases: без GigaAM-worker'а, MLX мокируется."""
    return AudioEngine(settings_get=settings_get, skip_gigaam_warmup=True)


class EngineInitQualityProfileTests(unittest.TestCase):
    def test_saved_max_is_applied(self) -> None:
        eng = _make_engine(settings_get=lambda k, d: "max")
        self.assertEqual(eng.quality_profile, "max")

    def test_default_without_callback_is_balanced(self) -> None:
        eng = _make_engine()
        self.assertEqual(eng.quality_profile, "balanced")

    def test_junk_value_falls_back_to_balanced(self) -> None:
        for junk in ("ultra", "", None, 123):
            with self.subTest(junk=junk):
                eng = _make_engine(settings_get=lambda k, d, j=junk: j)
                self.assertEqual(eng.quality_profile, "balanced")


if __name__ == "__main__":
    unittest.main()
