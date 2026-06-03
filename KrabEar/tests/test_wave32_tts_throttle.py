"""wave-32: TTS voice validation + max text length + synthesize_speech HEAVY throttle.

Tests:
  D1a — Kokoro voice param with path traversal / invalid chars → rejected → default voice used
  D1b — text > MAX_TTS_TEXT_LEN in handle_synthesize_speech → error before synthesis
  D2  — synthesize_speech is in HEAVY_METHODS (≤5/min bucket)

No ML libs required — all synthesis paths are mocked.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tts_service import (
    MAX_TTS_TEXT_LEN,
    TTSService,
    _KOKORO_DEFAULT_VOICE,
    _KOKORO_VOICE_RE,
)
from backend.ipc_throttle import (
    HEAVY_METHODS,
    IPCThrottle,
    _classify_method,
)


# ---------------------------------------------------------------------------
# D1a — Kokoro voice validation
# ---------------------------------------------------------------------------

class KokoroVoiceValidationTestCase(unittest.TestCase):
    """_synthesize_kokoro rejects invalid voice names and falls back to default."""

    def _make_service_with_kokoro(self) -> tuple[TTSService, list]:
        """Return a TTSService with a fake kokoro pipeline and a call log."""
        svc = TTSService()
        call_log: list = []

        def fake_pipeline(text, voice=None):
            call_log.append(voice)
            # Yield nothing so all_samples stays empty — result is None (not a crash)
            return iter([])

        svc._kokoro = fake_pipeline
        svc._kokoro_attempted = True
        return svc, call_log

    def test_valid_voice_passed_through(self):
        svc, call_log = self._make_service_with_kokoro()
        svc._synthesize_kokoro("hello world", voice="af_sky")
        self.assertEqual(call_log, ["af_sky"])

    def test_path_traversal_voice_rejected(self):
        """../../etc/passwd is not a valid Kokoro voice name → default used."""
        svc, call_log = self._make_service_with_kokoro()
        svc._synthesize_kokoro("hello", voice="../../etc/passwd")
        self.assertEqual(len(call_log), 1)
        self.assertEqual(call_log[0], _KOKORO_DEFAULT_VOICE)

    def test_uppercase_voice_rejected(self):
        """Uppercase start is not allowed by the regex."""
        svc, call_log = self._make_service_with_kokoro()
        svc._synthesize_kokoro("hello", voice="AF_sky")
        self.assertEqual(call_log[0], _KOKORO_DEFAULT_VOICE)

    def test_space_in_voice_rejected(self):
        """Spaces not allowed."""
        svc, call_log = self._make_service_with_kokoro()
        svc._synthesize_kokoro("hello", voice="af sky")
        self.assertEqual(call_log[0], _KOKORO_DEFAULT_VOICE)

    def test_semicolon_injection_rejected(self):
        """Shell-injection attempt → rejected."""
        svc, call_log = self._make_service_with_kokoro()
        svc._synthesize_kokoro("hello", voice="af_sky; rm -rf /")
        self.assertEqual(call_log[0], _KOKORO_DEFAULT_VOICE)

    def test_none_voice_uses_default(self):
        """None voice should use _KOKORO_DEFAULT_VOICE."""
        svc, call_log = self._make_service_with_kokoro()
        svc._synthesize_kokoro("hello", voice=None)
        self.assertEqual(call_log[0], _KOKORO_DEFAULT_VOICE)

    def test_valid_voices_regex(self):
        """Spot-check a few real Kokoro voice names against the compiled regex."""
        valid = ["af_sky", "bf_emma", "am_adam", "af_heart", "en_us_m1", "a1"]
        for v in valid:
            self.assertIsNotNone(_KOKORO_VOICE_RE.match(v), f"Should be valid: {v!r}")

    def test_invalid_voices_regex(self):
        """Names that must NOT match the Kokoro voice regex."""
        invalid = [
            "../../etc/passwd",
            "AF_sky",          # uppercase start
            "af sky",          # space
            "_af_sky",         # leading underscore
            "",                # empty
            "af-sky!",         # exclamation mark
        ]
        for v in invalid:
            self.assertIsNone(_KOKORO_VOICE_RE.match(v), f"Should be invalid: {v!r}")


# ---------------------------------------------------------------------------
# D1b — MAX_TTS_TEXT_LEN enforced in handle_synthesize_speech
# ---------------------------------------------------------------------------

class HandleSynthesizeSpeechTextLenTestCase(unittest.TestCase):
    """handle_synthesize_speech returns error for text > MAX_TTS_TEXT_LEN."""

    def setUp(self):
        self.svc = TTSService()

    def test_text_exactly_at_limit_accepted(self):
        """Text of exactly MAX_TTS_TEXT_LEN chars passes the guard."""
        text = "a" * MAX_TTS_TEXT_LEN
        # synthesize_speech is called but returns b"" because TTS_ENABLED=False
        # and we don't want subprocess side-effects — patch say
        with patch("backend.tts_service._say_to_wav", return_value=b""):
            result = self.svc.handle_synthesize_speech({"text": text})
        # Should NOT be an error from the length guard
        self.assertNotIn("exceeds maximum length", result.get("error", ""))

    def test_text_one_over_limit_rejected(self):
        """Text of MAX_TTS_TEXT_LEN + 1 chars is rejected before synthesis."""
        text = "a" * (MAX_TTS_TEXT_LEN + 1)
        with patch("backend.tts_service._say_to_wav") as mock_say:
            result = self.svc.handle_synthesize_speech({"text": text})
            mock_say.assert_not_called()  # synthesis must NOT be reached
        self.assertFalse(result.get("ok", True))
        self.assertIn("exceeds maximum length", result["error"])

    def test_large_text_rejected(self):
        """50 000-char text is cleanly rejected with an informative error."""
        text = "x" * 50_000
        with patch("backend.tts_service._say_to_wav") as mock_say:
            result = self.svc.handle_synthesize_speech({"text": text})
            mock_say.assert_not_called()
        self.assertFalse(result.get("ok", True))
        self.assertIn(str(MAX_TTS_TEXT_LEN), result["error"])

    def test_empty_text_rejected(self):
        """Empty text still returns existing 'text is required' error."""
        result = self.svc.handle_synthesize_speech({"text": ""})
        self.assertFalse(result.get("ok", True))
        self.assertIn("text is required", result["error"])

    def test_normal_text_reaches_synthesis(self):
        """Short text bypasses the length guard and reaches synthesis."""
        text = "Hello world"
        with patch("backend.tts_service._say_to_wav", return_value=b""):
            result = self.svc.handle_synthesize_speech({"text": text})
        # Length guard must NOT fire
        self.assertNotIn("exceeds maximum length", result.get("error", ""))


# ---------------------------------------------------------------------------
# D2 — synthesize_speech in HEAVY_METHODS
# ---------------------------------------------------------------------------

class SynthesizeSpeechThrottleTestCase(unittest.TestCase):
    """synthesize_speech must be in HEAVY_METHODS, not light."""

    def test_synthesize_speech_in_heavy_set(self):
        self.assertIn("synthesize_speech", HEAVY_METHODS)

    def test_synthesize_speech_classified_heavy(self):
        self.assertEqual(_classify_method("synthesize_speech"), "heavy")

    def test_synthesize_speech_not_in_medium(self):
        from backend.ipc_throttle import MEDIUM_METHODS
        self.assertNotIn("synthesize_speech", MEDIUM_METHODS)

    def test_heavy_bucket_limit_is_lower_than_light(self):
        """heavy bucket has smaller capacity than light — confirming synthesis is costly."""
        throttle = IPCThrottle()
        # Drain the heavy bucket for synthesize_speech
        allowed = sum(1 for _ in range(200) if throttle.check_rate("synthesize_speech"))
        # With capacity=5, only 5 should get through initially
        self.assertLessEqual(allowed, 5)

    def test_synthesize_speech_rate_limited_after_burst(self):
        """After exhausting the heavy bucket, further calls are rejected."""
        throttle = IPCThrottle()
        # Consume all 5 tokens
        for _ in range(5):
            throttle.check_rate("synthesize_speech")
        # Next call must be throttled
        allowed = throttle.check_rate("synthesize_speech")
        self.assertFalse(allowed)

    def test_ping_still_excluded(self):
        """Sanity: ping remains in EXCLUDED_METHODS (always allowed)."""
        from backend.ipc_throttle import EXCLUDED_METHODS
        self.assertIn("ping", EXCLUDED_METHODS)
        throttle = IPCThrottle()
        for _ in range(1000):
            self.assertTrue(throttle.check_rate("ping"))


if __name__ == "__main__":
    unittest.main()
