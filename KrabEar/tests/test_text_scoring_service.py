"""Unit tests — TextScoringService (3 IPC handlers).

Tests each handler directly against mocked collaborators, then an integration
smoke-test exercises them via BackendService.handle_request.

Handlers under test:
  - handle_warmup_rewriter    — LLM warmup probe
  - handle_extract_terms      — term extraction
  - handle_generate_auto_title — auto title generation (single + batch)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.text_scoring_service import TextScoringService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service(
    llm_rewriter=None,
    term_extractor=None,
    auto_title_generator=None,
    get_runtime_setting=None,
) -> TextScoringService:
    if get_runtime_setting is None:
        def get_runtime_setting(key, default):
            return default
    return TextScoringService(
        llm_rewriter=llm_rewriter,
        term_extractor=term_extractor or MagicMock(),
        auto_title_generator=auto_title_generator or MagicMock(),
        get_runtime_setting=get_runtime_setting,
    )


def _fake_term(term="краб", score=0.9, frequency=3, language="ru", category="tech"):
    return SimpleNamespace(
        term=term,
        score=score,
        frequency=frequency,
        language=language,
        category=category,
    )


# ---------------------------------------------------------------------------
# handle_warmup_rewriter
# ---------------------------------------------------------------------------

class TestWarmupRewriter(unittest.TestCase):

    def test_no_rewriter_returns_disabled(self) -> None:
        svc = _make_service(llm_rewriter=None)
        result = svc.handle_warmup_rewriter({})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rewriter_disabled")
        self.assertIsNone(result["model"])
        self.assertEqual(result["latency_ms"], 0)

    def test_calls_warmup_probe_default_timeout(self) -> None:
        fake_rewriter = MagicMock()
        fake_rewriter.warmup_probe.return_value = {"ok": True, "latency_ms": 42, "error": None}
        fake_rewriter._model = "qwen3-4b"
        svc = _make_service(llm_rewriter=fake_rewriter)
        result = svc.handle_warmup_rewriter({})
        fake_rewriter.warmup_probe.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "qwen3-4b")
        self.assertEqual(result["latency_ms"], 42)

    def test_calls_warmup_probe_custom_timeout(self) -> None:
        fake_rewriter = MagicMock()
        fake_rewriter.warmup_probe.return_value = {"ok": True, "latency_ms": 100, "error": None}
        fake_rewriter._model = None
        svc = _make_service(llm_rewriter=fake_rewriter)
        svc.handle_warmup_rewriter({"timeout_sec": 30})
        _, kwargs = fake_rewriter.warmup_probe.call_args
        self.assertAlmostEqual(kwargs["timeout_sec"], 30.0)

    def test_uses_runtime_setting_for_default_timeout(self) -> None:
        fake_rewriter = MagicMock()
        fake_rewriter.warmup_probe.return_value = {"ok": False, "latency_ms": 0, "error": "timeout"}
        fake_rewriter._model = None
        calls = []

        def get_setting(key, default):
            calls.append(key)
            return 20 if key == "rewriter_warmup_timeout_sec" else default

        svc = _make_service(llm_rewriter=fake_rewriter, get_runtime_setting=get_setting)
        svc.handle_warmup_rewriter({})
        self.assertIn("rewriter_warmup_timeout_sec", calls)
        _, kwargs = fake_rewriter.warmup_probe.call_args
        self.assertAlmostEqual(kwargs["timeout_sec"], 20.0)

    def test_rewriter_probe_error_propagates(self) -> None:
        fake_rewriter = MagicMock()
        fake_rewriter.warmup_probe.return_value = {
            "ok": False, "latency_ms": 200, "error": "connection refused"
        }
        fake_rewriter._model = "model-x"
        svc = _make_service(llm_rewriter=fake_rewriter)
        result = svc.handle_warmup_rewriter({})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "connection refused")
        self.assertEqual(result["model"], "model-x")


# ---------------------------------------------------------------------------
# handle_extract_terms
# ---------------------------------------------------------------------------

class TestExtractTerms(unittest.TestCase):

    def test_empty_text_returns_empty_terms(self) -> None:
        svc = _make_service()
        result = svc.handle_extract_terms({"text": ""})
        self.assertEqual(result, {"terms": []})

    def test_missing_text_returns_empty_terms(self) -> None:
        svc = _make_service()
        result = svc.handle_extract_terms({})
        self.assertEqual(result, {"terms": []})

    def test_calls_extractor_with_language(self) -> None:
        fake_extractor = MagicMock()
        term = _fake_term("AI", score=0.8, frequency=2, language="en", category="tech")
        fake_extractor.extract_terms.return_value = [term]
        svc = _make_service(term_extractor=fake_extractor)
        result = svc.handle_extract_terms({"text": "AI is great", "language": "en"})
        fake_extractor.extract_terms.assert_called_once_with("AI is great", language="en")
        self.assertEqual(len(result["terms"]), 1)
        self.assertEqual(result["terms"][0]["term"], "AI")
        self.assertEqual(result["terms"][0]["score"], 0.8)
        self.assertEqual(result["terms"][0]["frequency"], 2)
        self.assertEqual(result["terms"][0]["language"], "en")
        self.assertEqual(result["terms"][0]["category"], "tech")

    def test_default_language_is_ru(self) -> None:
        fake_extractor = MagicMock()
        fake_extractor.extract_terms.return_value = []
        svc = _make_service(term_extractor=fake_extractor)
        svc.handle_extract_terms({"text": "тест"})
        fake_extractor.extract_terms.assert_called_once_with("тест", language="ru")

    def test_multiple_terms_returned(self) -> None:
        fake_extractor = MagicMock()
        terms = [
            _fake_term("краб", score=0.9, frequency=5),
            _fake_term("ухо", score=0.7, frequency=2),
            _fake_term("голос", score=0.6, frequency=1),
        ]
        fake_extractor.extract_terms.return_value = terms
        svc = _make_service(term_extractor=fake_extractor)
        result = svc.handle_extract_terms({"text": "краб ухо голос"})
        self.assertEqual(len(result["terms"]), 3)
        self.assertEqual(result["terms"][0]["term"], "краб")
        self.assertEqual(result["terms"][2]["term"], "голос")


# ---------------------------------------------------------------------------
# handle_generate_auto_title
# ---------------------------------------------------------------------------

class TestGenerateAutoTitle(unittest.TestCase):

    def test_empty_text_returns_default_title(self) -> None:
        svc = _make_service()
        result = svc.handle_generate_auto_title({"text": ""})
        self.assertEqual(result, {"title": "Запись"})

    def test_missing_text_returns_default_title(self) -> None:
        svc = _make_service()
        result = svc.handle_generate_auto_title({})
        self.assertEqual(result, {"title": "Запись"})

    def test_single_mode_calls_generate_title(self) -> None:
        fake_gen = MagicMock()
        fake_gen.generate_title.return_value = "Обсуждение проекта"
        svc = _make_service(auto_title_generator=fake_gen)
        result = svc.handle_generate_auto_title({"text": "обсуждали проект краб"})
        fake_gen.generate_title.assert_called_once_with(
            "обсуждали проект краб", max_length=50
        )
        self.assertEqual(result["title"], "Обсуждение проекта")

    def test_single_mode_custom_max_length(self) -> None:
        fake_gen = MagicMock()
        fake_gen.generate_title.return_value = "Short"
        svc = _make_service(auto_title_generator=fake_gen)
        svc.handle_generate_auto_title({"text": "hello world", "max_length": 25})
        fake_gen.generate_title.assert_called_once_with("hello world", max_length=25)

    def test_with_date_and_timestamp_calls_generate_title_with_date(self) -> None:
        fake_gen = MagicMock()
        fake_gen.generate_title_with_date.return_value = "Встреча 2026-05-22"
        svc = _make_service(auto_title_generator=fake_gen)
        result = svc.handle_generate_auto_title({
            "text": "встреча по проекту",
            "timestamp": "2026-05-22T10:00:00",
            "with_date": True,
        })
        fake_gen.generate_title_with_date.assert_called_once_with(
            "встреча по проекту", "2026-05-22T10:00:00"
        )
        self.assertEqual(result["title"], "Встреча 2026-05-22")

    def test_with_date_false_does_not_call_with_date(self) -> None:
        fake_gen = MagicMock()
        fake_gen.generate_title.return_value = "Meeting"
        svc = _make_service(auto_title_generator=fake_gen)
        svc.handle_generate_auto_title({
            "text": "meeting notes",
            "timestamp": "2026-05-22T10:00:00",
            "with_date": False,
        })
        fake_gen.generate_title_with_date.assert_not_called()
        fake_gen.generate_title.assert_called_once()

    def test_batch_mode_calls_batch_generate(self) -> None:
        fake_gen = MagicMock()
        fake_gen.batch_generate.return_value = [
            {"id": "1", "title": "Встреча", "generated_at": "2026-05-22T00:00:00"},
        ]
        svc = _make_service(auto_title_generator=fake_gen)
        items = [{"id": "1", "text": "встреча"}]
        result = svc.handle_generate_auto_title({"items": items})
        fake_gen.batch_generate.assert_called_once_with(items)
        self.assertIn("titles", result)
        self.assertEqual(len(result["titles"]), 1)

    def test_batch_mode_invalid_items_raises(self) -> None:
        svc = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_generate_auto_title({"items": "not_a_list"})


# ---------------------------------------------------------------------------
# Integration: BackendService.handle_request dispatch
# ---------------------------------------------------------------------------

class TestTextScoringServiceIntegration(unittest.TestCase):
    """Smoke tests: verify BackendService delegates to TextScoringService."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def _make_backend(self):
        import numpy as np
        from backend.state_store import StateStore
        from backend.service import BackendService

        class _FakeRecorder:
            is_recording = False
            sample_rate = 16000
            last_stop_trim_ms = 0
            last_stop_timeout_sec = 3.0

            def start(self):
                self.is_recording = True
                return True

            def stop(self, timeout_sec=3.0, trim_tail_ms=0):
                if not self.is_recording:
                    return None
                self.is_recording = False
                return np.zeros(16000, dtype=np.float32), 1.0

        class _FakeTranscriber:
            def transcribe(self, audio, *a, **kw):
                return "ok", 0.9, []

            def load_profile(self, *a, **kw):
                pass

        store = StateStore(Path(self._tmpdir))
        svc = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
        )
        return svc

    def _call(self, svc, method: str, params: dict = None) -> dict:
        return svc.handle_request({"id": "t", "method": method, "params": params or {}})

    def test_extract_terms_dispatch(self) -> None:
        svc = self._make_backend()
        result = self._call(svc, "extract_terms", {"text": ""})
        self.assertIn("ok", result)
        self.assertTrue(result["ok"])
        self.assertIn("terms", result.get("result", {}))

    def test_warmup_rewriter_dispatch_no_rewriter(self) -> None:
        svc = self._make_backend()
        # With no real rewriter, expect ok=True (handled gracefully, no crash)
        result = self._call(svc, "warmup_rewriter", {})
        self.assertIn("ok", result)
        self.assertTrue(result["ok"])
        inner = result.get("result", {})
        # When rewriter disabled, warmup returns ok=False in inner result
        self.assertIn("ok", inner)

    def test_generate_auto_title_dispatch_empty(self) -> None:
        svc = self._make_backend()
        result = self._call(svc, "generate_auto_title", {"text": ""})
        self.assertIn("ok", result)
        self.assertTrue(result["ok"])
        inner = result.get("result", {})
        self.assertEqual(inner.get("title"), "Запись")

    def test_generate_auto_title_dispatch_with_text(self) -> None:
        svc = self._make_backend()
        result = self._call(svc, "generate_auto_title", {"text": "тестовая транскрибация"})
        self.assertIn("ok", result)
        self.assertTrue(result["ok"])
        inner = result.get("result", {})
        self.assertIn("title", inner)
        self.assertIsInstance(inner["title"], str)


if __name__ == "__main__":
    unittest.main()
