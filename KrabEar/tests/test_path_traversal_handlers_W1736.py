"""Wave 1736 — path-traversal regression tests for per-handler allowlists.

Covers three HIGH/MED-HIGH findings:
  Finding 2 — export_settings: arbitrary file WRITE via IPC
  Finding 3 — import_settings: arbitrary file READ via IPC
  Finding 1 — restore_history: arbitrary dir restore via IPC
  Finding 4 — audio analytics read handlers (LOW)
  Cleanup    — input_sanitizer.py must no longer exist on disk
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_service import SettingsService
from backend.history_service import HistoryService
from backend.state_store import StateStore
from backend.audio_analytics_service import (
    AudioAnalyticsService,
    _validate_audio_read_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_store(tmpdir: str) -> MagicMock:
    """Minimal fake store for SettingsService — no data_dir required."""
    from backend.models import DEFAULT_SETTINGS

    store = MagicMock()
    store.load_settings.return_value = dict(DEFAULT_SETTINGS)
    store.save_settings.return_value = {"ok": True}
    store.settings_path = Path(tmpdir) / "settings.json"
    store.data_dir = Path(tmpdir)
    return store


def _make_settings_svc(tmpdir: str) -> SettingsService:
    return SettingsService(store=_make_fake_store(tmpdir))


# ---------------------------------------------------------------------------
# Finding 2 — export_settings: arbitrary file WRITE
# ---------------------------------------------------------------------------

class TestExportSettingsPathTraversal(unittest.TestCase):
    """export_settings must reject paths outside the allowlist."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.svc = _make_settings_svc(self._tmp.name)

    def test_traversal_to_ssh_authorized_keys_rejected(self) -> None:
        """`~/.ssh/authorized_keys` must be rejected — raises RuntimeError."""
        evil_path = str(Path.home() / ".ssh" / "authorized_keys")
        with self.assertRaises(RuntimeError):
            self.svc.handle_export_settings({"file": evil_path})
        # File must NOT have been written
        self.assertFalse(
            Path(evil_path).exists() and
            Path(evil_path).read_text(errors="replace").startswith("{"),
            "authorized_keys must not have been overwritten with JSON",
        )

    def test_traversal_to_etc_passwd_rejected(self) -> None:
        """/etc/passwd must be rejected."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_export_settings({"file": "/etc/passwd"})

    def test_traversal_to_gitconfig_rejected(self) -> None:
        """~/.gitconfig must be rejected (outside allowlist)."""
        evil_path = str(Path.home() / ".gitconfig")
        with self.assertRaises(RuntimeError):
            self.svc.handle_export_settings({"file": evil_path})

    def test_legit_tmp_path_allowed(self) -> None:
        """A path inside /tmp must be allowed."""
        with tempfile.NamedTemporaryFile(
            suffix=".json", dir="/tmp", delete=False
        ) as tf:
            out_path = tf.name
        self.addCleanup(lambda: Path(out_path).unlink(missing_ok=True))
        result = self.svc.handle_export_settings({"file": out_path})
        self.assertIn("file", result)
        self.assertTrue(Path(out_path).exists())

    def test_legit_downloads_path_allowed(self) -> None:
        """A path inside ~/Downloads must be allowed."""
        downloads = Path.home() / "Downloads"
        if not downloads.exists():
            self.skipTest("~/Downloads does not exist on this machine")
        out_path = downloads / f"krab_test_export_w1736_{os.getpid()}.json"
        self.addCleanup(lambda: out_path.unlink(missing_ok=True))
        result = self.svc.handle_export_settings({"file": str(out_path)})
        self.assertIn("file", result)
        self.assertTrue(out_path.exists())


# ---------------------------------------------------------------------------
# Finding 3 — import_settings: arbitrary file READ
# ---------------------------------------------------------------------------

class TestImportSettingsPathTraversal(unittest.TestCase):
    """import_settings must reject paths outside the allowlist."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.svc = _make_settings_svc(self._tmp.name)

    def test_read_etc_hosts_rejected(self) -> None:
        """/etc/hosts must be rejected — raises RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_import_settings({"file": "/etc/hosts"})

    def test_read_etc_passwd_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            self.svc.handle_import_settings({"file": "/etc/passwd"})

    def test_read_ssh_known_hosts_rejected(self) -> None:
        """~/.ssh/known_hosts must be rejected."""
        evil = str(Path.home() / ".ssh" / "known_hosts")
        with self.assertRaises(RuntimeError):
            self.svc.handle_import_settings({"file": evil})

    def test_legit_tmp_json_allowed(self) -> None:
        """A valid settings JSON in /tmp must be accepted."""
        import json
        with tempfile.NamedTemporaryFile(
            suffix=".json", dir="/tmp", delete=False, mode="w"
        ) as tf:
            json.dump({"auto_paste": True}, tf)
            src_path = tf.name
        self.addCleanup(lambda: Path(src_path).unlink(missing_ok=True))
        result = self.svc.handle_import_settings({"file": src_path})
        self.assertIn("imported", result)


# ---------------------------------------------------------------------------
# Finding 1 — restore_history: arbitrary dir restore
# ---------------------------------------------------------------------------

class TestRestoreHistoryPathTraversal(unittest.TestCase):
    """restore_history must only accept paths inside data_dir/backups/."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = StateStore(Path(self._tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_restore_from_tmp_evil_rejected(self) -> None:
        """/tmp/evil_backup must be rejected — raises RuntimeError."""
        evil_dir = Path("/tmp/evil_backup_w1736")
        evil_dir.mkdir(exist_ok=True)
        (evil_dir / "history.ndjson").write_text("")
        with self.assertRaises(RuntimeError):
            self.svc.handle_restore_history({"backup_path": str(evil_dir)})

    def test_restore_from_home_dir_rejected(self) -> None:
        """A dir under ~ that isn't data_dir/backups/ must be rejected."""
        evil_dir = Path.home() / "fake_backup_w1736"
        evil_dir.mkdir(exist_ok=True)
        self.addCleanup(lambda: evil_dir.rmdir() if evil_dir.exists() else None)
        (evil_dir / "history.ndjson").write_text("")
        self.addCleanup(lambda: (evil_dir / "history.ndjson").unlink(missing_ok=True))
        with self.assertRaises(RuntimeError):
            self.svc.handle_restore_history({"backup_path": str(evil_dir)})

    def test_restore_from_legit_backups_dir_accepted(self) -> None:
        """A real backup inside data_dir/backups/ must be accepted."""
        import json
        backups_root = Path(self.store.data_dir) / "backups"
        backup_dir = backups_root / "backup_20260101T000000Z"
        backup_dir.mkdir(parents=True, exist_ok=True)
        # Write a valid (empty) history ndjson and meta
        (backup_dir / "history.ndjson").write_text("")
        (backup_dir / "backup_meta.json").write_text(
            json.dumps({"backup_ts": "2026-01-01T00:00:00Z"})
        )
        result = self.svc.handle_restore_history({"backup_path": str(backup_dir)})
        self.assertIn("restored_entries", result)


