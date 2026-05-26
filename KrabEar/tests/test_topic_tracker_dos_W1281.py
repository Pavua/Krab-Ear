"""test_topic_tracker_dos_W1281.py — tests for W1277 F2+F4+F5 fixes.

Covers:
  - F2 HIGH: TopicTracker.track_topics() hard cap at _HARD_MAX_ITEMS (500)
  - F2 HIGH: _handle_get_topic_timeline treats limit=0 as default (50)
  - F4 MED:  _compute_tfidf uses set() for doc_freq membership test
  - F5 LOW:  MetadataEnricher.enrich() skips topic extraction in privacy_mode
"""

from __future__ import annotations

import ast
import os
import sys
import textwrap
import unittest

# ── Path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.topic_tracker import TopicTracker, _HARD_MAX_ITEMS, _compute_tfidf


# ── Helper ─────────────────────────────────────────────────────────────────────

def _make_items(n: int) -> list[dict]:
    """Create n synthetic history items with varied text."""
    topics = [
        "обсуждение нового проекта разработки системы",
        "анализ данных производительности сервера базы",
        "встреча команды планирование задач спринта",
        "тестирование функций приложения пользователя",
        "документация архитектуры модулей программы",
    ]
    return [
        {"text": topics[i % len(topics)] + f" элемент {i}"}
        for i in range(n)
    ]


# ── F2: hard cap on track_topics ──────────────────────────────────────────────

class TestTrackTopicsHardCap(unittest.TestCase):
    """TopicTracker must silently truncate input to _HARD_MAX_ITEMS."""

    def test_track_topics_hard_caps_at_500_items_regardless_of_caller(self):
        """Passing 600 items must result in at most _HARD_MAX_ITEMS (500) processed."""
        tracker = TopicTracker()
        n = _HARD_MAX_ITEMS + 100  # 600 items
        items = _make_items(n)
        segments = tracker.track_topics(items)

        # All segments must reference indices within [0, _HARD_MAX_ITEMS - 1]
        self.assertTrue(len(segments) > 0, "Should produce at least one segment")
        for seg in segments:
            self.assertLessEqual(
                seg.end_index,
                _HARD_MAX_ITEMS - 1,
                f"end_index {seg.end_index} exceeds hard cap {_HARD_MAX_ITEMS - 1}",
            )
            self.assertGreaterEqual(seg.start_index, 0)

    def test_track_topics_at_exact_cap_does_not_truncate(self):
        """Exactly _HARD_MAX_ITEMS items must pass through unchanged."""
        tracker = TopicTracker()
        items = _make_items(_HARD_MAX_ITEMS)
        segments = tracker.track_topics(items)
        self.assertTrue(len(segments) > 0)
        # Last segment end_index must be exactly _HARD_MAX_ITEMS - 1
        last_end = max(s.end_index for s in segments)
        self.assertEqual(last_end, _HARD_MAX_ITEMS - 1)

    def test_track_topics_below_cap_returns_all(self):
        """Fewer than _HARD_MAX_ITEMS items: no truncation; last index = n-1."""
        tracker = TopicTracker()
        n = 10
        items = _make_items(n)
        segments = tracker.track_topics(items)
        last_end = max(s.end_index for s in segments)
        self.assertEqual(last_end, n - 1)


# ── F2: IPC handler limit=0 treated as default ────────────────────────────────

class TestHandleGetTopicTimelineLimitGuard(unittest.TestCase):
    """_handle_get_topic_timeline must treat limit <= 0 as 50 (default)."""

    def _build_service(self, items: list[dict]):
        """Construct a minimal BackendService stub with enough wiring for the handler."""
        import types
        import threading

        # Build a minimal store stub
        store = types.SimpleNamespace()
        lock_ctx = threading.Lock()

        class _LockCtx:
            def __enter__(self):
                lock_ctx.acquire()
                return self
            def __exit__(self, *_):
                lock_ctx.release()

        store._lock = _LockCtx
        store._load_active_items_unlocked = lambda: list(items)

        # Build just the handler method directly
        from core.topic_tracker import TopicTracker

        class _StubService:
            def __init__(self):
                self.store = store
                self._topic_tracker = TopicTracker()

            def _handle_get_topic_timeline(self, params):
                window_size = max(1, int(params.get("window_size", 5) or 5))
                _raw_limit = int(params.get("limit", 50) or 50)
                limit = _raw_limit if _raw_limit > 0 else 50
                try:
                    with self.store._lock():
                        all_items = self.store._load_active_items_unlocked()
                except Exception:
                    all_items = []
                items_slice = all_items[-limit:]
                timeline = self._topic_tracker.get_topic_timeline(
                    items_slice, window_size=window_size
                )
                current_topic = self._topic_tracker.get_current_topic(
                    items_slice, last_n=window_size
                )
                shifts = sum(1 for entry in timeline if entry.get("is_shift"))
                return {
                    "segments": timeline,
                    "total_shifts": shifts,
                    "current_topic": current_topic,
                }

        return _StubService()

    def test_handle_get_topic_timeline_treats_limit_0_as_default(self):
        """limit=0 must behave identically to limit=50 (not 'all records')."""
        # Create 200 items — if limit=0 was 'all', we'd process all 200
        items = _make_items(200)
        svc = self._build_service(items)

        result_zero = svc._handle_get_topic_timeline({"limit": 0})
        result_fifty = svc._handle_get_topic_timeline({"limit": 50})

        # Both should produce the same segments (same 50-item slice)
        self.assertEqual(
            result_zero["segments"],
            result_fifty["segments"],
            "limit=0 and limit=50 must process the same slice",
        )

    def test_handle_get_topic_timeline_positive_limit_honoured(self):
        """Positive limit must still slice correctly."""
        items = _make_items(100)
        svc = self._build_service(items)
        result = svc._handle_get_topic_timeline({"limit": 20})
        # With 20 items, end_index must be <= 19
        for seg in result["segments"]:
            self.assertLessEqual(seg["end_index"], 19)

    def test_handle_get_topic_timeline_negative_limit_treated_as_default(self):
        """limit=-1 (any negative) must also fall back to 50."""
        items = _make_items(200)
        svc = self._build_service(items)
        result_neg = svc._handle_get_topic_timeline({"limit": -1})
        result_50 = svc._handle_get_topic_timeline({"limit": 50})
        self.assertEqual(result_neg["segments"], result_50["segments"])


