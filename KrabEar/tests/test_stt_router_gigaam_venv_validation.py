"""Tests for STTRouter.get_gigaam_adapter() STT_GIGAAM_VENV_PYTHON path validation.

Fix D1 (MED): validate STT_GIGAAM_VENV_PYTHON before passing to subprocess.Popen
to prevent arbitrary binary execution when a rogue IPC client or malicious
settings.json sets the value to a non-Python binary (e.g. /usr/bin/curl).

Rules validated:
  1. The lexical (absolute, non-symlink-followed) path must be inside the user's
     home directory.  We use Path.absolute() rather than Path.resolve() because
     venv Python binaries are often symlinks into Homebrew Cellar -- resolving
     them would incorrectly reject all venv pythons on macOS.
  2. The filename (basename) must be a known Python interpreter name.
  3. Empty / whitespace-only values -> None (no adapter) -- pre-existing behaviour.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.stt_router import STTRouter  # noqa: E402


# Helpers ------------------------------------------------------------------

_HOME = str(Path.home().absolute())


def _home_path(*parts: str) -> str:
    """Build an absolute path under the user's home directory."""
    return str(Path(_HOME, *parts))


# ---------------------------------------------------------------------------
# _validate_gigaam_venv_python is a @staticmethod -- test it directly.
# ---------------------------------------------------------------------------

class TestValidateGigaamVenvPython(unittest.TestCase):
    """Unit tests for STTRouter._validate_gigaam_venv_python."""

    def _call(self, path: str):
        return STTRouter._validate_gigaam_venv_python(path)

    # --- Accepted cases ---

    def test_python3_inside_home_accepted(self):
        path = _home_path(".venv_krab_ear_gigaam", "bin", "python3")
        result = self._call(path)
        self.assertIsNotNone(result, f"Expected acceptance for path inside home: {path}")

    def test_python_basename_inside_home_accepted(self):
        path = _home_path("venv", "bin", "python")
        self.assertIsNotNone(self._call(path))

    def test_python312_basename_inside_home_accepted(self):
        path = _home_path(".venvs", "py312", "bin", "python3.12")
        self.assertIsNotNone(self._call(path))

    def test_python311_basename_inside_home_accepted(self):
        path = _home_path("venv", "bin", "python3.11")
        self.assertIsNotNone(self._call(path))

    def test_python310_basename_inside_home_accepted(self):
        path = _home_path("bin", "python3.10")
        self.assertIsNotNone(self._call(path))

    def test_returns_string(self):
        """Return value must be str, not Path or None for valid input."""
        path = _home_path(".venv_krab_ear_gigaam", "bin", "python3")
        result = self._call(path)
        self.assertIsInstance(result, str)

    def test_returned_path_is_absolute(self):
        path = _home_path(".venv_krab_ear_gigaam", "bin", "python3")
        result = self._call(path)
        self.assertIsNotNone(result)
        self.assertTrue(os.path.isabs(result), f"Expected absolute path, got: {result}")

    # --- Rejected cases: wrong basename ---

    def test_curl_rejected(self):
        path = "/usr/bin/curl"
        self.assertIsNone(self._call(path))

    def test_bash_inside_home_rejected(self):
        """Even inside home, a non-Python binary is rejected."""
        path = _home_path("bin", "bash")
        self.assertIsNone(self._call(path))

    def test_arbitrary_script_inside_home_rejected(self):
        path = _home_path(".local", "bin", "malicious.sh")
        self.assertIsNone(self._call(path))

    def test_python_with_extra_suffix_rejected(self):
        """'python3.12.exe' is not in the allowed set."""
        path = _home_path("bin", "python3.12.exe")
        self.assertIsNone(self._call(path))

    def test_wget_inside_home_rejected(self):
        path = _home_path("bin", "wget")
        self.assertIsNone(self._call(path))

    # --- Rejected cases: outside home ---

    def test_system_python_rejected(self):
        """System /usr/bin/python3 is outside home."""
        self.assertIsNone(self._call("/usr/bin/python3"))

    def test_opt_homebrew_python_rejected(self):
        """/opt/homebrew/bin/python3 is outside home."""
        self.assertIsNone(self._call("/opt/homebrew/bin/python3"))

    def test_tmp_python_rejected(self):
        """/tmp/python3 is outside home."""
        self.assertIsNone(self._call("/tmp/python3"))

    def test_sibling_prefix_attack_rejected(self):
        """Classic sibling-prefix attack: /home/user_evil vs /home/user.

        A path whose string starts with _HOME but is NOT a subdirectory
        must be rejected.
        """
        # e.g. /Users/pablito_evil/bin/python3 — starts with /Users/pablito
        # but is NOT relative to /Users/pablito.
        evil_path = _HOME + "_evil" + "/bin/python3"
        result = self._call(evil_path)
        self.assertIsNone(result, f"Sibling-prefix path should be rejected: {evil_path}")

    # --- Edge cases ---

    def test_empty_string_does_not_crash(self):
        # Even if resolved cwd is inside home, basename '' is not in valid set.
        result = self._call("")
        self.assertIsNone(result)

    def test_relative_path_does_not_crash(self):
        """A relative path may or may not be inside home; must not raise."""
        result = self._call("python3")
        self.assertIsInstance(result, (str, type(None)))


