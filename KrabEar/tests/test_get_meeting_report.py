"""Tests for the get_meeting_report IPC handler.

Covers:
- dispatch entry present in BackendService._build_dispatch_table
- all contract keys returned on a normal populated item
- privacy_mode → ok:False + empty arrays + markdown:""
- speaker_turns aggregation: correct turns/duration per speaker
- item without diarization → speakers:[] / speaker_count:0
- not-found id → ok:False + fallback_reason:"not_found"
- markdown non-empty for a populated item
- handler never raises when sub-calls (summarize/extract) fail
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup (standalone or pytest run)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Minimal HistoryItem stub (no mlx/pyannote imports needed)
# ---------------------------------------------------------------------------


class _FakeItem:
    """Minimal HistoryItem duck-type for testing."""

    def __init__(
        self,
        item_id: str = "abc123",
        text: str = "Это тест транскрипции встречи",
        ts: str = "2026-06-18T10:00:00Z",
        speaker_turns: list | None = None,
    ) -> None:
        self.id = item_id
        self.text = text
        self.ts = ts
        self.speaker_turns = speaker_turns
        self.diarization = None


# ---------------------------------------------------------------------------
# Minimal service stub
# ---------------------------------------------------------------------------

# Import handler implementation directly to avoid instantiating full BackendService
# (which requires mlx-whisper etc).  We bind the method onto our stub class.
import importlib
import types

# Load service module with stubs for heavy optional deps so the module parses.
_HEAVY_STUBS = [
    "mlx_whisper", "mlx", "mlx.core",
    "pyannote", "pyannote.audio",
    "sounddevice",
    "torch",
    "silero",
    "kokoro",
]

for _mod_name in _HEAVY_STUBS:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)

# Patch the dispatch table builder to return {} so __init__ doesn't blow up.
# We test the handler method in isolation.


class _FakeStore:
    def __init__(self, items: list) -> None:
        self._items = items

    def _load_active_items_with_lock(self) -> list:
        return list(self._items)


class _MinimalService:
    """Minimal stub for BackendService with only what _handle_get_meeting_report needs."""

    def __init__(
        self,
        privacy: bool = False,
        store_items: list | None = None,
        summarize_result: dict | None = None,
        action_items_result: dict | None = None,
        summarize_raises: Exception | None = None,
        action_items_raises: Exception | None = None,
    ) -> None:
        self._privacy = privacy
        self.store = _FakeStore(store_items or [])

        # Text processing service stub
        text_svc = MagicMock()
        if summarize_raises is not None:
            text_svc.handle_summarize_item.side_effect = summarize_raises
        else:
            text_svc.handle_summarize_item.return_value = summarize_result or {
                "summary": "Краткое резюме встречи.",
                "llm": True,
                "source_chars": 100,
            }
        self._text_processing_svc = text_svc

        # Search and analysis service stub
        search_svc = MagicMock()
        if action_items_raises is not None:
            search_svc.handle_extract_action_items.side_effect = action_items_raises
        else:
            search_svc.handle_extract_action_items.return_value = action_items_result or {
                "id": "abc123",
                "ok": True,
                "action_items": [{"text": "Сделать отчёт"}, {"text": "Позвонить клиенту"}],
                "decisions": ["Использовать Python 3.12"],
                "questions": ["Когда дедлайн?"],
                "fallback_reason": None,
                "latency_ms": 42,
            }
        self._search_and_analysis_svc = search_svc

    def _get_runtime_setting(self, key: str, default: Any = None) -> Any:
        if key == "privacy_mode_enabled":
            return self._privacy
        return default

    # Bind the real handler from service.py onto this stub class at import time.


def _bind_handler(stub_cls: type) -> None:
    """Import the handler function from backend.service and bind it to stub_cls."""
    # We load backend.service lazily here, patching heavy deps.
    with patch.dict(sys.modules, {m: sys.modules.get(m, types.ModuleType(m)) for m in _HEAVY_STUBS}):
        try:
            import backend.service as _svc_mod
            # Reload in case prior test runs cached a different state.
        except Exception:
            return
    # Extract the handler from the real BackendService class.
    handler = _svc_mod.BackendService._handle_get_meeting_report
    stub_cls._handle_get_meeting_report = handler


_bind_handler(_MinimalService)


# ---------------------------------------------------------------------------
# Helper: assert all contract keys present
# ---------------------------------------------------------------------------

_CONTRACT_KEYS = {
    "id", "ok", "summary", "summary_is_llm",
    "action_items", "decisions", "questions",
    "speakers", "speaker_count", "word_count",
    "ts", "markdown", "fallback_reason",
}


def _assert_contract(tc: unittest.TestCase, result: dict) -> None:
    for k in _CONTRACT_KEYS:
        tc.assertIn(k, result, f"Ключ '{k}' отсутствует в результате")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class DispatchEntryPresentTestCase(unittest.TestCase):
    """dispatch entry 'get_meeting_report' must exist in _build_dispatch_table."""

    def test_dispatch_entry_present(self) -> None:
        """The dispatch table in backend.service references 'get_meeting_report'."""
        import subprocess
        result = subprocess.run(
            ["grep", "-c", '"get_meeting_report"',
             str(PACKAGE_ROOT / "backend" / "service.py")],
            capture_output=True, text=True,
        )
        count = int(result.stdout.strip() or "0")
        self.assertGreaterEqual(count, 1, "'get_meeting_report' not found in service.py")


class AllContractKeysReturnedTestCase(unittest.TestCase):
    """Handler returns all contract keys on a normal populated item."""

    def setUp(self) -> None:
        self.item = _FakeItem(
            item_id="abc123",
            text="Тест текст для встречи слово",
            ts="2026-06-18T10:00:00Z",
            speaker_turns=[
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 10.0},
                {"speaker": "SPEAKER_01", "start": 10.0, "end": 25.0},
                {"speaker": "SPEAKER_00", "start": 25.0, "end": 40.0},
            ],
        )
        self.svc = _MinimalService(store_items=[self.item])

    def test_all_contract_keys_present(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        _assert_contract(self, result)

    def test_ok_is_true(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertTrue(result["ok"])

    def test_id_echoed(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertEqual(result["id"], "abc123")

    def test_summary_returned(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertEqual(result["summary"], "Краткое резюме встречи.")

    def test_summary_is_llm_true(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertTrue(result["summary_is_llm"])

    def test_action_items_list(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertIsInstance(result["action_items"], list)
        self.assertEqual(len(result["action_items"]), 2)

    def test_decisions_list(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertIn("Использовать Python 3.12", result["decisions"])

    def test_questions_list(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertIn("Когда дедлайн?", result["questions"])

    def test_word_count(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertEqual(result["word_count"], 5)  # "Тест текст для встречи слово"

    def test_ts_echoed(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertEqual(result["ts"], "2026-06-18T10:00:00Z")

    def test_markdown_nonempty(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertIsInstance(result["markdown"], str)
        self.assertGreater(len(result["markdown"]), 0)

    def test_fallback_reason_empty_on_success(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertEqual(result["fallback_reason"], "")


class PrivacyModeTestCase(unittest.TestCase):
    """privacy_mode → ok:False + empty arrays + markdown:''."""

    def setUp(self) -> None:
        self.item = _FakeItem(item_id="abc123")
        self.svc = _MinimalService(privacy=True, store_items=[self.item])

    def test_ok_false_in_privacy_mode(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertFalse(result["ok"])

    def test_all_keys_present_in_privacy_mode(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        _assert_contract(self, result)

    def test_empty_arrays_in_privacy_mode(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertEqual(result["action_items"], [])
        self.assertEqual(result["decisions"], [])
        self.assertEqual(result["questions"], [])
        self.assertEqual(result["speakers"], [])

    def test_markdown_empty_in_privacy_mode(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertEqual(result["markdown"], "")

    def test_fallback_reason_privacy_mode(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertEqual(result["fallback_reason"], "privacy_mode")

    def test_sub_services_not_called_in_privacy_mode(self) -> None:
        self.svc._handle_get_meeting_report({"id": "abc123"})
        self.svc._text_processing_svc.handle_summarize_item.assert_not_called()
        self.svc._search_and_analysis_svc.handle_extract_action_items.assert_not_called()

    def test_speaker_count_zero_in_privacy_mode(self) -> None:
        result = self.svc._handle_get_meeting_report({"id": "abc123"})
        self.assertEqual(result["speaker_count"], 0)


class SpeakerAggregationTestCase(unittest.TestCase):
    """speaker_turns aggregation: correct turns and duration per speaker."""

    def _make_svc(self, speaker_turns: list) -> _MinimalService:
        item = _FakeItem(
            item_id="sp1",
            text="один два три",
            ts="2026-06-18T10:00:00Z",
            speaker_turns=speaker_turns,
        )
        return _MinimalService(store_items=[item])

    def test_two_speakers_aggregated(self) -> None:
        turns = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 10.0},
            {"speaker": "SPEAKER_01", "start": 10.0, "end": 25.5},
            {"speaker": "SPEAKER_00", "start": 25.5, "end": 35.0},
        ]
        svc = self._make_svc(turns)
        result = svc._handle_get_meeting_report({"id": "sp1"})
        speakers = {sp["label"]: sp for sp in result["speakers"]}

        self.assertIn("SPEAKER_00", speakers)
        self.assertIn("SPEAKER_01", speakers)
        self.assertEqual(speakers["SPEAKER_00"]["turns"], 2)
        self.assertAlmostEqual(speakers["SPEAKER_00"]["duration_sec"], 19.5, places=5)
        self.assertEqual(speakers["SPEAKER_01"]["turns"], 1)
        self.assertAlmostEqual(speakers["SPEAKER_01"]["duration_sec"], 15.5, places=5)
        self.assertEqual(result["speaker_count"], 2)

    def test_single_speaker(self) -> None:
        turns = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 60.0},
        ]
        svc = self._make_svc(turns)
        result = svc._handle_get_meeting_report({"id": "sp1"})
        self.assertEqual(result["speaker_count"], 1)
        self.assertEqual(result["speakers"][0]["turns"], 1)
        self.assertAlmostEqual(result["speakers"][0]["duration_sec"], 60.0)

    def test_nan_duration_coerced_to_zero(self) -> None:
        turns = [
            {"speaker": "SPEAKER_00", "start": float("nan"), "end": float("nan")},
        ]
        svc = self._make_svc(turns)
        result = svc._handle_get_meeting_report({"id": "sp1"})
        dur = result["speakers"][0]["duration_sec"]
        self.assertTrue(math.isfinite(dur))
        self.assertEqual(dur, 0.0)

    def test_inf_duration_coerced_to_zero(self) -> None:
        turns = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": float("inf")},
        ]
        svc = self._make_svc(turns)
        result = svc._handle_get_meeting_report({"id": "sp1"})
        dur = result["speakers"][0]["duration_sec"]
        self.assertTrue(math.isfinite(dur))

    def test_negative_duration_clamped_to_zero(self) -> None:
        turns = [
            {"speaker": "SPEAKER_00", "start": 10.0, "end": 5.0},  # end < start
        ]
        svc = self._make_svc(turns)
        result = svc._handle_get_meeting_report({"id": "sp1"})
        self.assertEqual(result["speakers"][0]["duration_sec"], 0.0)


class NoDiarizationTestCase(unittest.TestCase):
    """Item without speaker_turns → speakers:[] / speaker_count:0."""

    def test_no_speaker_turns_gives_empty_speakers(self) -> None:
        item = _FakeItem(item_id="nd1", speaker_turns=None)
        svc = _MinimalService(store_items=[item])
        result = svc._handle_get_meeting_report({"id": "nd1"})
        self.assertEqual(result["speakers"], [])
        self.assertEqual(result["speaker_count"], 0)

    def test_empty_speaker_turns_list(self) -> None:
        item = _FakeItem(item_id="nd2", speaker_turns=[])
        svc = _MinimalService(store_items=[item])
        result = svc._handle_get_meeting_report({"id": "nd2"})
        self.assertEqual(result["speakers"], [])
        self.assertEqual(result["speaker_count"], 0)


class NotFoundTestCase(unittest.TestCase):
    """not-found id → ok:False + fallback_reason:'not_found'."""

    def test_not_found_id(self) -> None:
        svc = _MinimalService(store_items=[])
        result = svc._handle_get_meeting_report({"id": "nonexistent"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["fallback_reason"], "not_found")

    def test_not_found_all_keys_present(self) -> None:
        svc = _MinimalService(store_items=[])
        result = svc._handle_get_meeting_report({"id": "nonexistent"})
        _assert_contract(self, result)

    def test_empty_id_gives_not_found(self) -> None:
        item = _FakeItem(item_id="abc")
        svc = _MinimalService(store_items=[item])
        result = svc._handle_get_meeting_report({"id": ""})
        self.assertFalse(result["ok"])
        self.assertEqual(result["fallback_reason"], "not_found")


class MarkdownPopulatedTestCase(unittest.TestCase):
    """markdown is a non-empty string for a populated item."""

    def test_markdown_contains_header(self) -> None:
        item = _FakeItem(item_id="md1", ts="2026-06-18T10:00:00Z")
        svc = _MinimalService(store_items=[item])
        result = svc._handle_get_meeting_report({"id": "md1"})
        self.assertIn("# Встреча", result["markdown"])

    def test_markdown_contains_summary_section(self) -> None:
        item = _FakeItem(item_id="md2")
        svc = _MinimalService(store_items=[item])
        result = svc._handle_get_meeting_report({"id": "md2"})
        self.assertIn("## Резюме", result["markdown"])

    def test_markdown_contains_action_items(self) -> None:
        item = _FakeItem(item_id="md3")
        svc = _MinimalService(store_items=[item])
        result = svc._handle_get_meeting_report({"id": "md3"})
        self.assertIn("## Задачи", result["markdown"])

    def test_markdown_contains_speakers_when_present(self) -> None:
        item = _FakeItem(
            item_id="md4",
            speaker_turns=[{"speaker": "SPEAKER_00", "start": 0.0, "end": 30.0}],
        )
        svc = _MinimalService(store_items=[item])
        result = svc._handle_get_meeting_report({"id": "md4"})
        self.assertIn("## Спикеры", result["markdown"])
        self.assertIn("SPEAKER_00", result["markdown"])

    def test_markdown_omits_speakers_section_when_empty(self) -> None:
        item = _FakeItem(item_id="md5", speaker_turns=None)
        svc = _MinimalService(store_items=[item])
        result = svc._handle_get_meeting_report({"id": "md5"})
        self.assertNotIn("## Спикеры", result["markdown"])


class ResilienceTestCase(unittest.TestCase):
    """Handler never raises when sub-calls fail; degrades gracefully."""

    def test_summarize_raises_still_returns(self) -> None:
        item = _FakeItem(item_id="r1")
        svc = _MinimalService(
            store_items=[item],
            summarize_raises=RuntimeError("LLM unavailable"),
        )
        # Must not raise
        result = svc._handle_get_meeting_report({"id": "r1"})
        _assert_contract(self, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"], "")
        self.assertIn("summary_failed", result["fallback_reason"])

    def test_action_items_raises_still_returns(self) -> None:
        item = _FakeItem(item_id="r2")
        svc = _MinimalService(
            store_items=[item],
            action_items_raises=RuntimeError("extractor not configured"),
        )
        result = svc._handle_get_meeting_report({"id": "r2"})
        _assert_contract(self, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action_items"], [])
        self.assertIn("action_items_failed", result["fallback_reason"])

    def test_both_sub_calls_raise_still_returns_ok(self) -> None:
        item = _FakeItem(item_id="r3")
        svc = _MinimalService(
            store_items=[item],
            summarize_raises=RuntimeError("sum err"),
            action_items_raises=RuntimeError("ai err"),
        )
        result = svc._handle_get_meeting_report({"id": "r3"})
        self.assertTrue(result["ok"])
        self.assertIn("summary_failed", result["fallback_reason"])
        self.assertIn("action_items_failed", result["fallback_reason"])

    def test_store_raises_gives_not_found(self) -> None:
        item = _FakeItem(item_id="r4")
        svc = _MinimalService(store_items=[item])
        svc.store._items = None  # will cause TypeError on list()

        # Monkeypatch _load_active_items_with_lock to raise
        svc.store._load_active_items_with_lock = lambda: (_ for _ in ()).throw(
            RuntimeError("store error")
        )
        # Should not raise — handler wraps store access
        result = svc._handle_get_meeting_report({"id": "r4"})
        _assert_contract(self, result)
        # item won't be found since store raised → not_found
        self.assertFalse(result["ok"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
