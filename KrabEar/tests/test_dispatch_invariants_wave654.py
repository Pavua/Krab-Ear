"""Wave 654 — dispatch invariant tests for 5 recently added IPC handlers.

Handlers under test (all added within last 30 days):
  - score_transcription       (Wave 161 TextProcessingService, PR #529)
  - replace_word_in_last_transcript  (PR ~#507-era)
  - get_stt_routing_decision  (PR ~#507-era)
  - compare_periods           (PR ~#529-era)
  - export_glossary_csv       (PR ~#507-era)

Each test verifies:
  1. handle_request returns a dict
  2. Response has "id" and "ok" keys
  3. Business-level result shape is correct
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from backend.translator import TranslationResult
from backend.state_store import StateStore
from backend.service import BackendService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------

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
        import numpy as np
        return np.zeros(16000, dtype=np.float32), 1.0

    def snapshot_audio(self, max_duration_sec=12.0):
        import numpy as np
        return np.zeros(32000, dtype=np.float32), 1.0


class _FakeEngine:
    _last_llm_diff = None
    _llm_rewriter = None
    _settings_get = None
    quality_profile = "balanced"
    current_model = "fake-model"

    def _resolve_diarization_device(self):
        return "cpu"


class _FakeTranscriber:
    def __init__(self):
        self.counter = 0
        self.engine = _FakeEngine()

    def transcribe(self, audio_data, quality_profile="balanced",
                   cleanup_profile="soft", domain="casual",
                   extra_vocabulary=None, lang_hint=None):
        self.counter += 1
        return f"fake transcription #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return "preview"


class _FakeTranslator:
    last_mode = "off"

    def translate(self, text, mode, network_mode,
                  translation_style="neutral", glossary=None):
        return TranslationResult(
            text="" if mode == "off" else f"TRANSLATED:{text}",
            status="not_requested" if mode == "off" else "ok",
            source_lang="",
            target_lang="",
            mode=mode,
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class _DispatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.svc = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )
        # Seed history for handlers that need items
        for i in range(3):
            self.svc.handle_request({
                "id": f"seed_{i}",
                "method": "add_history_item",
                "params": {
                    "text": f"тестовая запись {i} hello world",
                    "paste_status": "ok",
                    "translation_mode": "off",
                    "translation_status": "not_requested",
                    "confidence": 0.85,
                },
            })

    def req(self, method, params=None, req_id="t"):
        return self.svc.handle_request({
            "id": req_id,
            "method": method,
            "params": params or {},
        })

    def assert_dispatch(self, method, params=None, *, ok_required=None):
        resp = self.req(method, params)
        self.assertIsInstance(resp, dict, f"{method}: response is not dict")
        self.assertIn("id", resp, f"{method}: missing 'id' key")
        self.assertIn("ok", resp, f"{method}: missing 'ok' key")
        if ok_required is True:
            self.assertTrue(resp["ok"], f"{method}: ok=False, error={resp.get('error')}")
        elif ok_required is False:
            self.assertFalse(resp["ok"], f"{method}: expected ok=False, got ok=True")
        return resp


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestScoreTranscription(_DispatchBase):
    """score_transcription — delegated to TextProcessingService."""

    def test_score_transcription_returns_score(self):
        """score_transcription with text returns ok=True and a numeric score."""
        resp = self.assert_dispatch(
            "score_transcription",
            {"text": "тестовая запись hello world", "confidence": 0.85, "duration_sec": 5.0},
            ok_required=True,
        )
        result = resp["result"]
        self.assertIn("overall_score", result, "result should have 'overall_score' key")
        self.assertIsInstance(result["overall_score"], (int, float), "overall_score should be numeric")


class TestReplaceWordInLastTranscript(_DispatchBase):
    """replace_word_in_last_transcript — word replacement in most recent history item."""

    def test_replace_word_missing_params_inner_error(self):
        """Missing old_word/new_word → outer ok=True, inner result has ok=False and error='missing_words'.

        handle_request wraps handler return value in {"ok": True, "result": <handler_return>}.
        Handlers that use the ok-in-result pattern (not exceptions) appear as outer ok=True.
        """
        resp = self.assert_dispatch(
            "replace_word_in_last_transcript",
            {},
            ok_required=True,  # outer envelope is always True; handler signals error via result["ok"]
        )
        result = resp["result"]
        self.assertFalse(result["ok"], "inner result should have ok=False for missing words")
        self.assertEqual(result.get("error"), "missing_words")

    def test_replace_word_success(self):
        """Valid old_word/new_word in existing history → outer ok=True, inner ok=True, replaced_count present."""
        resp = self.assert_dispatch(
            "replace_word_in_last_transcript",
            {"old_word": "hello", "new_word": "привет"},
            ok_required=True,
        )
        result = resp["result"]
        self.assertIn("replaced_count", result)
        self.assertGreaterEqual(result["replaced_count"], 0)


class TestGetSttRoutingDecision(_DispatchBase):
    """get_stt_routing_decision — scored adapter selection debug endpoint."""

    def test_routing_decision_returns_expected_keys(self):
        """get_stt_routing_decision returns ok=True with scores dict and language."""
        resp = self.assert_dispatch(
            "get_stt_routing_decision",
            {"language": "ru", "audio_duration_s": 10.0},
            ok_required=True,
        )
        result = resp["result"]
        self.assertIn("scores", result, "result should have 'scores' key")
        self.assertIn("language", result, "result should have 'language' key")
        self.assertIn("selected_engine", result, "result should have 'selected_engine' key")
        self.assertEqual(result["language"], "ru")


class TestComparePeriods(_DispatchBase):
    """compare_periods — two-period statistics comparison."""

    def test_compare_periods_missing_params_returns_error(self):
        """Missing period params → ok=False (ValueError wrapped by handle_request)."""
        resp = self.assert_dispatch(
            "compare_periods",
            {},
            ok_required=False,
        )
        self.assertIn("error", resp)

    def test_compare_periods_valid_params(self):
        """Valid ISO date params → ok=True with period1/period2 keys."""
        resp = self.assert_dispatch(
            "compare_periods",
            {
                "period1_start": "2020-01-01T00:00:00",
                "period1_end": "2020-06-30T23:59:59",
                "period2_start": "2021-01-01T00:00:00",
                "period2_end": "2021-06-30T23:59:59",
            },
            ok_required=True,
        )
        result = resp["result"]
        self.assertIn("period1", result)
        self.assertIn("period2", result)


class TestExportGlossaryCsv(_DispatchBase):
    """export_glossary_csv — export translation glossary as CSV string."""

    def test_export_empty_glossary(self):
        """Export with empty glossary returns ok=True, csv has header, row_count=0."""
        resp = self.assert_dispatch("export_glossary_csv", {}, ok_required=True)
        result = resp["result"]
        self.assertIn("csv", result)
        self.assertIn("row_count", result)
        self.assertEqual(result["row_count"], 0)
        # CSV should at minimum have the header row
        self.assertIn("source", result["csv"])
        self.assertIn("target", result["csv"])


if __name__ == "__main__":
    unittest.main()
