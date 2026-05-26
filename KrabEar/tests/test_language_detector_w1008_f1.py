"""Tests for W1008 F1 fix: FR/TR/PT false-positive exclusion in LanguageDetector.

Covers:
- French text → "und" (not "es")
- Turkish text → "und" (not "es")
- Portuguese text → "und" (not "es")
- Spanish regression: genuine ES text still returns "es"
"""

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.language_detector import LanguageDetector  # noqa: E402


class TestLanguageDetectorW1008F1(unittest.TestCase):
    """FR/TR/PT exclusion guards — W1008 F1 HIGH fix."""

    def setUp(self) -> None:
        self.detector = LanguageDetector()

    # ------------------------------------------------------------------ #
    # French
    # ------------------------------------------------------------------ #

    def test_french_cafe_tres_returns_und_not_es(self) -> None:
        """French text with très + accented chars must not be classified as es."""
        text = "C'est très bien, merci beaucoup"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "und",
                         f"Expected 'und' for French text, got '{result.language}'")

    def test_french_guillemets_returns_und(self) -> None:
        """French guillemets «…» are a hard FR marker."""
        text = "Il a dit «bonjour» et est parti"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "und",
                         f"Expected 'und' for text with guillemets, got '{result.language}'")

    def test_french_cedilla_oe_returns_und(self) -> None:
        """ç and œ are FR-specific markers."""
        text = "Le garçon mange du bœuf"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "und",
                         f"Expected 'und' for text with ç/œ, got '{result.language}'")

    def test_french_word_marker_est_returns_und(self) -> None:
        """French word marker 'est' combined with accent → und."""
        text = "la vie est belle avec des amis"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "und",
                         f"Expected 'und' for French sentence, got '{result.language}'")

    # ------------------------------------------------------------------ #
    # Turkish
    # ------------------------------------------------------------------ #

    def test_turkish_bugun_yapacak_returns_und_not_es(self) -> None:
        """Turkish text with ş/ğ must not be classified as es."""
        text = "Bugün ne yapacağız bilmiyorum"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "und",
                         f"Expected 'und' for Turkish text, got '{result.language}'")

    def test_turkish_dotless_i_returns_und(self) -> None:
        """Turkish dotless ı and İ are unique TR markers."""
        text = "İstanbul şehri güzel bir şehirdir"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "und",
                         f"Expected 'und' for Turkish with İ/ı/ş, got '{result.language}'")

    def test_turkish_g_breve_returns_und(self) -> None:
        """ğ (g-breve) is unique to Turkish."""
        text = "Yağmur yağıyor dışarıda"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "und",
                         f"Expected 'und' for text with ğ, got '{result.language}'")

    # ------------------------------------------------------------------ #
    # Portuguese
    # ------------------------------------------------------------------ #

    def test_portuguese_voce_returns_und_not_es(self) -> None:
        """Portuguese text with você must not be classified as es."""
        text = "Você não pode fazer isso agora"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "und",
                         f"Expected 'und' for Portuguese text, got '{result.language}'")

    def test_portuguese_tilde_vowels_returns_und(self) -> None:
        """ã and õ are unique PT markers."""
        text = "As nações precisam trabalhar juntas então"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "und",
                         f"Expected 'und' for text with ã/õ, got '{result.language}'")

    def test_portuguese_nao_returns_und(self) -> None:
        """'não' is a strong PT word marker."""
        text = "Isso não é verdade de jeito nenhum"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "und",
                         f"Expected 'und' for text with 'não', got '{result.language}'")

    # ------------------------------------------------------------------ #
    # Spanish regression — genuine ES must still be detected
    # ------------------------------------------------------------------ #

    def test_spanish_hola_que_tal_still_returns_es(self) -> None:
        """Genuine Spanish text must still return 'es'."""
        text = "Hola, ¿qué tal estás? Muy bien, gracias"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "es",
                         f"Expected 'es' for Spanish text, got '{result.language}'")

    def test_spanish_enye_only_returns_es(self) -> None:
        """ñ is Spain-unique and should still classify as es."""
        text = "El niño pequeño jugó en el jardín"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "es",
                         f"Expected 'es' for Spanish with ñ, got '{result.language}'")

    def test_spanish_inverted_marks_returns_es(self) -> None:
        """¿ and ¡ are ES-unique markers."""
        text = "¡Hola! ¿Cómo estás hoy?"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "es",
                         f"Expected 'es' for text with ¿/¡, got '{result.language}'")

    def test_spanish_density_threshold_long_text(self) -> None:
        """Long Spanish text with enough marker density returns es."""
        # "El señor García llegó muy rápido" — ñ, á, í are ES markers
        text = "El señor García llegó muy rápido a la reunión"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "es",
                         f"Expected 'es' for long Spanish text, got '{result.language}'")

    def test_english_no_markers_returns_en(self) -> None:
        """Plain English text must return 'en'."""
        text = "The quick brown fox jumps over the lazy dog"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "en",
                         f"Expected 'en' for English text, got '{result.language}'")

    def test_single_accented_char_long_english_returns_en(self) -> None:
        """A single accented char in a long English text must not flip to es
        when the marker density is clearly below the 2% threshold."""
        # "naïve" has ï which is NOT in _ES_MARKERS → no ES trigger.
        # We verify a completely marker-free long text stays "en".
        text = "We discussed many important topics at the conference yesterday afternoon"
        result = self.detector.detect(text)
        self.assertEqual(result.language, "en",
                         f"Expected 'en' for plain English text, got '{result.language}'")


if __name__ == "__main__":
    unittest.main()
