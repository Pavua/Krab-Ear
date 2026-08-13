"""Tests for wave-35 HistoryService mass privacy-mode gates.

Covers (all the same pattern: privacy_mode_enabled=True must short-circuit the
handler before any transcript content reaches the IPC response):

  B1 (HIGH) — handle_get_history_page      → empty items, next_cursor None
  B2 (HIGH) — handle_get_history_item      → ok:False
  B3 (HIGH) — handle_search_by_speaker     → empty items, count 0
  B4 (HIGH) — handle_search_by_tag         → empty items, count 0
  B4 (HIGH) — handle_get_favorites         → empty items, count 0
  B4 (HIGH) — handle_filter_by_confidence  → empty items, count 0
  B4 (HIGH) — handle_find_duplicates       → ok:False
  B5 (MED)  — handle_get_clipboard_history → empty items
  B5 (MED)  — handle_repaste_item          → ok:False
  B6 (MED)  — handle_get_annotation        → empty annotation
  B6 (MED)  — handle_search_annotations    → empty results
  B7 (MED)  — handle_export_history_srt    → empty content (inline-content export)
  B8 (LOW)  — handle_auto_summarize_batch  → ok:False

Plus the find_duplicates O(n^2) SequenceMatcher DoS cap (MAX_DEDUP_ITEMS=200):
  find_duplicates with 201 candidates (privacy OFF) → error response.
"""

from __future__ import annotations

import sys
from pathlib import Path

import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _CM:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeStore:
    """Minimal fake StateStore.

    Methods raise loudly if a privacy-gated handler ever reaches them — that
    is the whole point: the gate must return *before* any store access.
    """

    def __init__(self, page_items: list[dict] | None = None, settings: dict | None = None):
        self.data_dir = "."
        self._page_items = page_items if page_items is not None else []
        self._settings = settings if settings is not None else {}

    # privacy helper fallback (when no cached_settings callable is wired)
    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> dict:
        return dict(self._settings)

    def _lock(self):
        return _CM()

    # The following are intentionally tripwires for the privacy-ON tests.
    def _load_active_items_unlocked(self):  # pragma: no cover - tripwire
        raise AssertionError("store accessed despite privacy gate")

    def _load_active_items_with_lock(self):  # pragma: no cover - tripwire
        raise AssertionError("store accessed despite privacy gate")

    def get_annotation(self, item_id):  # pragma: no cover - tripwire
        raise AssertionError("store accessed despite privacy gate")

    def search_annotations(self, query):  # pragma: no cover - tripwire
        raise AssertionError("store accessed despite privacy gate")

    def get_history_item_by_id(self, item_id):  # pragma: no cover - tripwire
        raise AssertionError("store accessed despite privacy gate")

    def get_history_page_filtered(self, *args, **kwargs):
        # Used by find_duplicates (privacy OFF path) — returns configured items.
        return list(self._page_items), None


def _make_service(privacy_on: bool, page_items: list[dict] | None = None) -> HistoryService:
    store = _FakeStore(
        page_items=page_items,
        settings={"privacy_mode_enabled": privacy_on},
    )
    return HistoryService(
        store=store,
        cached_settings=lambda: {"privacy_mode_enabled": privacy_on},
    )


# ---------------------------------------------------------------------------
# Privacy-ON gates
# ---------------------------------------------------------------------------

