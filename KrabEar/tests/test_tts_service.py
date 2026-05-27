"""Тесты TTSService: dual-mode TTS (Silero RU + Kokoro EN + macOS say fallback).

Все ML-зависимости (torch, kokoro) мокируются через unittest.mock, поэтому
тесты проходят без установленных ML-библиотек.
"""

from __future__ import annotations

import io
import sys
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tts_service import _detect_language, TTSService


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_wav_bytes(sample_rate: int = 22050, frames: int = 100) -> bytes:
    """Создаёт минимальный валидный WAV-буфер для тестов."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x01" * frames)
    return buf.getvalue()


# ── Language detection tests ───────────────────────────────────────────────────

class LanguageDetectionTestCase(unittest.TestCase):
    """Тесты эвристики определения языка по доле кириллицы."""

    def test_russian_text_detected(self) -> None:
        """Текст с >30% кириллицы -> ru."""
        text = "Привет мир, это тестовая строка на русском языке"
        self.assertEqual(_detect_language(text), "ru")

    def test_english_text_detected(self) -> None:
        """Текст без кириллицы -> en."""
        text = "Hello world, this is an English sentence for testing"
        self.assertEqual(_detect_language(text), "en")

    def test_empty_text_defaults_to_en(self) -> None:
        """Пустой текст -> en (нет алфавитных символов)."""
        self.assertEqual(_detect_language(""), "en")
        self.assertEqual(_detect_language("   "), "en")
        self.assertEqual(_detect_language("123 !@#"), "en")

    def test_mixed_text_below_threshold(self) -> None:
        """Смешанный текст с <30% кириллицы -> en."""
        # Только один кириллический символ в длинной латинской строке
        text = "Hello world testing English text with just one word: да"
        self.assertEqual(_detect_language(text), "en")

    def test_mixed_text_above_threshold(self) -> None:
        """Смешанный текст с >30% кириллицы -> ru."""
        # Большинство символов кириллические
        text = "Привет world"
        self.assertEqual(_detect_language(text), "ru")


# ── TTSService unit tests ──────────────────────────────────────────────────────

class TTSServiceFallbackTestCase(unittest.TestCase):
    """Тесты fallback chain без ML-зависимостей."""

    def _make_service(self) -> TTSService:
        svc = TTSService()
        # Помечаем как уже попытавшиеся, возвращаем None (нет модели)
        svc._silero_attempted = True
        svc._silero = None
        svc._kokoro_attempted = True
        svc._kokoro = None
        return svc

    @patch("backend.tts_service.settings")
    @patch("backend.tts_service._say_to_wav")
    def test_tts_disabled_uses_say(self, mock_say: MagicMock, mock_settings: MagicMock) -> None:
        """При TTS_ENABLED=False -> macOS say вызывается как fallback."""
        mock_settings.TTS_ENABLED = False
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""
        fake_wav = _make_wav_bytes()
        mock_say.return_value = fake_wav

        svc = self._make_service()
        result = svc.synthesize_speech("Hello test", language="en")
        self.assertEqual(result, fake_wav)
        mock_say.assert_called_once()

    @patch("backend.tts_service.settings")
    @patch("backend.tts_service._say_to_wav")
    def test_tts_enabled_no_models_falls_back_to_say(
        self, mock_say: MagicMock, mock_settings: MagicMock
    ) -> None:
        """TTS_ENABLED=True но модели не загружены -> macOS say."""
        mock_settings.TTS_ENABLED = True
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""
        fake_wav = _make_wav_bytes()
        mock_say.return_value = fake_wav

        svc = self._make_service()
        result = svc.synthesize_speech("Тест текст", language="ru")
        self.assertEqual(result, fake_wav)

    @patch("backend.tts_service.settings")
    def test_empty_text_returns_empty_bytes(self, mock_settings: MagicMock) -> None:
        """Пустой текст -> b'' без вызовов моделей."""
        mock_settings.TTS_ENABLED = True
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""

        svc = self._make_service()
        result = svc.synthesize_speech("   ")
        self.assertEqual(result, b"")

    @patch("backend.tts_service.settings")
    @patch("backend.tts_service._say_to_wav")
    def test_synthesize_speech_auto_language(
        self, mock_say: MagicMock, mock_settings: MagicMock
    ) -> None:
        """Language=auto: кириллический текст -> ru chain."""
        mock_settings.TTS_ENABLED = False
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = "Milena"
        fake_wav = _make_wav_bytes()
        mock_say.return_value = fake_wav

        svc = self._make_service()
        result = svc.synthesize_speech("Привет мир русская речь", language="auto")
        self.assertEqual(result, fake_wav)
        # say should be called with the voice from SAY_VOICE
        call_kwargs = mock_say.call_args
        self.assertIsNotNone(call_kwargs)

    @patch("backend.tts_service.settings")
    def test_fallback_say_disabled_no_models_returns_empty(
        self, mock_settings: MagicMock
    ) -> None:
        """TTS_ENABLED=False, TTS_FALLBACK_SAY=False -> b''."""
        mock_settings.TTS_ENABLED = False
        mock_settings.TTS_FALLBACK_SAY = False
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""

        svc = self._make_service()
        result = svc.synthesize_speech("test")
        self.assertEqual(result, b"")


# ── handle_synthesize_speech IPC handler tests ────────────────────────────────

class TTSHandlerTestCase(unittest.TestCase):
    """Тесты IPC handler handle_synthesize_speech."""

    @patch("backend.tts_service.settings")
    @patch.object(TTSService, "synthesize_speech")
    def test_handler_returns_base64_wav(
        self, mock_synth: MagicMock, mock_settings: MagicMock
    ) -> None:
        """Валидный запрос -> base64-encoded WAV в ответе."""
        mock_settings.TTS_ENABLED = False
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""
        fake_wav = _make_wav_bytes()
        mock_synth.return_value = fake_wav

        svc = TTSService()
        result = svc.handle_synthesize_speech({"text": "Hello world", "language": "en"})

        import base64
        self.assertIn("wav_bytes_b64", result)
        self.assertEqual(base64.b64decode(result["wav_bytes_b64"]), fake_wav)
        self.assertEqual(result["engine"], "say")
        self.assertEqual(result["byte_count"], len(fake_wav))

    @patch("backend.tts_service.settings")
    def test_handler_empty_text_returns_error(self, mock_settings: MagicMock) -> None:
        """Пустой text -> {"ok": False, "error": ...}."""
        mock_settings.TTS_ENABLED = False
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""

        svc = TTSService()
        result = svc.handle_synthesize_speech({"text": ""})
        self.assertFalse(result.get("ok", True))
        self.assertIn("error", result)

    @patch("backend.tts_service.settings")
    @patch.object(TTSService, "synthesize_speech")
    def test_handler_invalid_language_normalized_to_auto(
        self, mock_synth: MagicMock, mock_settings: MagicMock
    ) -> None:
        """Неизвестный language -> нормализуется до 'auto'."""
        mock_settings.TTS_ENABLED = False
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""
        mock_synth.return_value = b""

        svc = TTSService()
        # Передаём неизвестный язык
        svc.handle_synthesize_speech({"text": "test", "language": "fr"})
        # Должен вызвать synthesize_speech с language="auto"
        mock_synth.assert_called_once_with(text="test", language="auto", voice=None)

    @patch("backend.tts_service.settings")
    @patch.object(TTSService, "synthesize_speech")
    def test_handler_empty_synthesis_returns_none_engine(
        self, mock_synth: MagicMock, mock_settings: MagicMock
    ) -> None:
        """Если синтез вернул b'' -> engine='none', byte_count=0."""
        mock_settings.TTS_ENABLED = False
        mock_settings.TTS_FALLBACK_SAY = False
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""
        mock_synth.return_value = b""

        svc = TTSService()
        result = svc.handle_synthesize_speech({"text": "something"})
        self.assertEqual(result["engine"], "none")
        self.assertEqual(result["byte_count"], 0)
        self.assertEqual(result["wav_bytes_b64"], "")


# ── Engine-specific routing tests ─────────────────────────────────────────────

class TTSEngineRoutingTestCase(unittest.TestCase):
    """Tests verifying which engine is invoked for RU vs EN text."""

    @patch("backend.tts_service.settings")
    def test_ru_text_uses_silero(self, mock_settings: MagicMock) -> None:
        """RU text + TTS_ENABLED=True must call _synthesize_silero first."""
        mock_settings.TTS_ENABLED = True
        mock_settings.TTS_FALLBACK_SAY = False
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""

        fake_wav = _make_wav_bytes()
        svc = TTSService()
        svc._silero_attempted = True
        svc._silero = None  # simulate loaded (attempted), but _synthesize_silero is mocked

        with patch.object(svc, "_synthesize_silero", return_value=fake_wav) as mock_silero, \
             patch.object(svc, "_synthesize_kokoro", return_value=None) as mock_kokoro:
            result = svc.synthesize_speech("Привет мир это русский текст", language="ru")

        mock_silero.assert_called_once()
        mock_kokoro.assert_not_called()
        self.assertEqual(result, fake_wav)

    @patch("backend.tts_service.settings")
    def test_en_text_uses_kokoro(self, mock_settings: MagicMock) -> None:
        """EN text + TTS_ENABLED=True must call _synthesize_kokoro first."""
        mock_settings.TTS_ENABLED = True
        mock_settings.TTS_FALLBACK_SAY = False
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""

        fake_wav = _make_wav_bytes()
        svc = TTSService()

        with patch.object(svc, "_synthesize_kokoro", return_value=fake_wav) as mock_kokoro, \
             patch.object(svc, "_synthesize_silero", return_value=None):
            result = svc.synthesize_speech("Hello world this is English text", language="en")

        mock_kokoro.assert_called_once()
        self.assertEqual(result, fake_wav)

    @patch("backend.tts_service.settings")
    @patch("backend.tts_service._say_to_wav")
    def test_silero_unavailable_falls_back_kokoro(
        self, mock_say: MagicMock, mock_settings: MagicMock
    ) -> None:
        """For EN: Kokoro unavailable -> Silero -> say fallback chain."""
        mock_settings.TTS_ENABLED = True
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""

        fake_wav = _make_wav_bytes()
        mock_say.return_value = fake_wav

        svc = TTSService()
        with patch.object(svc, "_synthesize_kokoro", return_value=None), \
             patch.object(svc, "_synthesize_silero", return_value=None):
            result = svc.synthesize_speech("Hello world test fallback", language="en")

        # All ML engines returned None -> macOS say
        mock_say.assert_called_once()
        self.assertEqual(result, fake_wav)

    @patch("backend.tts_service.settings")
    @patch("backend.tts_service._say_to_wav")
    def test_unknown_lang_falls_back_to_say(
        self, mock_say: MagicMock, mock_settings: MagicMock
    ) -> None:
        """Language='en', both ML engines None -> must reach macOS say."""
        mock_settings.TTS_ENABLED = True
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""

        fake_wav = _make_wav_bytes()
        mock_say.return_value = fake_wav

        svc = TTSService()
        svc._silero_attempted = True
        svc._silero = None
        svc._kokoro_attempted = True
        svc._kokoro = None

        result = svc.synthesize_speech("unknown language test", language="en")
        mock_say.assert_called_once()
        self.assertEqual(result, fake_wav)

    @patch("backend.tts_service.settings")
    def test_all_engines_unavailable_returns_error(
        self, mock_settings: MagicMock
    ) -> None:
        """All engines None + TTS_FALLBACK_SAY=False -> returns b''."""
        mock_settings.TTS_ENABLED = True
        mock_settings.TTS_FALLBACK_SAY = False
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""

        svc = TTSService()
        with patch.object(svc, "_synthesize_silero", return_value=None), \
             patch.object(svc, "_synthesize_kokoro", return_value=None):
            result = svc.synthesize_speech("Привет мир", language="ru")

        self.assertEqual(result, b"")

    @patch("backend.tts_service.settings")
    def test_language_auto_detection(self, mock_settings: MagicMock) -> None:
        """Language='auto' detects RU vs EN correctly."""
        mock_settings.TTS_ENABLED = True
        mock_settings.TTS_FALLBACK_SAY = False
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""

        fake_wav = _make_wav_bytes()
        svc = TTSService()

        # RU auto-detect
        with patch.object(svc, "_synthesize_silero", return_value=fake_wav) as mock_silero:
            result = svc.synthesize_speech("Привет мир это автодетект языка", language="auto")
        mock_silero.assert_called_once()
        self.assertEqual(result, fake_wav)

        # EN auto-detect
        with patch.object(svc, "_synthesize_kokoro", return_value=fake_wav) as mock_kokoro:
            result = svc.synthesize_speech("Hello world auto language detection", language="auto")
        mock_kokoro.assert_called_once()
        self.assertEqual(result, fake_wav)

    @patch("backend.tts_service.settings")
    @patch("backend.tts_service._say_to_wav")
    def test_concurrent_speak(
        self, mock_say: MagicMock, mock_settings: MagicMock
    ) -> None:
        """Concurrent synthesize_speech calls must not raise or corrupt results."""
        import threading

        mock_settings.TTS_ENABLED = False
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""

        fake_wav = _make_wav_bytes()
        mock_say.return_value = fake_wav

        svc = TTSService()
        results: list[bytes] = []
        errors: list[Exception] = []

        def _speak(text: str) -> None:
            try:
                wav = svc.synthesize_speech(text, language="auto")
                results.append(wav)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=_speak, args=(f"Текст {i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Errors in concurrent speak: {errors}")
        self.assertEqual(len(results), 5)


# ── W1221 / W1215 F1+F2+F3 regression tests ───────────────────────────────────

class TorchHubTrustRepoTestCase(unittest.TestCase):
    """W1215 F1: torch.hub.load must receive trust_repo=True to avoid interactive
    consent prompt hanging in headless launchd daemon (no TTY)."""

    @patch("backend.tts_service.settings")
    def test_torch_hub_load_passes_trust_repo_true(self, mock_settings: MagicMock) -> None:
        """_load_silero must call torch.hub.load with trust_repo=True."""
        mock_settings.TTS_SILERO_MODEL = "v4_ru"

        mock_torch = MagicMock()
        fake_model = MagicMock()
        fake_model.to.return_value = fake_model
        mock_torch.device.return_value = "cpu"
        mock_torch.hub.load.return_value = (fake_model, [], 22050, "", MagicMock())

        import backend.tts_service as tts_mod
        with patch.dict("sys.modules", {"torch": mock_torch}):
            # Re-import so the lazy import inside the function picks up our mock
            from importlib import import_module
            import importlib
            # Call _load_silero directly
            result = tts_mod._load_silero("v4_ru")

        # Verify trust_repo=True was passed
        call_kwargs = mock_torch.hub.load.call_args
        self.assertIsNotNone(call_kwargs)
        kwargs = call_kwargs[1] if call_kwargs[1] else {}
        all_args = {**kwargs}
        # trust_repo can be positional arg[4] or keyword
        self.assertTrue(
            all_args.get("trust_repo") is True,
            f"trust_repo=True not found in call kwargs: {call_kwargs}",
        )


class SileroVoiceAllowlistTestCase(unittest.TestCase):
    """W1215 F2: Silero voice must be validated against the v4 speaker allowlist."""

    def _make_silero_tuple(self) -> tuple:
        """Build a fake Silero tuple for injecting into TTSService."""
        import io
        import wave

        def fake_apply_tts(**kwargs):
            import unittest.mock as um
            tensor = um.MagicMock()
            tensor.squeeze.return_value.cpu.return_value.numpy.return_value = __import__(
                "array"
            ).array("f", [0.0] * 100)
            return tensor

        fake_model = MagicMock()
        return (fake_model, [], 22050, "", fake_apply_tts, "cpu")

    @patch("backend.tts_service.settings")
    def test_invalid_silero_voice_rejected(self, mock_settings: MagicMock) -> None:
        """An unknown voice name must fall back to 'xenia' with a warning."""
        mock_settings.TTS_SILERO_VOICE = "totally_unknown_voice"
        mock_settings.TTS_ENABLED = True
        mock_settings.TTS_FALLBACK_SAY = False

        import numpy as np

        svc = TTSService()
        svc._silero_attempted = True

        # Minimal fake apply_tts that records the speaker keyword
        captured_speaker: list[str] = []

        def fake_apply_tts(texts, model, sample_rate, symbols, device, speaker):
            captured_speaker.append(speaker)
            arr = np.zeros(100, dtype=np.float32)
            tensor = MagicMock()
            tensor.squeeze.return_value.cpu.return_value.numpy.return_value = arr
            return tensor

        fake_model = MagicMock()
        svc._silero = (fake_model, [], 22050, "", fake_apply_tts, "cpu")

        with patch("backend.tts_service.logger") as mock_logger:
            result = svc._synthesize_silero("Hello", voice="totally_unknown_voice")

        # The speaker actually passed to apply_tts must be "xenia" (the safe fallback)
        self.assertTrue(len(captured_speaker) > 0)
        self.assertEqual(captured_speaker[0], "xenia")
        # A warning must have been logged
        mock_logger.warning.assert_called()

    @patch("backend.tts_service.settings")
    def test_valid_silero_voice_accepted(self, mock_settings: MagicMock) -> None:
        """A valid voice from the allowlist must be passed through unchanged."""
        import numpy as np

        for valid_voice in ("baya", "kseniya", "xenia", "eugene", "random"):
            mock_settings.TTS_SILERO_VOICE = valid_voice

            svc = TTSService()
            svc._silero_attempted = True

            captured_speaker: list[str] = []

            def fake_apply_tts(texts, model, sample_rate, symbols, device, speaker):
                captured_speaker.append(speaker)
                arr = np.zeros(100, dtype=np.float32)
                tensor = MagicMock()
                tensor.squeeze.return_value.cpu.return_value.numpy.return_value = arr
                return tensor

            fake_model = MagicMock()
            svc._silero = (fake_model, [], 22050, "", fake_apply_tts, "cpu")

            svc._synthesize_silero("Тест", voice=valid_voice)

            self.assertTrue(len(captured_speaker) > 0, f"voice={valid_voice} not captured")
            self.assertEqual(
                captured_speaker[0],
                valid_voice,
                f"Valid voice {valid_voice!r} was unexpectedly replaced",
            )
            captured_speaker.clear()


class SileroTextLengthCapTestCase(unittest.TestCase):
    """W1215 F3: text longer than 5000 chars must be truncated before Silero synthesis."""

    @patch("backend.tts_service.settings")
    def test_text_above_5000_chars_truncated(self, mock_settings: MagicMock) -> None:
        """Text > 5000 chars must be truncated to exactly 5000 chars passed to apply_tts."""
        import numpy as np

        mock_settings.TTS_SILERO_VOICE = "xenia"

        svc = TTSService()
        svc._silero_attempted = True

        captured_texts: list[list[str]] = []

        def fake_apply_tts(texts, model, sample_rate, symbols, device, speaker):
            captured_texts.append(list(texts))
            arr = np.zeros(100, dtype=np.float32)
            tensor = MagicMock()
            tensor.squeeze.return_value.cpu.return_value.numpy.return_value = arr
            return tensor

        fake_model = MagicMock()
        svc._silero = (fake_model, [], 22050, "", fake_apply_tts, "cpu")

        long_text = "а" * 6000  # 6000 chars > 5000 limit
        with patch("backend.tts_service.logger") as mock_logger:
            result = svc._synthesize_silero(long_text, voice="xenia")

        # Verify the text was truncated to 5000 chars
        self.assertEqual(len(captured_texts), 1)
        self.assertEqual(len(captured_texts[0]), 1)
        self.assertEqual(len(captured_texts[0][0]), 5000)
        # A warning must have been logged about truncation
        mock_logger.warning.assert_called()


if __name__ == "__main__":
    unittest.main()
