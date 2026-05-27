"""Тесты W1189: IPCThrottle покрытие 7 пропущенных тяжёлых/средних методов.

W1183 F1 MED: semantic_search_reindex / export_html_report / generate_html_report
             были классифицированы как light (120/min) вместо heavy (5/min);
             get_timeline_view / generate_stats_report / get_sentiment_trends /
             semantic_search — как light вместо medium (30/min).

Проверяем только классификацию через AST-стабильный _classify_method().
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ipc_throttle import (  # noqa: E402
    _classify_method,
    HEAVY_METHODS,
    MEDIUM_METHODS,
)


class TestIPCThrottleW1189HeavyMethods(unittest.TestCase):
    """semantic_search_reindex, export_html_report, generate_html_report → heavy."""

    def test_semantic_search_reindex_in_heavy_set(self) -> None:
        self.assertIn(
            "semantic_search_reindex",
            HEAVY_METHODS,
            "semantic_search_reindex must be in HEAVY_METHODS (full re-embed)",
        )

    def test_semantic_search_reindex_classified_heavy(self) -> None:
        self.assertEqual(
            _classify_method("semantic_search_reindex"),
            "heavy",
            "semantic_search_reindex должен быть heavy",
        )

    def test_export_html_report_in_heavy_set(self) -> None:
        self.assertIn("export_html_report", HEAVY_METHODS)

    def test_export_html_report_classified_heavy(self) -> None:
        self.assertEqual(_classify_method("export_html_report"), "heavy")

    def test_generate_html_report_alias_in_heavy_set(self) -> None:
        self.assertIn("generate_html_report", HEAVY_METHODS)

    def test_generate_html_report_classified_heavy(self) -> None:
        self.assertEqual(_classify_method("generate_html_report"), "heavy")


class TestIPCThrottleW1189MediumMethods(unittest.TestCase):
    """get_timeline_view, generate_stats_report, get_sentiment_trends, semantic_search → medium."""

    def test_get_timeline_view_in_medium_set(self) -> None:
        self.assertIn("get_timeline_view", MEDIUM_METHODS)

    def test_get_timeline_view_classified_medium(self) -> None:
        self.assertEqual(_classify_method("get_timeline_view"), "medium")

    def test_generate_stats_report_in_medium_set(self) -> None:
        self.assertIn("generate_stats_report", MEDIUM_METHODS)

    def test_generate_stats_report_classified_medium(self) -> None:
        self.assertEqual(_classify_method("generate_stats_report"), "medium")

    def test_get_sentiment_trends_in_medium_set(self) -> None:
        self.assertIn("get_sentiment_trends", MEDIUM_METHODS)

    def test_get_sentiment_trends_classified_medium(self) -> None:
        self.assertEqual(_classify_method("get_sentiment_trends"), "medium")

    def test_semantic_search_in_medium_set(self) -> None:
        self.assertIn("semantic_search", MEDIUM_METHODS)

    def test_semantic_search_classified_medium(self) -> None:
        self.assertEqual(_classify_method("semantic_search"), "medium")


class TestIPCThrottleW1189NotLight(unittest.TestCase):
    """None of the 7 methods should fall through to light after the fix."""

    _ALL_SEVEN = [
        "semantic_search_reindex",
        "export_html_report",
        "generate_html_report",
        "get_timeline_view",
        "generate_stats_report",
        "get_sentiment_trends",
        "semantic_search",
    ]

    def test_none_classified_as_light(self) -> None:
        for method in self._ALL_SEVEN:
            category = _classify_method(method)
            with self.subTest(method=method):
                self.assertNotEqual(
                    category,
                    "light",
                    f"{method} должен быть heavy/medium, не light",
                )

    def test_heavy_trio_not_medium(self) -> None:
        for method in ("semantic_search_reindex", "export_html_report", "generate_html_report"):
            with self.subTest(method=method):
                self.assertNotEqual(_classify_method(method), "medium")

    def test_medium_quartet_not_heavy(self) -> None:
        for method in (
            "get_timeline_view",
            "generate_stats_report",
            "get_sentiment_trends",
            "semantic_search",
        ):
            with self.subTest(method=method):
                self.assertNotEqual(_classify_method(method), "heavy")


if __name__ == "__main__":
    unittest.main()
