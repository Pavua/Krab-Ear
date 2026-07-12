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
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tts_service import _detect_language, _sanitize_say_voice, TTSService


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


# ── macOS `say` voice sanitizer tests ──────────────────────────────────────────

class SayVoiceSanitizeTestCase(unittest.TestCase):
    """_sanitize_say_voice: accept accented voice names, reject unsafe input.

    Regression: the prior ASCII-only regex r"^[a-zA-Z0-9 _\\-]+$" rejected the
    Spanish voice 'Mónica' (accented 'ó') → silently fell back to the Russian
    'Milena' voice for ES TTS via macOS `say`.
    """

    def test_accented_latin_voice_preserved(self) -> None:
        # Fail-before: old regex rejected 'ó' → returned 'Milena'.
        self.assertEqual(_sanitize_say_voice("Mónica"), "Mónica")
        self.assertEqual(_sanitize_say_voice("Mónica (Enhanced)"), "Mónica (Enhanced)")

    def test_plain_ascii_voices_preserved(self) -> None:
        for v in ("Milena", "Anna", "Yuna", "Daniel", "Eddy (English (US))"):
            self.assertEqual(_sanitize_say_voice(v), v)

    def test_unsafe_voice_falls_back_to_default(self) -> None:
        # empty, newline, shell-meta, NUL, command-subst, over-64-length
        for bad in ("", "a\nb", "foo;rm -rf /", "v\x00x", "$(whoami)", "a" * 65):
            self.assertEqual(_sanitize_say_voice(bad), "Milena")


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


# ── fix/tts-ru-accent-routing regression: RU say-fallback voice + no-Kokoro ────

