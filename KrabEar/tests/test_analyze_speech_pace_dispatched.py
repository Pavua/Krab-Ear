"""test_analyze_speech_pace_dispatched.py — W1048 F2 fix verification.

Verifies that analyze_speech_pace is registered in BackendService.handle_request
dispatch table and the handler produces correct output.

W1048 finding: SpeechPaceAnalyzer was instantiated in BackendService.__init__
but analyze_speech_pace was never registered in handle_request, making it
unreachable from the Swift agent.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class AnalyzeSpeechPaceDispatchRegisteredTestCase(unittest.TestCase):
    """Checks that analyze_speech_pace is wired in the dispatch table."""

    def _service_source(self) -> str:
        return (PROJECT_ROOT / "backend" / "service.py").read_text(encoding="utf-8")

    def test_dispatch_entry_present(self) -> None:
        """'analyze_speech_pace' key is present in service.py dispatch dict."""
        self.assertIn('"analyze_speech_pace"', self._service_source(),
                      "dispatch entry missing — handler not reachable via IPC")

    def test_handler_method_defined(self) -> None:
        """'_handle_analyze_speech_pace' method is defined in service.py."""
        self.assertIn("_handle_analyze_speech_pace", self._service_source(),
                      "handler method definition missing in service.py")

    def test_dispatch_entry_references_handler(self) -> None:
        """The dispatch entry maps to self._handle_analyze_speech_pace."""
        src = self._service_source()
        # Both must appear, and the dispatch entry must reference the handler
        self.assertIn('"analyze_speech_pace": self._handle_analyze_speech_pace', src,
                      "dispatch entry does not reference self._handle_analyze_speech_pace")


class AnalyzeSpeechPaceHandlerUnitTestCase(unittest.TestCase):
    """Unit tests for _handle_analyze_speech_pace logic via stub object.

    Avoids instantiating full BackendService (mlx-whisper etc not available).
    Exercises the handler logic directly using the same call pattern.
    """

    def setUp(self) -> None:
        from core.speech_pace import SpeechPaceAnalyzer

        # Minimal stub that mirrors BackendService._handle_analyze_speech_pace
        class _StubService:
            def __init__(self) -> None:
                self._speech_pace_analyzer = SpeechPaceAnalyzer()

            def _handle_analyze_speech_pace(self, params: dict) -> dict:
                text = str(params.get("text", ""))
                raw_dur = params.get("duration_sec")
                if raw_dur is None:
                    return {"error": "duration_sec is required"}
                try:
                    duration_sec = float(raw_dur)
                except (TypeError, ValueError):
                    return {"error": "duration_sec must be a number"}
                report = self._speech_pace_analyzer.analyze(text, duration_sec)
                return report.as_dict()

        self.svc = _StubService()

    def test_normal_text_returns_pace_report(self) -> None:
        """Handler returns a dict with all PaceReport fields for valid input."""
        result = self.svc._handle_analyze_speech_pace({
            "text": " ".join(["word"] * 120),
            "duration_sec": 60.0,
        })
        expected_keys = {
            "words_per_minute", "chars_per_minute", "pace_category",
            "estimated_reading_time_sec", "word_count", "char_count", "duration_sec",
        }
        for key in expected_keys:
            self.assertIn(key, result, f"Missing key in PaceReport: {key}")

    def test_120_words_60s_is_normal_pace(self) -> None:
        """120 words / 60 sec = 120 wpm → normal category."""
        result = self.svc._handle_analyze_speech_pace({
            "text": " ".join(["word"] * 120),
            "duration_sec": 60.0,
        })
        self.assertAlmostEqual(result["words_per_minute"], 120.0, places=1)
        self.assertEqual(result["pace_category"], "normal")

    def test_empty_text_returns_zero_wpm(self) -> None:
        """Empty text yields words_per_minute == 0.0."""
        result = self.svc._handle_analyze_speech_pace({
            "text": "",
            "duration_sec": 30.0,
        })
        self.assertEqual(result["words_per_minute"], 0.0)
        self.assertEqual(result["word_count"], 0)

    def test_missing_duration_returns_error(self) -> None:
        """Missing duration_sec produces an error dict."""
        result = self.svc._handle_analyze_speech_pace({"text": "hello"})
        self.assertIn("error", result)

    def test_invalid_duration_returns_error(self) -> None:
        """Non-numeric duration_sec produces an error dict."""
        result = self.svc._handle_analyze_speech_pace({
            "text": "hello",
            "duration_sec": "not_a_number",
        })
        self.assertIn("error", result)

    def test_russian_text_is_analyzed(self) -> None:
        """Cyrillic text is correctly tokenized and wpm is non-zero."""
        result = self.svc._handle_analyze_speech_pace({
            "text": "Привет мир это тест темпа речи для Краб Ир системы",
            "duration_sec": 5.0,
        })
        self.assertGreater(result["words_per_minute"], 0.0)
        self.assertGreater(result["word_count"], 0)

    def test_very_fast_pace_category(self) -> None:
        """210 words / 60 sec = 210 wpm → very_fast category."""
        result = self.svc._handle_analyze_speech_pace({
            "text": " ".join(["word"] * 210),
            "duration_sec": 60.0,
        })
        self.assertEqual(result["pace_category"], "very_fast")


if __name__ == "__main__":
    unittest.main()