# ---------------------------------------------------------------------------
# Integration: get_gigaam_adapter() rejects bad venv_python and returns None
# ---------------------------------------------------------------------------

class _FakeSettings:
    STT_GIGAAM_ENABLED: bool = True
    STT_GIGAAM_MODE: str = "rnnt"
    STT_GIGAAM_DEVICE: str = "cpu"
    STT_GIGAAM_TRANSPORT: str = "subprocess"
    STT_GIGAAM_VENV_PYTHON: str = ""


def _make_settings(**overrides) -> _FakeSettings:
    s = _FakeSettings()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class TestGetGigaamAdapterVenvValidation(unittest.TestCase):
    """Integration: get_gigaam_adapter() rejects invalid STT_GIGAAM_VENV_PYTHON."""

    def _router(self, venv_python: str) -> STTRouter:
        return STTRouter(_make_settings(STT_GIGAAM_VENV_PYTHON=venv_python))

    def test_curl_returns_none(self):
        """/usr/bin/curl must be rejected -- adapter is None."""
        router = self._router("/usr/bin/curl")
        result = router.get_gigaam_adapter()
        self.assertIsNone(result)

    def test_system_python3_returns_none(self):
        """/usr/bin/python3 (outside home) must be rejected."""
        router = self._router("/usr/bin/python3")
        result = router.get_gigaam_adapter()
        self.assertIsNone(result)

    def test_opt_homebrew_python_returns_none(self):
        """Homebrew python outside home must be rejected."""
        router = self._router("/opt/homebrew/bin/python3")
        result = router.get_gigaam_adapter()
        self.assertIsNone(result)

    def test_valid_home_path_passes_validation(self):
        """A valid home-relative python3 path passes the validation layer.

        We verify this independently of whether GigaAMAdapter is importable.
        """
        valid_path = _home_path(".venv_krab_ear_gigaam", "bin", "python3")
        validated = STTRouter._validate_gigaam_venv_python(valid_path)
        self.assertIsNotNone(
            validated,
            f"Valid home-relative python3 path should pass validation: {valid_path}",
        )

    def test_disabled_gigaam_returns_none_regardless(self):
        """When STT_GIGAAM_ENABLED=False, adapter is None regardless of venv path."""
        router = STTRouter(_make_settings(
            STT_GIGAAM_ENABLED=False,
            STT_GIGAAM_VENV_PYTHON="/usr/bin/python3",
        ))
        self.assertIsNone(router.get_gigaam_adapter())

    def test_empty_venv_python_does_not_crash(self):
        """Empty STT_GIGAAM_VENV_PYTHON means no explicit venv override -- no exception."""
        router = STTRouter(_make_settings(STT_GIGAAM_VENV_PYTHON=""))
        # Validation is skipped for empty values (pre-existing logic).
        # The method may return an adapter or None depending on whether
        # GigaAMAdapter is importable in the current venv -- either is fine.
        result = router.get_gigaam_adapter()
        self.assertIsInstance(result, (type(None), object))


if __name__ == "__main__":
    unittest.main()
