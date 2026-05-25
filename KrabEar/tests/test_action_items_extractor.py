"""Тесты ActionItemsExtractor — извлечение задач/решений/вопросов из транскриптов.

15+ тестов: RU/ES/EN экстракция, invalid JSON, LM Studio недоступен,
CircuitBreaker open, threshold, IPC handlers, delta journal, get_pending.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.action_items_extractor import (  # noqa: E402
    ActionItem,
    ActionItemsExtractor,
    ActionItemsResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_TRANSCRIPT_RU = (
    "Итак, Иван возьмёт на себя задачу по обновлению документации до пятницы. "
    "Решено: переходим на Python 3.12 в следующем квартале. "
    "Открытый вопрос: как интегрировать новый API с legacy-системой?"
)

SAMPLE_TRANSCRIPT_ES = (
    "María se encargará de preparar el informe para el lunes. "
    "Decisión: migrar a la nueva plataforma en enero. "
    "Pregunta pendiente: ¿cómo gestionar los usuarios existentes durante la migración?"
)

SAMPLE_TRANSCRIPT_EN = (
    "John will update the backend tests by Thursday. "
    "Decision: we will use PostgreSQL instead of SQLite in production. "
    "Open question: how do we handle zero-downtime migrations?"
)

VALID_LLM_RESPONSE_RU = json.dumps({
    "action_items": [
        {"text": "Обновить документацию", "assignee": "Иван", "due": "пятница", "priority": "high"},
    ],
    "decisions": ["Переходим на Python 3.12 в следующем квартале"],
    "questions": ["Как интегрировать новый API с legacy-системой?"],
})

VALID_LLM_RESPONSE_ES = json.dumps({
    "action_items": [
        {"text": "Preparar el informe", "assignee": "María", "due": "lunes", "priority": "medium"},
    ],
    "decisions": ["Migrar a la nueva plataforma en enero"],
    "questions": ["¿Cómo gestionar los usuarios existentes durante la migración?"],
})

VALID_LLM_RESPONSE_EN = json.dumps({
    "action_items": [
        {"text": "Update backend tests", "assignee": "John", "due": "Thursday", "priority": "high"},
    ],
    "decisions": ["Use PostgreSQL instead of SQLite in production"],
    "questions": ["How do we handle zero-downtime migrations?"],
})


def make_extractor(**kwargs):
    """Create ActionItemsExtractor with test defaults."""
    defaults = dict(
        base_url="http://localhost:1234",
        api_key="test",
        model="qwen3-4b",
        timeout_sec=5.0,
    )
    defaults.update(kwargs)
    return ActionItemsExtractor(**defaults)


def make_mock_response(content: str, status_code: int = 200):
    """Create a mock requests.Response with given content."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    return resp


# ---------------------------------------------------------------------------
# 1. Basic extraction tests
# ---------------------------------------------------------------------------

class ActionItemsExtractorRuTest(unittest.TestCase):
    """Тест извлечения из RU транскрипта."""

    def test_extract_ru_action_items(self):
        extractor = make_extractor()
        with patch.object(extractor._session, "post", return_value=make_mock_response(VALID_LLM_RESPONSE_RU)):
            result = extractor.extract(SAMPLE_TRANSCRIPT_RU, language="ru")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.action_items), 1)
        self.assertEqual(result.action_items[0].text, "Обновить документацию")
        self.assertEqual(result.action_items[0].assignee, "Иван")
        self.assertEqual(result.action_items[0].priority, "high")
        self.assertEqual(len(result.decisions), 1)
        self.assertEqual(len(result.questions), 1)

    def test_extract_ru_decisions_and_questions(self):
        extractor = make_extractor()
        with patch.object(extractor._session, "post", return_value=make_mock_response(VALID_LLM_RESPONSE_RU)):
            result = extractor.extract(SAMPLE_TRANSCRIPT_RU, language="ru")
        self.assertIn("Python 3.12", result.decisions[0])
        self.assertIn("legacy", result.questions[0])


