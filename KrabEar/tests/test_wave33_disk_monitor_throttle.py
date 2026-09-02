"""Tests for wave-33 MED fixes:
  F1 — get_disk_status in HEAVY_METHODS (ipc_throttle.py) + 30s cache in DiskSpaceMonitor.
  F2 — check_now(force=True) 60s minimum interval between forced checks.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.disk_monitor import DiskSpaceMonitor
from backend.ipc_throttle import (
    HEAVY_METHODS,
    IPCThrottle,
    _classify_method,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(
    enabled: bool = True,
    interval_min: int = 60,
    warning_gb: float = 5.0,
    critical_gb: float = 1.0,
    history_large_mb: int = 500,
) -> MagicMock:
    s = MagicMock()
    s.DISK_MONITOR_ENABLED = enabled
    s.DISK_CHECK_INTERVAL_MIN = interval_min
    s.DISK_WARNING_GB = warning_gb
    s.DISK_CRITICAL_GB = critical_gb
    s.HISTORY_LARGE_MB = history_large_mb
    return s


def _make_event_bus() -> tuple[MagicMock, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []
    bus = MagicMock()

    def _emit(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    bus.emit.side_effect = _emit
    return bus, events


def _make_monitor(tmp_dir: Path, **kwargs) -> tuple[DiskSpaceMonitor, list]:
    s = _make_settings(**kwargs)
    bus, events = _make_event_bus()
    m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=tmp_dir)
    return m, events


# ---------------------------------------------------------------------------
# F1: get_disk_status in HEAVY_METHODS
# ---------------------------------------------------------------------------

class TestGetDiskStatusHeavyClassification(unittest.TestCase):
    """F1: get_disk_status должен быть в HEAVY_METHODS и throttled как heavy."""

    def test_get_disk_status_in_heavy_methods(self) -> None:
        self.assertIn(
            "get_disk_status",
            HEAVY_METHODS,
            "get_disk_status must be in HEAVY_METHODS for DoS protection",
        )

    def test_get_disk_status_classified_heavy(self) -> None:
        self.assertEqual(_classify_method("get_disk_status"), "heavy")

    def test_get_disk_status_throttled_after_5_per_min(self) -> None:
        """After 5 calls (heavy bucket capacity), 6th call must be rejected."""
        throttle = IPCThrottle(limits={"heavy": 5})
        allowed = [throttle.check_rate("get_disk_status") for _ in range(6)]
        # First 5 should pass, 6th throttled
        self.assertTrue(all(allowed[:5]), "First 5 calls must be allowed")
        self.assertFalse(allowed[5], "6th call must be throttled (heavy limit=5)")

    def test_get_disk_status_wait_time_positive_after_throttle(self) -> None:
        """After exhausting heavy bucket, wait_time > 0."""
        throttle = IPCThrottle(limits={"heavy": 1})
        throttle.check_rate("get_disk_status")  # consume the only token
        throttle.check_rate("get_disk_status")  # should be throttled
        wait = throttle.get_wait_time("get_disk_status")
        self.assertGreater(wait, 0.0, "Wait time must be > 0 after exhausting tokens")


# ---------------------------------------------------------------------------
# F1: 30s in-process cache inside DiskSpaceMonitor
# ---------------------------------------------------------------------------

class TestDiskMonitor30sCache(unittest.TestCase):
    """F1: DiskSpaceMonitor has 30s cache — _collect_status not re-called within TTL."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cache_attrs_exist_after_init(self) -> None:
        m, _ = _make_monitor(self._data_dir)
        self.assertTrue(hasattr(m, "_disk_cache"))
        self.assertTrue(hasattr(m, "_disk_cache_ts"))
        self.assertTrue(hasattr(m, "_DISK_CACHE_TTL_SEC"))
        self.assertEqual(m._DISK_CACHE_TTL_SEC, 30.0)

    def test_cache_populated_after_check_now(self) -> None:
        m, _ = _make_monitor(self._data_dir)
        self.assertEqual(m._disk_cache, {})
        m.check_now()
        self.assertNotEqual(m._disk_cache, {}, "Cache must be populated after check_now()")
        self.assertIn("level", m._disk_cache)

    def test_collect_status_updates_cache(self) -> None:
        """_collect_status() must update _disk_cache with fresh data."""
        m, _ = _make_monitor(self._data_dir)
        m._collect_status()
        self.assertNotEqual(m._disk_cache, {})
        self.assertGreater(m._disk_cache_ts, 0.0)

    def test_cache_ts_advances_after_collect_status(self) -> None:
        m, _ = _make_monitor(self._data_dir)
        m._collect_status()
        ts1 = m._disk_cache_ts
        # Ensure monotonic time advances
        time.sleep(0.01)
        m._collect_status()
        ts2 = m._disk_cache_ts
        self.assertGreater(ts2, ts1, "_disk_cache_ts must advance after second collect")


# ---------------------------------------------------------------------------
# F2: check_now force rate-limit (60s minimum interval)
# ---------------------------------------------------------------------------

