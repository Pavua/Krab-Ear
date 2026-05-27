"""Tests for handle_register_speaker embedding size + finiteness validation (W1227 F2 MED).

Covers:
  - Oversized embedding rejected
  - NaN value rejected
  - Inf value rejected
  - Non-numeric element rejected
  - Valid 512-dim embedding accepted
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

# Path setup for standalone and pytest runs
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.speaker_manager import SpeakerManager, _MAX_EMBEDDING_FLOATS  # noqa: E402


class TestRegisterSpeakerEmbeddingValidation(unittest.TestCase):
    """handle_register_speaker — input validation guards (W1227 F2 MED)."""

    def setUp(self) -> None:
        # data_dir=None → in-memory only, no disk I/O needed
        self.mgr = SpeakerManager()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _valid_embedding(self, dim: int = 512) -> list[float]:
        """Return a list of `dim` finite floats (unit vector)."""
        val = 1.0 / math.sqrt(dim)
        return [val] * dim

    def _call(self, embedding, name: str = "TestSpeaker") -> dict:
        return self.mgr.handle_register_speaker({"name": name, "embedding": embedding})

    # ------------------------------------------------------------------ #
    # oversized embedding
    # ------------------------------------------------------------------ #
    def test_register_speaker_rejects_oversized_embedding(self) -> None:
        """Embedding with more than _MAX_EMBEDDING_FLOATS elements must raise ValueError."""
        oversized = self._valid_embedding(dim=_MAX_EMBEDDING_FLOATS + 1)
        with self.assertRaises(ValueError) as ctx:
            self._call(oversized)
        self.assertIn(str(_MAX_EMBEDDING_FLOATS), str(ctx.exception))

    def test_register_speaker_rejects_exactly_oversized_by_one(self) -> None:
        """Boundary: _MAX_EMBEDDING_FLOATS + 1 must be rejected."""
        bad = [0.1] * (_MAX_EMBEDDING_FLOATS + 1)
        with self.assertRaises(ValueError):
            self._call(bad)

    def test_register_speaker_accepts_max_boundary(self) -> None:
        """Boundary: exactly _MAX_EMBEDDING_FLOATS elements must be accepted."""
        boundary = self._valid_embedding(dim=_MAX_EMBEDDING_FLOATS)
        result = self._call(boundary)
        self.assertIn("speaker_id", result)

    def test_register_speaker_rejects_million_floats(self) -> None:
        """A million-element embedding must be rejected fast (DoS guard)."""
        huge = [0.0001] * 1_000_000
        with self.assertRaises(ValueError):
            self._call(huge)

    # ------------------------------------------------------------------ #
    # NaN
    # ------------------------------------------------------------------ #
    def test_register_speaker_rejects_nan_in_embedding(self) -> None:
        """Embedding containing float('nan') must raise ValueError."""
        bad = self._valid_embedding()
        bad[0] = float("nan")
        with self.assertRaises(ValueError) as ctx:
            self._call(bad)
        msg = str(ctx.exception).lower()
        self.assertTrue(
            "nan" in msg or "конечн" in msg or "finite" in msg,
            f"Expected message about nan/finite, got: {ctx.exception}",
        )

    def test_register_speaker_rejects_nan_at_last_position(self) -> None:
        """NaN at the end of the embedding must also be caught."""
        bad = self._valid_embedding()
        bad[-1] = float("nan")
        with self.assertRaises(ValueError):
            self._call(bad)

    # ------------------------------------------------------------------ #
    # Inf
    # ------------------------------------------------------------------ #
    def test_register_speaker_rejects_inf_in_embedding(self) -> None:
        """Embedding containing float('inf') must raise ValueError."""
        bad = self._valid_embedding()
        bad[5] = float("inf")
        with self.assertRaises(ValueError) as ctx:
            self._call(bad)
        msg = str(ctx.exception).lower()
        self.assertTrue(
            "inf" in msg or "конечн" in msg or "finite" in msg,
            f"Expected message about inf/finite, got: {ctx.exception}",
        )

    def test_register_speaker_rejects_neg_inf_in_embedding(self) -> None:
        """Embedding containing float('-inf') must raise ValueError."""
        bad = self._valid_embedding()
        bad[10] = float("-inf")
        with self.assertRaises(ValueError):
            self._call(bad)

    # ------------------------------------------------------------------ #
    # Non-numeric elements
    # ------------------------------------------------------------------ #
    def test_register_speaker_rejects_non_numeric_string(self) -> None:
        """Embedding containing a string element must raise ValueError."""
        bad = self._valid_embedding()
        bad[3] = "oops"  # type: ignore[assignment]
        with self.assertRaises(ValueError) as ctx:
            self._call(bad)
        self.assertIn("3", str(ctx.exception))

    def test_register_speaker_rejects_non_numeric_dict(self) -> None:
        """Embedding containing a dict element must raise ValueError."""
        bad: list = self._valid_embedding()
        bad[7] = {"x": 1.0}  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            self._call(bad)

    def test_register_speaker_rejects_non_numeric_list(self) -> None:
        """Embedding containing a nested list element must raise ValueError."""
        bad: list = self._valid_embedding()
        bad[2] = [0.1, 0.2]  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            self._call(bad)

    def test_register_speaker_rejects_non_numeric_none(self) -> None:
        """Embedding containing None element must raise ValueError."""
        bad: list = self._valid_embedding()
        bad[1] = None  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            self._call(bad)

    def test_register_speaker_rejects_non_numeric_bool(self) -> None:
        """Embedding containing a bare bool must raise ValueError (bool is int subclass guard)."""
        bad: list = self._valid_embedding()
        bad[0] = True  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            self._call(bad)

    def test_register_speaker_rejects_non_numeric_in_embedding(self) -> None:
        """Named test matching task spec: non-numeric element rejected."""
        bad: list = self._valid_embedding()
        bad[50] = "bad_value"  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            self._call(bad)

    # ------------------------------------------------------------------ #
    # valid path
    # ------------------------------------------------------------------ #
    def test_register_speaker_accepts_valid_512_dim(self) -> None:
        """Standard 512-dim pyannote embedding must be accepted and return a speaker_id."""
        emb = self._valid_embedding(dim=512)
        result = self._call(emb, name="ValidSpeaker")
        self.assertIn("speaker_id", result)
        self.assertTrue(result["speaker_id"].startswith("Speaker_"))
        self.assertEqual(result["name"], "ValidSpeaker")

    def test_register_speaker_accepts_integer_elements(self) -> None:
        """Embedding with integer values (valid numeric type) must be accepted."""
        emb = [1] * 512  # integers are valid numeric
        # Note: bools are excluded but plain ints are fine
        result = self._call(emb)
        self.assertIn("speaker_id", result)

    def test_register_speaker_accepts_mixed_int_float(self) -> None:
        """Embedding mixing ints and floats must be accepted."""
        emb: list = [0.5] * 256 + [1] * 256
        result = self._call(emb)
        self.assertIn("speaker_id", result)

    def test_register_speaker_empty_embedding_rejected(self) -> None:
        """Empty embedding list must raise ValueError (pre-existing guard)."""
        with self.assertRaises(ValueError):
            self._call([])


if __name__ == "__main__":
    unittest.main()
