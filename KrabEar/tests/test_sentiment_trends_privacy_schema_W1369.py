"""Тесты консистентности схемы ответа get_sentiment_trends при privacy_mode.

W1360 F2 MED: privacy_mode ответ возвращал "trends": [] вместо "daily_sentiment": []
что ломало Swift JSON decoder. Тесты проверяют что схема совпадает.
"""

from __future__ import annotations

import ast
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sentiment_trends import SentimentTrendAnalyzer, SentimentTrendReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(text: str, days_ago: float = 1.0, language: str = "ru") -> dict:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"text": text, "ts": ts.isoformat(), "language": language}


# W#47: the privacy gate now lives in the extracted AnalyticsService — the dead
# in-class _handle_get_sentiment_trends duplicate in service.py was deleted.
# Source-scraping tests below target the LIVE handler in analytics_service.py.
ANALYTICS_PY = PROJECT_ROOT / "backend" / "analytics_service.py"


def _build_service_stub(privacy_enabled: bool):
    """Возвращает источник analytics_service.py (LIVE handle_get_sentiment_trends)."""
    source = ANALYTICS_PY.read_text(encoding="utf-8")
    return source


# ---------------------------------------------------------------------------
# TestCase 1: privacy response uses "daily_sentiment" key
# ---------------------------------------------------------------------------

class PrivacyResponseKeyTestCase(unittest.TestCase):
    """Проверяет что privacy_mode ответ использует ключ 'daily_sentiment'."""

    def test_privacy_response_uses_daily_sentiment_key(self) -> None:
        """Privacy gate должен возвращать 'daily_sentiment', не 'trends'."""
        source = ANALYTICS_PY.read_text(encoding="utf-8")

        # Найдём блок handle_get_sentiment_trends (LIVE extracted handler)
        idx = source.find("def handle_get_sentiment_trends")
        self.assertGreater(idx, 0, "handle_get_sentiment_trends не найден в analytics_service.py")

        # Вырежем ~30 строк после определения функции
        snippet = source[idx: idx + 1200]

        # Ключ 'trends' без 'daily_' не должен появляться в privacy-ответе
        # privacy_mode_active указывает что это наш новый privacy gate
        self.assertIn("privacy_mode_active", snippet,
                      "privacy gate должен включать reason='privacy_mode_active'")

        self.assertIn('"daily_sentiment"', snippet,
                      "privacy gate должен использовать ключ 'daily_sentiment'")

        self.assertNotIn('"trends"', snippet,
                         "устаревший ключ 'trends' не должен присутствовать в privacy gate")

    def test_privacy_response_includes_reason(self) -> None:
        """Privacy gate должен включать поле 'reason' = 'privacy_mode_active'."""
        source = ANALYTICS_PY.read_text(encoding="utf-8")

        idx = source.find("def handle_get_sentiment_trends")
        snippet = source[idx: idx + 1200]

        self.assertIn("reason", snippet,
                      "privacy gate должен содержать поле 'reason'")
        self.assertIn("privacy_mode_active", snippet,
                      "reason должен быть 'privacy_mode_active'")

    def test_privacy_response_includes_mood_trend(self) -> None:
        """Privacy gate должен включать 'mood_trend' для консистентности схемы."""
        source = ANALYTICS_PY.read_text(encoding="utf-8")

        idx = source.find("def handle_get_sentiment_trends")
        snippet = source[idx: idx + 1200]

        self.assertIn("mood_trend", snippet,
                      "privacy gate должен содержать 'mood_trend' для полной схемы")

    def test_privacy_response_includes_ok_flag(self) -> None:
        """Privacy gate должен возвращать 'ok': True чтобы не вызывать ошибку клиента."""
        source = ANALYTICS_PY.read_text(encoding="utf-8")

        idx = source.find("def handle_get_sentiment_trends")
        snippet = source[idx: idx + 1200]

        self.assertIn('"ok": True', snippet,
                      "privacy gate должен возвращать 'ok': True")


# ---------------------------------------------------------------------------
# TestCase 2: normal response schema unchanged
# ---------------------------------------------------------------------------