# ---------------------------------------------------------------------------
# Finding 4 — audio analytics read path allowlist helper
# ---------------------------------------------------------------------------

class TestValidateAudioReadPath(unittest.TestCase):
    """_validate_audio_read_path rejects paths outside home+/tmp+data_dir."""

    def test_etc_passwd_rejected(self) -> None:
        """/etc/passwd must be rejected."""
        with self.assertRaises(ValueError):
            _validate_audio_read_path("/etc/passwd", data_dir=None)

    def test_usr_lib_rejected(self) -> None:
        """/usr/lib/something.wav must be rejected."""
        with self.assertRaises(ValueError):
            _validate_audio_read_path("/usr/lib/audio.wav", data_dir=None)

    def test_tmp_path_allowed(self) -> None:
        """/tmp/test.wav must be allowed."""
        _validate_audio_read_path("/tmp/test_w1736.wav", data_dir=None)

    def test_home_path_allowed(self) -> None:
        """~/Downloads/audio.wav must be allowed."""
        p = str(Path.home() / "Downloads" / "audio.wav")
        _validate_audio_read_path(p, data_dir=None)

    def test_data_dir_path_allowed(self) -> None:
        """A path inside a custom data_dir must be allowed."""
        with tempfile.TemporaryDirectory() as d:
            audio_path = str(Path(d) / "recordings" / "r.wav")
            _validate_audio_read_path(audio_path, data_dir=Path(d))


