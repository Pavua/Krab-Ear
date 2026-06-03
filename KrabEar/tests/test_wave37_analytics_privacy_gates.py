"""wave-37 regression tests: privacy gates for get_recording_stats + compare_periods.

B1 (MED) — BackendService._handle_get_recording_stats:
  privacy_mode_enabled=True → returns ok=False, reason=privacy_mode_active
  without touching history store.

B2 (MED) — AnalyticsService.handle_compare_periods:
  privacy_mode_enabled=True → returns ok=False, reason=privacy_mode_active
  without calling period_comparison.compare_periods.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from backend.analytics_service import AnalyticsService
    from backend.state_store import StateStore
    from backend.service import BackendService
    _SKIP = False
except ImportError:
    _SKIP = True


# ---------------------------------------------------------------------------
# Helpers for AnalyticsService unit tests (B2)
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


def _make_analytics_svc(*, privacy_on: bool) -> AnalyticsService:
    return AnalyticsService(
        analytics_dashboard=MagicMock(),
        sentiment_trends=MagicMock(),
        activity_calendar=MagicMock(),
        keyword_cloud_gen=MagicMock(),
        timeline_view=MagicMock(),
        store=_FakeStore(),
        settings_get=lambda k, d: privacy_on if k == "privacy_mode_enabled" else d,
    )


# ---------------------------------------------------------------------------
# B1 — BackendService._handle_get_recording_stats privacy gate
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP, "BackendService or StateStore not available")
class GetRecordingStatsPrivacyGateTestCase(unittest.TestCase):
    """B1: _handle_get_recording_stats must gate on privacy_mode_enabled."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(store=self.store)

    def _request(self, method: str, params: dict | None = None) -> dict:
        resp = self.service.handle_request({"id": "t", "method": method, "params": params or {}})
        return resp

    # ------------------------------------------------------------------
    # B1-a: privacy ON → ok=False, reason=privacy_mode_active
    # ------------------------------------------------------------------
    def test_returns_privacy_blocked_when_privacy_on(self) -> None:
        """privacy_mode_enabled=True → ok=False, reason=privacy_mode_active."""
        # Seed one item so a non-privacy call would return real stats.
        self.store.add_history_item(
            text="секретная запись",
            paste_status="ok",
            source_lang="ru",
            audio_duration_sec=30.0,
        )
        self.store.save_settings({"privacy_mode_enabled": True})

        resp = self._request("get_recording_stats")
        result = resp.get("result", resp)

        self.assertFalse(
            result.get("ok", True),
            "ok must be False in privacy mode",
        )
        self.assertEqual(
            result.get("reason"), "privacy_mode_active",
            "reason must be privacy_mode_active",
        )
        self.assertEqual(result.get("total_count"), 0,
                         "total_count must be 0 in privacy mode")
        self.assertEqual(result.get("total_duration_sec"), 0.0,
                         "total_duration_sec must be 0.0 in privacy mode")
        self.assertEqual(result.get("lang_distribution"), [],
                         "lang_distribution must be [] in privacy mode")

    # ------------------------------------------------------------------
    # B1-b: privacy OFF → normal stats returned
    # ------------------------------------------------------------------
    def test_returns_real_stats_when_privacy_off(self) -> None:
        """privacy_mode_enabled=False → normal aggregated stats returned."""
        self.store.add_history_item(
            text="тестовая запись",
            paste_status="ok",
            source_lang="ru",
            audio_duration_sec=15.0,
        )
        self.store.save_settings({"privacy_mode_enabled": False})

        resp = self._request("get_recording_stats")
        result = resp.get("result", resp)

        self.assertNotEqual(
            result.get("reason"), "privacy_mode_active",
            "reason must not be privacy_mode_active when privacy is off",
        )
        self.assertEqual(result.get("total_count"), 1)
        self.assertEqual(result.get("total_duration_sec"), 15.0)

    # ------------------------------------------------------------------
    # B1-c: privacy ON → store._load_active_items_with_lock NOT called
    # ------------------------------------------------------------------
    def test_store_not_accessed_in_privacy_mode(self) -> None:
        """Store must not be read when privacy mode is enabled."""
        self.store.save_settings({"privacy_mode_enabled": True})

        with patch.object(
            self.store,
            "_load_active_items_with_lock",
            wraps=self.store._load_active_items_with_lock,
        ) as mock_load:
            self._request("get_recording_stats")
            mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# B2 — AnalyticsService.handle_compare_periods privacy gate
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP, "AnalyticsService not available")
class ComparePeriodsPrivacyGateTestCase(unittest.TestCase):
    """B2: handle_compare_periods must gate on privacy_mode_enabled."""

    # ------------------------------------------------------------------
    # B2-a: privacy ON → ok=False, reason=privacy_mode_active
    # ------------------------------------------------------------------
    def test_returns_privacy_blocked_when_privacy_on(self) -> None:
        """privacy_mode_enabled=True → ok=False, reason=privacy_mode_active."""
        svc = _make_analytics_svc(privacy_on=True)

        result = svc.handle_compare_periods({
            "period1_start": "2024-01-01",
            "period1_end": "2024-01-31",
            "period2_start": "2024-02-01",
            "period2_end": "2024-02-29",
        })

        self.assertFalse(
            result.get("ok", True),
            "ok must be False in privacy mode",
        )
        self.assertEqual(
            result.get("reason"), "privacy_mode_active",
            "reason must be privacy_mode_active",
        )

    # ------------------------------------------------------------------
    # B2-b: privacy ON → period1/period2 schema parity (zeroed)
    # ------------------------------------------------------------------
    def test_period_schema_parity_in_privacy_mode(self) -> None:
        """Response schema matches normal output (zeroed fields) in privacy mode."""
        svc = _make_analytics_svc(privacy_on=True)

        result = svc.handle_compare_periods({
            "period1_start": "2024-01-01",
            "period1_end": "2024-01-31",
            "period2_start": "2024-02-01",
            "period2_end": "2024-02-29",
        })

        self.assertIn("period1", result)
        self.assertIn("period2", result)
        for period_key in ("period1", "period2"):
            p = result[period_key]
            self.assertEqual(p["recordings"], 0)
            self.assertEqual(p["duration_sec"], 0.0)
            self.assertEqual(p["words"], 0)
            self.assertEqual(p["avg_confidence"], 0.0)
            self.assertIsInstance(p["languages"], dict)

        self.assertEqual(result.get("recordings_change_pct"), 0.0)
        self.assertEqual(result.get("duration_change_pct"), 0.0)
        self.assertEqual(result.get("confidence_change"), 0.0)
        self.assertEqual(result.get("new_languages"), [])

    # ------------------------------------------------------------------
    # B2-c: privacy ON → compare_periods fn NOT called
    # ------------------------------------------------------------------
    def test_period_comparison_fn_not_called_in_privacy_mode(self) -> None:
        """backend.period_comparison.compare_periods must not be called in privacy mode."""
        svc = _make_analytics_svc(privacy_on=True)

        with patch("backend.period_comparison.compare_periods") as mock_fn:
            svc.handle_compare_periods({
                "period1_start": "2024-01-01",
                "period1_end": "2024-01-31",
                "period2_start": "2024-02-01",
                "period2_end": "2024-02-29",
            })
            mock_fn.assert_not_called()

    # ------------------------------------------------------------------
    # B2-d: privacy OFF → compare_periods fn IS called (normal path)
    # ------------------------------------------------------------------
    def test_compare_periods_fn_called_when_privacy_off(self) -> None:
        """compare_periods fn must be called when privacy mode is off."""
        svc = _make_analytics_svc(privacy_on=False)

        with patch("backend.analytics_service.AnalyticsService.handle_compare_periods",
                   wraps=svc.handle_compare_periods):
            with patch("backend.period_comparison.compare_periods") as mock_fn:
                # Set up a valid return value to avoid errors.
                mock_report = MagicMock()
                mock_report.period1.recordings = 5
                mock_report.period1.duration_sec = 100.0
                mock_report.period1.words = 50
                mock_report.period1.avg_confidence = 0.9
                mock_report.period1.languages = {}
                mock_report.period2.recordings = 3
                mock_report.period2.duration_sec = 60.0
                mock_report.period2.words = 30
                mock_report.period2.avg_confidence = 0.85
                mock_report.period2.languages = {}
                mock_report.recordings_change_pct = -40.0
                mock_report.duration_change_pct = -40.0
                mock_report.confidence_change = -0.05
                mock_report.new_languages = []
                mock_report.summary = "test"
                mock_fn.return_value = mock_report

                svc.handle_compare_periods({
                    "period1_start": "2024-01-01",
                    "period1_end": "2024-01-31",
                    "period2_start": "2024-02-01",
                    "period2_end": "2024-02-29",
                })

                mock_fn.assert_called_once()

    # ------------------------------------------------------------------
    # B2-e: privacy ON + missing params → still gates (no ValueError)
    # ------------------------------------------------------------------
    def test_privacy_gate_before_param_validation(self) -> None:
        """Privacy gate fires before parameter validation — no ValueError raised."""
        svc = _make_analytics_svc(privacy_on=True)

        # Missing required params would normally raise ValueError, but
        # privacy gate should fire first.
        result = svc.handle_compare_periods({})
        self.assertEqual(result.get("reason"), "privacy_mode_active")


if __name__ == "__main__":
    unittest.main()