class NormalResponseSchemaTestCase(unittest.TestCase):
    """Проверяет что нормальный (не-privacy) ответ использует 'daily_sentiment'."""

    def setUp(self) -> None:
        self._analyzer = SentimentTrendAnalyzer()

    def test_normal_response_schema_unchanged(self) -> None:
        """to_dict() должен всегда возвращать ключ 'daily_sentiment'."""
        items = [_make_item("хорошо сегодня")]
        report = self._analyzer.analyze_sentiment_trends(items)
        result = self._analyzer.to_dict(report)

        self.assertIn("daily_sentiment", result,
                      "нормальный ответ должен содержать 'daily_sentiment'")
        self.assertNotIn("trends", result,
                         "нормальный ответ не должен содержать устаревший ключ 'trends'")

    def test_normal_response_has_mood_trend_key(self) -> None:
        """to_dict() должен содержать 'mood_trend' (не 'trend')."""
        items = [_make_item("отлично")]
        report = self._analyzer.analyze_sentiment_trends(items)
        result = self._analyzer.to_dict(report)

        self.assertIn("mood_trend", result,
                      "нормальный ответ должен содержать 'mood_trend'")
        self.assertNotIn("trend", [k for k in result if k != "mood_trend"],
                         "нет отдельного ключа 'trend' — только 'mood_trend'")

    def test_normal_response_full_schema(self) -> None:
        """to_dict() возвращает все ключи, ожидаемые Swift-декодером."""
        items = [_make_item("отличный день")]
        report = self._analyzer.analyze_sentiment_trends(items)
        result = self._analyzer.to_dict(report)

        expected_keys = {
            "daily_sentiment",
            "overall_sentiment",
            "sentiment_distribution",
            "mood_trend",
            "most_positive_day",
            "most_negative_day",
        }
        for key in expected_keys:
            self.assertIn(key, result, f"ключ '{key}' обязателен в схеме ответа")

    def test_empty_items_normal_response_schema(self) -> None:
        """Пустой список items → to_dict() тоже возвращает 'daily_sentiment'."""
        report = self._analyzer.analyze_sentiment_trends([])
        result = self._analyzer.to_dict(report)

        self.assertIn("daily_sentiment", result)
        self.assertIsInstance(result["daily_sentiment"], list)
        self.assertEqual(result["daily_sentiment"], [])


# ---------------------------------------------------------------------------
# TestCase 3: AST validation — no "trends" bare key in privacy gate
# ---------------------------------------------------------------------------

class ASTPrivacyGateValidationTestCase(unittest.TestCase):
    """AST-проверка: нет литерала 'trends' в privacy gate функции."""

    def test_no_bare_trends_key_in_handler_ast(self) -> None:
        """AST parse analytics_service.py и проверяем что 'trends' не является ключом
        словаря внутри handle_get_sentiment_trends (старый баг W1295)."""
        source = ANALYTICS_PY.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(ANALYTICS_PY))

        handler_body_lineno = None
        handler_end_lineno = None

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "handle_get_sentiment_trends":
                handler_body_lineno = node.lineno
                handler_end_lineno = getattr(node, "end_lineno", node.lineno + 50)
                break

        self.assertIsNotNone(handler_body_lineno,
                             "handle_get_sentiment_trends не найден в AST")

        # Собираем все строковые ключи словарей внутри функции
        dict_keys_in_handler: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            node_line = getattr(node, "lineno", 0)
            if handler_body_lineno <= node_line <= handler_end_lineno:
                for key in node.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        dict_keys_in_handler.append(key.value)

        # "trends" не должен быть ключом словаря внутри этого обработчика
        self.assertNotIn(
            "trends",
            dict_keys_in_handler,
            f"Устаревший ключ 'trends' обнаружен в dict внутри хендлера: {dict_keys_in_handler}",
        )

        # "daily_sentiment" должен присутствовать (privacy gate)
        self.assertIn(
            "daily_sentiment",
            dict_keys_in_handler,
            f"Ожидаемый ключ 'daily_sentiment' не найден в хендлере: {dict_keys_in_handler}",
        )

    def test_ast_handler_has_privacy_check(self) -> None:
        """handle_get_sentiment_trends должен содержать проверку privacy_mode_enabled."""
        source = ANALYTICS_PY.read_text(encoding="utf-8")

        idx = source.find("def handle_get_sentiment_trends")
        snippet = source[idx: idx + 1200]

        self.assertIn("privacy_mode_enabled", snippet,
                      "Обработчик должен проверять privacy_mode_enabled")


# ---------------------------------------------------------------------------
# TestCase 4: privacy gate schema matches to_dict schema
# ---------------------------------------------------------------------------

class SchemaParityTestCase(unittest.TestCase):
    """Privacy gate и to_dict() должны иметь идентичный набор ключей."""

    def test_privacy_gate_keys_match_to_dict_keys(self) -> None:
        """Ключи privacy gate ответа должны совпадать с to_dict() ключами."""
        # Получаем ключи нормального ответа через to_dict()
        analyzer = SentimentTrendAnalyzer()
        empty_report = analyzer.analyze_sentiment_trends([])
        normal_keys = set(analyzer.to_dict(empty_report).keys())

        # Из analytics_service.py (LIVE handler) вырезаем строки privacy gate.
        source = ANALYTICS_PY.read_text(encoding="utf-8")

        idx = source.find("def handle_get_sentiment_trends")
        snippet = source[idx: idx + 1200]

        # Ключи присутствия которых мы ожидаем в privacy gate
        for key in normal_keys:
            self.assertIn(f'"{key}"', snippet,
                          f"Ключ '{key}' из нормальной схемы отсутствует в privacy gate")


if __name__ == "__main__":
    unittest.main()