class ActionItemsExtractorEsTest(unittest.TestCase):
    """Тест извлечения из ES транскрипта."""

    def test_extract_es_action_items(self):
        extractor = make_extractor()
        with patch.object(extractor._session, "post", return_value=make_mock_response(VALID_LLM_RESPONSE_ES)):
            result = extractor.extract(SAMPLE_TRANSCRIPT_ES, language="es")
        self.assertTrue(result.ok)
        self.assertEqual(result.action_items[0].assignee, "María")
        self.assertEqual(len(result.decisions), 1)


class ActionItemsExtractorEnTest(unittest.TestCase):
    """Тест извлечения из EN транскрипта."""

    def test_extract_en_action_items(self):
        extractor = make_extractor()
        with patch.object(extractor._session, "post", return_value=make_mock_response(VALID_LLM_RESPONSE_EN)):
            result = extractor.extract(SAMPLE_TRANSCRIPT_EN, language="en")
        self.assertTrue(result.ok)
        self.assertEqual(result.action_items[0].assignee, "John")
        self.assertEqual(result.action_items[0].due, "Thursday")


# ---------------------------------------------------------------------------
# 2. Invalid JSON from LLM
# ---------------------------------------------------------------------------

class InvalidJsonTest(unittest.TestCase):
    """Тест graceful fallback при невалидном JSON."""

    def test_invalid_json_returns_empty_struct(self):
        extractor = make_extractor()
        bad_response = "This is not JSON at all, LLM hallucination."
        with patch.object(extractor._session, "post", return_value=make_mock_response(bad_response)):
            result = extractor.extract(SAMPLE_TRANSCRIPT_RU)
        self.assertFalse(result.ok)
        self.assertEqual(result.action_items, [])
        self.assertEqual(result.decisions, [])
        self.assertEqual(result.questions, [])
        self.assertIn(result.fallback_reason, ("no_json", "invalid_json"))

    def test_partial_json_returns_empty_struct(self):
        extractor = make_extractor()
        # Valid JSON but missing required keys
        with patch.object(extractor._session, "post", return_value=make_mock_response('{"foo": "bar"}')):
            result = extractor.extract(SAMPLE_TRANSCRIPT_RU)
        # Should succeed but with empty lists
        self.assertTrue(result.ok)
        self.assertEqual(result.action_items, [])

    def test_json_in_markdown_fences_parsed_correctly(self):
        extractor = make_extractor()
        fenced = f"```json\n{VALID_LLM_RESPONSE_EN}\n```"
        with patch.object(extractor._session, "post", return_value=make_mock_response(fenced)):
            result = extractor.extract(SAMPLE_TRANSCRIPT_EN, language="en")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.action_items), 1)

    def test_empty_transcript_returns_empty_input_error(self):
        extractor = make_extractor()
        result = extractor.extract("")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_input")

    def test_whitespace_transcript_returns_empty_input_error(self):
        extractor = make_extractor()
        result = extractor.extract("   \n\t  ")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_input")


# ---------------------------------------------------------------------------
# 3. LM Studio unreachable
# ---------------------------------------------------------------------------

class LMStudioUnreachableTest(unittest.TestCase):
    """Тест fallback при недоступном LM Studio."""

    def test_connection_error_returns_empty(self):
        import requests as req
        extractor = make_extractor()
        with patch.object(extractor._session, "post", side_effect=req.ConnectionError("refused")):
            result = extractor.extract(SAMPLE_TRANSCRIPT_RU)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "connection_error")

    def test_timeout_returns_empty(self):
        import requests as req
        extractor = make_extractor()
        with patch.object(extractor._session, "post", side_effect=req.Timeout("timed out")):
            result = extractor.extract(SAMPLE_TRANSCRIPT_RU)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "timeout")

    def test_http_500_returns_empty(self):
        extractor = make_extractor()
        bad_resp = MagicMock()
        bad_resp.status_code = 500
        with patch.object(extractor._session, "post", return_value=bad_resp):
            result = extractor.extract(SAMPLE_TRANSCRIPT_RU)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "http_500")


