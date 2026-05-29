"""W1535 regression tests — restore _VOXTRAL_REPO_ALLOWLIST.

W1525 meta-audit (SECURITY HIGH) found _VOXTRAL_REPO_ALLOWLIST was reverted
by W1497 cherry-pick train, re-opening supply-chain, DoS, and resource-
exhaustion attack surface via arbitrary HuggingFace repo strings.

These tests are the canonical W1535 gate: they must all pass after the fix.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import _VOXTRAL_REPO_ALLOWLIST, _validate_voxtral_repo, AudioEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bare_engine() -> AudioEngine:
    """Minimal AudioEngine without __init__ — avoids heavy model loading."""
    engine = AudioEngine.__new__(AudioEngine)
    engine.quality_profile = "balanced"
    engine.current_model = "mlx-community/whisper-large-v3-turbo"
    engine._unavailable_models = set()
    engine._voxtral_model = None
    engine._voxtral_load_error = None
    return engine


# ---------------------------------------------------------------------------
# W1535 gate tests
# ---------------------------------------------------------------------------

class TestVoxtralAllowlistW1535(unittest.TestCase):
    """W1535 — _VOXTRAL_REPO_ALLOWLIST constant and validator presence/behaviour."""

    def test_voxtral_allowlist_constant_present(self) -> None:
        """_VOXTRAL_REPO_ALLOWLIST must be a non-empty frozenset of repo strings."""
        self.assertIsInstance(
            _VOXTRAL_REPO_ALLOWLIST,
            frozenset,
            "_VOXTRAL_REPO_ALLOWLIST must be a frozenset",
        )
        self.assertTrue(
            len(_VOXTRAL_REPO_ALLOWLIST) > 0,
            "_VOXTRAL_REPO_ALLOWLIST must not be empty",
        )
        # Every entry must look like "org/repo"
        for repo in _VOXTRAL_REPO_ALLOWLIST:
            self.assertIn(
                "/",
                repo,
                f"Allowlist entry '{repo}' does not look like 'org/repo'",
            )

    def test_voxtral_allowed_repo_passes(self) -> None:
        """_validate_voxtral_repo returns the repo_id unchanged for allowlisted repos."""
        for repo_id in sorted(_VOXTRAL_REPO_ALLOWLIST):
            with self.subTest(repo_id=repo_id):
                result = _validate_voxtral_repo(repo_id)
                self.assertEqual(
                    result,
                    repo_id,
                    f"_validate_voxtral_repo should return '{repo_id}' unchanged",
                )

    def test_voxtral_disallowed_repo_raises_value_error(self) -> None:
        """_validate_voxtral_repo raises ValueError for repos not in the allowlist."""
        bad_repos = [
            "evil/arbitrary-model-injection",
            "hacker/voxtral-lookalike",
            "",
            "mistralai/NotVoxtral",
            "mlx-community/some-random-model",
        ]
        for bad_repo in bad_repos:
            with self.subTest(repo=bad_repo):
                with self.assertRaises(ValueError) as ctx:
                    _validate_voxtral_repo(bad_repo)
                self.assertIn(
                    bad_repo,
                    str(ctx.exception),
                    f"ValueError message should include the rejected repo: '{bad_repo}'",
                )

    def test_load_voxtral_model_rejects_disallowed_repo(self) -> None:
        """_load_voxtral_model() raises RuntimeError before snapshot_download for bad repos."""
        engine = _bare_engine()

        with patch("core.engine.settings") as mock_settings, \
             patch("core.engine._voxtral_available", True), \
             patch("core.engine._profiler") as mock_profiler:
            mock_settings.VOXTRAL_MODEL = "malicious/repo-injection"
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)

            with self.assertRaises(RuntimeError) as ctx:
                engine._load_voxtral_model()

        error_text = str(ctx.exception)
        # Must mention the rejected repo
        self.assertIn(
            "malicious/repo-injection",
            error_text,
            "RuntimeError must include the rejected repo ID",
        )
        # Must not silently fall through to snapshot_download
        self.assertIn(
            "допустимы только",
            error_text.lower() if "допустимы только" not in error_text else error_text,
            "RuntimeError should indicate only allowlisted repos are accepted",
        )

    def test_load_voxtral_model_stores_error_for_disallowed_repo(self) -> None:
        """After rejection, _voxtral_load_error is set so subsequent calls fast-fail."""
        engine = _bare_engine()

        with patch("core.engine.settings") as mock_settings, \
             patch("core.engine._voxtral_available", True), \
             patch("core.engine._profiler") as mock_profiler:
            mock_settings.VOXTRAL_MODEL = "attacker/fake-voxtral"
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)

            try:
                engine._load_voxtral_model()
            except RuntimeError:
                pass

        self.assertIsNotNone(
            engine._voxtral_load_error,
            "_voxtral_load_error must be set after allowlist rejection for fast-fail on retry",
        )


if __name__ == "__main__":
    unittest.main()