class TestHistoryPrivacyGates(unittest.TestCase):

    def test_b1_get_history_page_empty(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_get_history_page({"limit": 50})
        self.assertEqual(res["items"], [])
        self.assertIsNone(res["next_cursor"])
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b2_get_history_item_ok_false(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_get_history_item({"id": "abc"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b3_search_by_speaker_empty(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_search_by_speaker({"speaker": "SPEAKER_00"})
        self.assertEqual(res["items"], [])
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b4_search_by_tag_empty(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_search_by_tag({"tag": "work"})
        self.assertEqual(res["items"], [])
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b4_get_favorites_empty(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_get_favorites({})
        self.assertEqual(res["items"], [])
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b4_filter_by_confidence_empty(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_filter_by_confidence({"min_confidence": 0.5})
        self.assertEqual(res["items"], [])
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["avg_confidence"], 0.0)
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b4_find_duplicates_ok_false(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_find_duplicates({})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b5_clipboard_history_empty(self):
        svc = _make_service(privacy_on=True)
        # Seed clipboard so we prove the gate (not emptiness) is what hides it.
        svc._clipboard_history.append({"text": "secret", "history_id": "h1"})
        res = svc.handle_get_clipboard_history({})
        self.assertEqual(res["items"], [])
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b5_repaste_item_ok_false(self):
        svc = _make_service(privacy_on=True)
        svc._clipboard_history.append({"text": "secret", "history_id": "h1"})
        res = svc.handle_repaste_item({"history_id": "h1"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b6_get_annotation_empty(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_get_annotation({"id": "abc"})
        self.assertIsNone(res["note"])
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b6_search_annotations_empty(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_search_annotations({"query": "foo"})
        self.assertEqual(res["results"], [])
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b7_export_history_srt_empty_content(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_export_history_srt({"id": "abc"})
        self.assertEqual(res["content"], "")
        self.assertIsNone(res["path"])
        self.assertEqual(res["reason"], "privacy_mode_active")

    def test_b8_auto_summarize_batch_ok_false(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_auto_summarize_batch({"ids": ["a", "b"]})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "privacy_mode_active")


# ---------------------------------------------------------------------------
# find_duplicates O(n^2) cap (privacy OFF)
# ---------------------------------------------------------------------------

class TestFindDuplicatesCap(unittest.TestCase):

    def _items(self, n: int) -> list[dict]:
        return [{"id": f"id{i}", "text": f"text {i}", "ts": f"2026-01-01T00:00:{i:02d}"}
                for i in range(n)]

    def test_201_items_returns_error(self):
        svc = _make_service(privacy_on=False, page_items=self._items(201))
        res = svc.handle_find_duplicates({"limit": 5000})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "too many items for deduplication")

    def test_200_items_within_cap(self):
        # Exactly at the cap → not rejected by the DoS guard (proceeds to detector).
        svc = _make_service(privacy_on=False, page_items=self._items(200))
        res = svc.handle_find_duplicates({"limit": 5000})
        # Should NOT be the cap rejection; real detection returns groups/total.
        self.assertNotEqual(res.get("reason"), "too many items for deduplication")
        self.assertIn("groups", res)
        self.assertIn("total_duplicates", res)


# ---------------------------------------------------------------------------
# Privacy-OFF default-safe sanity (no reason key when gate is off)
# ---------------------------------------------------------------------------

class TestPrivacyOffPassthrough(unittest.TestCase):

    def test_get_clipboard_history_off_returns_items(self):
        svc = _make_service(privacy_on=False)
        svc._clipboard_history.extend([
            {"text": "a", "history_id": "h1"},
            {"text": "b", "history_id": "h2"},
        ])
        res = svc.handle_get_clipboard_history({"limit": 10})
        self.assertEqual(len(res["items"]), 2)
        self.assertNotIn("reason", res)

    def test_repaste_item_off_returns_text(self):
        svc = _make_service(privacy_on=False)
        svc._clipboard_history.append({"text": "hello", "history_id": "h1"})
        res = svc.handle_repaste_item({"history_id": "h1"})
        self.assertTrue(res["found"])
        self.assertEqual(res["text"], "hello")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Wave-38/39 privacy gates: get_history_statistics + get_history_overview
# ---------------------------------------------------------------------------

class TestHistoryAggregatePrivacyGates(unittest.TestCase):
    """Verify wave-38/39 aggregate gates return zeroed dicts before store access."""

    def test_get_history_statistics_privacy_on(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_get_history_statistics({})
        self.assertEqual(res.get("total_items"), 0)
        self.assertEqual(res.get("total_duration_sec"), 0.0)
        self.assertEqual(res.get("top_speakers"), {})
        self.assertEqual(res.get("daily_counts"), {})
        self.assertIsNone(res.get("date_range"))
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    def test_get_history_statistics_no_store_access(self):
        store_accessed = []
        svc = _make_service(privacy_on=True)
        original = svc.store._load_active_items_unlocked
        svc.store._load_active_items_unlocked = lambda: store_accessed.append(1) or []
        svc.handle_get_history_statistics({})
        self.assertEqual(store_accessed, [], "store must not be accessed in privacy mode")
        svc.store._load_active_items_unlocked = original

    def test_get_history_statistics_privacy_off_passthrough(self):
        # privacy OFF -> gate does not fire -> store IS accessed (tripwire raises AssertionError)
        # That proves the gate is absent, which is the correct behaviour when privacy is off.
        svc = _make_service(privacy_on=False)
        with self.assertRaises(AssertionError):
            svc.handle_get_history_statistics({})

    def test_get_history_overview_privacy_on(self):
        svc = _make_service(privacy_on=True)
        res = svc.handle_get_history_overview({})
        self.assertEqual(res.get("today_count"), 0)
        self.assertEqual(res.get("last_24h_count"), 0)
        self.assertEqual(res.get("source_langs"), [])
        self.assertEqual(res.get("today_text_chars"), 0)
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    def test_get_history_overview_no_store_access(self):
        # handle_get_history_overview calls self.store.get_history_overview()
        # which does not exist on _FakeStore — but the privacy gate fires FIRST.
        # So no AttributeError should be raised.
        svc = _make_service(privacy_on=True)
        res = svc.handle_get_history_overview({})
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    def test_get_history_overview_privacy_off_passthrough(self):
        # privacy OFF -> gate does not fire -> store IS accessed (get_history_overview missing in fake)
        svc = _make_service(privacy_on=False)
        with self.assertRaises((AssertionError, AttributeError, TypeError)):
            svc.handle_get_history_overview({})
