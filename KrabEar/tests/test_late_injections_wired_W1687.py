"""W1687 — Tests verifying that all 7 late-injection wirings from the W1686
decorative-architecture meta-audit are present in BackendService.__init__.

Bugs fixed:
    F1 HIGH  DiskSpaceMonitor._error_bus  (disk.warn/critical KrabErrors were dropped)
    F2 HIGH  EventReplayManager._settings_provider  (privacy redaction silently skipped)
    F3 HIGH  ErrorReporter._settings_provider  (get_error_report leaked in privacy mode)
    F5 MED   RecapScheduler._settings_provider  (runtime schedule changes ignored)
    F6 MED   ExportScheduler._settings_provider  (privacy guard in check_and_export skipped)
    F7 MED   ArchiveManager._recording_chain_mgr  (ghost chain refs after archive)
    F8 MED   ArchiveManager.semantic_searcher  (archived items stayed in semantic index)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore
from backend.service import BackendService


# ---------------------------------------------------------------------------
# Minimal stubs (same pattern as existing wiring tests)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    is_recording = False
    sample_rate = 16_000

    def start(self) -> bool:
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        return None

    def snapshot_audio(self) -> bytes:
        return b""


class _FakeTranscriber:
    def transcribe(self, audio, language=None, initial_prompt=None):
        return "test", 0.9, []

    def get_profile(self):
        return "balanced"

    def set_profile(self, profile: str) -> None:
        pass

    def get_vocabulary(self):
        return []

    def set_vocabulary(self, words) -> None:
        pass


class _FakeTranslator:
    def translate(self, text, source_language=None, target_language=None):
        from backend.translator import TranslationResult
        return TranslationResult(
            translated_text=text,
            source_language="ru",
            target_language="es",
        )


def _make_service(tmp_dir: str) -> BackendService:
    store = StateStore(Path(tmp_dir) / "data")
    return BackendService(
        store=store,
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
    )


# ===========================================================================
# Tests
# ===========================================================================

class TestW1687F1DiskMonitorErrorBusWired(unittest.TestCase):
    """F1 HIGH: DiskSpaceMonitor._error_bus must be non-None after init."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = _make_service(self.tmp.name)

    def test_disk_monitor_error_bus_is_wired(self):
        """_disk_monitor._error_bus must be the same ErrorBus as BackendService._error_bus."""
        svc = self.svc
        self.assertTrue(hasattr(svc, "_disk_monitor"), "_disk_monitor must exist")
        self.assertTrue(hasattr(svc, "_error_bus"), "_error_bus must exist")
        self.assertIsNotNone(
            svc._disk_monitor._error_bus,
            "W1687 F1: _disk_monitor._error_bus must be wired — "
            "disk.warn / disk.critical errors were silently dropped",
        )

    def test_disk_monitor_error_bus_is_same_instance(self):
        """_disk_monitor._error_bus must be the exact same ErrorBus instance."""
        svc = self.svc
        self.assertIs(
            svc._disk_monitor._error_bus,
            svc._error_bus,
            "W1687 F1: _disk_monitor._error_bus must be the same object as "
            "BackendService._error_bus",
        )


class TestW1687F2EventReplaySettingsProviderWired(unittest.TestCase):
    """F2 HIGH: EventReplayManager._settings_provider must be non-None after init."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = _make_service(self.tmp.name)

    def test_event_replay_settings_provider_is_wired(self):
        """_event_replay._settings_provider must not be None."""
        svc = self.svc
        self.assertTrue(hasattr(svc, "_event_replay"), "_event_replay must exist")
        self.assertIsNotNone(
            svc._event_replay._settings_provider,
            "W1687 F2: _event_replay._settings_provider must be wired — "
            "privacy-mode redaction in get_event_log was silently skipped",
        )

    def test_event_replay_settings_provider_is_callable(self):
        """_event_replay._settings_provider must be callable."""
        svc = self.svc
        self.assertTrue(
            callable(svc._event_replay._settings_provider),
            "W1687 F2: _event_replay._settings_provider must be callable "
            "(should be self._settings_svc.cached_settings)",
        )


class TestW1687F3ErrorReporterSettingsProviderWired(unittest.TestCase):
    """F3 HIGH: ErrorReporter._settings_provider must be non-None after init."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = _make_service(self.tmp.name)

    def test_error_reporter_settings_provider_is_wired(self):
        """_error_reporter._settings_provider must not be None."""
        svc = self.svc
        self.assertTrue(hasattr(svc, "_error_reporter"), "_error_reporter must exist")
        self.assertIsNotNone(
            svc._error_reporter._settings_provider,
            "W1687 F3: _error_reporter._settings_provider must be wired — "
            "get_error_report leaked error content in privacy mode",
        )

    def test_error_reporter_settings_provider_is_callable(self):
        """_error_reporter._settings_provider must be callable."""
        svc = self.svc
        self.assertTrue(
            callable(svc._error_reporter._settings_provider),
            "W1687 F3: _error_reporter._settings_provider must be callable",
        )


