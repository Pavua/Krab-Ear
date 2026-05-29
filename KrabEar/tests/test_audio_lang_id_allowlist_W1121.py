"""Tests for AudioLanguageID supported-language allowlist (W1109 F3 MED).

Verifies:
- test_unsupported_lang_logged_but_returned: unsupported lang codes are returned
  but trigger a WARNING log with structured extra fields.
- test_restrict_mode_filters_unsupported: restrict_to_supported=True causes
  unsupported lang to be suppressed (returns None).
- test_supported_lang_passes_through: supported langs pass through silently.
"""

import os
import sys
import unittest
import logging

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class _FakeMLXWhisper:
    """Minimal stub that impersonates mlx_whisper for unit tests."""

    def __init__(self, lang: str, confidence: float = 0.9):
        self._lang = lang

        class _Audio:
            @staticmethod
            def log_mel_spectrogram(audio):
                return object()

        class _Decoding:
            def __init__(self, lang):
                self._lang = lang

            def detect_language(self, model, mel):
                return (self._lang, {self._lang: 0.9})

        class _LoadModels:
            @staticmethod
            def load_model(path):
                return object()

        self.audio = _Audio()
        self.decoding = _Decoding(lang)
        self.load_models = _LoadModels()


class TestAudioLangIDAllowlist(unittest.TestCase):

    def setUp(self):
        # Reset module-level model cache between tests
        from core.audio_lang_id import AudioLanguageID
        AudioLanguageID._model_cache.clear()

    def _make_lid(self, fake_lang: str, restrict: bool = False):
        """Return an AudioLanguageID whose _run_detect is monkey-patched."""
        from core.audio_lang_id import AudioLanguageID

        lid = AudioLanguageID(
            model_path="fake-model",
            preview_sec=5.0,
            restrict_to_supported=restrict,
        )
        # Monkey-patch _run_detect to avoid actual MLX dependency
        lid._run_detect = lambda audio: fake_lang  # type: ignore[method-assign]
        return lid

    def _make_long_audio(self):
        """Return a 2-second silent float32 array at 16 kHz."""
        import numpy as np
        return np.zeros(32000, dtype=np.float32)

    def test_supported_lang_passes_through(self):
        """Supported languages (ru/uk/en/es) are returned without warnings."""
        for lang in ("ru", "uk", "en", "es"):
            lid = self._make_lid(lang)

            warning_records = []

            class _WarnCapture(logging.Handler):
                def emit(self, record):
                    if record.levelno >= logging.WARNING:
                        warning_records.append(record)

            handler = _WarnCapture()
            handler.setLevel(logging.WARNING)
            log = logging.getLogger("KrabEar.AudioLanguageID")
            log.addHandler(handler)
            try:
                result = lid.detect(self._make_long_audio(), sample_rate=16000)
            finally:
                log.removeHandler(handler)

            self.assertEqual(result, lang, f"Expected {lang}, got {result}")
            unsupported_warns = [r for r in warning_records if "unsupported" in r.getMessage()]
            self.assertEqual(
                unsupported_warns, [],
                f"Should not warn for supported lang={lang}, got: {unsupported_warns}",
            )

    def test_unsupported_lang_logged_but_returned(self):
        """Unknown lang codes trigger WARNING with structured extra, but are still returned."""
        for lang in ("fr", "tr", "pt", "de", "zh"):
            lid = self._make_lid(lang)
            with self.assertLogs("KrabEar.AudioLanguageID", level="WARNING") as cm:
                result = lid.detect(self._make_long_audio(), sample_rate=16000)

            # Must still return the detected code — STTRouter decides what to do
            self.assertEqual(result, lang, f"Expected {lang} to be returned, got {result}")

            # At least one WARNING must mention the gap
            warning_msgs = [m for m in cm.output if "WARNING" in m and "unsupported" in m]
            self.assertTrue(
                len(warning_msgs) >= 1,
                f"Expected >=1 WARNING about unsupported lang={lang}, got: {cm.output}",
            )

    def test_restrict_mode_filters_unsupported(self):
        """restrict_to_supported=True: unsupported lang → None."""
        for lang in ("fr", "tr", "pt"):
            lid = self._make_lid(lang, restrict=True)
            result = lid.detect(self._make_long_audio(), sample_rate=16000)
            self.assertIsNone(
                result,
                f"restrict_to_supported=True should return None for lang={lang}, got {result}",
            )

    def test_restrict_mode_keeps_supported(self):
        """restrict_to_supported=True must NOT filter out supported languages."""
        for lang in ("ru", "en", "es", "uk"):
            lid = self._make_lid(lang, restrict=True)
            result = lid.detect(self._make_long_audio(), sample_rate=16000)
            self.assertEqual(
                result, lang,
                f"restrict_to_supported=True should keep lang={lang}, got {result}",
            )

    def test_supported_languages_constant(self):
        """SUPPORTED_LANGUAGES frozenset contains expected codes."""
        from core.audio_lang_id import SUPPORTED_LANGUAGES
        self.assertIsInstance(SUPPORTED_LANGUAGES, frozenset)
        self.assertIn("ru", SUPPORTED_LANGUAGES)
        self.assertIn("uk", SUPPORTED_LANGUAGES)
        self.assertIn("en", SUPPORTED_LANGUAGES)
        self.assertIn("es", SUPPORTED_LANGUAGES)
        # Unsupported ones must NOT be in the set
        self.assertNotIn("fr", SUPPORTED_LANGUAGES)
        self.assertNotIn("tr", SUPPORTED_LANGUAGES)

    def test_warning_has_structured_extra(self):
        """Warning log record carries detected_lang and fallback in extra fields."""
        from core.audio_lang_id import AudioLanguageID, SUPPORTED_LANGUAGES

        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        handler.setLevel(logging.WARNING)
        log = logging.getLogger("KrabEar.AudioLanguageID")
        log.addHandler(handler)
        try:
            lid = self._make_lid("fr")
            lid.detect(self._make_long_audio(), sample_rate=16000)
        finally:
            log.removeHandler(handler)

        warning_records = [r for r in records if r.levelno == logging.WARNING and "unsupported" in r.getMessage()]
        self.assertTrue(len(warning_records) >= 1, "Expected at least one WARNING record")
        rec = warning_records[0]
        self.assertEqual(getattr(rec, "detected_lang", None), "fr")
        self.assertEqual(getattr(rec, "fallback", None), "other")


    def test_uk_included_in_supported_languages(self):
        """Ukrainian (uk) is in SUPPORTED_LANGUAGES — W1581 canonical check.

        W1561 PR #1425 used {ru,es,en,de,fr,it,pt} which omitted Ukrainian.
        W1581 restores the W1121 contract: {ru,uk,en,es} — uk is critical.
        """
        from core.audio_lang_id import SUPPORTED_LANGUAGES
        self.assertIn(
            "uk", SUPPORTED_LANGUAGES,
            "Ukrainian 'uk' must be in SUPPORTED_LANGUAGES (W1121 contract, W1581 regression fix)",
        )
        # Also verify de/fr/it/pt are absent (they were the W1561 regression set)
        for code in ("de", "fr", "it", "pt"):
            self.assertNotIn(
                code, SUPPORTED_LANGUAGES,
                f"{code!r} must NOT be in SUPPORTED_LANGUAGES (W1561 regression codes)",
            )


if __name__ == "__main__":
    unittest.main()
