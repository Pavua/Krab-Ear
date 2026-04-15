"""Тесты handle_auto_summarize_batch в HistoryService."""

from __future__ import annotations
from backend.llm_rewriter import LLMRewriteResult
from backend.state_store import StateStore
from backend.history_service import HistoryService

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _make_service(tmp_dir: Path, llm_rewriter=None) -> HistoryService:
    """Создаёт HistoryService с реальным StateStore во временной директории."""
    store = StateStore(data_dir=tmp_dir)
    return HistoryService(store=store, llm_rewriter=llm_rewriter)


def _add_items(svc: HistoryService, texts: list[str]) -> list[str]:
    """Добавляет записи в историю, возвращает список ID."""
    ids = []
    for text in texts:
        item = svc.handle_add_history_item({"text": text, "paste_status": "ok"})
        ids.append(item["id"])
    return ids


def _make_ok_rewriter(summary_text: str) -> MagicMock:
    """Создаёт mock LLMRewriter, который возвращает успешный результат."""
    rewriter = MagicMock()
    rewriter._circuit = MagicMock()
    rewriter._circuit.state = "closed"
    rewriter.summarize.return_value = LLMRewriteResult(
        ok=True,
        text=summary_text,
        fallback_reason=None,
        latency_ms=42,
    )
    return rewriter


def _make_fail_rewriter(reason: str = "timeout") -> MagicMock:
    """Создаёт mock LLMRewriter, который возвращает ошибку."""
    rewriter = MagicMock()
    rewriter._circuit = MagicMock()
    rewriter._circuit.state = "closed"
    rewriter.summarize.return_value = LLMRewriteResult(
        ok=False,
        text=None,
        fallback_reason=reason,
        latency_ms=None,
    )
    return rewriter


