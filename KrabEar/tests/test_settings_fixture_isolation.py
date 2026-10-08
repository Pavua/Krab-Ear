"""Settings fixture не запускает disk→REST при низком свободном месте."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from _settings_test_helpers import safe_backend_for_settings
from backend.disk_monitor import DiskSpaceMonitor
from backend.event_bus import EventBus
from backend.service import BackendService
from backend.startup_diagnostics import StartupDiagnostics
from backend.state_store import StateStore


class SettingsFixtureIsolationTest(unittest.TestCase):
    def test_low_disk_does_not_post_and_real_chain_positive_control(self):
        sent_disk_events = []

        def intercepted_post(_url, payload, _token, _timeout):
            sent_disk_events.append(any(event.get("type") == "disk.warning"
                                        for event in payload["events"]))
            return True

        disk_start = DiskSpaceMonitor.start

        def deterministic_initial_check(monitor):
            disk_start(monitor)
            # thread.start подавлен; check_now — настоящий disk→bus путь.
            if monitor._thread is not None:
                monitor.check_now()

        report = SimpleNamespace(status="ready", errors=[], warnings=[],
                                 startup_time_ms=0, checks=[])
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "profile")
            store.initialize_startup_plaintext_policy(new_profile=True)
            with patch("backend.service.event_bus", EventBus()), \
                    patch("backend.service.settings.EVENT_BRIDGE_ENABLED", True), \
                    patch("backend.service.settings.DISK_MONITOR_ENABLED", True), \
                    patch("backend.service.settings.DISK_WARNING_GB", 5.0), \
                    patch("backend.service.settings.DISK_CRITICAL_GB", 1.0), \
                    patch("backend.service.settings.REST_IN_PROCESS_ENABLED", False), \
                    patch("backend.service.settings.LLM_ENABLED", False), \
                    patch.object(threading.Thread, "start", return_value=None), \
                    patch("backend.service.PurgeScheduler.start", return_value=None), \
                    patch.object(BackendService, "_check_binary_drift_on_startup", return_value=None), \
                    patch.object(StartupDiagnostics, "run_all_checks", return_value=report), \
                    patch.object(StartupDiagnostics, "_start_lm_studio_background_check", return_value=None), \
                    patch("backend.event_bridge._default_post_fn", intercepted_post), \
                    patch("backend.disk_monitor.shutil.disk_usage", return_value=SimpleNamespace(
                        total=100 * 1024**3, used=96 * 1024**3, free=4 * 1024**3)), \
                    patch.object(DiskSpaceMonitor, "start", deterministic_initial_check):
                service = safe_backend_for_settings(self, store)
                try:
                    service._event_bridge._tick()
                    self.assertEqual(len(sent_disk_events), 0,
                                     "settings fixture must not send disk events")
                    self.assertIsNone(service._disk_monitor._thread)
                    self.assertFalse(service._event_bridge._enabled)
                    # Positive control с теми же настоящими adapters и settings.
                    control = BackendService(store=store, recorder=service.recorder,
                                             transcriber=service.transcriber, translator=service.translator)
                    try:
                        control._event_bridge._tick()
                        self.assertTrue(any(sent_disk_events),
                                        "real low-disk→EventBridge chain was not exercised")
                    finally:
                        control.close()
                finally:
                    service.close()
