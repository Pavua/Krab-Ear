"""test_context_memory_redos_w18.py — regression tests for wave-18 ReDoS fix.

Guards:
  1. The previously-quadratic ('a-'*20000)+'1' input completes in <200ms.
  2. Legitimate technical tokens are still extracted correctly.
  3. MAX_NOTABLE_TEXT_LEN cap is applied.
"""

from __future__ import annotations

import sys
import os
import time
import unittest

# ── path setup ───────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.context_memory import (  # noqa: E402
    _extract_notable_words,
    MAX_NOTABLE_TEXT_LEN,
    _MAX_TECH_TOKEN_LEN,
)


class TestReDoSGuard(unittest.TestCase):
    """_RE_TECH must not exhibit quadratic backtracking on adversarial input."""

    def test_adversarial_input_completes_fast(self) -> None:
        """('a-'*20000)+'1' must complete in well under 200 ms."""
        adversarial = ("a-" * 20000) + "1"
        t0 = time.monotonic()
        _extract_notable_words(adversarial)
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.assertLess(
            elapsed_ms,
            200,
            f"_extract_notable_words took {elapsed_ms:.1f}ms on adversarial input — "
            "quadratic ReDoS regression!",
        )

    def test_adversarial_input_inside_normal_text_completes_fast(self) -> None:
        """Adversarial token embedded in normal sentence must also be fast."""
        adversarial_token = ("a-" * 500) + "1"
        text = f"Today we use Python3 and {adversarial_token} and qwen3-30b."
        t0 = time.monotonic()
        _extract_notable_words(text)
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.assertLess(
            elapsed_ms,
            200,
            f"_extract_notable_words took {elapsed_ms:.1f}ms — ReDoS regression!",
        )


class TestTechTermsStillMatch(unittest.TestCase):
    """Legitimate technical tokens must still be extracted after the fix."""

    def _extract(self, text: str) -> list:
        return _extract_notable_words(text)

    def test_qwen3(self) -> None:
        result = self._extract("using qwen3 model")
        self.assertIn("qwen3", result)

    def test_gpt4(self) -> None:
        result = self._extract("OpenAI gpt4 benchmark")
        self.assertIn("gpt4", result)

    def test_iphone13(self) -> None:
        result = self._extract("recorded on iPhone13")
        self.assertIn("iPhone13", result)

    def test_python3(self) -> None:
        result = self._extract("Python3 script runs fine")
        self.assertIn("Python3", result)

    def test_mlx4bit(self) -> None:
        result = self._extract("mlx4bit quantization")
        self.assertIn("mlx4bit", result)

    def test_v2(self) -> None:
        result = self._extract("version v2 released")
        self.assertIn("v2", result)

    def test_gpt4o(self) -> None:
        result = self._extract("GPT-4o supports vision")
        self.assertIn("GPT-4o", result)

    def test_qwen3_30b(self) -> None:
        result = self._extract("using qwen3-30b locally")
        self.assertIn("qwen3-30b", result)

    def test_mlx4(self) -> None:
        result = self._extract("MLX4 hardware acceleration")
        self.assertIn("MLX4", result)

    def test_mp3(self) -> None:
        result = self._extract("encoded as mp3 audio")
        self.assertIn("mp3", result)


class TestInputLengthCap(unittest.TestCase):
    """Input longer than MAX_NOTABLE_TEXT_LEN must be silently truncated."""

    def test_oversized_input_does_not_raise(self) -> None:
        big = "hello world " * 2000  # ~24 000 chars
        self.assertGreater(len(big), MAX_NOTABLE_TEXT_LEN)
        # Must not raise and must return something meaningful from first 8000 chars
        result = _extract_notable_words(big)
        self.assertIsInstance(result, list)

    def test_content_beyond_cap_is_ignored(self) -> None:
        """Words placed exclusively after MAX_NOTABLE_TEXT_LEN must not appear."""
        padding = "x " * (MAX_NOTABLE_TEXT_LEN // 2 + 10)  # just beyond cap
        text = padding + "Python3"
        self.assertGreater(len(text), MAX_NOTABLE_TEXT_LEN)
        result = _extract_notable_words(text)
        self.assertNotIn("Python3", result)

    def test_max_token_len_constant_is_reasonable(self) -> None:
        """_MAX_TECH_TOKEN_LEN must be set to a sane value (16–128)."""
        self.assertGreaterEqual(_MAX_TECH_TOKEN_LEN, 16)
        self.assertLessEqual(_MAX_TECH_TOKEN_LEN, 128)


class TestContextMemoryIntegration(unittest.TestCase):
    """ContextMemory.update() must handle adversarial input without hanging."""

    def test_update_with_adversarial_text(self) -> None:
        from core.context_memory import ContextMemory

        cm = ContextMemory(window_size=5)
        adversarial = ("a-" * 20000) + "1"
        t0 = time.monotonic()
        cm.update(adversarial)
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.assertLess(
            elapsed_ms,
            200,
            f"ContextMemory.update() took {elapsed_ms:.1f}ms — ReDoS regression!",
        )

    def test_update_then_get_context_words(self) -> None:
        from core.context_memory import ContextMemory

        cm = ContextMemory(window_size=5)
        cm.update("Using Python3 and qwen3-30b and mlx4bit today")
        words = cm.get_context_words(max_words=20)
        self.assertIsInstance(words, list)
        # At least one tech term should surface
        lowered = [w.lower() for w in words]
        self.assertTrue(
            any(t in lowered for t in ["python3", "qwen3-30b", "mlx4bit"]),
            f"No expected tech terms in context words: {words}",
        )


if __name__ == "__main__":
    unittest.main()
