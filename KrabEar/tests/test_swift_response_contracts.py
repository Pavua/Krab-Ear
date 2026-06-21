# -*- coding: utf-8 -*-
"""PIN tests — Swift IPC response-key contracts.

Each test calls the real handler through BackendService.handle_request and asserts
that result contains the exact keys the Swift UI reads.  Value is NOT asserted —
only key PRESENCE — so a future rename or silent drop fails loudly at the seam
before Swift ever notices.

Handlers covered:
    1. get_usage_stats
    2. score_transcription
    3. health_check
    4. export_settings
    5. suggest_medical_glossary_terms
    6. auto_summarize_batch

CI-flake lesson (BackendService daemon threads, PR #1782):
    Every test that constructs BackendService MUST call service.close() in tearDown.
    BackendService.__init__ starts DiskSpaceMonitor / RecapScheduler / ExportScheduler;
    without close() those threads crash stderr at interpreter shutdown and fail the
    entire chunk file on ubuntu CI even when all asserts are green.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.service import BackendService
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service() -> BackendService:
    """Minimal BackendService backed by a temp StateStore."""
    tmp = Path(tempfile.mkdtemp())
    store = StateStore(data_dir=tmp / "data")
    return BackendService(store=store)


def _call(service: BackendService, method: str, params: dict) -> dict:
    """Call handle_request and return the result dict (raises on IPC error)."""
    response = service.handle_request({"id": "t", "method": method, "params": params})
    assert response.get("ok") is True, (
        f"handle_request returned ok=False for method={method!r}: {response}"
    )
    return response["result"]


# ---------------------------------------------------------------------------
# 1. get_usage_stats
# ---------------------------------------------------------------------------

class TestGetUsageStatsContract(unittest.TestCase):
    """Pins response shape read by Swift UsageStatsViewController."""

    def setUp(self) -> None:
        self.service = _make_service()

    def tearDown(self) -> None:
        self.service.close()

    def test_top_level_period_keys_present(self) -> None:
        result = _call(self.service, "get_usage_stats", {})
        for key in ("today", "this_week", "all_time"):
            self.assertIn(
                key, result,
                msg=f"get_usage_stats result missing top-level key {key!r}",
            )

    def test_today_contains_recordings(self) -> None:
        result = _call(self.service, "get_usage_stats", {})
        today = result["today"]
        self.assertIsInstance(today, dict, "result['today'] must be a dict")
        self.assertIn("recordings", today, "result['today'] missing key 'recordings'")

    def test_this_week_contains_recordings(self) -> None:
        result = _call(self.service, "get_usage_stats", {})
        this_week = result["this_week"]
        self.assertIsInstance(this_week, dict, "result['this_week'] must be a dict")
        self.assertIn("recordings", this_week, "result['this_week'] missing key 'recordings'")

    def test_all_time_contains_recordings(self) -> None:
        result = _call(self.service, "get_usage_stats", {})
        all_time = result["all_time"]
        self.assertIsInstance(all_time, dict, "result['all_time'] must be a dict")
        self.assertIn("recordings", all_time, "result['all_time'] missing key 'recordings'")


# ---------------------------------------------------------------------------
# 2. score_transcription
# ---------------------------------------------------------------------------

class TestScoreTranscriptionContract(unittest.TestCase):
    """Pins response shape read by Swift quality-score UI.

    Privacy mode is OFF by default (default settings); the handler privacy-gates
    and returns a degraded response when privacy_mode_enabled=True, so we ensure
    privacy is off (it is by default) to exercise the normal path.
    """

    def setUp(self) -> None:
        self.service = _make_service()

    def tearDown(self) -> None:
        self.service.close()

    def test_overall_score_present(self) -> None:
        result = _call(
            self.service,
            "score_transcription",
            {"text": "Это пробный текст для оценки качества транскрипции."},
        )
        self.assertIn(
            "overall_score", result,
            "score_transcription result missing key 'overall_score'",
        )

    def test_grade_present(self) -> None:
        result = _call(
            self.service,
            "score_transcription",
            {"text": "Это пробный текст для оценки качества транскрипции."},
        )
        self.assertIn(
            "grade", result,
            "score_transcription result missing key 'grade'",
        )


# ---------------------------------------------------------------------------
# 3. health_check
# ---------------------------------------------------------------------------

class TestHealthCheckContract(unittest.TestCase):
    """Pins response shape read by Swift StatusIndicatorView / DiagnosticsPanel."""

    def setUp(self) -> None:
        self.service = _make_service()

    def tearDown(self) -> None:
        self.service.close()

    def test_status_present(self) -> None:
        result = _call(self.service, "health_check", {})
        self.assertIn("status", result, "health_check result missing key 'status'")

    def test_checks_present(self) -> None:
        result = _call(self.service, "health_check", {})
        self.assertIn("checks", result, "health_check result missing key 'checks'")

    def test_checks_is_dict(self) -> None:
        result = _call(self.service, "health_check", {})
        self.assertIsInstance(result["checks"], dict, "result['checks'] must be a dict")

    def test_checks_contains_stt_model(self) -> None:
        result = _call(self.service, "health_check", {})
        self.assertIn(
            "stt_model", result["checks"],
            "result['checks'] missing key 'stt_model'",
        )

    def test_checks_contains_llm(self) -> None:
        result = _call(self.service, "health_check", {})
        self.assertIn("llm", result["checks"], "result['checks'] missing key 'llm'")

    def test_checks_contains_history_store(self) -> None:
        result = _call(self.service, "health_check", {})
        self.assertIn(
            "history_store", result["checks"],
            "result['checks'] missing key 'history_store'",
        )

    def test_stt_model_has_status(self) -> None:
        result = _call(self.service, "health_check", {})
        stt = result["checks"]["stt_model"]
        self.assertIsInstance(stt, dict, "checks['stt_model'] must be a dict")
        self.assertIn("status", stt, "checks['stt_model'] missing key 'status'")

    def test_llm_has_status(self) -> None:
        result = _call(self.service, "health_check", {})
        llm = result["checks"]["llm"]
        self.assertIsInstance(llm, dict, "checks['llm'] must be a dict")
        self.assertIn("status", llm, "checks['llm'] missing key 'status'")

    def test_history_store_has_status(self) -> None:
        result = _call(self.service, "health_check", {})
        hs = result["checks"]["history_store"]
        self.assertIsInstance(hs, dict, "checks['history_store'] must be a dict")
        self.assertIn("status", hs, "checks['history_store'] missing key 'status'")


# ---------------------------------------------------------------------------
# 4. export_settings
# ---------------------------------------------------------------------------

class TestExportSettingsContract(unittest.TestCase):
    """Pins response shape read by Swift settings-export UI."""

    def setUp(self) -> None:
        self.service = _make_service()

    def tearDown(self) -> None:
        self.service.close()

    def test_file_key_present(self) -> None:
        # export_settings writes a file to ~/.  We do not pass a custom path so
        # the handler generates a timestamped file in ~/ — clean up after.
        result = _call(self.service, "export_settings", {})
        self.assertIn("file", result, "export_settings result missing key 'file'")
        # Best-effort cleanup so we do not litter ~/ with test artefacts.
        try:
            Path(result["file"]).unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 5. suggest_medical_glossary_terms
# ---------------------------------------------------------------------------

class TestSuggestMedicalGlossaryTermsContract(unittest.TestCase):
    """Pins response shape read by Swift glossary-suggestion UI.

    Privacy mode is OFF by default.  With an empty store the handler returns
    an empty suggestions list — that is the normal schema (not a privacy gate
    short-circuit), so the key is still present.
    """

    def setUp(self) -> None:
        self.service = _make_service()

    def tearDown(self) -> None:
        self.service.close()

    def test_suggestions_key_present(self) -> None:
        result = _call(
            self.service,
            "suggest_medical_glossary_terms",
            {"limit": 5},
        )
        self.assertIn(
            "suggestions", result,
            "suggest_medical_glossary_terms result missing key 'suggestions'",
        )

    def test_suggestions_is_list(self) -> None:
        result = _call(
            self.service,
            "suggest_medical_glossary_terms",
            {"limit": 5},
        )
        self.assertIsInstance(
            result["suggestions"], list,
            "result['suggestions'] must be a list",
        )


# ---------------------------------------------------------------------------
# 6. auto_summarize_batch
# ---------------------------------------------------------------------------

class TestAutoSummarizeBatchContract(unittest.TestCase):
    """Pins response shape read by Swift auto-summarize UI.

    Strategy: insert a real history item with non-empty text, then call
    auto_summarize_batch with that item's ID.  Because no LLM is configured in
    the test service (_llm_rewriter is None), the handler takes the fallback path
    (keys: summary, key_points, items_processed, total_words, llm, fallback,
    error) — all required Swift keys are present on this path too, so no mocking
    of the LLM is needed.

    NOTE: the handler raises RuntimeError (→ ok=False) for empty/invalid ids, so
    we must supply at least one real ID.  We use handle_add_history_item via IPC
    to insert the item so the test is fully integration-level.
    """

    def setUp(self) -> None:
        self.service = _make_service()
        # Insert a history item so we have a valid ID to pass.
        add_resp = self.service.handle_request({
            "id": "setup-1",
            "method": "add_history_item",
            "params": {
                "text": "Тестовый текст для пакетного резюме авто-суммаризатора.",
                "paste_status": "pasted",
            },
        })
        assert add_resp.get("ok") is True, f"setup add_history_item failed: {add_resp}"
        self._item_id = add_resp["result"]["id"]

    def tearDown(self) -> None:
        self.service.close()

    def _call_batch(self) -> dict:
        return _call(
            self.service,
            "auto_summarize_batch",
            {"ids": [self._item_id]},
        )

    def test_summary_key_present(self) -> None:
        result = self._call_batch()
        self.assertIn("summary", result, "auto_summarize_batch result missing key 'summary'")

    def test_key_points_present(self) -> None:
        result = self._call_batch()
        self.assertIn(
            "key_points", result,
            "auto_summarize_batch result missing key 'key_points'",
        )

    def test_items_processed_present(self) -> None:
        result = self._call_batch()
        self.assertIn(
            "items_processed", result,
            "auto_summarize_batch result missing key 'items_processed'",
        )

    def test_total_words_present(self) -> None:
        result = self._call_batch()
        self.assertIn(
            "total_words", result,
            "auto_summarize_batch result missing key 'total_words'",
        )

    def test_llm_flag_present(self) -> None:
        result = self._call_batch()
        self.assertIn(
            "llm", result,
            "auto_summarize_batch result missing key 'llm'",
        )

    def test_fallback_flag_present(self) -> None:
        result = self._call_batch()
        self.assertIn(
            "fallback", result,
            "auto_summarize_batch result missing key 'fallback'",
        )


if __name__ == "__main__":
    unittest.main()
