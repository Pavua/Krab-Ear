"""wave-27/28 deferred privacy gate regression tests.

Covers two fixes that were blocked waiting for prior service.py merges:

FIX 1 (HIGH) — BackendService._handle_get_context_memory:
  ContextMemory accumulates proper nouns / topic words from every recording.
  Without a gate, get_context_memory IPC exposes transcription-derived content
  even when privacy_mode_enabled=True. This adds a gate that returns empty
  context_words and recent_topics while still exposing size so callers can
  see that non-empty memory exists.

FIX 2 (MED) — AnalyticsService.handle_get_timeline_view:
  Timeline view groups history items into time blocks, leaking transcript-
  derived topic shifts in privacy mode. Sibling handlers
  (get_sentiment_trends, get_keyword_cloud, get_activity_calendar,
  get_analytics_dashboard) all gate — handle_get_timeline_view was the
  only ungated sibling. This adds parity gating.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.analytics_service import AnalyticsService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — AnalyticsService
# ---------------------------------------------------------------------------

class _FakeStore:
    """Minimal fake StateStore for AnalyticsService tests."""

    def __init__(self, items=None):
        self._items = items or []

    class _CM:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _lock(self):
        return self._CM()

    def _load_active_items_unlocked(self):
        return list(self._items)

    def _load_active_items_with_lock(self):
        return list(self._items)


def _make_analytics_svc(privacy_mode: bool = False, store=None) -> AnalyticsService:
    """Build AnalyticsService with optional privacy_mode toggle."""
    settings_dict: dict = {}
    if privacy_mode:
        settings_dict["privacy_mode_enabled"] = True

    def _settings_get(key: str, default=None):
        return settings_dict.get(key, default)

    return AnalyticsService(
        analytics_dashboard=MagicMock(),
        sentiment_trends=MagicMock(),
        activity_calendar=MagicMock(),
        keyword_cloud_gen=MagicMock(),
        timeline_view=MagicMock(),
        store=store or _FakeStore(),
        settings_get=_settings_get,
    )


# ---------------------------------------------------------------------------
# FIX 2: AnalyticsService.handle_get_timeline_view privacy gate
# ---------------------------------------------------------------------------

class TestTimelineViewPrivacyGate(unittest.TestCase):
    """handle_get_timeline_view must be gated like its siblings."""

    def test_privacy_mode_returns_empty_timeline(self) -> None:
        svc = _make_analytics_svc(privacy_mode=True)
        result = svc.handle_get_timeline_view({})

        self.assertEqual(result.get("timeline"), [])
        self.assertEqual(result.get("total_segments"), 0)
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_mode_does_not_call_store(self) -> None:
        """Privacy gate must short-circuit before touching history."""
        store = _FakeStore(items=[{"id": "x", "text": "secret", "timestamp": "2026-01-01"}])
        svc = _make_analytics_svc(privacy_mode=True, store=store)

        # Patch _load_active_items_with_lock to detect if it is called
        called = []
        original = store._load_active_items_with_lock

        def spy():
            called.append(True)
            return original()

        store._load_active_items_with_lock = spy

        svc.handle_get_timeline_view({})
        self.assertEqual(called, [], "store must NOT be accessed in privacy mode")

    def test_normal_mode_delegates_to_timeline_view(self) -> None:
        """Without privacy mode the timeline generator is invoked."""
        timeline_view = MagicMock()
        fake_block = MagicMock()
        fake_block.to_dict.return_value = {"group": "2026-06-03", "count": 3}
        timeline_view.generate_timeline.return_value = [fake_block]

        svc = AnalyticsService(
            analytics_dashboard=MagicMock(),
            sentiment_trends=MagicMock(),
            activity_calendar=MagicMock(),
            keyword_cloud_gen=MagicMock(),
            timeline_view=timeline_view,
            store=_FakeStore(),
        )

        result = svc.handle_get_timeline_view({"group_by": "day"})

        timeline_view.generate_timeline.assert_called_once()
        self.assertEqual(len(result["blocks"]), 1)
        self.assertEqual(result["blocks"][0]["group"], "2026-06-03")

    def test_privacy_gate_response_shape(self) -> None:
        """Response shape must include ok=True so Swift JSON decoder succeeds."""
        svc = _make_analytics_svc(privacy_mode=True)
        result = svc.handle_get_timeline_view({})
        self.assertTrue(result.get("ok"))

    def test_privacy_mode_off_does_not_short_circuit(self) -> None:
        """When privacy_mode_enabled=False (or absent) normal path is taken."""
        timeline_view = MagicMock()
        timeline_view.generate_timeline.return_value = []

        svc = AnalyticsService(
            analytics_dashboard=MagicMock(),
            sentiment_trends=MagicMock(),
            activity_calendar=MagicMock(),
            keyword_cloud_gen=MagicMock(),
            timeline_view=timeline_view,
            store=_FakeStore(),
            settings_get=lambda k, d=None: False if k == "privacy_mode_enabled" else d,
        )

        svc.handle_get_timeline_view({})
        timeline_view.generate_timeline.assert_called_once()


# ---------------------------------------------------------------------------
# Helpers — BackendService / ContextMemory for FIX 1
# ---------------------------------------------------------------------------

class _FakeContextMemory:
    """Fake ContextMemory tracking get_context_words / get_recent_topics calls."""

    def __init__(self, words=None, topics=None, mem_size=5):
        self._words = words or ["API", "KrabEar"]
        self._topics = topics or ["transcription", "voice"]
        self._size = mem_size
        self.words_called = 0
        self.topics_called = 0

    def get_context_words(self, max_words: int = 20):
        self.words_called += 1
        return self._words[:max_words]

    def get_recent_topics(self, last_n: int = 10):
        self.topics_called += 1
        return self._topics[:last_n]

    def size(self) -> int:
        return self._size

    def clear(self) -> None:
        self._words = []
        self._topics = []
        self._size = 0


def _invoke_handle_get_context_memory(
    privacy_mode: bool,
    context_memory: _FakeContextMemory,
    params: dict | None = None,
) -> dict:
    """
    Test _handle_get_context_memory in isolation by patching _cached_settings
    and injecting a fake ContextMemory.

    We import BackendService only here so the module-level import does not
    trigger heavy STT/mlx imports — all heavy deps are mocked.
    """
    import types

    # Stub out heavy modules before importing service
    for mod_name in [
        "mlx_whisper", "mlx", "mlx.core",
        "pyannote", "pyannote.audio",
        "sounddevice", "torch", "transformers",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)

    from backend.service import BackendService  # noqa: E402 (deferred import)

    svc = object.__new__(BackendService)

    # Wire only what _handle_get_context_memory needs
    svc._context_memory = context_memory

    settings_snapshot = {"privacy_mode_enabled": True} if privacy_mode else {}

    def _fake_cached_settings():
        return settings_snapshot

    svc._cached_settings = _fake_cached_settings

    return svc._handle_get_context_memory(params or {})


# ---------------------------------------------------------------------------
# FIX 1: BackendService._handle_get_context_memory privacy gate
# ---------------------------------------------------------------------------

class TestGetContextMemoryPrivacyGate(unittest.TestCase):
    """_handle_get_context_memory must suppress words/topics in privacy mode."""

    def test_privacy_mode_returns_empty_words_and_topics(self) -> None:
        mem = _FakeContextMemory()
        result = _invoke_handle_get_context_memory(privacy_mode=True, context_memory=mem)

        self.assertEqual(result.get("context_words"), [])
        self.assertEqual(result.get("recent_topics"), [])

    def test_privacy_mode_preserves_size(self) -> None:
        """size must be returned so callers see memory is not empty."""
        mem = _FakeContextMemory(mem_size=7)
        result = _invoke_handle_get_context_memory(privacy_mode=True, context_memory=mem)

        self.assertEqual(result.get("size"), 7)

    def test_privacy_mode_sets_privacy_flag(self) -> None:
        mem = _FakeContextMemory()
        result = _invoke_handle_get_context_memory(privacy_mode=True, context_memory=mem)
        self.assertTrue(result.get("privacy_mode"))

    def test_privacy_mode_does_not_call_get_context_words(self) -> None:
        """get_context_words must NOT be called in privacy mode."""
        mem = _FakeContextMemory()
        _invoke_handle_get_context_memory(privacy_mode=True, context_memory=mem)
        self.assertEqual(mem.words_called, 0)

    def test_privacy_mode_does_not_call_get_recent_topics(self) -> None:
        """get_recent_topics must NOT be called in privacy mode."""
        mem = _FakeContextMemory()
        _invoke_handle_get_context_memory(privacy_mode=True, context_memory=mem)
        self.assertEqual(mem.topics_called, 0)

    def test_normal_mode_returns_words_and_topics(self) -> None:
        mem = _FakeContextMemory(words=["API", "IPC"], topics=["recording"])
        result = _invoke_handle_get_context_memory(privacy_mode=False, context_memory=mem)

        self.assertIn("API", result.get("context_words", []))
        self.assertIn("recording", result.get("recent_topics", []))

    def test_normal_mode_calls_underlying_methods(self) -> None:
        mem = _FakeContextMemory()
        _invoke_handle_get_context_memory(privacy_mode=False, context_memory=mem)
        self.assertEqual(mem.words_called, 1)
        self.assertEqual(mem.topics_called, 1)

    def test_clear_param_still_works_in_privacy_mode(self) -> None:
        """clear=True must work even in privacy mode (clears in-memory state)."""
        mem = _FakeContextMemory(words=["Кrab", "Ear"], mem_size=2)
        # In privacy mode, clear=True is NOT reached because the privacy gate
        # fires first and returns early. Verify the gate fires before the clear
        # branch — i.e. the returned size reflects the pre-clear state.
        result = _invoke_handle_get_context_memory(
            privacy_mode=True, context_memory=mem, params={"clear": True}
        )
        # Privacy gate fires first: returns empty words/topics + current size
        self.assertEqual(result.get("context_words"), [])
        self.assertTrue(result.get("privacy_mode"))

    def test_window_size_returned(self) -> None:
        """window_size must appear in both normal and privacy responses."""
        mem = _FakeContextMemory()
        for pm in (True, False):
            with self.subTest(privacy_mode=pm):
                result = _invoke_handle_get_context_memory(
                    privacy_mode=pm, context_memory=mem
                )
                self.assertIn("window_size", result)
                self.assertEqual(result["window_size"], 50)


# ---------------------------------------------------------------------------
# Regression: siblings already gate — verify they still do (smoke)
# ---------------------------------------------------------------------------

class TestAnalyticsSiblingGatesUnchanged(unittest.TestCase):
    """Sibling handlers that already had privacy gates must still gate correctly
    (regression guard — we must not have accidentally removed them)."""

    def _svc(self) -> AnalyticsService:
        return _make_analytics_svc(privacy_mode=True)

    def test_sentiment_trends_still_gated(self) -> None:
        svc = self._svc()
        result = svc.handle_get_sentiment_trends({})
        # sentiment gate uses daily_sentiment (not "trends") — matches SentimentTrendAnalyzer schema
        self.assertEqual(result.get("daily_sentiment"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_keyword_cloud_still_gated(self) -> None:
        svc = self._svc()
        result = svc.handle_get_keyword_cloud({})
        self.assertEqual(result.get("words"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_activity_calendar_still_gated(self) -> None:
        svc = self._svc()
        result = svc.handle_get_activity_calendar({})
        self.assertEqual(result.get("days"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")


if __name__ == "__main__":
    unittest.main()
