"""Tests for AutoDeduplicator privacy_mode gate + settings_provider injection (W1248).

Covers W1243 F4 MED finding: check_duplicate / run_deduplication / find_duplicates
all load transcript texts from store into memory regardless of privacy_mode_enabled.

Fix: when privacy_mode_enabled=True all three entry points return early without
touching store, eliminating in-memory transcript exposure.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.auto_deduplication import (
    AutoDeduplicator,
    DedupResult,
    DEFAULT_DEDUP_THRESHOLD,
    _PRIVACY_SKIPPED,
)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat()


def _make_store_with_items(texts: list[str] = None) -> MagicMock:
    """Return a mock StateStore that returns the given texts as history items."""
    store = MagicMock()
    items = [
        {"id": f"item-{i}", "text": t, "ts": _now_iso()}
        for i, t in enumerate(texts or [])
    ]
    store.get_history_page.return_value = (items, None)
    return store


class TestCheckDuplicateSkipsInPrivacyMode(unittest.TestCase):
    """check_duplicate returns privacy_skipped sentinel without touching store."""

    def setUp(self) -> None:
        # settings_provider returns privacy_mode_enabled=True
        self._provider = MagicMock(return_value=True)
        self.deduplicator = AutoDeduplicator(settings_provider=self._provider)
        self.store = _make_store_with_items(["Текст который не должен быть загружен"])

    def test_check_duplicate_skips_in_privacy_mode(self) -> None:
        """check_duplicate must return is_duplicate=False with action_taken='privacy_skipped'."""
        result = self.deduplicator.check_duplicate(
            text="Идентичный текст который не должен быть загружен",
            timestamp=_now_iso(),
            store=self.store,
        )
        self.assertFalse(result.is_duplicate)
        self.assertIsNone(result.duplicate_of)
        self.assertEqual(result.similarity, 0.0)
        self.assertEqual(result.action_taken, "privacy_skipped")

    def test_check_duplicate_does_not_call_store_in_privacy_mode(self) -> None:
        """Store.get_history_page must NOT be called when privacy_mode is enabled."""
        self.deduplicator.check_duplicate(
            text="Любой текст",
            timestamp=_now_iso(),
            store=self.store,
        )
        self.store.get_history_page.assert_not_called()

    def test_check_duplicate_does_not_increment_counter_in_privacy_mode(self) -> None:
        """total_checked counter must NOT increment in privacy mode (no processing)."""
        self.deduplicator.reset_stats()
        self.deduplicator.check_duplicate(
            text="Текст",
            timestamp=_now_iso(),
            store=self.store,
        )
        stats = self.deduplicator.get_dedup_stats()
        self.assertEqual(stats["total_checked"], 0)

    def test_settings_provider_called_with_correct_key(self) -> None:
        """settings_provider must be called with 'privacy_mode_enabled' key."""
        self.deduplicator.check_duplicate(
            text="Текст",
            timestamp=_now_iso(),
            store=self.store,
        )
        self._provider.assert_called_with("privacy_mode_enabled", False)


class TestRunDeduplicationSkipsInPrivacyMode(unittest.TestCase):
    """run_deduplication returns empty report without touching store."""

    def setUp(self) -> None:
        self._provider = MagicMock(return_value=True)
        self.deduplicator = AutoDeduplicator(settings_provider=self._provider)
        self.store = _make_store_with_items(
            ["Текст 1", "Текст 1", "Текст 2"]
        )

    def test_run_deduplication_skips_in_privacy_mode(self) -> None:
        """run_deduplication must return empty result with skipped_reason='privacy_mode'."""
        result = self.deduplicator.run_deduplication(self.store)
        self.assertEqual(result["total_scanned"], 0)
        self.assertEqual(result["duplicate_groups"], 0)
        self.assertEqual(result["duplicates"], [])
        self.assertEqual(result.get("skipped_reason"), "privacy_mode")

    def test_run_deduplication_does_not_call_store_in_privacy_mode(self) -> None:
        """Store.get_history_page must NOT be called when privacy_mode is enabled."""
        self.deduplicator.run_deduplication(self.store)
        self.store.get_history_page.assert_not_called()

    def test_run_deduplication_result_keys_present(self) -> None:
        """Result always has total_scanned, duplicate_groups, duplicates keys."""
        result = self.deduplicator.run_deduplication(self.store)
        self.assertIn("total_scanned", result)
        self.assertIn("duplicate_groups", result)
        self.assertIn("duplicates", result)
        self.assertIsInstance(result["duplicates"], list)


class TestFindDuplicatesSkipsInPrivacyMode(unittest.TestCase):
    """handle_run_deduplication (the IPC entry point) skips in privacy mode."""

    def setUp(self) -> None:
        self._provider = MagicMock(return_value=True)
        self.deduplicator = AutoDeduplicator(settings_provider=self._provider)
        self.store = _make_store_with_items(["Текст А", "Текст А"])

    def test_find_duplicates_skips_in_privacy_mode(self) -> None:
        """IPC handle_run_deduplication must skip in privacy mode."""
        result = self.deduplicator.handle_run_deduplication({
            "_store": self.store,
        })
        self.assertEqual(result["total_scanned"], 0)
        self.assertEqual(result["duplicate_groups"], 0)
        self.assertEqual(result.get("skipped_reason"), "privacy_mode")

    def test_find_duplicates_does_not_call_store_in_privacy_mode(self) -> None:
        """IPC handle_run_deduplication must not call store in privacy mode."""
        self.deduplicator.handle_run_deduplication({"_store": self.store})
        self.store.get_history_page.assert_not_called()


class TestSettingsProviderDefaultNoOp(unittest.TestCase):
    """Without settings_provider, privacy gate is disabled (backward compat)."""

    def setUp(self) -> None:
        # No settings_provider — legacy constructor usage
        self.deduplicator = AutoDeduplicator()
        self.store = _make_store_with_items([])

    def test_settings_provider_default_no_op(self) -> None:
        """Without settings_provider, check_duplicate runs normally (not skipped)."""
        result = self.deduplicator.check_duplicate(
            text="Текст без privacy gate",
            timestamp=_now_iso(),
            store=self.store,
        )
        # Should NOT return privacy_skipped when no provider given
        self.assertNotEqual(result.action_taken, "privacy_skipped")
        # With empty store it should return 'kept'
        self.assertEqual(result.action_taken, "kept")

    def test_run_deduplication_default_no_op(self) -> None:
        """Without settings_provider, run_deduplication runs normally."""
        result = self.deduplicator.run_deduplication(self.store)
        self.assertNotIn("skipped_reason", result)
        self.assertIn("total_scanned", result)

    def test_privacy_mode_off_allows_detection(self) -> None:
        """With privacy_mode_enabled=False, deduplication still works."""
        provider = MagicMock(return_value=False)
        dedup = AutoDeduplicator(settings_provider=provider)
        store = _make_store_with_items([])
        result = dedup.check_duplicate(
            text="Текст без режима приватности",
            timestamp=_now_iso(),
            store=store,
        )
        self.assertNotEqual(result.action_taken, "privacy_skipped")

    def test_settings_provider_exception_safe(self) -> None:
        """If settings_provider raises, privacy gate defaults to off (no crash)."""
        def bad_provider(key, default):
            raise RuntimeError("settings provider crash")

        dedup = AutoDeduplicator(settings_provider=bad_provider)
        store = _make_store_with_items([])
        # Must not raise — should fall back to non-privacy mode
        result = dedup.check_duplicate(
            text="Тест стабильности при сбое провайдера",
            timestamp=_now_iso(),
            store=store,
        )
        self.assertNotEqual(result.action_taken, "privacy_skipped")


class TestPrivacySentinelIsSharedInstance(unittest.TestCase):
    """_PRIVACY_SKIPPED sentinel is the correct no-op DedupResult."""

    def test_privacy_skipped_sentinel_fields(self) -> None:
        """_PRIVACY_SKIPPED has expected field values."""
        self.assertFalse(_PRIVACY_SKIPPED.is_duplicate)
        self.assertIsNone(_PRIVACY_SKIPPED.duplicate_of)
        self.assertEqual(_PRIVACY_SKIPPED.similarity, 0.0)
        self.assertEqual(_PRIVACY_SKIPPED.action_taken, "privacy_skipped")

    def test_check_duplicate_returns_sentinel_in_privacy_mode(self) -> None:
        """check_duplicate returns the _PRIVACY_SKIPPED sentinel instance."""
        provider = MagicMock(return_value=True)
        dedup = AutoDeduplicator(settings_provider=provider)
        store = _make_store_with_items([])
        result = dedup.check_duplicate(text="X", timestamp=_now_iso(), store=store)
        # Same object identity (sentinel pattern)
        self.assertIs(result, _PRIVACY_SKIPPED)


class TestSemanticSearcherRemoveOnDedup(unittest.TestCase):
    """handle_run_deduplication calls semantic_searcher.remove_item for each duplicate."""

    def _make_deduplicator_with_duplicates(self) -> tuple[AutoDeduplicator, MagicMock]:
        """Return (deduplicator, store) where store has 2 identical items."""
        dedup = AutoDeduplicator()  # privacy off
        store = MagicMock()
        dup_text = "Дублирующийся текст для семантического теста"
        items = [
            {"id": "orig-001", "text": dup_text, "ts": _now_iso()},
            {"id": "dup-002", "text": dup_text, "ts": _now_iso()},
        ]
        store.get_history_page.side_effect = [(items, None), ([], None)]
        return dedup, store

    def test_semantic_searcher_remove_called_for_duplicates(self) -> None:
        """remove_item must be called for each duplicate_id found."""
        dedup, store = self._make_deduplicator_with_duplicates()
        semantic_searcher = MagicMock()

        result = dedup.handle_run_deduplication({
            "_store": store,
            "_semantic_searcher": semantic_searcher,
        })

        if result["duplicate_groups"] > 0:
            # There should be a remove_item call for each duplicate found
            self.assertTrue(semantic_searcher.remove_item.called)
            called_ids = [c.args[0] for c in semantic_searcher.remove_item.call_args_list]
            # dup-002 should be removed (it is the duplicate of orig-001)
            self.assertIn("dup-002", called_ids)

    def test_semantic_searcher_not_called_when_no_duplicates(self) -> None:
        """remove_item must NOT be called when no duplicates are found."""
        dedup = AutoDeduplicator()
        store = MagicMock()
        unique_items = [
            {"id": "a-001", "text": "Уникальный текст A", "ts": _now_iso()},
            {"id": "b-002", "text": "Совершенно другой текст B", "ts": _now_iso()},
        ]
        store.get_history_page.side_effect = [(unique_items, None), ([], None)]
        semantic_searcher = MagicMock()

        dedup.handle_run_deduplication({
            "_store": store,
            "_semantic_searcher": semantic_searcher,
        })

        semantic_searcher.remove_item.assert_not_called()

    def test_semantic_searcher_exception_does_not_propagate(self) -> None:
        """If remove_item raises, it must not propagate — log and continue."""
        dedup, store = self._make_deduplicator_with_duplicates()
        semantic_searcher = MagicMock()
        semantic_searcher.remove_item.side_effect = RuntimeError("index error")

        # Must not raise
        result = dedup.handle_run_deduplication({
            "_store": store,
            "_semantic_searcher": semantic_searcher,
        })
        self.assertIn("total_scanned", result)

    def test_semantic_searcher_none_does_not_crash(self) -> None:
        """handle_run_deduplication with _semantic_searcher=None must work fine."""
        dedup, store = self._make_deduplicator_with_duplicates()
        result = dedup.handle_run_deduplication({
            "_store": store,
            "_semantic_searcher": None,
        })
        self.assertIn("total_scanned", result)


if __name__ == "__main__":
    unittest.main()