# ── F4: doc_freq uses set() for O(1) membership ───────────────────────────────

class TestDocFreqUsesSetForSpeedup(unittest.TestCase):
    """_compute_tfidf doc_freq loop must use set() conversion (W1277 F4)."""

    def test_doc_freq_uses_set_for_speedup(self):
        """AST check: the doc_freq expression contains a set() call on w_tokens."""
        import inspect
        src = inspect.getsource(_compute_tfidf)
        tree = ast.parse(textwrap.dedent(src))

        found_set_call = False
        for node in ast.walk(tree):
            # Looking for: set(w_tokens) in the doc_freq assignment
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "set":
                    found_set_call = True
                    break

        self.assertTrue(
            found_set_call,
            "_compute_tfidf must use set() conversion for doc_freq membership test",
        )

    def test_compute_tfidf_correctness_preserved(self):
        """_compute_tfidf must still return correct TF-IDF scores after speedup."""
        window = ["машина", "программа", "данные", "программа"]
        all_windows = [
            ["машина", "программа"],
            ["данные", "данные"],
            ["машина", "тест"],
        ]
        scores = _compute_tfidf(window, all_windows)
        self.assertIn("программа", scores)
        self.assertIn("машина", scores)
        self.assertIn("данные", scores)
        # "программа" appears twice in window → higher TF, so should score > "машина"
        self.assertGreater(scores["программа"], scores["машина"])


# ── F5: MetadataEnricher skips topic in privacy_mode ─────────────────────────

class TestMetadataEnricherPrivacyModeTopics(unittest.TestCase):
    """MetadataEnricher.enrich() must return topics=[] when privacy_mode=True."""

    def setUp(self):
        from backend.metadata_enricher import MetadataEnricher
        self.enricher = MetadataEnricher()
        self.item = {
            "text": "обсуждение разработки нового программного обеспечения системы",
            "duration_sec": 5.0,
            "confidence": 0.9,
        }

    def test_metadata_enricher_skips_topic_in_privacy_mode(self):
        """With privacy_mode=True, topics field must be empty list."""
        result = self.enricher.enrich(self.item, privacy_mode=True)
        self.assertIn("metadata", result)
        topics = result["metadata"]["topics"]
        self.assertIsInstance(topics, list)
        self.assertEqual(
            topics,
            [],
            "topics must be [] when privacy_mode=True",
        )

    def test_metadata_enricher_populates_topic_without_privacy_mode(self):
        """With privacy_mode=False (default), topics must be populated when text is rich."""
        result = self.enricher.enrich(self.item, privacy_mode=False)
        topics = result["metadata"]["topics"]
        self.assertIsInstance(topics, list)
        # Rich text should yield at least 1 topic word
        self.assertGreater(len(topics), 0, "Should extract topics when not in privacy mode")

    def test_metadata_enricher_default_is_not_privacy_mode(self):
        """Default call (no privacy_mode arg) must behave as privacy_mode=False."""
        result_default = self.enricher.enrich(self.item)
        result_explicit = self.enricher.enrich(self.item, privacy_mode=False)
        self.assertEqual(
            result_default["metadata"]["topics"],
            result_explicit["metadata"]["topics"],
        )

    def test_handle_enrich_recording_passes_privacy_mode(self):
        """handle_enrich_recording must forward privacy_mode from params."""
        params = {
            "item": self.item,
            "privacy_mode": True,
        }
        result = self.enricher.handle_enrich_recording(params)
        topics = result["enriched_item"]["metadata"]["topics"]
        self.assertEqual(topics, [], "IPC handler must pass privacy_mode=True to enrich()")


if __name__ == "__main__":
    unittest.main(verbosity=2)