class RuSayFallbackVoiceTestCase(unittest.TestCase):
    """2026-07-12 incident: ConversationErrorAnnouncer speaks RU text with a
    noticeable foreign accent. Root cause found: for RU text falling back to
    macOS `say` (Silero unavailable, or TTS_ENABLED=False -- both true in the
    reported prod launchd config, which had no TTS_ENABLED env var set),
    ``synthesize_speech`` called ``_say_to_wav`` with ``voice=None`` whenever
    neither the IPC caller nor ``settings.SAY_VOICE`` supplied a voice.
    ``_say_to_wav``'s ``if voice:`` guard then skips the ``-v`` flag entirely,
    so macOS `say` speaks with the SYSTEM DEFAULT voice (commonly English on
    this machine), not the intended RU voice ``_SAY_DEFAULT_VOICE="Milena"``
    -- exactly matching the reported symptom. The Kokoro-before-say hypothesis
    (EN engine getting RU text) was checked and is NOT present: the RU branch
    of ``synthesize_speech`` never calls ``_synthesize_kokoro``. Fix: the say
    fallback now defaults an unset voice to ``_SAY_DEFAULT_VOICE`` for RU
    text specifically, without touching EN behaviour or an explicit
    SAY_VOICE/voice override.
    """

    def _make_service(self) -> TTSService:
        svc = TTSService()
        svc._silero_attempted = True
        svc._silero = None
        svc._kokoro_attempted = True
        svc._kokoro = None
        return svc

    @patch("backend.tts_service.settings")
    @patch("backend.tts_service._say_to_wav")
    def test_ru_say_fallback_defaults_to_milena_when_tts_disabled(
        self, mock_say: MagicMock, mock_settings: MagicMock
    ) -> None:
        """Prod launchd config (TTS_ENABLED unset -> False), SAY_VOICE unset,
        no explicit voice (exactly ConversationErrorAnnouncer's call shape):
        RU text via say MUST use the Milena RU voice, not the macOS system
        default voice (often English)."""
        mock_settings.TTS_ENABLED = False
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""
        fake_wav = _make_wav_bytes()
        mock_say.return_value = fake_wav

        svc = self._make_service()
        result = svc.synthesize_speech("Голосовой шлюз недоступен.", language="ru")

        self.assertEqual(result, fake_wav)
        mock_say.assert_called_once()
        _call_args, call_kwargs = mock_say.call_args
        self.assertEqual(
            call_kwargs.get("voice"),
            "Milena",
            "RU say-fallback must default to the Milena RU voice when no "
            "voice/SAY_VOICE is configured — got "
            f"{call_kwargs.get('voice')!r} (system default, likely non-RU, "
            "explains the reported foreign accent)",
        )

    @patch("backend.tts_service.settings")
    @patch("backend.tts_service._say_to_wav")
    def test_ru_say_fallback_defaults_to_milena_when_silero_unavailable(
        self, mock_say: MagicMock, mock_settings: MagicMock
    ) -> None:
        """TTS_ENABLED=True but Silero failed to load (or timed out): the
        same Milena default must apply on the say fallback for RU text."""
        mock_settings.TTS_ENABLED = True
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""
        fake_wav = _make_wav_bytes()
        mock_say.return_value = fake_wav

        svc = self._make_service()
        result = svc.synthesize_speech("Связь с голосовым шлюзом потеряна.", language="ru")

        self.assertEqual(result, fake_wav)
        _call_args, call_kwargs = mock_say.call_args
        self.assertEqual(call_kwargs.get("voice"), "Milena")

    @patch("backend.tts_service.settings")
    @patch("backend.tts_service._say_to_wav")
    def test_ru_say_fallback_respects_explicit_say_voice_setting(
        self, mock_say: MagicMock, mock_settings: MagicMock
    ) -> None:
        """If the user explicitly configured SAY_VOICE, it must win over the
        Milena default — the fix must not override a deliberate user choice."""
        mock_settings.TTS_ENABLED = False
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = "Yuri"
        fake_wav = _make_wav_bytes()
        mock_say.return_value = fake_wav

        svc = self._make_service()
        svc.synthesize_speech("Произошла ошибка.", language="ru")

        _call_args, call_kwargs = mock_say.call_args
        self.assertEqual(call_kwargs.get("voice"), "Yuri")

    @patch("backend.tts_service.settings")
    @patch("backend.tts_service._say_to_wav")
    def test_en_say_fallback_voice_selection_unaffected(
        self, mock_say: MagicMock, mock_settings: MagicMock
    ) -> None:
        """The RU-only Milena default must NOT leak into the EN say-fallback
        path — EN text with no voice configured still passes voice=None
        (macOS system default) through unchanged."""
        mock_settings.TTS_ENABLED = False
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""
        fake_wav = _make_wav_bytes()
        mock_say.return_value = fake_wav

        svc = self._make_service()
        svc.synthesize_speech("Hello, this is English text.", language="en")

        _call_args, call_kwargs = mock_say.call_args
        self.assertIsNone(call_kwargs.get("voice"))

    @patch("backend.tts_service.settings")
    def test_ru_text_never_reaches_kokoro(self, mock_settings: MagicMock) -> None:
        """Contract regression (task b): Kokoro (EN-only phonemizer) must
        NEVER receive RU text, regardless of Silero availability. Mocks
        Silero unavailable + Kokoro available/would-succeed to prove the RU
        branch structurally cannot call it -- verified NOT already broken,
        kept as a permanent contract guard."""
        mock_settings.TTS_ENABLED = True
        mock_settings.TTS_FALLBACK_SAY = True
        mock_settings.TTS_SILERO_MODEL = "v4_ru"
        mock_settings.TTS_SILERO_VOICE = "baya"
        mock_settings.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
        mock_settings.SAY_VOICE = ""

        fake_wav = _make_wav_bytes()
        svc = self._make_service()

        with patch.object(svc, "_synthesize_silero", return_value=None) as mock_silero, \
             patch.object(svc, "_synthesize_kokoro", return_value=fake_wav) as mock_kokoro, \
             patch("backend.tts_service._say_to_wav", return_value=fake_wav):
            svc.synthesize_speech("Привет, это русский текст для регрессии.", language="ru")

        mock_silero.assert_called_once()
        mock_kokoro.assert_not_called()


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
            tts_mod._load_silero("v4_ru")

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


# ── Silero v4 version-agnostic load/synthesis regression (fix/tts-silero-v4-api) ──

