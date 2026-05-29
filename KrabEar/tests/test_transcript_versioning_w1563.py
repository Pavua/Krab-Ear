"""W1563: _MAX_TEXT_BYTES cap restored in transcript_versioning.

Tests:
- test_max_text_bytes_constant_present
- test_save_version_under_cap_succeeds
- test_save_version_over_cap_raises
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.transcript_versioning import (
    TranscriptVersionManager,
    _MAX_TEXT_BYTES,
)


class TestTranscriptVersioningW1563(unittest.TestCase):
    """W1563: _MAX_TEXT_BYTES size cap restored after W1497 cherry-pick revert."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # ------------------------------------------------------------------
    # test_max_text_bytes_constant_present
    # ------------------------------------------------------------------

    def test_max_text_bytes_constant_present(self) -> None:
        """_MAX_TEXT_BYTES is exported from transcript_versioning and equals 256 KB."""
        self.assertEqual(_MAX_TEXT_BYTES, 256 * 1024)

    # ------------------------------------------------------------------
    # test_save_version_under_cap_succeeds
    # ------------------------------------------------------------------

    def test_save_version_under_cap_succeeds(self) -> None:
        """save_version succeeds for text at or below _MAX_TEXT_BYTES."""
        # exactly at the cap boundary (ASCII chars, 1 byte each)
        text_at_cap = "x" * _MAX_TEXT_BYTES
        result = self.manager.save_version("item_at_cap", text_at_cap, "manual")
        self.assertIsNotNone(result)
        self.assertEqual(result["item_id"], "item_at_cap")
        self.assertEqual(result["version_num"], 1)
        self.assertEqual(len(result["text"]), _MAX_TEXT_BYTES)

        # well below the cap
        text_small = "Привет, мир!"
        result2 = self.manager.save_version("item_small", text_small, "stt_raw")
        self.assertIsNotNone(result2)
        self.assertEqual(result2["text"], text_small)

    # ------------------------------------------------------------------
    # test_save_version_over_cap_raises
    # ------------------------------------------------------------------

    def test_save_version_over_cap_raises(self) -> None:
        """save_version raises ValueError when text exceeds _MAX_TEXT_BYTES."""
        # one byte over the cap (ASCII, so len == byte size)
        oversized_text = "A" * (_MAX_TEXT_BYTES + 1)
        with self.assertRaises(ValueError) as ctx:
            self.manager.save_version("item_over", oversized_text, "manual")
        self.assertIn("_MAX_TEXT_BYTES", str(ctx.exception))

        # much larger (simulate a huge blob)
        huge_text = "B" * (_MAX_TEXT_BYTES * 2)
        with self.assertRaises(ValueError):
            self.manager.save_version("item_huge", huge_text, "llm_rewrite")

        # ensure nothing was persisted for the oversized items
        self.assertEqual(self.manager.get_versions("item_over"), [])
        self.assertEqual(self.manager.get_versions("item_huge"), [])


if __name__ == "__main__":
    unittest.main()
