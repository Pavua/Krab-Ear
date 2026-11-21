"""KE05C-03 / KE05C-27: временный WAV диаризации встречи — 0600, каталог 0700, остатки чистятся.

``_job_diar_window`` писал аудио встречи в ``<data_dir>/tmp_meeting/diar_*.wav`` с правами
по umask (читаемо другими локальными пользователями), а после аварийного завершения
процесса файл оставался навсегда (finally удаляет только WAV текущего тика).
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.meeting_session_service as mss  # noqa: E402
from backend.meeting_session_service import MeetingSessionService  # noqa: E402


class _Recorder:
    sample_rate = 16000
    is_recording = True

    def get_duration_sec(self):
        return 100.0

    def snapshot_range(self, a, b):
        return np.ones(max(0, int((b - a) * 16000)), dtype=np.float32)


class _Transcriber:
    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return {"text": "x"}


class _Core:
    def handle_start_recording(self, params):
        return {"status": "recording", "is_recording": True}

    def handle_stop_recording(self, params):
        return {"history_id": "h"}

    def pause_realtime_partials(self):
        pass

    def resume_realtime_partials(self):
        pass


class _Bus:
    def emit(self, *a, **k):
        pass


class _SpySf:
    """Подмена soundfile: открывает путь как реальный soundfile (truncate существующего inode)."""

    def __init__(self) -> None:
        self.modes: list[int] = []
        self.dir_modes: list[int] = []

    def write(self, path, data, samplerate):
        p = Path(path)
        self.dir_modes.append(stat.S_IMODE(p.parent.stat().st_mode))
        with open(p, "wb") as fh:  # как soundfile: открытие на запись без смены режима
            fh.write(b"RIFFfake")
        self.modes.append(stat.S_IMODE(p.stat().st_mode))


def _service(tmp):
    settings = {
        "meeting_live_speakers_enabled": True,
        "meeting_diar_interval_sec": 90.0,
        "meeting_diar_window_sec": 90.0,
        "meeting_speaker_match_threshold": 0.72,
        "llm_brain_lease_enabled": False,
    }
    return MeetingSessionService(
        recorder=_Recorder(),
        transcriber=_Transcriber(),
        recording_core=_Core(),
        action_items_extractor=None,
        settings_get=lambda k, d=None: settings.get(k, d),
        event_bus=_Bus(),
        diarize_window=lambda p: {"segments": [], "speaker_embeddings": {}},
        data_dir=Path(tmp),
    )


class _Base(unittest.TestCase):
    def setUp(self):
        self.spy = _SpySf()
        orig = mss._sf
        mss._sf = self.spy
        self.addCleanup(lambda: setattr(mss, "_sf", orig))
        old_umask = os.umask(0)  # самый permissive umask — права должны задаваться явно
        self.addCleanup(os.umask, old_umask)


class TempWavPermissions(_Base):
    def test_wav_is_owner_only_and_dir_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp)
            try:
                self.assertTrue(svc.handle_meeting_start({})["ok"])
                svc._job_diar_window(svc._session)
            finally:
                svc.close()
            self.assertEqual(self.spy.modes, [0o600])
            self.assertEqual(self.spy.dir_modes, [0o700])

    def test_existing_loose_dir_is_tightened(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "tmp_meeting"
            d.mkdir()
            os.chmod(d, 0o755)
            svc = _service(tmp)
            try:
                self.assertTrue(svc.handle_meeting_start({})["ok"])
                svc._job_diar_window(svc._session)
            finally:
                svc.close()
            self.assertEqual(self.spy.dir_modes, [0o700])

    def test_symlink_at_target_name_is_not_followed(self):
        # O_EXCL|O_NOFOLLOW: подложенный симлинк с тем же именем не получает аудио.
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim.txt"
            victim.write_text("keep")
            svc = _service(tmp)
            try:
                self.assertTrue(svc.handle_meeting_start({})["ok"])
                d = Path(tmp) / "tmp_meeting"
                d.mkdir(exist_ok=True)
                import uuid as _uuid

                fixed = _uuid.UUID(int=1)
                (d / f"diar_{fixed.hex}.wav").symlink_to(victim)
                orig_uuid4 = mss.uuid.uuid4
                mss.uuid.uuid4 = lambda: fixed
                try:
                    svc._job_diar_window(svc._session)
                finally:
                    mss.uuid.uuid4 = orig_uuid4
            finally:
                svc.close()
            self.assertEqual(victim.read_text(), "keep")


class StaleSweep(_Base):
    def test_stale_diar_wavs_removed_on_fresh_start_other_files_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "tmp_meeting"
            d.mkdir()
            (d / "diar_aaaa.wav").write_bytes(b"RIFFold")
            (d / "diar_bbbb.wav").write_bytes(b"RIFFold")
            (d / "notes.txt").write_text("keep")
            svc = _service(tmp)
            try:
                self.assertTrue(svc.handle_meeting_start({})["ok"])
            finally:
                svc.close()
            self.assertEqual(sorted(p.name for p in d.iterdir()), ["notes.txt"])


if __name__ == "__main__":
    unittest.main()
