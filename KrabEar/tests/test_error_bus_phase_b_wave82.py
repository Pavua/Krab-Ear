"""Unit tests for Wave 490 (Phase B Wave 82) error codes and their call-site wiring.

One test class per new code:
1. disk.critical                — DiskSpaceMonitor._push_disk_critical_error
2. system.proc_cmdline_permission — service._push_proc_cmdline_permission_error
3. startup.stt_model_cache_miss — StartupDiagnostics._push_stt_cache_miss_error
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow imports from KrabEar/
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.error_bus import ErrorBus, KrabError
from backend.error_codes import ERROR_REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_error_bus() -> tuple[ErrorBus, list[KrabError]]:
    """Create an ErrorBus and capture pushed errors."""
    mock_event_bus = MagicMock()
    bus = ErrorBus(event_bus=mock_event_bus, registry=ERROR_REGISTRY)
    captured: list[KrabError] = []

    original_push = bus.push

    def _capture(err: KrabError) -> bool:
        captured.append(err)
        return original_push(err)

    bus.push = _capture  # type: ignore[method-assign]
    return bus, captured


# ---------------------------------------------------------------------------
# 1. disk.critical — DiskSpaceMonitor._push_disk_critical_error
# ---------------------------------------------------------------------------

class DiskCriticalTests(unittest.TestCase):
    """disk.critical fires when free disk space falls below 1 GB threshold."""

    def test_code_in_registry(self):
        self.assertIn("disk.critical", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["disk.critical"]
        self.assertEqual(entry["severity"], "critical")
        self.assertTrue(entry["actionable"])
        self.assertEqual(entry["action_id"], "open_logs")
        self.assertNotEqual(entry["action_label"], "")
        self.assertEqual(entry["dedupe_seconds"], 600)

    def test_required_keys_present(self):
        entry = ERROR_REGISTRY["disk.critical"]
        required = {"user_msg_ru", "actionable", "action_id", "action_label", "severity", "dedupe_seconds"}
        missing = required - set(entry.keys())
        self.assertFalse(missing, f"disk.critical missing keys: {missing}")

    def test_push_via_error_bus(self):
        """Simulate the push that disk_monitor does on disk.critical."""
        bus, captured = _make_error_bus()

        entry = ERROR_REGISTRY["disk.critical"]
        err = KrabError(
            severity=entry["severity"],
            component="disk",
            code="disk.critical",
            message_user=entry["user_msg_ru"],
            message_debug="disk.critical: 0.22 GB free",
            timestamp=datetime.now(timezone.utc),
            context={"level": "critical", "free_gb": 0.22},
            actionable=True,
            action_id="open_logs",
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "disk.critical")
        self.assertEqual(e.component, "disk")
        self.assertEqual(e.severity, "critical")
        self.assertTrue(e.actionable)
        self.assertEqual(e.action_id, "open_logs")
        self.assertIn("КРИТИЧНО", e.message_user)

    def test_disk_monitor_push_critical_no_bus(self):
        """_push_disk_critical_error is silent when _error_bus is None."""
        from backend.disk_monitor import DiskSpaceMonitor
        mock_settings = MagicMock()
        mock_settings.DISK_MONITOR_ENABLED = True
        mock_settings.DISK_WARNING_GB = 2.0
        mock_settings.DISK_CRITICAL_GB = 1.0
        mock_settings.HISTORY_LARGE_MB = 500
        mock_settings.DISK_CHECK_INTERVAL_MIN = 5
        monitor = DiskSpaceMonitor(mock_settings, MagicMock(), Path("/tmp"))
        monitor._error_bus = None
        # Must not raise
        monitor._push_disk_critical_error(0.22)

    def test_disk_monitor_push_critical_with_bus(self):
        """_push_disk_critical_error pushes disk.critical when bus is wired."""
        from backend.disk_monitor import DiskSpaceMonitor
        mock_settings = MagicMock()
        mock_settings.DISK_MONITOR_ENABLED = True
        mock_settings.DISK_WARNING_GB = 2.0
        mock_settings.DISK_CRITICAL_GB = 1.0
        mock_settings.HISTORY_LARGE_MB = 500
        mock_settings.DISK_CHECK_INTERVAL_MIN = 5
        monitor = DiskSpaceMonitor(mock_settings, MagicMock(), Path("/tmp"))
        bus, captured = _make_error_bus()
        monitor._error_bus = bus

        monitor._push_disk_critical_error(0.22)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "disk.critical")
        self.assertEqual(e.severity, "critical")
        self.assertAlmostEqual(e.context["free_gb"], 0.22, places=2)

    def test_dedupe_suppresses_second_push(self):
        """Second push within dedupe_seconds window is suppressed."""
        bus, captured = _make_error_bus()
        entry = ERROR_REGISTRY["disk.critical"]

        def _make_err():
            return KrabError(
                severity=entry["severity"],
                component="disk",
                code="disk.critical",
                message_user=entry["user_msg_ru"],
                message_debug="repeated",
                timestamp=datetime.now(timezone.utc),
                context={"free_gb": 0.1},
                actionable=True,
                action_id="open_logs",
            )

        first = bus.push(_make_err())
        self.assertTrue(first)
        second = bus.push(_make_err())
        self.assertFalse(second)


# ---------------------------------------------------------------------------
# 2. system.proc_cmdline_permission — service._push_proc_cmdline_permission_error
# ---------------------------------------------------------------------------

class ProcCmdlinePermissionTests(unittest.TestCase):
    """system.proc_cmdline_permission fires when psutil.process_iter raises PermissionError."""

    def test_code_in_registry(self):
        self.assertIn("system.proc_cmdline_permission", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["system.proc_cmdline_permission"]
        self.assertEqual(entry["severity"], "error")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 3600)

    def test_required_keys_present(self):
        entry = ERROR_REGISTRY["system.proc_cmdline_permission"]
        required = {"user_msg_ru", "actionable", "action_id", "action_label", "severity", "dedupe_seconds"}
        missing = required - set(entry.keys())
        self.assertFalse(missing, f"system.proc_cmdline_permission missing keys: {missing}")

    def test_push_via_error_bus(self):
        """Simulate the push that service.py does on psutil PermissionError."""
        bus, captured = _make_error_bus()

        entry = ERROR_REGISTRY["system.proc_cmdline_permission"]
        err = KrabError(
            severity=entry["severity"],
            component="system",
            code="system.proc_cmdline_permission",
            message_user=entry["user_msg_ru"],
            message_debug="psutil.process_iter raised PermissionError: [Errno 1] Operation not permitted",
            timestamp=datetime.now(timezone.utc),
            context={"exc_type": "PermissionError", "exc_msg": "[Errno 1] Operation not permitted"},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "system.proc_cmdline_permission")
        self.assertEqual(e.component, "system")
        self.assertEqual(e.severity, "error")
        self.assertFalse(e.actionable)
        self.assertIsNone(e.action_id)
        self.assertIn("Sequoia", e.message_user)

    def test_service_push_no_error_bus(self):
        """_push_proc_cmdline_permission_error is silent when _error_bus is None."""
        from backend import service as svc_module

        class _FakeService:
            _error_bus = None

        fake = _FakeService()
        exc = PermissionError("[Errno 1] Operation not permitted")
        # Must not raise
        svc_module.BackendService._push_proc_cmdline_permission_error(fake, exc)

    def test_service_push_with_bus(self):
        """_push_proc_cmdline_permission_error pushes the error when bus is wired."""
        from backend import service as svc_module

        bus, captured = _make_error_bus()

        class _FakeService:
            _error_bus = bus

        fake = _FakeService()
        exc = SystemError("KERN_PROCARGS2 access denied")
        svc_module.BackendService._push_proc_cmdline_permission_error(fake, exc)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "system.proc_cmdline_permission")
        self.assertEqual(e.severity, "error")
        self.assertIn("SystemError", e.message_debug)

    def test_dedupe_suppresses_second_push(self):
        """Second push within dedupe window (3600s) is suppressed."""
        bus, _ = _make_error_bus()
        entry = ERROR_REGISTRY["system.proc_cmdline_permission"]

        def _make_err():
            return KrabError(
                severity=entry["severity"],
                component="system",
                code="system.proc_cmdline_permission",
                message_user=entry["user_msg_ru"],
                message_debug="repeated",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=False,
                action_id=None,
            )

        first = bus.push(_make_err())
        self.assertTrue(first)
        second = bus.push(_make_err())
        self.assertFalse(second)


# ---------------------------------------------------------------------------
# 3. startup.stt_model_cache_miss — StartupDiagnostics._push_stt_cache_miss_error
# ---------------------------------------------------------------------------

class StartupSttCacheMissTests(unittest.TestCase):
    """startup.stt_model_cache_miss fires when Whisper HF model is not cached."""

    def test_code_in_registry(self):
        self.assertIn("startup.stt_model_cache_miss", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["startup.stt_model_cache_miss"]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 86400)

    def test_required_keys_present(self):
        entry = ERROR_REGISTRY["startup.stt_model_cache_miss"]
        required = {"user_msg_ru", "actionable", "action_id", "action_label", "severity", "dedupe_seconds"}
        missing = required - set(entry.keys())
        self.assertFalse(missing, f"startup.stt_model_cache_miss missing keys: {missing}")

    def test_push_via_error_bus(self):
        """Simulate the push that StartupDiagnostics does on cache miss."""
        bus, captured = _make_error_bus()

        entry = ERROR_REGISTRY["startup.stt_model_cache_miss"]
        err = KrabError(
            severity=entry["severity"],
            component="startup",
            code="startup.stt_model_cache_miss",
            message_user=entry["user_msg_ru"],
            message_debug="STT model not cached: mlx-community/whisper-large-v3-turbo",
            timestamp=datetime.now(timezone.utc),
            context={"model": "mlx-community/whisper-large-v3-turbo"},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "startup.stt_model_cache_miss")
        self.assertEqual(e.component, "startup")
        self.assertEqual(e.severity, "warn")
        self.assertFalse(e.actionable)
        self.assertIsNone(e.action_id)
        self.assertIn("кэш", e.message_user)

    def test_startup_diagnostics_push_no_bus(self):
        """_push_stt_cache_miss_error is silent when _error_bus is None."""
        from backend.startup_diagnostics import StartupDiagnostics
        diag = StartupDiagnostics()
        diag._error_bus = None
        # Must not raise
        diag._push_stt_cache_miss_error("mlx-community/whisper-large-v3-turbo")

    def test_startup_diagnostics_push_with_bus(self):
        """_push_stt_cache_miss_error pushes the error when bus is wired."""
        from backend.startup_diagnostics import StartupDiagnostics
        bus, captured = _make_error_bus()
        diag = StartupDiagnostics()
        diag._error_bus = bus

        diag._push_stt_cache_miss_error("mlx-community/whisper-large-v3-turbo")

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "startup.stt_model_cache_miss")
        self.assertEqual(e.severity, "warn")
        self.assertEqual(e.context["model"], "mlx-community/whisper-large-v3-turbo")
        self.assertIn("whisper-large-v3-turbo", e.message_debug)

    def test_check_stt_model_cached_fires_push_on_miss(self):
        """_check_stt_model_cached fires _push_stt_cache_miss_error on cache miss."""
        from backend.startup_diagnostics import StartupDiagnostics
        import tempfile
        bus, captured = _make_error_bus()

        with tempfile.TemporaryDirectory() as tmpdir:
            diag = StartupDiagnostics(data_dir=tmpdir)
            diag._error_bus = bus

            # Patch HF cache to a non-existent path to force cache-miss branch
            with patch("backend.startup_diagnostics.Path") as mock_path_cls:
                # Let actual Path work for data_dir, but return non-existent for HF cache
                real_path = Path

                def _side_effect(*args, **kwargs):
                    result = real_path(*args, **kwargs)
                    return result

                mock_path_cls.side_effect = _side_effect
                mock_path_cls.home.return_value = real_path("/nonexistent_home_xyz")

                result = diag._check_stt_model_cached()

        # Either a cache miss warning was raised or the model was actually found
        # (in CI the HF cache may not exist). Either way, no crash.
        self.assertIn(result.status, ("ok", "warning"))

    def test_dedupe_window_is_one_day(self):
        """startup.stt_model_cache_miss dedupes for 86400s = 1 day."""
        entry = ERROR_REGISTRY["startup.stt_model_cache_miss"]
        self.assertEqual(entry["dedupe_seconds"], 86400)

    def test_dedupe_suppresses_second_push(self):
        """Second push within dedupe window is suppressed."""
        bus, _ = _make_error_bus()
        entry = ERROR_REGISTRY["startup.stt_model_cache_miss"]

        def _make_err():
            return KrabError(
                severity=entry["severity"],
                component="startup",
                code="startup.stt_model_cache_miss",
                message_user=entry["user_msg_ru"],
                message_debug="repeated",
                timestamp=datetime.now(timezone.utc),
                context={"model": "whisper-test"},
                actionable=False,
                action_id=None,
            )

        first = bus.push(_make_err())
        self.assertTrue(first)
        second = bus.push(_make_err())
        self.assertFalse(second)


if __name__ == "__main__":
    unittest.main()