class SileroV4TwoTupleLoadTestCase(unittest.TestCase):
    """Прод-баг (найден живым прогоном 2026-07-09; эталон фикса --
    wake_word_models/train_krab.py::_load_silero_tts, commit e89e6e37):
    _load_silero распаковывал результат torch.hub.load как ЛЕГАСИ 5-кортеж
    ``(model, symbols, sample_rate, example_text, apply_tts)``. Дефолтный
    TTS_SILERO_MODEL="v4_ru" (core/config.py) на современных пакетах
    snakers4/silero-models возвращает 2-кортеж ``(model, example_text)`` --
    синтез идёт МЕТОДОМ ``model.apply_tts(...)``. До фикса КАЖДАЯ загрузка
    падала ``ValueError: not enough values to unpack (expected 5, got 2)``
    внутри загрузочного треда (перехватывается, возвращает None) -- заявленный
    primary RU TTS-движок был фактически мёртв, прод тихо деградировал на
    macOS `say` без единого явного сообщения об ошибке в логах пользователя.
    """

    def _mock_torch_with_hub_result(self, hub_result: Any) -> MagicMock:
        mock_torch = MagicMock()
        mock_torch.device.return_value = "cpu"
        mock_torch.hub.load.return_value = hub_result
        return mock_torch

    @patch("backend.tts_service.settings")
    def test_two_tuple_hub_result_does_not_raise(self, mock_settings: MagicMock) -> None:
        """Fail-before: 2-кортеж (как реальный v4_ru) ломал unpacking -> None."""
        fake_model = MagicMock()
        fake_model.to.return_value = fake_model
        mock_torch = self._mock_torch_with_hub_result((fake_model, "example text"))

        import backend.tts_service as tts_mod
        with patch.dict("sys.modules", {"torch": mock_torch}):
            result = tts_mod._load_silero("v4_ru")

        self.assertIsNotNone(
            result,
            "_load_silero вернул None для валидного 2-кортежа v4_ru -- должен "
            "успешно построить v4-контекст (было: ValueError not enough values "
            "to unpack, expected 5, got 2, перехваченный внутри loader-треда)",
        )

    @patch("backend.tts_service.settings")
    def test_v4_model_to_returning_none_keeps_original_model(self, mock_settings: MagicMock) -> None:
        """Второй слой прод-бага (живая загрузка v4_ru 2026-07-09): v4-обёртка
        Silero -- НЕ nn.Module, её .to(device) двигает модель IN-PLACE и
        возвращает None. Переприсваивание ``_model = _model.to(_device)``
        обнуляло модель -- ctx['model'] становился None, синтез падал.
        MagicMock это скрывал (.to() у него truthy), поэтому мок здесь
        воспроизводит реальное поведение обёртки: .to() -> None."""
        fake_model = MagicMock()
        fake_model.to.return_value = None  # реальное поведение v4-обёртки
        mock_torch = self._mock_torch_with_hub_result((fake_model, "example text"))

        import backend.tts_service as tts_mod
        with patch.dict("sys.modules", {"torch": mock_torch}):
            result = tts_mod._load_silero("v4_ru")

        self.assertIsNotNone(result)
        self.assertIs(
            result["model"], fake_model,
            "ctx['model'] обязан остаться исходным объектом модели, когда "
            ".to(device) вернул None (in-place move v4-обёртки)",
        )

    @patch("backend.tts_service.settings")
    def test_two_tuple_hub_result_trust_repo_still_passed(self, mock_settings: MagicMock) -> None:
        """W1215 F1 регрессия: version-agnostic ветка тоже обязана передавать trust_repo=True."""
        fake_model = MagicMock()
        fake_model.to.return_value = fake_model
        mock_torch = self._mock_torch_with_hub_result((fake_model, "example text"))

        import backend.tts_service as tts_mod
        with patch.dict("sys.modules", {"torch": mock_torch}):
            tts_mod._load_silero("v4_ru")

        call_kwargs = mock_torch.hub.load.call_args
        self.assertIsNotNone(call_kwargs)
        kwargs = call_kwargs[1] if call_kwargs[1] else {}
        self.assertTrue(kwargs.get("trust_repo") is True)

    @patch("backend.tts_service.settings")
    def test_five_tuple_legacy_hub_result_still_works(self, mock_settings: MagicMock) -> None:
        """Регрессия: старые per-speaker пакеты (легаси 5-кортеж) не должны сломаться."""
        fake_model = MagicMock()
        fake_model.to.return_value = fake_model
        hub_result = (fake_model, ["a", "b"], 22050, "example", MagicMock())
        mock_torch = self._mock_torch_with_hub_result(hub_result)

        import backend.tts_service as tts_mod
        with patch.dict("sys.modules", {"torch": mock_torch}):
            result = tts_mod._load_silero("v3_1_ru")

        self.assertIsNotNone(result)


