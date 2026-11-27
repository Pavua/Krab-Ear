"""Спасательный WAV и privacy (ревью ChatGPT R-Krab-Ear-fix-02, RKE-F02-01/-07; 27.11.2026).

На base phase_c писал сырой голос в ``failed_recordings/`` БЕЗУСЛОВНО и оставлял его после
краха STT — при включённом privacy plaintext-аудио оседало на диске. Теперь:
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


if __name__ == "__main__":
    unittest.main()
