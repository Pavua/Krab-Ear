"""Тесты для AudioEngine._transcribe_remote (переход на backend.cloud_stt).

История: до этого фикса _transcribe_remote слал requests.post() на
settings.STT_GATEWAY_URL — тот же host:port, что и локальный OpenClaw
gateway (/v1/chat/completions), который НЕ реализует /v1/audio/transcriptions
→ гарантированный 404 на каждый вызов (18 вхождений "Ошибка Remote STT: 404"
в logs/krab-ear-rest.err.log). Фикс переиспользует уже захардненную
абстракцию backend.cloud_stt.get_cloud_stt_provider (openai/deepgram/
assemblyai) — тот же провайдер, что уже использует WS /v1/stream мост
Voice Gateway (backend/rest_server.py).
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TranscribeRemoteTestCase(unittest.TestCase):
    """_transcribe_remote должен работать и для str-путей, и для ndarray."""

    def setUp(self):
        from core.engine import AudioEngine
        from core.config import settings
        os.makedirs(str(settings.DATA_DIR), exist_ok=True)
        self.engine = AudioEngine()

    def _fake_provider(self, result: dict):
        provider = MagicMock()
        provider.transcribe.return_value = result
        return provider

    @patch("backend.cloud_stt.get_cloud_stt_provider")
    def test_ndarray_input_converts_to_pcm16_and_calls_provider(self, mock_get_provider):
        """np.ndarray (float32 16kHz) → PCM16 bytes → provider.transcribe → success."""
        import numpy as np

        provider = self._fake_provider({"text": "ok", "lang": "ru", "confidence": 0.9})
        mock_get_provider.return_value = provider

        audio = np.zeros(1600, dtype=np.float32)  # 0.1s @ 16kHz
        result = self.engine._transcribe_remote(audio, "test prompt")

        self.assertEqual(result["text"], "ok")
        self.assertEqual(result["engine"], "remote")
        mock_get_provider.assert_called_once_with("openai")  # default provider
        args, _ = provider.transcribe.call_args
        pcm_bytes, sample_rate, source_lang = args
        self.assertIsInstance(pcm_bytes, bytes)
        self.assertEqual(len(pcm_bytes), 1600 * 2)  # int16 = 2 bytes/sample
        self.assertEqual(sample_rate, 16000)
        self.assertEqual(source_lang, "ru")  # settings.TRANSCRIBE_LANGUAGE default

    @patch("backend.cloud_stt.get_cloud_stt_provider")
    def test_str_path_input_reads_native_sample_rate_via_soundfile(self, mock_get_provider):
        """Существующий use case со str-path: PCM16 читается через soundfile, sample_rate НЕ форсится в 16kHz."""
        import soundfile as sf
        import numpy as np
        import tempfile

        provider = self._fake_provider({"text": "file ok", "lang": "ru", "confidence": 1.0})
        mock_get_provider.return_value = provider

        # Записываем реальный WAV на 8kHz — проверяем что engine передаёт РЕАЛЬНЫЙ
        # sample_rate файла, а не хардкодит 16000.
        samples = np.zeros(800, dtype=np.int16)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            sf.write(tmp_path, samples, 8000, subtype="PCM_16")

            result = self.engine._transcribe_remote(tmp_path, "prompt")
            self.assertEqual(result["text"], "file ok")
            self.assertEqual(result["engine"], "remote")

            args, _ = provider.transcribe.call_args
            pcm_bytes, sample_rate, _source_lang = args
            self.assertEqual(sample_rate, 8000)
            self.assertEqual(len(pcm_bytes), 800 * 2)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @patch("backend.cloud_stt.get_cloud_stt_provider")
    def test_stereo_wav_downmixed_to_mono(self, mock_get_provider):
        """Многоканальный WAV сводится в моно перед отправкой провайдеру."""
        import soundfile as sf
        import numpy as np
        import tempfile

        provider = self._fake_provider({"text": "stereo ok"})
        mock_get_provider.return_value = provider

        stereo = np.zeros((400, 2), dtype=np.int16)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            sf.write(tmp_path, stereo, 16000, subtype="PCM_16")
            self.engine._transcribe_remote(tmp_path, "prompt")

            args, _ = provider.transcribe.call_args
            pcm_bytes, _sample_rate, _lang = args
            self.assertEqual(len(pcm_bytes), 400 * 2)  # mono, not 400*2*2
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def test_unsupported_type_raises_type_error(self):
        """Неподдерживаемый тип audio_data → TypeError c информативным сообщением."""
        with self.assertRaises(TypeError) as ctx:
            self.engine._transcribe_remote(12345, "prompt")
        self.assertIn("unsupported audio_data type", str(ctx.exception))
        self.assertIn("int", str(ctx.exception))

    @patch("backend.cloud_stt.get_cloud_stt_provider")
    def test_privacy_mode_enabled_blocks_before_provider_lookup(self, mock_get_provider):
        """privacy_mode_enabled=True → get_cloud_stt_provider НЕ вызывается вообще."""
        import numpy as np

        self.engine._settings_get = lambda k, d=None: True if k == "privacy_mode_enabled" else d

        with self.assertRaises(RuntimeError) as ctx:
            self.engine._transcribe_remote(np.zeros(1600, dtype=np.float32), "prompt")

        self.assertIn("privacy_mode_enabled", str(ctx.exception))
        mock_get_provider.assert_not_called()

    def test_unknown_provider_raises_runtime_error(self):
        """Неизвестное имя провайдера в cloud_stt_provider → понятный RuntimeError, без крэша."""
        import numpy as np

        self.engine._settings_get = lambda k, d=None: (
            "bogus_provider" if k == "cloud_stt_provider" else d
        )

        with self.assertRaises(RuntimeError) as ctx:
            self.engine._transcribe_remote(np.zeros(1600, dtype=np.float32), "prompt")

        self.assertIn("bogus_provider", str(ctx.exception))

    @patch("backend.cloud_stt.get_cloud_stt_provider")
    def test_provider_error_result_raises_runtime_error_not_crash(self, mock_get_provider):
        """Провайдер без API-ключа возвращает {"error": "no_api_key", ...} → RuntimeError, не падение."""
        import numpy as np

        provider = self._fake_provider({
            "error": "no_api_key",
            "provider": "openai",
            "message": "OpenAI API key is missing in settings",
        })
        mock_get_provider.return_value = provider

        with self.assertRaises(RuntimeError) as ctx:
            self.engine._transcribe_remote(np.zeros(1600, dtype=np.float32), "prompt")

        self.assertIn("no_api_key", str(ctx.exception))

    @patch("backend.privacy_audit.get_privacy_audit_logger")
    @patch("backend.cloud_stt.get_cloud_stt_provider")
    def test_privacy_audit_logged_on_success(self, mock_get_provider, mock_audit_logger):
        """Успешный cloud STT вызов пишет privacy-audit событие (аудио покинуло устройство)."""
        import numpy as np

        provider = self._fake_provider({"text": "ok"})
        mock_get_provider.return_value = provider
        fake_logger = MagicMock()
        mock_audit_logger.return_value = fake_logger

        self.engine._transcribe_remote(np.zeros(1600, dtype=np.float32), "prompt")

        fake_logger.log_event.assert_called_once()
        _, kwargs = fake_logger.log_event.call_args
        self.assertEqual(kwargs["category"], "cloud_stt")
        self.assertEqual(kwargs["action"], "cloud_stt_used")


class CloudSttSettingsTestCase(unittest.TestCase):
    """Настройки cloud_stt_provider (DEFAULT_SETTINGS + validator enum)."""

    def test_default_cloud_stt_provider_is_openai(self):
        from core.config import DEFAULT_SETTINGS
        self.assertEqual(DEFAULT_SETTINGS.get("cloud_stt_provider"), "openai")

    def test_cloud_stt_provider_enum_validated(self):
        from backend.settings_validator import _ENUM_FIELDS
        self.assertIn("cloud_stt_provider", _ENUM_FIELDS)
        self.assertEqual(
            _ENUM_FIELDS["cloud_stt_provider"], ("openai", "deepgram", "assemblyai")
        )

    def test_dead_gateway_settings_removed(self):
        """STT_GATEWAY_URL/STT_GATEWAY_TIMEOUT_SEC/STT_MODEL были мёртвым scaffold'ом — удалены."""
        from core.config import Settings
        s = Settings()
        self.assertFalse(hasattr(s, "STT_GATEWAY_URL"))
        self.assertFalse(hasattr(s, "STT_GATEWAY_TIMEOUT_SEC"))
        self.assertFalse(hasattr(s, "STT_MODEL"))


if __name__ == "__main__":
    unittest.main()
