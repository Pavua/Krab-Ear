"""test_search_analysis_wave29_action_items.py — regression tests for wave-29 fixes.

FIX D1 (MED privacy) — handle_extract_action_items had NO privacy gate:
  Full raw transcript was POSTed to LLM (LM Studio) even when privacy_mode_enabled=True.
  Fix: added privacy_mode_enabled guard returning empty-but-schema-parity dict.

FIX D2 (MED DoS) — handle_batch_extract_action_items had unbounded ids list:
  Attacker could pass a list of arbitrary length and trigger serial LLM calls for each.
  Fix: MAX_BATCH_ACTION_ITEMS = 20; list > 20 → RuntimeError.
  Also: batch_extract_action_items added to HEAVY_METHODS in ipc_throttle.py.

Tests:
  D1 — privacy=True → extract_action_items returns empty schema-parity dict (no LLM call).
  D1 — privacy=False → normal flow proceeds (LLM called).
  D2 — batch with 21 ids → RuntimeError raised.
  D2 — batch with exactly 20 ids → OK.
  D2 — batch with 0 ids → OK (empty results).
  D2 — privacy=True → batch returns empty results without LLM calls.
  throttle — batch_extract_action_items is in HEAVY_METHODS.
"""

from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.search_and_analysis_service import SearchAndAnalysisService
from backend.ipc_throttle import HEAVY_METHODS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_item(item_id: str, text: str = "встреча по продукту") -> Any:
    item = types.SimpleNamespace()
    item.id = item_id
    item.text = text
    item.ts = 1_700_000_000.0
    item.audio_duration_sec = 30.0
    item.confidence = 0.9
    item.language = "ru"
    item.action_items = None
    return item


def _make_fake_store(items: list[Any] | None = None) -> Any:
    _items = items or [_make_fake_item("i1"), _make_fake_item("i2")]
    lock = threading.Lock()

    class _LockCtx:
        def __enter__(self):
            lock.acquire()
            return self

        def __exit__(self, *_):
            lock.release()

    store = types.SimpleNamespace()
    store._lock = _LockCtx
    store._load_active_items_unlocked = lambda: list(_items)
    store.update_history_item_action_items = MagicMock()
    return store


def _make_extractor_returning(
    ok: bool = True,
    action_items: list | None = None,
    decisions: list | None = None,
    questions: list | None = None,
    latency_ms: int = 42,
) -> Any:
    """Build a fake ActionItemsExtractor whose extract() returns a controlled result."""

    class _FakeActionItem:
        def to_dict(self):
            return {"task": "сделать что-то", "priority": "high"}

    result = types.SimpleNamespace()
    result.ok = ok
    result.action_items = action_items if action_items is not None else [_FakeActionItem()]
    result.decisions = decisions if decisions is not None else ["решение A"]
    result.questions = questions if questions is not None else ["вопрос B"]
    result.fallback_reason = None
    result.latency_ms = latency_ms

    extractor = MagicMock()
    extractor.extract.return_value = result
    return extractor


def _make_svc(
    privacy_enabled: bool,
    extractor: Any = None,
    items: list[Any] | None = None,
) -> SearchAndAnalysisService:
    mock_searcher = MagicMock()
    mock_searcher.is_enabled = False

    def _settings_get(key: str, default: Any = None) -> Any:
        if key == "privacy_mode_enabled":
            return privacy_enabled
        return default

    store = _make_fake_store(items)

    return SearchAndAnalysisService(
        store=store,
        semantic_searcher=mock_searcher,
        action_items_extractor=extractor,
        topic_tracker=MagicMock(),
        recording_insights=MagicMock(),
        recording_comparison=MagicMock(),
        stats_report=MagicMock(),
        settings_get=_settings_get,
    )


# ---------------------------------------------------------------------------
# D1 — Privacy gate on handle_extract_action_items
# ---------------------------------------------------------------------------