class SileroV4SynthesisTestCase(unittest.TestCase):
    """Синтез на v4-контексте (2-кортеж загрузки) должен вызывать МЕТОД
    ``model.apply_tts(text=..., speaker=..., sample_rate=...)`` -- НЕ свободную
    функцию 5-аргументного легаси apply_tts. Мирроит
    wake_word_models/train_krab.py::_synthesize_one (v4-ветка)."""

    @patch("backend.tts_service.settings")
    def test_v4_synthesis_calls_model_apply_tts_method(self, mock_settings: MagicMock) -> None:
        """model.apply_tts должен получить text=, speaker=, sample_rate= kwargs."""
        import numpy as np

        mock_settings.TTS_SILERO_VOICE = "baya"

        captured_kwargs: list[dict] = []

        def fake_model_apply_tts(**kwargs):
            captured_kwargs.append(kwargs)
            tensor = MagicMock()
            tensor.squeeze.return_value.cpu.return_value.numpy.return_value = np.zeros(
                100, dtype=np.float32
            )
            return tensor

        fake_model = MagicMock()
        fake_model.apply_tts.side_effect = fake_model_apply_tts

        svc = TTSService()
        svc._silero_attempted = True
        svc._silero = {"api": "v4", "model": fake_model, "sample_rate": 24000, "device": "cpu"}

        result = svc._synthesize_silero("Привет", voice="baya")

        self.assertTrue(len(captured_kwargs) > 0, "model.apply_tts не был вызван")
        self.assertEqual(captured_kwargs[0].get("speaker"), "baya")
        self.assertEqual(captured_kwargs[0].get("sample_rate"), 24000)
        self.assertEqual(captured_kwargs[0].get("text"), "Привет")
        self.assertIsNotNone(result)

    @patch("backend.tts_service.settings")
    def test_v4_synthesis_returns_wav_with_correct_sample_rate(
        self, mock_settings: MagicMock
    ) -> None:
        """WAV-заголовок должен нести реальный sample_rate v4-контекста (24000)."""
        import numpy as np

        mock_settings.TTS_SILERO_VOICE = "baya"

        def fake_model_apply_tts(**kwargs):
            tensor = MagicMock()
            tensor.squeeze.return_value.cpu.return_value.numpy.return_value = np.zeros(
                100, dtype=np.float32
            )
            return tensor

        fake_model = MagicMock()
        fake_model.apply_tts.side_effect = fake_model_apply_tts

        svc = TTSService()
        svc._silero_attempted = True
        svc._silero = {"api": "v4", "model": fake_model, "sample_rate": 24000, "device": "cpu"}

        result = svc._synthesize_silero("Тест", voice="baya")

        self.assertIsNotNone(result)
        with wave.open(io.BytesIO(result), "rb") as wf:
            self.assertEqual(wf.getframerate(), 24000)

    @patch("backend.tts_service.settings")
    def test_v4_synthesis_validates_voice_allowlist(self, mock_settings: MagicMock) -> None:
        """W1215 F2 должен сохраняться и на v4-ветке: неизвестный голос -> 'xenia'."""
        import numpy as np

        mock_settings.TTS_SILERO_VOICE = "baya"

        captured_kwargs: list[dict] = []

        def fake_model_apply_tts(**kwargs):
            captured_kwargs.append(kwargs)
            tensor = MagicMock()
            tensor.squeeze.return_value.cpu.return_value.numpy.return_value = np.zeros(
                100, dtype=np.float32
            )
            return tensor

        fake_model = MagicMock()
        fake_model.apply_tts.side_effect = fake_model_apply_tts

        svc = TTSService()
        svc._silero_attempted = True
        svc._silero = {"api": "v4", "model": fake_model, "sample_rate": 24000, "device": "cpu"}

        with patch("backend.tts_service.logger") as mock_logger:
            svc._synthesize_silero("Привет", voice="totally_unknown_voice")

        self.assertEqual(captured_kwargs[0].get("speaker"), "xenia")
        mock_logger.warning.assert_called()