class TestAudioAnalyticsHandlerPathTraversal(unittest.TestCase):
    """Full-handler test: analyze_audio_quality + analyze_silence reject bad paths."""

    def _make_svc(self) -> AudioAnalyticsService:
        store = MagicMock()
        store.data_dir = "/tmp"
        return AudioAnalyticsService(
            audio_converter=MagicMock(),
            quality_trends=MagicMock(),
            audio_fingerprinter=MagicMock(),
            word_timing_analyzer=MagicMock(),
            store=store,
        )

    def test_analyze_audio_quality_rejects_etc_path(self) -> None:
        svc = self._make_svc()
        with self.assertRaises(ValueError):
            svc.handle_analyze_audio_quality({"file_path": "/etc/passwd"})

    def test_analyze_silence_rejects_etc_path(self) -> None:
        svc = self._make_svc()
        with self.assertRaises(ValueError):
            svc.handle_analyze_silence({"file_path": "/etc/passwd"})

    def test_get_waveform_rejects_etc_path(self) -> None:
        svc = self._make_svc()
        with self.assertRaises(ValueError):
            svc.handle_get_waveform({"file_path": "/etc/passwd"})

    def test_get_audio_info_rejects_etc_path(self) -> None:
        svc = self._make_svc()
        with self.assertRaises(ValueError):
            svc.handle_get_audio_info({"path": "/etc/passwd"})

    def test_profile_noise_rejects_etc_path(self) -> None:
        svc = self._make_svc()
        with self.assertRaises(ValueError):
            svc.handle_profile_noise({"file_path": "/etc/passwd"})


# ---------------------------------------------------------------------------
# Cleanup — input_sanitizer module must no longer exist
# ---------------------------------------------------------------------------

class TestInputSanitizerDeleted(unittest.TestCase):
    """The dead InputSanitizer module must have been removed from disk (W1736)."""

    def test_input_sanitizer_file_gone(self) -> None:
        """backend/input_sanitizer.py must not exist."""
        candidates = [
            PROJECT_ROOT / "backend" / "input_sanitizer.py",
            Path(__file__).resolve().parents[2] / "KrabEar" / "backend" / "input_sanitizer.py",
        ]
        for p in candidates:
            self.assertFalse(
                p.exists(),
                f"input_sanitizer.py still exists at {p} — should have been deleted in W1736",
            )

    def test_input_sanitizer_not_importable(self) -> None:
        """Importing backend.input_sanitizer must raise ImportError or ModuleNotFoundError."""
        import importlib
        with self.assertRaises((ImportError, ModuleNotFoundError)):
            importlib.import_module("backend.input_sanitizer")


# ---------------------------------------------------------------------------
# NEW (reviewer fix): Audio sibling-prefix bypass — is_relative_to vs startswith
# ---------------------------------------------------------------------------

class TestAudioSiblingPrefixBypass(unittest.TestCase):
    """Regression: startswith('/tmp') admitted '/tmp_evil/x.wav' — is_relative_to rejects it.

    These tests FAIL before the fix (startswith) and PASS after (is_relative_to).
    """

    def test_tmp_sibling_rejected(self) -> None:
        """/private/tmp_evil/x.wav must be rejected — tmp_evil is NOT under /tmp."""
        tmp_root = Path("/tmp").resolve()
        # Build sibling by appending "_evil" to the resolved tmp name
        sibling = tmp_root.parent / (tmp_root.name + "_evil") / "x.wav"
        with self.assertRaises(ValueError):
            _validate_audio_read_path(str(sibling), data_dir=None)

    def test_home_sibling_rejected(self) -> None:
        """A path whose name starts with home but is a sibling must be rejected."""
        home = Path.home().resolve()
        sibling = home.parent / (home.name + "_evil") / "x.wav"
        with self.assertRaises(ValueError):
            _validate_audio_read_path(str(sibling), data_dir=None)

    def test_data_dir_sibling_rejected(self) -> None:
        """data_dir sibling outside all allowed roots must be rejected.

        We use /private/var which is NOT in the allowlist so the sibling stays out.
        """
        parent = Path("/private/var/krab_test_w1736_unit")
        data_dir = parent / "krab"
        # sibling shares the string prefix of data_dir but is NOT under it
        sibling = parent / (data_dir.name + "_evil") / "r.wav"
        with self.assertRaises(ValueError):
            _validate_audio_read_path(str(sibling), data_dir=data_dir)

    def test_tmp_exact_child_allowed(self) -> None:
        """/tmp child is still allowed after fix (regression guard)."""
        p = Path("/tmp") / "krab_test_w1736_exact.wav"
        _validate_audio_read_path(str(p), data_dir=None)

    def test_home_exact_child_allowed(self) -> None:
        """Exact child of home is allowed after fix (regression guard)."""
        p = Path.home() / "Downloads" / "test_w1736.wav"
        _validate_audio_read_path(str(p), data_dir=None)