# ---------------------------------------------------------------------------
# 4. CircuitBreaker open
# ---------------------------------------------------------------------------

class CircuitBreakerTest(unittest.TestCase):
    """Тест поведения при открытом circuit breaker."""

    def test_circuit_open_returns_circuit_open(self):
        extractor = make_extractor(circuit_fail_threshold=1, circuit_initial_reset_sec=999)
        # Trigger circuit open by forcing a failure
        import requests as req
        with patch.object(extractor._session, "post", side_effect=req.ConnectionError("fail")):
            extractor.extract(SAMPLE_TRANSCRIPT_RU)  # first call → fail
        # Second call should get circuit_open
        result = extractor.extract(SAMPLE_TRANSCRIPT_RU)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "circuit_open")

    def test_circuit_state_open_after_failures(self):
        extractor = make_extractor(circuit_fail_threshold=2, circuit_initial_reset_sec=999)
        import requests as req
        for _ in range(2):
            with patch.object(extractor._session, "post", side_effect=req.ConnectionError("fail")):
                extractor.extract(SAMPLE_TRANSCRIPT_RU)
        self.assertEqual(extractor.circuit_state, "open")


# ---------------------------------------------------------------------------
# 5. ActionItem model
# ---------------------------------------------------------------------------

class ActionItemModelTest(unittest.TestCase):
    """Тест ActionItem dataclass."""

    def test_from_dict_valid(self):
        ai = ActionItem.from_dict({"text": "Test task", "assignee": "Alice", "due": "Mon", "priority": "high"})
        self.assertEqual(ai.text, "Test task")
        self.assertEqual(ai.priority, "high")

    def test_from_dict_invalid_priority_normalized(self):
        ai = ActionItem.from_dict({"text": "x", "priority": "URGENT"})
        self.assertEqual(ai.priority, "medium")

    def test_to_dict_roundtrip(self):
        ai = ActionItem(text="Do something", assignee="Bob", due="Friday", priority="low")
        d = ai.to_dict()
        ai2 = ActionItem.from_dict(d)
        self.assertEqual(ai.text, ai2.text)
        self.assertEqual(ai.priority, ai2.priority)

    def test_empty_result_is_empty(self):
        r = ActionItemsResult.empty("test_reason")
        self.assertTrue(r.is_empty)
        self.assertFalse(r.ok)
        self.assertEqual(r.fallback_reason, "test_reason")


# ---------------------------------------------------------------------------
# 6. StateStore delta journal
# ---------------------------------------------------------------------------