class SileroVoiceAllowlistTestCase(unittest.TestCase):
    """W1215 F2: Silero voice must be validated against the v4 speaker allowlist.

    Uses the legacy dict-context shape (``api="legacy"``) to exercise the
    free-function ``apply_tts(texts=, model=, sample_rate=, symbols=,
    device=, speaker=)`` call signature -- see SileroV4SynthesisTestCase for
    the equivalent v4 (``model.apply_tts`` method) coverage.
    """

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
        svc._silero = {
            "api": "legacy", "model": fake_model, "symbols": [],
            "sample_rate": 22050, "apply_tts": fake_apply_tts, "device": "cpu",
        }

        with patch("backend.tts_service.logger") as mock_logger:
            result = svc._synthesize_silero("Hello", voice="totally_unknown_voice")

        # The speaker actually passed to apply_tts must be "xenia" (the safe fallback)
        self.assertTrue(len(captured_speaker) > 0)
        self.assertEqual(captured_speaker[0], "xenia")
        # A warning must have been logged
        mock_logger.warning.assert_called()
        self.assertIsNotNone(result)

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
            svc._silero = {
                "api": "legacy", "model": fake_model, "symbols": [],
                "sample_rate": 22050, "apply_tts": fake_apply_tts, "device": "cpu",
            }

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
        svc._silero = {
            "api": "legacy", "model": fake_model, "symbols": [],
            "sample_rate": 22050, "apply_tts": fake_apply_tts, "device": "cpu",
        }

        long_text = "а" * 6000  # 6000 chars > 5000 limit
        with patch("backend.tts_service.logger") as mock_logger:
            result = svc._synthesize_silero(long_text, voice="xenia")

        # Verify the text was truncated to 5000 chars
        self.assertEqual(len(captured_texts), 1)
        self.assertEqual(len(captured_texts[0]), 1)
        self.assertEqual(len(captured_texts[0][0]), 5000)
        # A warning must have been logged about truncation
        mock_logger.warning.assert_called()
        self.assertIsNotNone(result)


# ── W1739 security regression: say option-injection via text argument ─────────


class SayOptionInjectionRegressionTestCase(unittest.TestCase):
    """W1739: _say_to_wav must insert '--' before user text so that say(1) never
    parses the text as command-line options.

    Without the fix, text='--input-file=/etc/passwd' causes say to read and
    synthesize an arbitrary local file (confirmed exploitable: 55 MB AIFF from
    /etc/passwd).  The '--' end-of-options sentinel blocks this.
    """

    def _call_say_to_wav(self, text: str) -> list:
        """Run _say_to_wav with subprocess.run mocked; return the captured argv."""
        import backend.tts_service as tts_mod

        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured.append(list(cmd))
            result = MagicMock()
            result.returncode = 0
            return result

        # Also mock os.path.exists / os.path.getsize so the WAV branch is taken
        with patch("backend.tts_service.subprocess.run", side_effect=fake_run), \
             patch("backend.tts_service.os.path.exists", return_value=True), \
             patch("backend.tts_service.os.path.getsize", return_value=1024), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"RIFF")):
            try:
                tts_mod._say_to_wav(text)
            except Exception:  # noqa: BLE001
                pass  # WAV read may fail — we only care about the argv

        # First call is 'say', second is 'afconvert'
        return captured[0] if captured else []

    def test_option_like_text_preceded_by_double_dash(self) -> None:
        """text='--input-file=/etc/passwd' must appear after '--' in say argv."""
        malicious_text = "--input-file=/etc/passwd"
        argv = self._call_say_to_wav(malicious_text)

        self.assertIn("say", argv, "say must be the command")
        self.assertIn("--", argv, "end-of-options '--' sentinel must be present in argv")

        dash_dash_index = argv.index("--")
        text_index = argv.index(malicious_text)
        self.assertGreater(
            text_index,
            dash_dash_index,
            f"text must come AFTER '--': argv={argv}",
        )

    def test_short_option_like_text_preceded_by_double_dash(self) -> None:
        """text='-o/tmp/x.aiff' must also appear after '--' in say argv."""
        malicious_text = "-o/tmp/x.aiff"
        argv = self._call_say_to_wav(malicious_text)

        self.assertIn("--", argv, "end-of-options '--' sentinel must be present in argv")
        dash_dash_index = argv.index("--")
        text_index = argv.index(malicious_text)
        self.assertGreater(
            text_index,
            dash_dash_index,
            f"text must come AFTER '--': argv={argv}",
        )

    def test_normal_text_still_preceded_by_double_dash(self) -> None:
        """Normal text must also be placed after '--' (sentinel always present)."""
        normal_text = "Hello, this is normal speech"
        argv = self._call_say_to_wav(normal_text)

        self.assertIn("--", argv, "end-of-options '--' sentinel must always be present")
        dash_dash_index = argv.index("--")
        text_index = argv.index(normal_text)
        self.assertGreater(
            text_index,
            dash_dash_index,
            f"text must come AFTER '--': argv={argv}",
        )

    def test_double_dash_flag_text_preceded_by_double_dash(self) -> None:
        """text='--foo' must appear after '--' so say never interprets it as --foo."""
        malicious_text = "--foo"
        argv = self._call_say_to_wav(malicious_text)

        self.assertIn("--", argv, "end-of-options '--' sentinel must be present")
        # Find the *first* '--' (our sentinel) — it must precede the text
        first_dd = next(i for i, a in enumerate(argv) if a == "--")
        # The text "--foo" appears after the first "--"
        self.assertIn(malicious_text, argv[first_dd + 1:],
                      f"'--foo' must appear after sentinel '--': argv={argv}")


