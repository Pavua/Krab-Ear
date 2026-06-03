"""wave-27 regression tests.

Covers three fixes:

C1 (HIGH) — period_comparison.compare_periods:
  - NaN/Inf confidence & audio_duration_sec in history items must NOT poison the
    output (result must be JSON-serialisable with allow_nan=False).
  - History is loaded ONCE (single _load_active_items_with_lock call) and split
    in-memory, instead of re-reading the whole NDJSON per pagination page
    (quadratic). get_history_page_filtered must NOT be called on the fast path.
  - Legacy fallback (store without _load_active_items_with_lock) still works.

C2 (MED) — AnalyticsService.handle_get_analytics_dashboard:
  - Honours the privacy_mode_enabled gate (parity with sentiment / keyword /
    activity_calendar siblings); returns an empty dashboard without touching
    the history-backed dashboard generator.
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.analytics_service import AnalyticsService  # noqa: E402
from backend.period_comparison import (  # noqa: E402
    _finite,
    _pct_change,
    _report_to_dict,
    compare_periods,
)


# ---------------------------------------------------------------------------
# Fake stores
# ---------------------------------------------------------------------------

class _SingleLoadStore:
    """Store exposing _load_active_items_with_lock (the fast path).

    Records how many times the bulk loader is called, and fails loudly if the
    legacy paginated path is used (it must not be on this store).
    """

    def __init__(self, items: list[dict]):
        self._items = items
        self.load_calls = 0

    def _load_active_items_with_lock(self):
        self.load_calls += 1
        return list(self._items)

    def get_history_page_filtered(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError(
            "get_history_page_filtered must not be called when "
            "_load_active_items_with_lock is available (quadratic path)"
        )


class _ObjItem:
    """Item that is not a dict but exposes .to_dict() (like HistoryItem)."""

    def __init__(self, d: dict):
        self._d = d

    def to_dict(self) -> dict:
        return dict(self._d)


class _LegacyPaginatedStore:
    """Store WITHOUT _load_active_items_with_lock → legacy paginated fallback."""

    def __init__(self, pages: list[tuple[list[dict], object]]):
        # pages: list of (items, next_cursor) returned in sequence
        self._pages = list(pages)
        self.page_calls = 0

    def get_history_page_filtered(self, *args, **kwargs):
        idx = self.page_calls
        self.page_calls += 1
        if idx < len(self._pages):
            return self._pages[idx]
        return ([], None)


def _item(ts: str, *, dur=None, conf=None, text="hola mundo", lang="ES") -> dict:
    return {
        "ts": ts,
        "audio_duration_sec": dur,
        "confidence": conf,
        "text": text,
        "source_lang": lang,
    }


def _make_analytics(*, privacy_on: bool, dashboard=None) -> AnalyticsService:
    return AnalyticsService(
        analytics_dashboard=dashboard or MagicMock(),
        sentiment_trends=MagicMock(),
        activity_calendar=MagicMock(),
        keyword_cloud_gen=MagicMock(),
        timeline_view=MagicMock(),
        store=MagicMock(),
        settings_get=lambda k, d: privacy_on if k == "privacy_mode_enabled" else d,
    )


# ---------------------------------------------------------------------------
# C1 — _finite helper
# ---------------------------------------------------------------------------

class FiniteHelperTestCase(unittest.TestCase):
    def test_nan_returns_default(self) -> None:
        self.assertEqual(_finite(float("nan")), 0.0)
        self.assertEqual(_finite(float("nan"), default=7.0), 7.0)

    def test_inf_returns_default(self) -> None:
        self.assertEqual(_finite(float("inf")), 0.0)
        self.assertEqual(_finite(float("-inf"), default=-1.0), -1.0)

    def test_finite_passthrough(self) -> None:
        self.assertEqual(_finite(3.5), 3.5)
        self.assertEqual(_finite("2.0"), 2.0)

    def test_non_numeric_returns_default(self) -> None:
        self.assertEqual(_finite("abc", default=4.0), 4.0)
        self.assertEqual(_finite(None), 0.0)


# ---------------------------------------------------------------------------
# C1 — NaN/Inf must not poison compare_periods output
# ---------------------------------------------------------------------------

class NaNInfGuardTestCase(unittest.TestCase):
    def test_nan_inf_confidence_and_duration_do_not_poison(self) -> None:
        items = [
            # period1 (Jan): one clean, one NaN conf, one Inf duration
            _item("2024-01-02T10:00:00", dur=100.0, conf=0.9),
            _item("2024-01-03T10:00:00", dur=50.0, conf=float("nan")),
            _item("2024-01-04T10:00:00", dur=float("inf"), conf=0.8),
            # period2 (Feb): Inf confidence + NaN duration
            _item("2024-02-02T10:00:00", dur=float("nan"), conf=float("inf")),
            _item("2024-02-03T10:00:00", dur=200.0, conf=0.95),
        ]
        store = _SingleLoadStore(items)

        report = compare_periods(
            store, "2024-01-01", "2024-01-31", "2024-02-01", "2024-02-29"
        )

        # All numeric stats finite
        for stats in (report.period1, report.period2):
            self.assertTrue(math.isfinite(stats.duration_sec), stats.duration_sec)
            self.assertTrue(math.isfinite(stats.avg_confidence), stats.avg_confidence)
        self.assertTrue(math.isfinite(report.confidence_change))

        # period1 duration: 100 + 50 + (Inf dropped → 0) = 150
        self.assertAlmostEqual(report.period1.duration_sec, 150.0, places=2)
        # period1 avg_confidence: (0.9 + 0.8)/2 = 0.85 (NaN dropped from count)
        self.assertAlmostEqual(report.period1.avg_confidence, 0.85, places=4)
        # period2 duration: NaN dropped → 200
        self.assertAlmostEqual(report.period2.duration_sec, 200.0, places=2)
        # period2 avg_confidence: Inf dropped → only 0.95
        self.assertAlmostEqual(report.period2.avg_confidence, 0.95, places=4)

    def test_report_dict_is_strict_json_serialisable(self) -> None:
        """allow_nan=False must succeed — proves no NaN/Infinity leaked into JSON."""
        items = [
            _item("2024-01-02T10:00:00", dur=float("inf"), conf=float("nan")),
            _item("2024-02-02T10:00:00", dur=float("-inf"), conf=float("inf")),
        ]
        store = _SingleLoadStore(items)
        report = compare_periods(
            store, "2024-01-01", "2024-01-31", "2024-02-01", "2024-02-29"
        )
        payload = _report_to_dict(report)
        # Raises ValueError if any NaN/Infinity present.
        json.dumps(payload, allow_nan=False)

    def test_pct_change_with_nan_inf_inputs(self) -> None:
        # NaN baseline → finite-ized to 0 → no_baseline
        self.assertEqual(_pct_change(float("nan"), 10.0), "no_baseline")
        # Inf new → finite-ized to 0
        self.assertEqual(_pct_change(10.0, float("inf")), -100.0)


# ---------------------------------------------------------------------------
# C1 — single load (no quadratic re-read) + in-memory split correctness
# ---------------------------------------------------------------------------

class SingleLoadQuadraticTestCase(unittest.TestCase):
    def test_history_loaded_exactly_once(self) -> None:
        items = [_item(f"2024-01-{d:02d}T12:00:00", dur=10.0, conf=0.9) for d in range(1, 20)]
        items += [_item(f"2024-02-{d:02d}T12:00:00", dur=20.0, conf=0.8) for d in range(1, 20)]
        store = _SingleLoadStore(items)

        compare_periods(
            store, "2024-01-01", "2024-01-31", "2024-02-01", "2024-02-29"
        )

        # The whole point of the fix: load once, split in-memory.
        self.assertEqual(store.load_calls, 1)

    def test_in_memory_split_by_period(self) -> None:
        items = [
            _item("2024-01-05T09:00:00", dur=30.0, conf=0.9, lang="RU"),
            _item("2024-01-06T09:00:00", dur=30.0, conf=0.9, lang="RU"),
            # outside both periods — must be excluded
            _item("2024-01-20T09:00:00", dur=999.0, conf=0.1, lang="EN"),
            _item("2024-02-10T09:00:00", dur=60.0, conf=0.8, lang="ES"),
        ]
        store = _SingleLoadStore(items)

        report = compare_periods(
            store, "2024-01-01", "2024-01-10", "2024-02-01", "2024-02-15"
        )

        self.assertEqual(report.period1.recordings, 2)
        self.assertEqual(report.period2.recordings, 1)
        self.assertEqual(report.period1.languages, ["RU"])
        self.assertEqual(report.period2.languages, ["ES"])
        # ES is new in period2
        self.assertEqual(report.new_languages, ["ES"])

    def test_tz_aware_ts_normalised_for_split(self) -> None:
        """tz-aware +00:00 timestamps still land in the right period."""
        items = [
            _item("2024-01-05T09:00:00+00:00", dur=10.0, conf=0.9),
            _item("2024-02-05T09:00:00Z", dur=10.0, conf=0.9),
        ]
        store = _SingleLoadStore(items)
        report = compare_periods(
            store, "2024-01-01", "2024-01-31", "2024-02-01", "2024-02-29"
        )
        self.assertEqual(report.period1.recordings, 1)
        self.assertEqual(report.period2.recordings, 1)

    def test_objects_with_to_dict_supported(self) -> None:
        """_load_active_items_with_lock returning HistoryItem-like objects works."""
        items = [
            _ObjItem(_item("2024-01-05T09:00:00", dur=12.0, conf=0.9)),
            _ObjItem(_item("2024-02-05T09:00:00", dur=24.0, conf=0.8)),
        ]
        store = _SingleLoadStore(items)
        report = compare_periods(
            store, "2024-01-01", "2024-01-31", "2024-02-01", "2024-02-29"
        )
        self.assertEqual(report.period1.recordings, 1)
        self.assertEqual(report.period2.recordings, 1)
        self.assertAlmostEqual(report.period1.duration_sec, 12.0, places=2)


# ---------------------------------------------------------------------------
# C1 — legacy fallback path still works (store without bulk loader)
# ---------------------------------------------------------------------------

class LegacyFallbackTestCase(unittest.TestCase):
    def test_fallback_uses_paginated_path(self) -> None:
        store = _LegacyPaginatedStore(
            pages=[
                ([_item("2024-01-05", dur=10.0, conf=0.9)], None),   # period1
                ([_item("2024-02-05", dur=20.0, conf=0.8)], None),   # period2
            ]
        )
        report = compare_periods(
            store, "2024-01-01", "2024-01-31", "2024-02-01", "2024-02-29"
        )
        self.assertEqual(report.period1.recordings, 1)
        self.assertEqual(report.period2.recordings, 1)
        # One paginated call per period (the original contract).
        self.assertEqual(store.page_calls, 2)

    def test_fallback_nan_inf_guard(self) -> None:
        store = _LegacyPaginatedStore(
            pages=[
                ([_item("2024-01-05", dur=float("inf"), conf=float("nan"))], None),
                ([_item("2024-02-05", dur=10.0, conf=float("inf"))], None),
            ]
        )
        report = compare_periods(
            store, "2024-01-01", "2024-01-31", "2024-02-01", "2024-02-29"
        )
        payload = _report_to_dict(report)
        json.dumps(payload, allow_nan=False)
        self.assertTrue(math.isfinite(report.period1.duration_sec))


# ---------------------------------------------------------------------------
# C2 — analytics dashboard privacy gate
# ---------------------------------------------------------------------------

class DashboardPrivacyGateTestCase(unittest.TestCase):
    def test_privacy_on_returns_empty_payload(self) -> None:
        dashboard = MagicMock()
        svc = _make_analytics(privacy_on=True, dashboard=dashboard)

        result = svc.handle_get_analytics_dashboard({"days": 30})

        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["total_recordings"], 0)
        self.assertEqual(result["total_duration_sec"], 0.0)
        self.assertEqual(result["recent_recordings"], [])
        self.assertEqual(result["sentiment_summary"], {})
        self.assertEqual(result["keyword_summary"], {})
        self.assertEqual(result["quality_summary"], {})

    def test_privacy_on_does_not_touch_dashboard(self) -> None:
        dashboard = MagicMock()
        svc = _make_analytics(privacy_on=True, dashboard=dashboard)

        svc.handle_get_analytics_dashboard({"days": 30})

        dashboard.get_full_dashboard.assert_not_called()

    def test_privacy_off_delegates_to_dashboard(self) -> None:
        dashboard = MagicMock()
        dashboard.get_full_dashboard.return_value = {"overview": {"total": 9}}
        svc = _make_analytics(privacy_on=False, dashboard=dashboard)

        result = svc.handle_get_analytics_dashboard({"days": 30})

        dashboard.get_full_dashboard.assert_called_once()
        self.assertEqual(result["overview"]["total"], 9)
        self.assertNotIn("reason", result)

    def test_privacy_default_off_when_no_settings_get(self) -> None:
        """Without settings_get wired, gate is off (default-safe)."""
        dashboard = MagicMock()
        dashboard.get_full_dashboard.return_value = {"overview": {}}
        svc = AnalyticsService(
            analytics_dashboard=dashboard,
            sentiment_trends=MagicMock(),
            activity_calendar=MagicMock(),
            keyword_cloud_gen=MagicMock(),
            timeline_view=MagicMock(),
            store=MagicMock(),
        )
        result = svc.handle_get_analytics_dashboard({})
        dashboard.get_full_dashboard.assert_called_once()
        self.assertNotIn("reason", result)


if __name__ == "__main__":
    unittest.main()