def _make_open_circuit_rewriter() -> MagicMock:
    """Создаёт mock LLMRewriter с открытым circuit breaker."""
    rewriter = MagicMock()
    rewriter._circuit = MagicMock()
    rewriter._circuit.state = "open"
    return rewriter


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestAutoSummarizeBatchByIds(unittest.TestCase):
    """Резюмирование по списку ID."""

    def test_llm_success_returns_structured_result(self):
        """Успешный LLM-вызов возвращает summary, key_points и метаданные."""
        with tempfile.TemporaryDirectory() as tmp:
            llm_response = (
                "РЕЗЮМЕ: Обсуждение архитектуры бэкенда.\n"
                "ТЕЗИСЫ:\n"
                "- Перенести логику в отдельные сервисы\n"
                "- Добавить тесты на circuit breaker\n"
                "- Обновить документацию"
            )
            rewriter = _make_ok_rewriter(llm_response)
            svc = _make_service(Path(tmp), llm_rewriter=rewriter)

            ids = _add_items(svc, [
                "Нужно перенести логику в отдельные сервисы.",
                "Важно добавить тесты на circuit breaker и обновить документацию.",
            ])

            result = svc.handle_auto_summarize_batch({"ids": ids})

        self.assertFalse(result["fallback"])
        self.assertTrue(result["llm"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["items_processed"], 2)
        self.assertGreater(result["total_words"], 0)
        self.assertIn("архитектур", result["summary"])
        self.assertIsInstance(result["key_points"], list)
        self.assertGreater(len(result["key_points"]), 0)
        # summarize должен быть вызван ровно один раз
        rewriter.summarize.assert_called_once()

    def test_llm_unavailable_circuit_open_falls_back(self):
        """При открытом circuit breaker возвращается graceful fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            rewriter = _make_open_circuit_rewriter()
            svc = _make_service(Path(tmp), llm_rewriter=rewriter)

            ids = _add_items(svc, [
                "Первая транскрипция для тестирования.",
                "Вторая транскрипция с дополнительными данными.",
            ])

            result = svc.handle_auto_summarize_batch({"ids": ids})

        self.assertTrue(result["fallback"])
        self.assertFalse(result["llm"])
        self.assertEqual(result["error"], "LLM unavailable")
        self.assertEqual(result["items_processed"], 2)
        self.assertIsInstance(result["key_points"], list)
        self.assertGreater(len(result["key_points"]), 0)
        # summarize не должен вызываться при открытом circuit
        rewriter.summarize.assert_not_called()

    def test_no_llm_configured_falls_back_gracefully(self):
        """Без LLMRewriter (llm_rewriter=None) возвращается fallback с базовой статистикой."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(Path(tmp), llm_rewriter=None)

            ids = _add_items(svc, [
                "Транскрипция совещания по проекту Krab Ear.",
                "Обсуждение архитектуры и планов на следующий спринт.",
            ])

            result = svc.handle_auto_summarize_batch({"ids": ids})

        self.assertTrue(result["fallback"])
        self.assertFalse(result["llm"])
        self.assertEqual(result["error"], "LLM unavailable")
        self.assertEqual(result["items_processed"], 2)
        self.assertGreater(result["total_words"], 0)
        self.assertIsInstance(result["key_points"], list)
        self.assertIsInstance(result["summary"], str)
        self.assertTrue(len(result["summary"]) > 0)

    def test_llm_failure_falls_back_to_heuristic(self):
        """При ошибке LLM (timeout, parse_error и т.д.) возвращается эвристический fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            rewriter = _make_fail_rewriter("timeout")
            svc = _make_service(Path(tmp), llm_rewriter=rewriter)

            ids = _add_items(svc, [
                "Текст для тестирования таймаута LLM.",
            ])

            result = svc.handle_auto_summarize_batch({"ids": ids})

        self.assertTrue(result["fallback"])
        self.assertFalse(result["llm"])
        self.assertEqual(result["error"], "timeout")
        self.assertEqual(result["items_processed"], 1)

    def test_empty_ids_raises(self):
        """Пустой список ids вызывает RuntimeError."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(Path(tmp))
            with self.assertRaises(RuntimeError):
                svc.handle_auto_summarize_batch({"ids": []})

    def test_nonexistent_ids_raises(self):
        """Несуществующие ID вызывают RuntimeError."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(Path(tmp))
            with self.assertRaises(RuntimeError):
                svc.handle_auto_summarize_batch({"ids": ["nonexistent-id-123"]})

    def test_total_words_count(self):
        """total_words соответствует реальному количеству слов в текстах."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(Path(tmp), llm_rewriter=None)
            text1 = "один два три"       # 3 слова
            text2 = "четыре пять шесть"  # 3 слова
            ids = _add_items(svc, [text1, text2])

            result = svc.handle_auto_summarize_batch({"ids": ids})

        self.assertEqual(result["total_words"], 6)
        self.assertEqual(result["items_processed"], 2)


class TestAutoSummarizeBatchByDateRange(unittest.TestCase):
    """Резюмирование по временному диапазону."""

    def test_date_range_without_ids(self):
        """Запрос без ids, но с from_ts/to_ts работает корректно."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(Path(tmp), llm_rewriter=None)
            _add_items(svc, [
                "Запись для проверки диапазона дат.",
                "Вторая запись для диапазона.",
            ])

            # Запрос без фильтрации — должны вернуться все записи
            result = svc.handle_auto_summarize_batch({})

        self.assertGreaterEqual(result["items_processed"], 2)
        self.assertTrue(result["fallback"])

    def test_limit_respected(self):
        """Параметр limit ограничивает выборку по диапазону."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(Path(tmp), llm_rewriter=None)
            _add_items(svc, [
                "Первая.",
                "Вторая.",
                "Третья.",
                "Четвёртая.",
                "Пятая.",
            ])

            result = svc.handle_auto_summarize_batch({"limit": 2})

        self.assertLessEqual(result["items_processed"], 2)


class TestParseLlmBatchResponse(unittest.TestCase):
    """Юнит-тесты парсера структурированного ответа LLM."""

    def test_parses_structured_response(self):
        """Правильно парсит РЕЗЮМЕ + ТЕЗИСЫ."""
        text = (
            "РЕЗЮМЕ: Краткое резюме обсуждения.\n"
            "ТЕЗИСЫ:\n"
            "- Первый тезис\n"
            "- Второй тезис\n"
        )
        result = HistoryService._parse_llm_batch_response(text)
        self.assertEqual(result["summary"], "Краткое резюме обсуждения.")
        self.assertEqual(result["key_points"], ["Первый тезис", "Второй тезис"])

    def test_fallback_on_unstructured_response(self):
        """При нераспознанном формате весь текст уходит в summary."""
        text = "Это просто текст без структуры."
        result = HistoryService._parse_llm_batch_response(text)
        self.assertIn("просто текст", result["summary"])
        self.assertIsInstance(result["key_points"], list)

    def test_empty_string_fallback(self):
        """Пустая строка не ломает парсер."""
        result = HistoryService._parse_llm_batch_response("   ")
        self.assertIsInstance(result["summary"], str)
        self.assertIsInstance(result["key_points"], list)


class TestBuildFallbackSummary(unittest.TestCase):
    """Юнит-тесты эвристического fallback summary."""

    def test_returns_first_sentences_as_key_points(self):
        """Первые предложения каждой записи становятся тезисами."""
        texts = [
            "Первое предложение записи один. Второе предложение.",
            "Начало второй записи. Продолжение.",
        ]
        result = HistoryService._build_fallback_summary(texts)
        self.assertEqual(len(result["key_points"]), 2)
        self.assertIn("Первое", result["key_points"][0])
        self.assertEqual(result["summary"], result["key_points"][0])

    def test_handles_single_sentence(self):
        """Работает с однопредложенным текстом."""
        texts = ["Единственное предложение без точки в конце"]
        result = HistoryService._build_fallback_summary(texts)
        self.assertEqual(len(result["key_points"]), 1)
        self.assertTrue(len(result["summary"]) > 0)


if __name__ == "__main__":
    unittest.main()
