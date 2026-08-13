"""C2b: privacy-purge удаляет <data_dir>/tmp_meeting/ (временные диар-окна).

`_job_diar_window` пишет WAV-окно с голосом пользователя в tmp_meeting/ и
удаляет его в finally того же тика; после краха backend посреди тика файл
пережил бы purge без шага 13b в handle_purge_all_data. Пинится гейтом
scripts/audit_purge_coverage.py.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService  # noqa: E402


class _FakeStore:
    """Минимальный StateStore fake (паттерн test_purge_privacy_gaps_w1767)."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._lock_obj = threading.Lock()
        self._settings: dict = {}

    def _lock(self):
        return self._lock_obj

    def _load_active_items_unlocked(self) -> list:
        return []

    def _append_ndjson(self, path: Any, payload: dict) -> None:
        pass

    @property
    def tombstones_path(self) -> str:
        return "fake_tombstones.ndjson"

    def compact_with_stats(self) -> dict:
        return {"before_active_count": 0, "after_active_count": 0}

    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> dict:
        return dict(self._settings)

    def save_settings(self, settings: dict) -> dict:
        self._settings = dict(settings)
        return dict(settings)


class TmpMeetingPurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_tmp_meeting_dir_removed_on_purge(self) -> None:
        """Осиротевший после краха WAV-файл окна не переживает purge."""
        tmp_meeting = Path(self._tmpdir) / "tmp_meeting"
        tmp_meeting.mkdir(parents=True)
        (tmp_meeting / "diar_deadbeef.wav").write_bytes(b"RIFFfake-voice-bytes")

        svc = HistoryService(store=_FakeStore(self._tmpdir))
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), str(result))
        self.assertFalse(tmp_meeting.exists(),
                         "tmp_meeting/ должен быть удалён после purge_all_data")

    def test_no_tmp_meeting_dir_no_crash(self) -> None:
        svc = HistoryService(store=_FakeStore(self._tmpdir))
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"), str(result))
        self.assertNotIn("tmp_meeting", result.get("errors", []))


if __name__ == "__main__":
    unittest.main()
