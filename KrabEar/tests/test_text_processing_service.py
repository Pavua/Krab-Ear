"""Unit tests — TextProcessingService (11 IPC handlers).

Tests each handler directly against mocked collaborators.
Also smoke-tests delegation from BackendService.handle_request.
"""

from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.text_processing_service import TextProcessingService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_score_result(**kwargs) -> SimpleNamespace:
    defaults = dict(
        flesch_score=60.0,
        avg_sentence_length=12.0,
        avg_word_length=4.5,
        vocabulary_level="moderate",
        sentence_count=3,
        word_count=36,
        longest_sentence="This is the longest sentence in the text.",
        shortest_sentence="Hi.",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_quality_result(**kwargs) -> SimpleNamespace:
    defaults = dict(
        overall_score=80,
        grade="B",
        factors={"confidence": 0.9},
        recommendations=[],
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_emotion_result(**kwargs) -> SimpleNamespace:
    defaults = dict(
        primary_emotion="neutral",
        confidence=0.7,
        indicators=[],
        exclamation_count=0,
        question_count=1,
        caps_ratio=0.0,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_compare_result(**kwargs) -> SimpleNamespace:
    defaults = dict(
        similarity=0.8,
        text_1="hello",
        text_2="hello world",
        common_phrases=["hello"],
        unique_to_1=[],
        unique_to_2=["world"],
        word_count_diff=1,
        summary="Almost identical",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_post_process_result(**kwargs) -> SimpleNamespace:
    defaults = dict(
        text="processed text",
        steps_applied=["strip_whitespace"],
        changes_count=1,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_service(llm_rewriter=None) -> tuple[TextProcessingService, dict]:
    mocks: dict = {
        "readability_scorer": MagicMock(),
        "transcription_scorer": MagicMock(),
        "emotion_detector": MagicMock(),
        "text_comparator": MagicMock(),
        "abbreviation_expander": MagicMock(),
        "text_postprocessor": MagicMock(),
        "store": MagicMock(),
    }
    svc = TextProcessingService(
        readability_scorer=mocks["readability_scorer"],
        transcription_scorer=mocks["transcription_scorer"],
        emotion_detector=mocks["emotion_detector"],
        text_comparator=mocks["text_comparator"],
        abbreviation_expander=mocks["abbreviation_expander"],
        text_postprocessor=mocks["text_postprocessor"],
        store=mocks["store"],
        llm_rewriter=llm_rewriter,
    )
    return svc, mocks


# ===========================================================================
# summarize_text
# ===========================================================================

class TestSummarizeText(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = _make_service()

    def test_short_summary(self) -> None:
        result = self.svc.handle_summarize_text({"text": "Hello world. Foo bar. Baz qux."})
        self.assertIn("summary", result)
        self.assertIn("bullets", result)
        self.assertEqual(result["mode"], "summary_short")
        self.assertIsInstance(result["source_chars"], int)
        self.assertGreater(result["source_chars"], 0)

    def test_detailed_summary(self) -> None:
        text = "Sentence one. Sentence two. Sentence three. Sentence four."
        result = self.svc.handle_summarize_text({"text": text, "mode": "summary_detailed", "max_points": 2})
        self.assertEqual(result["mode"], "summary_detailed")
        self.assertLessEqual(len(result["bullets"]), 2)

    def test_empty_text_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self.svc.handle_summarize_text({"text": ""})

    def test_max_points_clamped(self) -> None:
        text = "A. B. C. D. E. F. G. H. I. J. K. L. M."
        result = self.svc.handle_summarize_text({"text": text, "max_points": 100})
        self.assertLessEqual(len(result["bullets"]), 12)

    def test_single_sentence(self) -> None:
        result = self.svc.handle_summarize_text({"text": "Only one sentence here"})
        self.assertIsInstance(result["summary"], str)
        self.assertIsInstance(result["bullets"], list)


# ===========================================================================
# _summarize_text_locally (static helper)
# ===========================================================================

class TestSummarizeTextLocally(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        result = TextProcessingService._summarize_text_locally("", "summary_short", 3)
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["bullets"], [])

    def test_short_mode(self) -> None:
        result = TextProcessingService._summarize_text_locally(
            "First. Second. Third.", "summary_short", 2
        )
        self.assertEqual(result["mode"], "summary_short")
        self.assertLessEqual(len(result["bullets"]), 2)

    def test_detailed_mode(self) -> None:
        result = TextProcessingService._summarize_text_locally(
            "A. B. C. D.", "summary_detailed", 3
        )
        self.assertEqual(result["mode"], "summary_detailed")
        self.assertLessEqual(len(result["bullets"]), 3)


# ===========================================================================
# summarize_item
# ===========================================================================

def _mock_store_with_items(items: list) -> MagicMock:
    """Create a store mock whose _lock() is a proper context manager."""
    store = MagicMock()

    @contextmanager
    def _lock():
        yield

    store._lock = _lock
    store._load_active_items_unlocked.return_value = items
    return store


class TestSummarizeItem(unittest.TestCase):
    def _make_item(self, item_id: str, text: str) -> MagicMock:
        item = MagicMock()
        item.id = item_id
        item.text = text
        return item

    def test_missing_id_raises(self) -> None:
        svc, _ = _make_service()
        with self.assertRaises(RuntimeError):
            svc.handle_summarize_item({})

    def test_item_not_found_raises(self) -> None:
        svc, mocks = _make_service()
        mocks["store"] = _mock_store_with_items([])
        svc._store = mocks["store"]
        with self.assertRaises(RuntimeError):
            svc.handle_summarize_item({"id": "nonexistent"})

    def test_text_too_short_raises(self) -> None:
        svc, mocks = _make_service()
        item = self._make_item("id1", "Short")
        mocks["store"] = _mock_store_with_items([item])
        svc._store = mocks["store"]
        with self.assertRaises(RuntimeError):
            svc.handle_summarize_item({"id": "id1"})

    def test_fallback_when_no_llm(self) -> None:
        svc, mocks = _make_service(llm_rewriter=None)
        long_text = "This is a sufficiently long transcript text. " * 5
        item = self._make_item("id1", long_text)
        mocks["store"] = _mock_store_with_items([item])
        svc._store = mocks["store"]
        result = svc.handle_summarize_item({"id": "id1"})
        self.assertEqual(result["id"], "id1")
        self.assertFalse(result["llm"])
        self.assertIsInstance(result["summary"], str)

    def test_llm_summary_used_when_available(self) -> None:
        llm = MagicMock()
        llm_result = MagicMock()
        llm_result.ok = True
        llm_result.text = "LLM summary"
        llm_result.latency_ms = 200
        llm.summarize.return_value = llm_result
        svc, mocks = _make_service(llm_rewriter=llm)
        long_text = "This is a sufficiently long transcript text. " * 5
        item = self._make_item("id1", long_text)
        mocks["store"] = _mock_store_with_items([item])
        svc._store = mocks["store"]
        result = svc.handle_summarize_item({"id": "id1"})
        self.assertTrue(result["llm"])
        self.assertEqual(result["summary"], "LLM summary")


# ===========================================================================
# compare_texts
# ===========================================================================

class TestCompareTexts(unittest.TestCase):
    def test_compare_by_text(self) -> None:
        svc, mocks = _make_service()
        mocks["text_comparator"].compare_texts.return_value = _make_compare_result()
        result = svc.handle_compare_texts({"text1": "hello", "text2": "hello world"})
        mocks["text_comparator"].compare_texts.assert_called_once_with("hello", "hello world")
        self.assertAlmostEqual(result["similarity"], 0.8)
        self.assertEqual(result["summary"], "Almost identical")

    def test_compare_by_item_ids(self) -> None:
        svc, mocks = _make_service()
        mocks["text_comparator"].compare_items.return_value = _make_compare_result(similarity=0.5)
        result = svc.handle_compare_texts({"item_id_1": "id1", "item_id_2": "id2"})
        mocks["text_comparator"].compare_items.assert_called_once_with("id1", "id2", mocks["store"])
        self.assertAlmostEqual(result["similarity"], 0.5)


# ===========================================================================
# score_readability
# ===========================================================================

class TestScoreReadability(unittest.TestCase):
    def test_empty_text_returns_zeros(self) -> None:
        svc, _ = _make_service()
        result = svc.handle_score_readability({"text": ""})
        self.assertEqual(result["flesch_score"], 0.0)
        self.assertEqual(result["sentence_count"], 0)

    def test_non_empty_text_delegates(self) -> None:
        svc, mocks = _make_service()
        mocks["readability_scorer"].score.return_value = _make_score_result(flesch_score=75.0)
        result = svc.handle_score_readability({"text": "Some text."})
        mocks["readability_scorer"].score.assert_called_once_with("Some text.")
        self.assertAlmostEqual(result["flesch_score"], 75.0)
        self.assertEqual(result["vocabulary_level"], "moderate")


# ===========================================================================
# score_transcription
# ===========================================================================

class TestScoreTranscription(unittest.TestCase):
    def test_score_with_defaults(self) -> None:
        svc, mocks = _make_service()
        mocks["transcription_scorer"].score.return_value = _make_quality_result()
        result = svc.handle_score_transcription({"text": "Hello world", "confidence": 0.9, "duration_sec": 5.0})
        mocks["transcription_scorer"].score.assert_called_once_with(
            text="Hello world",
            confidence=0.9,
            duration_sec=5.0,
            has_diarization=False,
            has_llm_enhancement=False,
        )
        self.assertEqual(result["overall_score"], 80)
        self.assertEqual(result["grade"], "B")

    def test_score_with_flags(self) -> None:
        svc, mocks = _make_service()
        mocks["transcription_scorer"].score.return_value = _make_quality_result(overall_score=95, grade="A")
        result = svc.handle_score_transcription({
            "text": "Hi",
            "confidence": 0.95,
            "duration_sec": 2.0,
            "has_diarization": True,
            "has_llm_enhancement": True,
        })
        call_kwargs = mocks["transcription_scorer"].score.call_args.kwargs
        self.assertTrue(call_kwargs["has_diarization"])
        self.assertTrue(call_kwargs["has_llm_enhancement"])
        self.assertEqual(result["grade"], "A")


# ===========================================================================
# detect_emotion
# ===========================================================================

class TestDetectEmotion(unittest.TestCase):
    def test_default_language(self) -> None:
        svc, mocks = _make_service()
        mocks["emotion_detector"].detect.return_value = _make_emotion_result(primary_emotion="positive")
        result = svc.handle_detect_emotion({"text": "Отлично!"})
        mocks["emotion_detector"].detect.assert_called_once_with("Отлично!", language="ru")
        self.assertEqual(result["primary_emotion"], "positive")

    def test_explicit_language(self) -> None:
        svc, mocks = _make_service()
        mocks["emotion_detector"].detect.return_value = _make_emotion_result()
        svc.handle_detect_emotion({"text": "Hola", "language": "es"})
        mocks["emotion_detector"].detect.assert_called_once_with("Hola", language="es")

    def test_all_fields_returned(self) -> None:
        svc, mocks = _make_service()
        mocks["emotion_detector"].detect.return_value = _make_emotion_result(
            exclamation_count=2, question_count=1, caps_ratio=0.1
        )
        result = svc.handle_detect_emotion({"text": "WOW!"})
        self.assertIn("primary_emotion", result)
        self.assertIn("confidence", result)
        self.assertIn("indicators", result)
        self.assertEqual(result["exclamation_count"], 2)
        self.assertEqual(result["caps_ratio"], 0.1)


# ===========================================================================
# add_abbreviation / expand_abbreviations / remove_abbreviation / list_abbreviations
# ===========================================================================

class TestAbbreviationHandlers(unittest.TestCase):
    def test_add_abbreviation_dispatched(self) -> None:
        """handle_add_abbreviation вызывает add_abbreviation у expander и возвращает added=True."""
        svc, mocks = _make_service()
        result = svc.handle_add_abbreviation(
            {"abbreviation": "т.н.", "expansion": "так называемый", "language": "ru"}
        )
        mocks["abbreviation_expander"].add_abbreviation.assert_called_once_with(
            "т.н.", "так называемый", language="ru", flags=""
        )
        self.assertTrue(result["added"])
        self.assertEqual(result["abbreviation"], "т.н.")
        self.assertEqual(result["expansion"], "так называемый")
        self.assertEqual(result["language"], "ru")

    def test_add_abbreviation_persists(self) -> None:
        """handle_add_abbreviation с флагом no_after_digit прокидывает его в expander."""
        svc, mocks = _make_service()
        result = svc.handle_add_abbreviation(
            {
                "abbreviation": "кв.",
                "expansion": "квадратный",
                "language": "ru",
                "flags": "no_after_digit",
            }
        )
        mocks["abbreviation_expander"].add_abbreviation.assert_called_once_with(
            "кв.", "квадратный", language="ru", flags="no_after_digit"
        )
        self.assertTrue(result["added"])

    def test_add_abbreviation_default_language(self) -> None:
        """handle_add_abbreviation использует 'ru' как язык по умолчанию."""
        svc, mocks = _make_service()
        svc.handle_add_abbreviation({"abbreviation": "т.е.", "expansion": "то есть"})
        mocks["abbreviation_expander"].add_abbreviation.assert_called_once_with(
            "т.е.", "то есть", language="ru", flags=""
        )

    def test_add_abbreviation_empty_abbr_raises(self) -> None:
        """handle_add_abbreviation с пустым abbreviation выбрасывает ValueError."""
        svc, _ = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_add_abbreviation({"abbreviation": "", "expansion": "что-то"})

    def test_add_abbreviation_empty_expansion_raises(self) -> None:
        """handle_add_abbreviation с пустым expansion выбрасывает ValueError."""
        svc, _ = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_add_abbreviation({"abbreviation": "т.н.", "expansion": ""})

    def test_expand_changed(self) -> None:
        svc, mocks = _make_service()
        mocks["abbreviation_expander"].expand.return_value = "доктор медицинских наук"
        result = svc.handle_expand_abbreviations({"text": "д.м.н.", "language": "ru"})
        self.assertEqual(result["expanded"], "доктор медицинских наук")
        self.assertTrue(result["changed"])

    def test_expand_unchanged(self) -> None:
        svc, mocks = _make_service()
        mocks["abbreviation_expander"].expand.return_value = "no change"
        result = svc.handle_expand_abbreviations({"text": "no change"})
        self.assertFalse(result["changed"])

    def test_expand_default_language_ru(self) -> None:
        svc, mocks = _make_service()
        mocks["abbreviation_expander"].expand.return_value = "текст"
        svc.handle_expand_abbreviations({"text": "txt"})
        mocks["abbreviation_expander"].expand.assert_called_once_with("txt", language="ru")

    def test_remove_abbreviation(self) -> None:
        svc, mocks = _make_service()
        mocks["abbreviation_expander"].remove_abbreviation.return_value = True
        result = svc.handle_remove_abbreviation({"abbr": "д.м.н.", "language": "ru"})
        self.assertTrue(result["removed"])

    def test_remove_abbreviation_not_found(self) -> None:
        svc, mocks = _make_service()
        mocks["abbreviation_expander"].remove_abbreviation.return_value = False
        result = svc.handle_remove_abbreviation({"abbr": "xyz"})
        self.assertFalse(result["removed"])

    def test_list_abbreviations(self) -> None:
        svc, mocks = _make_service()
        abbrs = [{"abbr": "д.м.н.", "expansion": "доктор медицинских наук"}]
        mocks["abbreviation_expander"].list_abbreviations.return_value = abbrs
        result = svc.handle_list_abbreviations({"language": "ru"})
        self.assertEqual(result["abbreviations"], abbrs)
        self.assertEqual(result["language"], "ru")
        self.assertEqual(result["count"], 1)

    def test_list_abbreviations_default_language(self) -> None:
        svc, mocks = _make_service()
        mocks["abbreviation_expander"].list_abbreviations.return_value = []
        result = svc.handle_list_abbreviations({})
        mocks["abbreviation_expander"].list_abbreviations.assert_called_once_with(language="ru")
        self.assertEqual(result["language"], "ru")


# ===========================================================================
# post_process_text / list_post_process_steps
# ===========================================================================

class TestPostProcessHandlers(unittest.TestCase):
    def test_post_process_no_steps(self) -> None:
        svc, mocks = _make_service()
        mocks["text_postprocessor"].process.return_value = _make_post_process_result()
        result = svc.handle_post_process_text({"text": "  hello  "})
        mocks["text_postprocessor"].process.assert_called_once_with("  hello  ", steps=None)
        self.assertEqual(result["text"], "processed text")
        self.assertEqual(result["changes_count"], 1)

    def test_post_process_with_steps(self) -> None:
        svc, mocks = _make_service()
        mocks["text_postprocessor"].process.return_value = _make_post_process_result(steps_applied=["strip_whitespace", "fix_punctuation"])
        result = svc.handle_post_process_text({"text": "hello", "steps": ["strip_whitespace", "fix_punctuation"]})
        call_args = mocks["text_postprocessor"].process.call_args
        self.assertEqual(call_args.kwargs["steps"], ["strip_whitespace", "fix_punctuation"])
        self.assertEqual(len(result["steps_applied"]), 2)

    def test_post_process_invalid_steps_type_raises(self) -> None:
        svc, _ = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_post_process_text({"text": "hi", "steps": "not_a_list"})

    def test_list_post_process_steps(self) -> None:
        svc, mocks = _make_service()
        mocks["text_postprocessor"].list_steps.return_value = ["strip_whitespace", "fix_punctuation", "normalize_entities"]
        result = svc.handle_list_post_process_steps({})
        self.assertEqual(len(result["steps"]), 3)
        self.assertIn("strip_whitespace", result["steps"])


# ===========================================================================
# Integration via BackendService.handle_request
# ===========================================================================

class TestBackendServiceDelegation(unittest.TestCase):
    """Smoke-test: verify TextProcessingService API contract via direct instantiation."""

    def test_text_processing_svc_instantiated(self) -> None:
        """Verify the service has the _text_processing_svc attribute."""
        svc, _ = _make_service()
        self.assertIsInstance(svc, TextProcessingService)

    def test_summarize_text_roundtrip(self) -> None:
        """Ensure summarize_text produces expected output shape."""
        svc, _ = _make_service()
        result = svc.handle_summarize_text({"text": "Hello world. Foo bar baz."})
        self.assertIn("summary", result)
        self.assertIn("bullets", result)
        self.assertIn("source_chars", result)
        self.assertEqual(result["mode"], "summary_short")

    def test_score_readability_empty(self) -> None:
        """Empty text returns zero-filled response without calling scorer."""
        svc, mocks = _make_service()
        result = svc.handle_score_readability({"text": ""})
        mocks["readability_scorer"].score.assert_not_called()
        self.assertEqual(result["sentence_count"], 0)

    def test_detect_emotion_returns_all_keys(self) -> None:
        svc, mocks = _make_service()
        mocks["emotion_detector"].detect.return_value = _make_emotion_result()
        result = svc.handle_detect_emotion({"text": "Привет"})
        for key in ("primary_emotion", "confidence", "indicators", "exclamation_count", "question_count", "caps_ratio"):
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