class DeltaJournalTest(unittest.TestCase):
    """Тест delta-журнала action_items в StateStore."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from backend.state_store import StateStore
        self.store = StateStore(data_dir=Path(self.tmpdir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_update_action_items_returns_false_for_unknown_id(self):
        result = self.store.update_history_item_action_items(
            "nonexistent-id",
            action_items=[{"text": "x"}],
            decisions=["dec"],
            questions=["q"],
        )
        self.assertFalse(result)

    def test_update_action_items_persists_for_known_id(self):
        item = self.store.add_history_item(text="Meeting discussion about Q3 planning.")
        success = self.store.update_history_item_action_items(
            item_id=item.id,
            action_items=[{"text": "Prepare report", "assignee": "Alice", "due": "", "priority": "medium"}],
            decisions=["Go with plan A"],
            questions=["Budget?"],
        )
        self.assertTrue(success)

        # Reload and verify
        with self.store._lock():
            items = self.store._load_active_items_unlocked()
        loaded = next(it for it in items if it.id == item.id)
        self.assertIsNotNone(loaded.action_items)
        self.assertEqual(len(loaded.action_items), 1)
        self.assertEqual(loaded.action_items[0]["text"], "Prepare report")
        self.assertEqual(loaded.decisions, ["Go with plan A"])
        self.assertEqual(loaded.questions, ["Budget?"])

    def test_update_action_items_last_write_wins(self):
        item = self.store.add_history_item(text="Short text meeting")
        self.store.update_history_item_action_items(
            item.id, [{"text": "Old task"}], ["Old decision"], ["Old question"]
        )
        self.store.update_history_item_action_items(
            item.id, [{"text": "New task"}], ["New decision"], ["New question"]
        )
        with self.store._lock():
            items = self.store._load_active_items_unlocked()
        loaded = next(it for it in items if it.id == item.id)
        # last-write-wins
        self.assertEqual(loaded.action_items[0]["text"], "New task")
        self.assertEqual(loaded.decisions[0], "New decision")

    def test_action_items_cleared_on_compact(self):
        item = self.store.add_history_item(text="Compact test meeting")
        self.store.update_history_item_action_items(
            item.id, [{"text": "Task 1"}], ["Decision 1"], []
        )
        # Compact merges into main history
        self.store.compact()
        # Verify action_items_path is cleared
        content = self.store.action_items_path.read_text(encoding="utf-8").strip()
        self.assertEqual(content, "")
        # But data should be in main history now
        with self.store._lock():
            items = self.store._load_active_items_unlocked()
        loaded = next(it for it in items if it.id == item.id)
        self.assertIsNotNone(loaded.action_items)


# ---------------------------------------------------------------------------
# 7. IPC handler tests via BackendService
# ---------------------------------------------------------------------------

class IPCHandlerTest(unittest.TestCase):
    """Тест IPC handlers через BackendService."""

    def _make_service(self):
        """Create a minimal BackendService with mocked dependencies."""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        self._test_tmpdir = tmpdir

        from backend.state_store import StateStore

        store = StateStore(data_dir=Path(tmpdir))

        # Patch settings to avoid real LM Studio
        with patch("backend.service.settings") as mock_settings:
            mock_settings.LLM_ENABLED = False
            mock_settings.LLM_BASE_URL = "http://localhost:1234"
            mock_settings.LLM_API_KEY = "test"
            mock_settings.LLM_MODEL = "test-model"
            mock_settings.LLM_TIMEOUT_SEC = 4.0
            mock_settings.LLM_CIRCUIT_FAIL_THRESHOLD = 3
            mock_settings.LLM_CIRCUIT_INITIAL_RESET_SEC = 60
            mock_settings.LLM_CIRCUIT_MAX_RESET_SEC = 600
            mock_settings.DEFAULT_DATA_DIR = tmpdir
            mock_settings.BACKEND_LOG_LEVEL = "WARNING"

            from backend.service import BackendService
            svc = BackendService.__new__(BackendService)
            svc.store = store
            svc._action_items_extractor = None
            svc._llm_rewriter = None
            return svc, store

    def tearDown(self):
        import shutil
        if hasattr(self, "_test_tmpdir"):
            shutil.rmtree(self._test_tmpdir, ignore_errors=True)

    def test_extract_action_items_raises_when_llm_disabled(self):
        svc, store = self._make_service()
        item = store.add_history_item(text="Some meeting text here")
        with self.assertRaises(RuntimeError) as ctx:
            svc._handle_extract_action_items({"id": item.id})
        self.assertIn("LLM", str(ctx.exception))

    def test_extract_action_items_raises_for_missing_id(self):
        svc, store = self._make_service()
        with self.assertRaises(RuntimeError):
            svc._handle_extract_action_items({"id": ""})

    def test_extract_action_items_raises_for_unknown_id(self):
        svc, store = self._make_service()
        # Give it an extractor so we get past the LLM check
        svc._action_items_extractor = MagicMock()
        svc._action_items_extractor.extract.return_value = ActionItemsResult(ok=True)
        with self.assertRaises(RuntimeError) as ctx:
            svc._handle_extract_action_items({"id": "nonexistent-uuid"})
        self.assertIn("не найден", str(ctx.exception))

    def test_get_pending_action_items_returns_all_without_extraction(self):
        svc, store = self._make_service()
        item1 = store.add_history_item(text="Meeting one")
        item2 = store.add_history_item(text="Meeting two")
        result = svc._handle_get_pending_action_items({})
        ids = [p["id"] for p in result["pending"]]
        self.assertIn(item1.id, ids)
        self.assertIn(item2.id, ids)
        self.assertEqual(result["count"], 2)

    def test_get_pending_filters_already_extracted(self):
        svc, store = self._make_service()
        item1 = store.add_history_item(text="Meeting one")
        item2 = store.add_history_item(text="Meeting two")
        # Mark item1 as extracted
        store.update_history_item_action_items(item1.id, [], [], [])
        result = svc._handle_get_pending_action_items({})
        ids = [p["id"] for p in result["pending"]]
        self.assertNotIn(item1.id, ids)
        self.assertIn(item2.id, ids)

    def test_get_pending_filters_by_min_duration(self):
        svc, store = self._make_service()
        short_item = store.add_history_item(text="Quick note", audio_duration_sec=30.0)
        long_item = store.add_history_item(text="Long meeting", audio_duration_sec=120.0)
        result = svc._handle_get_pending_action_items({"min_duration_sec": 60.0})
        ids = [p["id"] for p in result["pending"]]
        self.assertNotIn(short_item.id, ids)
        self.assertIn(long_item.id, ids)

    def test_batch_extract_raises_when_llm_disabled(self):
        svc, store = self._make_service()
        item = store.add_history_item(text="Meeting")
        with self.assertRaises(RuntimeError) as ctx:
            svc._handle_batch_extract_action_items({"ids": [item.id]})
        self.assertIn("LLM", str(ctx.exception))

    def test_batch_extract_handles_not_found(self):
        svc, store = self._make_service()
        extractor = make_extractor()
        svc._action_items_extractor = extractor
        with patch.object(extractor._session, "post", return_value=make_mock_response(VALID_LLM_RESPONSE_EN)):
            result = svc._handle_batch_extract_action_items({
                "ids": ["nonexistent-id"],
                "language": "en",
            })
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["ok"], False)
        self.assertEqual(result["results"][0]["error"], "not_found")


# ---------------------------------------------------------------------------
# 8. Wave 121 additional coverage
# ---------------------------------------------------------------------------

class Wave121ActionItemsTest(unittest.TestCase):
    """Дополнительные тесты для Wave 121 — short transcript, unicode, concurrent, priority."""

    def test_extract_task_with_priority_high(self):
        """Задача с priority=high корректно разбирается."""
        extractor = make_extractor()
        payload = json.dumps({
            "action_items": [
                {"text": "Deploy hotfix ASAP", "assignee": "DevOps", "due": "today", "priority": "high"},
            ],
            "decisions": [],
            "questions": [],
        })
        with patch.object(extractor._session, "post", return_value=make_mock_response(payload)):
            result = extractor.extract("Deploy hotfix today urgently", language="en")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.action_items), 1)
        self.assertEqual(result.action_items[0].priority, "high")
        self.assertEqual(result.action_items[0].assignee, "DevOps")

    def test_extract_decision(self):
        """Решение (decision) корректно извлекается."""
        extractor = make_extractor()
        payload = json.dumps({
            "action_items": [],
            "decisions": ["Migrate all services to Kubernetes by Q4"],
            "questions": [],
        })
        with patch.object(extractor._session, "post", return_value=make_mock_response(payload)):
            result = extractor.extract("We decided to migrate to Kubernetes.", language="en")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.decisions), 1)
        self.assertIn("Kubernetes", result.decisions[0])
        self.assertFalse(result.is_empty)

    def test_extract_question(self):
        """Открытый вопрос корректно извлекается."""
        extractor = make_extractor()
        payload = json.dumps({
            "action_items": [],
            "decisions": [],
            "questions": ["Who owns the monitoring dashboard?"],
        })
        with patch.object(extractor._session, "post", return_value=make_mock_response(payload)):
            result = extractor.extract("Who is responsible for the monitoring dashboard?", language="en")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.questions), 1)
        self.assertIn("monitoring", result.questions[0])

    def test_no_actionable_items_returns_empty_lists(self):
        """Транскрипт без задач/решений/вопросов → ok=True, все списки пустые."""
        extractor = make_extractor()
        payload = json.dumps({"action_items": [], "decisions": [], "questions": []})
        with patch.object(extractor._session, "post", return_value=make_mock_response(payload)):
            result = extractor.extract("It was a nice sunny day.", language="en")
        self.assertTrue(result.ok)
        self.assertTrue(result.is_empty)
        self.assertEqual(result.action_items, [])
        self.assertEqual(result.decisions, [])
        self.assertEqual(result.questions, [])

    def test_handles_short_transcript(self):
        """Очень короткий транскрипт (одно слово) проходит без ошибок."""
        extractor = make_extractor()
        payload = json.dumps({"action_items": [], "decisions": [], "questions": []})
        with patch.object(extractor._session, "post", return_value=make_mock_response(payload)):
            result = extractor.extract("OK", language="en")
        # Either ok=True with empty lists, or a graceful empty result — never raises.
        self.assertIsInstance(result, ActionItemsResult)
        self.assertIsNone(result.fallback_reason) if result.ok else self.assertIsNotNone(result.fallback_reason)

    def test_unicode_text(self):
        """Транскрипт с Unicode символами (emoji, CJK, арабские) не вызывает исключений."""
        extractor = make_extractor()
        unicode_transcript = (
            "Задача: обновить 📋 dashboard. "
            "Решение: используем 日本語テスト и Arabic: مرحبا. "
            "Вопрос: когда это будет готово? 🚀"
        )
        payload = json.dumps({
            "action_items": [{"text": "Обновить dashboard", "assignee": "", "due": "", "priority": "medium"}],
            "decisions": ["Используем 日本語テスト"],
            "questions": ["Когда это будет готово?"],
        })
        with patch.object(extractor._session, "post", return_value=make_mock_response(payload)):
            result = extractor.extract(unicode_transcript, language="ru")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.action_items), 1)
        self.assertEqual(len(result.decisions), 1)
        self.assertEqual(len(result.questions), 1)

    def test_concurrent_extract(self):
        """10 параллельных потоков вызывают extract() — нет исключений, все результаты валидны."""
        import threading
        extractor = make_extractor()
        payload = json.dumps({
            "action_items": [{"text": "Task", "assignee": "", "due": "", "priority": "medium"}],
            "decisions": ["Decision"],
            "questions": ["Question?"],
        })

        results: list[ActionItemsResult] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker():
            try:
                with patch.object(extractor._session, "post", return_value=make_mock_response(payload)):
                    r = extractor.extract(SAMPLE_TRANSCRIPT_EN, language="en")
                with lock:
                    results.append(r)
            except BaseException as exc:  # pragma: no cover
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors!r}")
        self.assertEqual(len(results), 10)
        for r in results:
            self.assertIsInstance(r, ActionItemsResult)

    def test_handles_llm_failure_gracefully(self):
        """При любой ошибке LLM extract() никогда не raises, возвращает empty result."""
        extractor = make_extractor()
        # Simulate a completely unexpected exception from requests
        with patch.object(extractor._session, "post", side_effect=RuntimeError("unexpected boom")):
            result = extractor.extract(SAMPLE_TRANSCRIPT_RU)
        self.assertIsInstance(result, ActionItemsResult)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.fallback_reason)


if __name__ == "__main__":
    unittest.main()