class TestCheckNowForceRateLimit(unittest.TestCase):
    """F2: check_now() second call within 60s returns cached result without re-emit."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_force_rate_limit_attrs_exist(self) -> None:
        m, _ = _make_monitor(self._data_dir)
        self.assertTrue(hasattr(m, "_last_force_ts"))
        self.assertTrue(hasattr(m, "_FORCE_MIN_INTERVAL_SEC"))
        self.assertEqual(m._last_force_ts, 0.0)
        self.assertEqual(m._FORCE_MIN_INTERVAL_SEC, 60.0)

    def test_first_check_now_always_runs(self) -> None:
        """First call (last_force_ts=0.0) must always execute collect+emit."""
        m, events = _make_monitor(self._data_dir, warning_gb=0.0, critical_gb=0.0)
        # Patch collect to count calls
        original_collect = m._collect_status
        call_count = [0]

        def counting_collect():
            call_count[0] += 1
            return original_collect()

        m._collect_status = counting_collect
        status = m.check_now()
        self.assertEqual(call_count[0], 1, "First check_now must call _collect_status")
        self.assertIn("level", status)

    def test_second_check_now_within_60s_returns_cache(self) -> None:
        """Second call within 60s must return cached result without calling collect again."""
        m, events = _make_monitor(self._data_dir, warning_gb=0.0, critical_gb=0.0)
        original_collect = m._collect_status
        call_count = [0]

        def counting_collect():
            call_count[0] += 1
            return original_collect()

        m._collect_status = counting_collect

        # First call — runs collect
        status1 = m.check_now()
        self.assertEqual(call_count[0], 1)
        emit_count_after_first = len(events)

        # Second call immediately — must use cache (no collect, no emit)
        status2 = m.check_now()
        self.assertEqual(call_count[0], 1, "Second check_now within 60s must NOT call _collect_status")
        self.assertEqual(
            len(events),
            emit_count_after_first,
            "Second check_now within 60s must NOT re-emit SSE events",
        )
        # Status shape must be the same
        self.assertEqual(status1.get("level"), status2.get("level"))

    def test_second_check_now_after_60s_runs_fresh(self) -> None:
        """After 60s TTL expires, next check_now must call collect and emit again."""
        m, events = _make_monitor(
            self._data_dir,
            warning_gb=0.0,
            critical_gb=0.0,
        )
        original_collect = m._collect_status
        call_count = [0]

        def counting_collect():
            call_count[0] += 1
            return original_collect()

        m._collect_status = counting_collect

        # First call
        m.check_now()
        self.assertEqual(call_count[0], 1)

        # Simulate 61s elapsed by backdating _last_force_ts
        m._last_force_ts = time.monotonic() - 61.0

        # Second call after TTL — must re-collect
        m.check_now()
        self.assertEqual(call_count[0], 2, "After TTL expires, check_now must re-collect")

    def test_check_now_with_warning_level_re_emits_after_ttl(self) -> None:
        """After force TTL, new check that still has warning emits again."""
        m, events = _make_monitor(self._data_dir, warning_gb=10.0, critical_gb=1.0)
        fake_usage = MagicMock()
        fake_usage.free = int(3 * 1024 ** 3)   # 3 GB < 10 GB warning
        fake_usage.total = int(100 * 1024 ** 3)

        with patch("backend.disk_monitor.shutil.disk_usage", return_value=fake_usage):
            m.check_now()
            emit_count_after_first = len(events)
            warning_count = sum(1 for t, _ in events if t == "disk.warning")
            self.assertEqual(warning_count, 1, "First call must emit disk.warning")

            # Second call within 60s — no re-emit
            m.check_now()
            self.assertEqual(
                len(events),
                emit_count_after_first,
                "Second check within 60s must NOT re-emit",
            )

            # Expire TTL, reset _last_disk_level to allow re-emit
            m._last_force_ts = time.monotonic() - 61.0
            m._last_disk_level = None

            m.check_now()
            final_warning_count = sum(1 for t, _ in events if t == "disk.warning")
            self.assertEqual(
                final_warning_count,
                2,
                "After TTL expires, disk.warning must be re-emitted",
            )

    def test_check_now_cache_is_empty_initially_runs_fresh(self) -> None:
        """If cache is empty (brand new monitor), check_now always runs."""
        m, _ = _make_monitor(self._data_dir)
        # Manually set last_force_ts to make it look like < 60s ago,
        # but cache is empty — must still run fresh
        m._last_force_ts = time.monotonic() - 1.0   # 1s ago
        # Cache is empty so guard (self._disk_cache) is falsy → runs fresh
        original_collect = m._collect_status
        call_count = [0]

        def counting_collect():
            call_count[0] += 1
            return original_collect()

        m._collect_status = counting_collect
        m.check_now()
        self.assertEqual(call_count[0], 1, "Must collect when cache is empty even if < 60s")


# ---------------------------------------------------------------------------
# Integration: F1 + F2 together
# ---------------------------------------------------------------------------

class TestWave33Integration(unittest.TestCase):
    """Integration: F1 (HEAVY) + F2 (cache) work correctly together."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_get_disk_status_heavy_bucket_5_per_min(self) -> None:
        """Full scenario: get_disk_status throttled at 5/min as heavy method."""
        throttle = IPCThrottle()
        results = [throttle.check_rate("get_disk_status") for _ in range(6)]
        true_count = sum(results)
        false_count = sum(1 for r in results if not r)
        self.assertEqual(true_count, 5, "Exactly 5 calls allowed per heavy bucket")
        self.assertGreaterEqual(false_count, 1, "At least 1 call must be throttled")

    def test_check_now_twice_in_1s_second_cached(self) -> None:
        """Task spec: check_now force=True twice in 1s -> second cached."""
        m, events = _make_monitor(self._data_dir, warning_gb=0.0, critical_gb=0.0)
        original_collect = m._collect_status
        call_count = [0]

        def counting_collect():
            call_count[0] += 1
            return original_collect()

        m._collect_status = counting_collect

        status1 = m.check_now()
        # Immediately call again (well within 60s)
        status2 = m.check_now()

        self.assertEqual(call_count[0], 1, "collect_status called only once for two rapid calls")
        self.assertEqual(status1.get("level"), status2.get("level"),
                         "Both calls return consistent status")


if __name__ == "__main__":
    unittest.main()
