"""Спасательный WAV и privacy (ревью ChatGPT R-Krab-Ear-fix-02, RKE-F02-01/-07; 27.11.2026).

На base phase_c писал сырой голос в ``failed_recordings/`` БЕЗУСЛОВНО и оставлял его после
краха STT. Теперь (ТОЛЬКО эта отдельная копия; R1-спилл ``rescue/`` под privacy остаётся
осознанно по решению владельца 27.11.2026 — см. ``TestRescueSpillPolicyUnderPrivacy``):
  - privacy ON ⇒ (неизвестное состояние — fail-closed ``_privacy_mode_enabled``, тестируется отдельно)
     спасательная копия не создаётся;
  - privacy включили, пока STT работал, и он упал ⇒ файл удаляется, путь не отдаётся;
  - ``failed_recordings`` — симлинк ⇒ запись за пределы data_dir не идёт.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_phase_c_audio_presave import _make_service, _ObservingTranscriber  # noqa: E402
from tests.test_phase_c_stt_crash_recovery_W1177 import _FakeSettingsSvc  # noqa: E402


class _PrivacySettings(_FakeSettingsSvc):
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def cached_settings(self):
        return {"privacy_mode_enabled": self.enabled}


class _Crashing:
    def transcribe(self, audio, **kwargs):
        raise RuntimeError("stt crash")


def _wavs(data_dir: Path) -> list[Path]:
    return sorted((data_dir / "failed_recordings").glob("*.wav"))


class TestRecoveryWavPrivacy(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_privacy_on_writes_no_recovery_wav_before_stt(self):
        transcriber = _ObservingTranscriber(Path(self._tmp))
        svc, store = _make_service(self._tmp, transcriber, settings_svc=_PrivacySettings(True))
        svc.handle_start_recording({})
        svc.handle_stop_recording({})
        self.assertEqual(transcriber.seen_files, [], "под privacy сырое аудио не должно лежать на диске")
        self.assertEqual(_wavs(Path(store.data_dir)), [])

    def test_privacy_on_stt_crash_leaves_no_wav_and_no_path(self):
        svc, store = _make_service(self._tmp, _Crashing(), settings_svc=_PrivacySettings(True))
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({})
        self.assertIsNone(result.get("audio_recovery_path"))
        self.assertEqual(_wavs(Path(store.data_dir)), [])

    def test_privacy_enabled_while_stt_runs_then_crash_removes_wav(self):
        settings = _PrivacySettings(False)

        class _EnablePrivacyThenCrash:
            def transcribe(_self, audio, **kwargs):
                # файл уже на диске (privacy был OFF), затем владелец включает privacy и STT падает
                self.assertEqual(len(_wavs(Path(self._tmp))), 1)
                settings.enabled = True
                raise RuntimeError("crash after privacy enabled")

        svc, store = _make_service(self._tmp, _EnablePrivacyThenCrash(), settings_svc=settings)
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({})
        self.assertIsNone(result.get("audio_recovery_path"))
        self.assertEqual(_wavs(Path(store.data_dir)), [], "plaintext WAV пережил включение privacy")

    def test_privacy_off_crash_still_keeps_recovery_wav(self):
        """Регресс W1177: без privacy спасательная копия по-прежнему отдаётся."""
        svc, store = _make_service(self._tmp, _Crashing(), settings_svc=_PrivacySettings(False))
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({})
        rel = result.get("audio_recovery_path")
        self.assertIsNotNone(rel)
        self.assertTrue((Path(store.data_dir) / rel).exists())

    def test_failed_recordings_symlink_is_not_followed(self):
        outside = Path(tempfile.mkdtemp())
        data_dir = Path(self._tmp)
        (data_dir / "failed_recordings").symlink_to(outside, target_is_directory=True)
        svc, _store = _make_service(self._tmp, _Crashing(), settings_svc=_PrivacySettings(False))
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({})
        self.assertEqual(list(outside.iterdir()), [], "запись ушла по симлинку за пределы data_dir")
        self.assertIsNone(result.get("audio_recovery_path"))


class TestRescueSpillPolicyUnderPrivacy(unittest.TestCase):
    """Политика владельца (27.11.2026): под privacy при stt_failed R1-спилл и восстановленный WAV
    остаются на диске ОСОЗНАННО (для восстановления), транскрипция не запускается. Тест фиксирует
    это поведение, чтобы дрейф в любую сторону не прошёл молча (ревью Opus #2307, P1)."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self._tmp = self._tmp_ctx.name
        self.addCleanup(self._tmp_ctx.cleanup)
        self.rescue_dir = Path(self._tmp) / "rescue"

    def _stop_with_failed_stt(self, privacy: bool):
        from tests.test_recording_spill_wiring import _FakeRecorder, _RaisingTranscriber, _make_service as _mk
        recorder = _FakeRecorder()
        svc = _mk(
            self._tmp, self.rescue_dir, recorder=recorder, transcriber=_RaisingTranscriber(),
            settings_overrides={"recording_spill_enabled": True, "privacy_mode_enabled": privacy},
        )
        svc.handle_start_recording({})
        self.assertIsNotNone(recorder.received_spill, "предусловие: рекордер получил spill")
        return svc.handle_stop_recording({})

    def test_privacy_on_stt_crash_keeps_spill_deliberately(self):
        result = self._stop_with_failed_stt(privacy=True)
        self.assertEqual(result.get("status"), "stt_failed")
        self.assertTrue(list(self.rescue_dir.glob("*.part")), "спилл должен остаться для восстановления")
        self.assertIsNone(result.get("audio_recovery_path"), "копия failed_recordings под privacy не создаётся")

    def test_privacy_on_rescan_keeps_wav_and_does_not_transcribe(self):
        from backend.recording_rescue import run_rescue_scan
        from tests.test_recording_rescue import FakeCollectionManager, FakeErrorBus, FakeRecordingCore, _seed_part
        _seed_part(self.rescue_dir)
        core = FakeRecordingCore()
        result = run_rescue_scan(
            rescue_dir=self.rescue_dir, recording_core=core, error_bus=FakeErrorBus(),
            settings_get=lambda key, default=None: True if key == "privacy_mode_enabled" else default,
            collection_manager=FakeCollectionManager(),
        )
        self.assertEqual(result, {"rescued": 1, "transcribed": 0, "kept_wavs": 1})
        self.assertEqual(core.calls, [], "под privacy транскрипция рескана не запускается")
        self.assertTrue(list(self.rescue_dir.glob("*.rescued.wav")))

    def test_privacy_off_stt_crash_keeps_spill(self):
        result = self._stop_with_failed_stt(privacy=False)
        self.assertEqual(result.get("status"), "stt_failed")
        self.assertTrue(list(self.rescue_dir.glob("*.part")))


if __name__ == "__main__":
    unittest.main()