class TestW1687F5RecapSchedulerSettingsProviderWired(unittest.TestCase):
    """F5 MED: RecapScheduler._settings_provider must be non-None after init."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = _make_service(self.tmp.name)

    def test_recap_scheduler_settings_provider_is_wired(self):
        """_recap_scheduler._settings_provider must not be None."""
        svc = self.svc
        self.assertTrue(hasattr(svc, "_recap_scheduler"), "_recap_scheduler must exist")
        self.assertIsNotNone(
            svc._recap_scheduler._settings_provider,
            "W1687 F5: _recap_scheduler._settings_provider must be wired — "
            "runtime changes to recap_enabled / recap_time_hour were ignored",
        )

    def test_recap_scheduler_settings_provider_is_callable(self):
        """_recap_scheduler._settings_provider must be callable."""
        svc = self.svc
        self.assertTrue(
            callable(svc._recap_scheduler._settings_provider),
            "W1687 F5: _recap_scheduler._settings_provider must be callable",
        )


class TestW1687F6ExportSchedulerSettingsProviderWired(unittest.TestCase):
    """F6 MED: ExportScheduler._settings_provider must be non-None after init."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = _make_service(self.tmp.name)

    def test_export_scheduler_settings_provider_is_wired(self):
        """_export_scheduler._settings_provider must not be None."""
        svc = self.svc
        self.assertTrue(hasattr(svc, "_export_scheduler"), "_export_scheduler must exist")
        self.assertIsNotNone(
            svc._export_scheduler._settings_provider,
            "W1687 F6: _export_scheduler._settings_provider must be wired — "
            "privacy guard in check_and_export was silently skipped",
        )

    def test_export_scheduler_settings_provider_is_callable(self):
        """_export_scheduler._settings_provider must be callable."""
        svc = self.svc
        self.assertTrue(
            callable(svc._export_scheduler._settings_provider),
            "W1687 F6: _export_scheduler._settings_provider must be callable",
        )


class TestW1687F7ArchiveManagerRecordingChainMgrWired(unittest.TestCase):
    """F7 MED: ArchiveManager._recording_chain_mgr must be non-None after init."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = _make_service(self.tmp.name)

    def test_archive_manager_recording_chain_mgr_is_wired(self):
        """_archive_manager._recording_chain_mgr must not be None."""
        svc = self.svc
        self.assertTrue(hasattr(svc, "_archive_manager"), "_archive_manager must exist")
        self.assertIsNotNone(
            svc._archive_manager._recording_chain_mgr,
            "W1687 F7: _archive_manager._recording_chain_mgr must be wired — "
            "archived items left ghost ID references in RecordingChain objects",
        )

    def test_archive_manager_recording_chain_mgr_is_same_instance(self):
        """_archive_manager._recording_chain_mgr must be the same chains instance."""
        svc = self.svc
        self.assertIs(
            svc._archive_manager._recording_chain_mgr,
            svc._chains,
            "W1687 F7: _archive_manager._recording_chain_mgr must be the same "
            "object as BackendService._chains",
        )


class TestW1687F8ArchiveManagerSemanticSearcherWired(unittest.TestCase):
    """F8 MED: ArchiveManager.semantic_searcher must be non-None after init."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = _make_service(self.tmp.name)

    def test_archive_manager_semantic_searcher_is_wired(self):
        """_archive_manager._semantic_searcher must not be None after late-inject."""
        svc = self.svc
        self.assertTrue(hasattr(svc, "_archive_manager"), "_archive_manager must exist")
        self.assertIsNotNone(
            svc._archive_manager._semantic_searcher,
            "W1687 F8: _archive_manager._semantic_searcher must be wired — "
            "archived items remained in the semantic search index",
        )

    def test_archive_manager_semantic_searcher_is_same_instance(self):
        """_archive_manager._semantic_searcher must be the same SemanticSearcher instance."""
        svc = self.svc
        self.assertIs(
            svc._archive_manager._semantic_searcher,
            svc._semantic_searcher,
            "W1687 F8: _archive_manager._semantic_searcher must be the same "
            "object as BackendService._semantic_searcher",
        )


if __name__ == "__main__":
    unittest.main()
