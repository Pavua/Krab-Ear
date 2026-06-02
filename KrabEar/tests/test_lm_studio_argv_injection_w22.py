"""Wave-22 — test_lm_studio_argv_injection_w22.py

MED flag-injection guard for _try_cli() in lm_studio_lifecycle.py.

Attack vector: model_id set via IPC set_settings (attacker-controlled).
  - "--all" as model_id → lms unload --all → unloads ALL loaded models.
Mitigation: leading-dash / unsafe-charset rejection + POSIX "--" separator.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.lm_studio_lifecycle import _try_cli  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _proc(rc: int = 0) -> MagicMock:
    p = MagicMock()
    p.returncode = rc
    return p


SAFE_LMS = "/usr/local/bin/lms"
SAFE_MODEL = "qwen3.6-35b-a3b"
DASH_ALL = "--all"
SINGLE_DASH = "-v"


# ---------------------------------------------------------------------------
# Wave-22.1: rejection of leading-dash model IDs
# ---------------------------------------------------------------------------

class TestFlagInjectionRejection(unittest.TestCase):
    """_try_cli() must reject model_id values that start with a dash."""

    def test_double_dash_all_is_rejected(self):
        """'--all' as model_id must be rejected; subprocess.run NOT called."""
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run") as mock_run:
                result = _try_cli("unload", DASH_ALL)
        self.assertFalse(result, "'--all' injection must return False")
        mock_run.assert_not_called()

    def test_single_dash_flag_is_rejected(self):
        """'-v' (single-dash flag) as model_id must be rejected."""
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run") as mock_run:
                result = _try_cli("load", SINGLE_DASH)
        self.assertFalse(result)
        mock_run.assert_not_called()

    def test_dash_followed_by_model_name_is_rejected(self):
        """'-qwen3' still starts with dash → must be rejected."""
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run") as mock_run:
                result = _try_cli("load", "-qwen3")
        self.assertFalse(result)
        mock_run.assert_not_called()

    def test_empty_model_id_is_rejected(self):
        """Empty string does not match safe charset (length 0) → rejected."""
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run") as mock_run:
                result = _try_cli("load", "")
        self.assertFalse(result)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Wave-22.2: unsafe charset rejection
# ---------------------------------------------------------------------------

class TestUnsafeCharsetRejection(unittest.TestCase):
    """model_id with shell-special or non-safe characters must be rejected."""

    def test_semicolon_is_rejected(self):
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run") as mock_run:
                result = _try_cli("unload", "qwen3;rm -rf /")
        self.assertFalse(result)
        mock_run.assert_not_called()

    def test_space_is_rejected(self):
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run") as mock_run:
                result = _try_cli("load", "model with spaces")
        self.assertFalse(result)
        mock_run.assert_not_called()

    def test_null_byte_is_rejected(self):
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run") as mock_run:
                result = _try_cli("load", "model\x00name")
        self.assertFalse(result)
        mock_run.assert_not_called()

    def test_dollar_sign_is_rejected(self):
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run") as mock_run:
                result = _try_cli("unload", "model$name")
        self.assertFalse(result)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Wave-22.3: POSIX "--" separator present for valid model IDs
# ---------------------------------------------------------------------------

class TestDoubleDashSeparatorPresent(unittest.TestCase):
    """For a valid model_id the argv list must contain a literal '--' before model_id."""

    def test_valid_model_has_double_dash_before_model_id(self):
        """Normal model_id → subprocess.run called with '--' separator in argv."""
        proc = _proc(0)
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run", return_value=proc) as mock_run:
                result = _try_cli("unload", SAFE_MODEL)

        self.assertTrue(result)
        mock_run.assert_called_once()
        argv = mock_run.call_args[0][0]

        # "--" must appear in argv
        self.assertIn("--", argv, "'--' separator missing from argv")

        # model_id must appear AFTER "--"
        sep_idx = argv.index("--")
        model_idx = argv.index(SAFE_MODEL)
        self.assertGreater(model_idx, sep_idx,
                           "model_id must appear after '--' separator")

    def test_model_id_never_at_flag_position(self):
        """model_id must NOT appear immediately after action without '--' between them."""
        proc = _proc(0)
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run", return_value=proc) as mock_run:
                _try_cli("load", SAFE_MODEL)

        argv = mock_run.call_args[0][0]
        # argv should be: [lms, "load", "--", SAFE_MODEL]
        # Confirm no layout where model_id is at position 2 (no "--" guard):
        self.assertNotEqual(argv[2], SAFE_MODEL,
                            "model_id must not be at argv[2] without '--' separator")

    def test_valid_model_with_dots_and_colon(self):
        """Model IDs with dots, colons, hyphens (common in HF names) still accepted."""
        model = "lmstudio-community/Qwen3-30B-A3B:q4"
        proc = _proc(0)
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run", return_value=proc) as mock_run:
                result = _try_cli("load", model)

        self.assertTrue(result)
        argv = mock_run.call_args[0][0]
        self.assertIn("--", argv)
        self.assertIn(model, argv)

    def test_lms_absent_returns_false_without_subprocess(self):
        """When lms is not in PATH, return False without calling subprocess."""
        with patch("shutil.which", return_value=None):
            with patch("subprocess.run") as mock_run:
                result = _try_cli("load", SAFE_MODEL)
        self.assertFalse(result)
        mock_run.assert_not_called()

    def test_nonzero_exit_returns_false(self):
        """lms non-zero exit → _try_cli returns False even for valid model_id."""
        proc = _proc(1)
        with patch("shutil.which", return_value=SAFE_LMS):
            with patch("subprocess.run", return_value=proc):
                result = _try_cli("unload", SAFE_MODEL)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