class ExtractActionItemsPrivacyGuardTestCase(unittest.TestCase):
    """handle_extract_action_items must return empty schema-parity dict when privacy_mode_enabled."""

    def test_privacy_on_returns_ok_true(self) -> None:
        """ok must be True (not an error) when privacy mode is on."""
        svc = _make_svc(privacy_enabled=True, extractor=_make_extractor_returning())
        result = svc.handle_extract_action_items({"id": "i1"})
        self.assertTrue(result.get("ok"))

    def test_privacy_on_returns_empty_action_items(self) -> None:
        """action_items must be [] when privacy_mode_enabled=True."""
        svc = _make_svc(privacy_enabled=True, extractor=_make_extractor_returning())
        result = svc.handle_extract_action_items({"id": "i1"})
        self.assertEqual(result.get("action_items"), [])

    def test_privacy_on_returns_empty_decisions(self) -> None:
        """decisions must be [] when privacy_mode_enabled=True."""
        svc = _make_svc(privacy_enabled=True, extractor=_make_extractor_returning())
        result = svc.handle_extract_action_items({"id": "i1"})
        self.assertEqual(result.get("decisions"), [])

    def test_privacy_on_returns_empty_questions(self) -> None:
        """questions must be [] when privacy_mode_enabled=True."""
        svc = _make_svc(privacy_enabled=True, extractor=_make_extractor_returning())
        result = svc.handle_extract_action_items({"id": "i1"})
        self.assertEqual(result.get("questions"), [])

    def test_privacy_on_returns_reason_flag(self) -> None:
        """Response includes reason=privacy_mode_active when privacy on."""
        svc = _make_svc(privacy_enabled=True, extractor=_make_extractor_returning())
        result = svc.handle_extract_action_items({"id": "i1"})
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("privacy_mode_active"))

    def test_privacy_on_extractor_not_called(self) -> None:
        """LLM extractor must NOT be called when privacy_mode_enabled=True."""
        extractor = _make_extractor_returning()
        svc = _make_svc(privacy_enabled=True, extractor=extractor)
        svc.handle_extract_action_items({"id": "i1"})
        extractor.extract.assert_not_called()

    def test_privacy_on_store_not_written(self) -> None:
        """No store update must happen when privacy mode is on."""
        extractor = _make_extractor_returning()
        svc = _make_svc(privacy_enabled=True, extractor=extractor)
        svc.handle_extract_action_items({"id": "i1"})
        svc._store.update_history_item_action_items.assert_not_called()

    def test_privacy_on_schema_parity_keys(self) -> None:
        """Response has all expected keys even in privacy-mode (schema parity)."""
        svc = _make_svc(privacy_enabled=True, extractor=_make_extractor_returning())
        result = svc.handle_extract_action_items({"id": "i1"})
        for key in ("id", "ok", "action_items", "decisions", "questions", "fallback_reason",
                    "latency_ms"):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_privacy_off_extractor_is_called(self) -> None:
        """Privacy off: LLM extractor IS called with the transcript text."""
        extractor = _make_extractor_returning()
        items = [_make_fake_item("i1", text="обсуждение задач")]
        svc = _make_svc(privacy_enabled=False, extractor=extractor, items=items)
        svc.handle_extract_action_items({"id": "i1"})
        extractor.extract.assert_called_once()

    def test_privacy_off_returns_real_action_items(self) -> None:
        """Privacy off: action_items is populated from extractor result."""
        extractor = _make_extractor_returning(ok=True)
        items = [_make_fake_item("i1")]
        svc = _make_svc(privacy_enabled=False, extractor=extractor, items=items)
        result = svc.handle_extract_action_items({"id": "i1"})
        self.assertTrue(result.get("ok"))
        self.assertIsInstance(result.get("action_items"), list)
        self.assertGreater(len(result["action_items"]), 0)

    def test_privacy_off_no_reason_key(self) -> None:
        """Privacy off: response must NOT include reason/privacy_mode_active keys."""
        extractor = _make_extractor_returning()
        items = [_make_fake_item("i1")]
        svc = _make_svc(privacy_enabled=False, extractor=extractor, items=items)
        result = svc.handle_extract_action_items({"id": "i1"})
        self.assertNotIn("reason", result)
        self.assertNotIn("privacy_mode_active", result)


# ---------------------------------------------------------------------------
# D2 — DoS limit on handle_batch_extract_action_items
# ---------------------------------------------------------------------------