class SaySubprocessTimeoutTestCase(unittest.TestCase):
    """W1758 MED: _say_to_wav должна передавать timeout= в обе subprocess.run и обрезать текст.

    До фикса: subprocess.run вызывались без timeout= → при входе ~1 MB (IPC cap)
    say/afconvert висели бесконечно, блокируя daemon-поток (local DoS).
    Фикс: timeout=_SAY_SUBPROCESS_TIMEOUT / _AFCONVERT_TIMEOUT + text cap _SAY_MAX_TEXT_LEN.
    """

    def _captured_say_argv(self, text: str) -> tuple[list[list], dict]:
        """Запускает _say_to_wav с мок subprocess.run; возвращает (список argv, kwargs первого вызова)."""
        import backend.tts_service as tts_mod

        captured_calls: list[tuple[list, dict]] = []

        def fake_run(cmd, **kwargs):
            captured_calls.append((list(cmd), kwargs))
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("backend.tts_service.subprocess.run", side_effect=fake_run), \
             patch("backend.tts_service.os.path.exists", return_value=True), \
             patch("backend.tts_service.os.path.getsize", return_value=1024), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"RIFF")):
            try:
                tts_mod._say_to_wav(text)
            except Exception:  # noqa: BLE001
                pass

        return captured_calls

    def test_say_subprocess_receives_timeout_kwarg(self) -> None:
        """say вызов должен получить kwarg timeout=."""
        calls = self._captured_say_argv("Привет мир")
        self.assertTrue(len(calls) >= 1, "subprocess.run должен быть вызван хотя бы один раз")
        # Первый вызов — say
        say_argv, say_kwargs = calls[0]
        self.assertIn("say", say_argv, "первый вызов должен быть 'say'")
        self.assertIn("timeout", say_kwargs, "say subprocess.run должен иметь timeout= kwarg")
        self.assertIsNotNone(say_kwargs["timeout"])
        self.assertGreater(say_kwargs["timeout"], 0)

    def test_afconvert_subprocess_receives_timeout_kwarg(self) -> None:
        """afconvert вызов должен получить kwarg timeout=."""
        calls = self._captured_say_argv("Hello world")
        # Второй вызов — afconvert
        if len(calls) < 2:
            self.skipTest("afconvert не был вызван (say упал?)")
        afconvert_argv, afconvert_kwargs = calls[1]
        self.assertIn("afconvert", afconvert_argv, "второй вызов должен быть 'afconvert'")
        self.assertIn("timeout", afconvert_kwargs, "afconvert subprocess.run должен иметь timeout= kwarg")
        self.assertIsNotNone(afconvert_kwargs["timeout"])
        self.assertGreater(afconvert_kwargs["timeout"], 0)

    def test_say_timeout_expired_returns_empty_bytes(self) -> None:
        """При TimeoutExpired в say: возвращается b'', temp-файлы чистятся (finally)."""
        import backend.tts_service as tts_mod
        import subprocess as real_subprocess

        call_count = [0]

        def fake_run_timeout(cmd, **kwargs):
            call_count[0] += 1
            if "say" in cmd:
                raise real_subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("backend.tts_service.subprocess.run", side_effect=fake_run_timeout), \
             patch("backend.tts_service.os.path.exists", return_value=False), \
             patch("backend.tts_service.os.unlink", return_value=None):
            result = tts_mod._say_to_wav("Тест таймаут")

        self.assertEqual(result, b"", "TimeoutExpired в say должен вернуть b''")

    def test_afconvert_timeout_expired_returns_empty_bytes(self) -> None:
        """При TimeoutExpired в afconvert: возвращается b''."""
        import backend.tts_service as tts_mod
        import subprocess as real_subprocess

        def fake_run_afconvert_timeout(cmd, **kwargs):
            if "afconvert" in cmd:
                raise real_subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("backend.tts_service.subprocess.run", side_effect=fake_run_afconvert_timeout), \
             patch("backend.tts_service.os.path.exists", return_value=False), \
             patch("backend.tts_service.os.unlink", return_value=None):
            result = tts_mod._say_to_wav("Тест afconvert timeout")

        self.assertEqual(result, b"", "TimeoutExpired в afconvert должен вернуть b''")

    def test_say_text_cap_truncates_long_text(self) -> None:
        """Текст длиннее _SAY_MAX_TEXT_LEN обрезается до лимита перед передачей в say."""
        import backend.tts_service as tts_mod

        captured_argv: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured_argv.append(list(cmd))
            result = MagicMock()
            result.returncode = 0
            return result

        long_text = "а" * (tts_mod._SAY_MAX_TEXT_LEN + 1000)

        with patch("backend.tts_service.subprocess.run", side_effect=fake_run), \
             patch("backend.tts_service.os.path.exists", return_value=True), \
             patch("backend.tts_service.os.path.getsize", return_value=1024), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"RIFF")):
            try:
                tts_mod._say_to_wav(long_text)
            except Exception:  # noqa: BLE001
                pass

        self.assertTrue(len(captured_argv) >= 1, "subprocess.run должен быть вызван")
        say_argv = captured_argv[0]
        self.assertIn("say", say_argv)
        # Текст передаётся последним аргументом (после --)
        double_dash_idx = say_argv.index("--")
        actual_text = say_argv[double_dash_idx + 1]
        self.assertLessEqual(
            len(actual_text),
            tts_mod._SAY_MAX_TEXT_LEN,
            f"текст не обрезан: len={len(actual_text)} > {tts_mod._SAY_MAX_TEXT_LEN}",
        )

    def test_say_text_at_exact_limit_not_truncated(self) -> None:
        """Текст ровно _SAY_MAX_TEXT_LEN символов НЕ обрезается."""
        import backend.tts_service as tts_mod

        captured_argv: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured_argv.append(list(cmd))
            result = MagicMock()
            result.returncode = 0
            return result

        exact_text = "б" * tts_mod._SAY_MAX_TEXT_LEN

        with patch("backend.tts_service.subprocess.run", side_effect=fake_run), \
             patch("backend.tts_service.os.path.exists", return_value=True), \
             patch("backend.tts_service.os.path.getsize", return_value=1024), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"RIFF")):
            try:
                tts_mod._say_to_wav(exact_text)
            except Exception:  # noqa: BLE001
                pass

        self.assertTrue(len(captured_argv) >= 1)
        say_argv = captured_argv[0]
        double_dash_idx = say_argv.index("--")
        actual_text = say_argv[double_dash_idx + 1]
        self.assertEqual(
            len(actual_text),
            tts_mod._SAY_MAX_TEXT_LEN,
            "текст ровно на лимите не должен обрезаться",
        )


if __name__ == "__main__":
    unittest.main()