# ---------------------------------------------------------------------------
# NEW (reviewer fix): ../ escape reject for each main handler
# ---------------------------------------------------------------------------

class TestDotDotEscapeHandlers(unittest.TestCase):
    """../ escapes must be rejected for export_settings, import_settings, restore_history."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_export_settings_dotdot_escape_rejected(self) -> None:
        """export_settings dotdot path to .ssh/authorized_keys must be rejected."""
        svc = _make_settings_svc(self._tmp.name)
        evil = str(
            Path.home() / "Library" / "Application Support" / "KrabEar"
            / ".." / ".." / ".." / ".ssh" / "authorized_keys"
        )
        with self.assertRaises(RuntimeError):
            svc.handle_export_settings({"file": evil})

    def test_import_settings_dotdot_escape_rejected(self) -> None:
        """import_settings with /tmp/../etc/hosts must be rejected."""
        svc = _make_settings_svc(self._tmp.name)
        with self.assertRaises(RuntimeError):
            svc.handle_import_settings({"file": "/tmp/../etc/hosts"})

    def test_restore_history_dotdot_escape_rejected(self) -> None:
        """restore_history with backups/../../etc must be rejected."""
        from backend.history_service import HistoryService
        from backend.state_store import StateStore
        store = StateStore(Path(self._tmp.name) / "data")
        svc = HistoryService(store=store)
        data_dir = Path(store.data_dir)
        evil_path = str(data_dir / "backups" / ".." / ".." / "etc")
        with self.assertRaises(RuntimeError):
            svc.handle_restore_history({"backup_path": evil_path})


# ---------------------------------------------------------------------------
# NEW (reviewer fix): restore_history symlink escape rejected
# ---------------------------------------------------------------------------

class TestRestoreHistorySymlinkEscape(unittest.TestCase):
    """A symlink inside backups/ pointing outside data_dir must be rejected."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_symlink_inside_backups_to_etc_rejected(self) -> None:
        """Symlink <data_dir>/backups/evil -> /etc must be rejected."""
        from backend.history_service import HistoryService
        from backend.state_store import StateStore
        store = StateStore(Path(self._tmp.name) / "data")
        svc = HistoryService(store=store)
        data_dir = Path(store.data_dir)
        backups_dir = data_dir / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        evil_link = backups_dir / "evil_etc"
        try:
            evil_link.symlink_to("/etc")
        except (OSError, NotImplementedError):
            self.skipTest("Cannot create symlinks on this filesystem")
        self.addCleanup(lambda: evil_link.unlink(missing_ok=True))
        with self.assertRaises(RuntimeError):
            svc.handle_restore_history({"backup_path": str(evil_link)})


# ---------------------------------------------------------------------------
# NEW (reviewer fix): export_settings no-side-effect on reject
# ---------------------------------------------------------------------------

class TestExportSettingsNoSideEffectOnReject(unittest.TestCase):
    """A rejected export_settings call must NOT create or modify the target file."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_evil_file_unchanged_after_reject(self) -> None:
        """The /etc target must be absent (or unchanged) after a rejected call."""
        svc = _make_settings_svc(self._tmp.name)
        evil_path = "/etc/krab_test_w1736_no_such_file"
        # Ensure it doesn't exist beforehand (it's in /etc so it shouldn't)
        assert not Path(evil_path).exists(), f"{evil_path} unexpectedly exists"
        with self.assertRaises(RuntimeError):
            svc.handle_export_settings({"file": evil_path})
        # Must not have been created
        self.assertFalse(
            Path(evil_path).exists(),
            "Rejected export must not create the file",
        )


if __name__ == "__main__":
    unittest.main()