class BatchExtractActionItemsDoSLimitTestCase(unittest.TestCase):
    """handle_batch_extract_action_items must reject lists > MAX_BATCH_ACTION_ITEMS."""

    def test_21_ids_raises_runtime_error(self) -> None:
        """Passing 21 ids must raise RuntimeError — above MAX_BATCH_ACTION_ITEMS=20."""
        extractor = _make_extractor_returning()
        svc = _make_svc(privacy_enabled=False, extractor=extractor)
        with self.assertRaises(RuntimeError) as ctx:
            svc.handle_batch_extract_action_items({"ids": [f"id{i}" for i in range(21)]})
        self.assertIn("20", str(ctx.exception))

    def test_100_ids_raises_runtime_error(self) -> None:
        """Passing 100 ids must raise RuntimeError."""
        extractor = _make_extractor_returning()
        svc = _make_svc(privacy_enabled=False, extractor=extractor)
        with self.assertRaises(RuntimeError):
            svc.handle_batch_extract_action_items({"ids": [f"id{i}" for i in range(100)]})

    def test_exactly_20_ids_succeeds(self) -> None:
        """Passing exactly 20 ids must not raise (at the limit, not over)."""
        extractor = _make_extractor_returning()
        # Build 20 items in the store
        items = [_make_fake_item(f"item{i}") for i in range(20)]
        svc = _make_svc(privacy_enabled=False, extractor=extractor, items=items)
        ids = [f"item{i}" for i in range(20)]
        result = svc.handle_batch_extract_action_items({"ids": ids})
        self.assertEqual(result.get("count"), 20)

    def test_zero_ids_succeeds(self) -> None:
        """Passing empty ids list must return empty results."""
        extractor = _make_extractor_returning()
        svc = _make_svc(privacy_enabled=False, extractor=extractor)
        result = svc.handle_batch_extract_action_items({"ids": []})
        self.assertEqual(result.get("results"), [])
        self.assertEqual(result.get("count"), 0)

    def test_max_constant_is_20(self) -> None:
        """MAX_BATCH_ACTION_ITEMS class constant must equal 20."""
        self.assertEqual(SearchAndAnalysisService.MAX_BATCH_ACTION_ITEMS, 20)

    def test_non_list_ids_raises_runtime_error(self) -> None:
        """Non-list ids must still raise RuntimeError (existing guard)."""
        extractor = _make_extractor_returning()
        svc = _make_svc(privacy_enabled=False, extractor=extractor)
        with self.assertRaises(RuntimeError):
            svc.handle_batch_extract_action_items({"ids": "not-a-list"})


class BatchExtractActionItemsPrivacyGuardTestCase(unittest.TestCase):
    """handle_batch_extract_action_items must return empty dict when privacy_mode_enabled."""

    def test_privacy_on_returns_empty_results(self) -> None:
        """results must be [] when privacy_mode_enabled=True."""
        extractor = _make_extractor_returning()
        svc = _make_svc(privacy_enabled=True, extractor=extractor)
        result = svc.handle_batch_extract_action_items({"ids": ["i1", "i2"]})
        self.assertEqual(result.get("results"), [])
        self.assertEqual(result.get("count"), 0)

    def test_privacy_on_returns_reason_flag(self) -> None:
        """Response includes reason=privacy_mode_active when privacy on."""
        extractor = _make_extractor_returning()
        svc = _make_svc(privacy_enabled=True, extractor=extractor)
        result = svc.handle_batch_extract_action_items({"ids": ["i1"]})
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("privacy_mode_active"))

    def test_privacy_on_extractor_not_called(self) -> None:
        """LLM extractor must NOT be called when privacy_mode_enabled=True."""
        extractor = _make_extractor_returning()
        svc = _make_svc(privacy_enabled=True, extractor=extractor)
        svc.handle_batch_extract_action_items({"ids": ["i1", "i2"]})
        extractor.extract.assert_not_called()

    def test_privacy_on_with_21_ids_returns_empty_not_error(self) -> None:
        """Even 21 ids with privacy=True must return empty (privacy check before size check)."""
        extractor = _make_extractor_returning()
        svc = _make_svc(privacy_enabled=True, extractor=extractor)
        # Privacy guard fires before the size check — must not raise
        result = svc.handle_batch_extract_action_items({"ids": [f"id{i}" for i in range(21)]})
        self.assertEqual(result.get("results"), [])
        self.assertEqual(result.get("count"), 0)


# ---------------------------------------------------------------------------
# Throttle classification: batch_extract_action_items must be in HEAVY_METHODS
# ---------------------------------------------------------------------------


class BatchExtractActionItemsThrottleClassificationTestCase(unittest.TestCase):
    """batch_extract_action_items must be classified as HEAVY to rate-limit serial LLM calls."""

    def test_batch_extract_in_heavy_methods(self) -> None:
        """batch_extract_action_items must be present in HEAVY_METHODS set."""
        self.assertIn(
            "batch_extract_action_items",
            HEAVY_METHODS,
            "batch_extract_action_items must be in HEAVY_METHODS "
            "(serial LLM calls = DoS risk without rate limit)",
        )

    def test_extract_action_items_in_heavy_methods(self) -> None:
        """extract_action_items (single) must remain in HEAVY_METHODS."""
        self.assertIn(
            "extract_action_items",
            HEAVY_METHODS,
            "extract_action_items must stay in HEAVY_METHODS",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
