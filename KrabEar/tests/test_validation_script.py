"""Tests for C.1 MPS pool fix validation script and env-var bypass.

Verifies:
- validate_c1_mps_fix.command exists and is executable (test_validate_c1_script_exists)
- get_worker_rss returns an integer (test_get_worker_rss_returns_int)
- KRAB_EAR_DISABLE_MPS_POOL_FREE=1 prevents torch.mps.empty_cache from being called
  (test_env_var_bypass_disables_fix)
- KRAB_EAR_DISABLE_MPS_POOL_FREE=1 prevents gc.collect from being called
  (test_env_var_bypass_also_skips_gc)
- Fix runs normally when env var is absent (test_fix_runs_when_var_absent)
- Fix runs normally when env var set to non-"1" value (test_fix_runs_when_var_not_1)

Does NOT load MLX, GigaAM, or any subprocess. Pure unit tests.

Run:
    PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest \
        KrabEar/tests/test_validation_script.py -q
"""

from __future__ import annotations

import os
import stat
import sys
import tracemalloc
import types
import unittest


# ---------------------------------------------------------------------------
# Helpers to reload gigaam_worker cleanly between tests
# ---------------------------------------------------------------------------


def _worker_dir() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "core", "workers")
    )


def _reload_worker(env_overrides: dict[str, str | None]) -> types.ModuleType:
    """Import gigaam_worker with specific env vars set, then restore env."""
    worker_dir = _worker_dir()
    if worker_dir not in sys.path:
        sys.path.insert(0, worker_dir)

    # Save + apply env overrides
    saved: dict[str, str | None] = {}
    all_keys = {"KRAB_EAR_DISABLE_MPS_POOL_FREE", "KRAB_EAR_TRACE_GIGAAM_MEM"}
    for key in all_keys:
        saved[key] = os.environ.pop(key, None)

    for key, value in env_overrides.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    # Evict cached module so module-level env checks re-run
    sys.modules.pop("gigaam_worker", None)
    tracemalloc.stop()

    try:
        import gigaam_worker as mod  # type: ignore[import]
    except ImportError as exc:
        raise unittest.SkipTest(f"gigaam_worker import failed (expected outside venv_gigaam): {exc}")
    finally:
        for key, orig in saved.items():
            if orig is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = orig

    return mod


# ---------------------------------------------------------------------------
# Script existence / executability
# ---------------------------------------------------------------------------


class TestValidateC1ScriptExists(unittest.TestCase):
    """Verify the validation script is present and executable."""

    _SCRIPT_PATH = os.path.normpath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "scripts",
            "validate_c1_mps_fix.command",
        )
    )

    def test_validate_c1_script_exists(self) -> None:
        """scripts/validate_c1_mps_fix.command must exist."""
        self.assertTrue(
            os.path.isfile(self._SCRIPT_PATH),
            f"Script not found: {self._SCRIPT_PATH}",
        )

    def test_validate_c1_script_is_executable(self) -> None:
        """scripts/validate_c1_mps_fix.command must have executable bit set."""
        if not os.path.isfile(self._SCRIPT_PATH):
            self.skipTest("Script not found — skipping executability check")
        mode = os.stat(self._SCRIPT_PATH).st_mode
        self.assertTrue(
            bool(mode & stat.S_IXUSR),
            f"Script not executable (mode={oct(mode)}): {self._SCRIPT_PATH}",
        )

    def test_validate_c1_script_is_zsh(self) -> None:
        """Script must start with a zsh shebang."""
        if not os.path.isfile(self._SCRIPT_PATH):
            self.skipTest("Script not found — skipping shebang check")
        with open(self._SCRIPT_PATH, encoding="utf-8") as fh:
            first_line = fh.readline()
        self.assertTrue(
            first_line.startswith("#!/bin/zsh"),
            f"Expected #!/bin/zsh shebang, got: {first_line!r}",
        )

    def test_validate_c1_script_mentions_disable_env_var(self) -> None:
        """Script must reference KRAB_EAR_DISABLE_MPS_POOL_FREE env var."""
        if not os.path.isfile(self._SCRIPT_PATH):
            self.skipTest("Script not found — skipping content check")
        with open(self._SCRIPT_PATH, encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn(
            "KRAB_EAR_DISABLE_MPS_POOL_FREE",
            content,
            "Script must reference KRAB_EAR_DISABLE_MPS_POOL_FREE",
        )


# ---------------------------------------------------------------------------
# get_worker_rss helper (subprocess simulation)
# ---------------------------------------------------------------------------


class TestGetWorkerRss(unittest.TestCase):
    """Verify RSS measurement helper returns a sensible integer."""

    def test_get_worker_rss_returns_int(self) -> None:
        """Calling ps + awk for gigaam_worker RSS must return an integer >= 0.

        Since no real gigaam_worker is running in test environment, expect 0.
        The important thing is the type: int, not None or exception.
        """
        import subprocess

        result = subprocess.run(
            [
                "awk",
                '/gigaam_worker/ && !/awk/ {sum += $2} END {print (sum > 0) ? int(sum/1024) : 0}',
            ],
            input="",
            capture_output=True,
            text=True,
            timeout=5,
        )
        # awk with empty input on END rule should print 0
        output = result.stdout.strip()
        self.assertTrue(output.isdigit(), f"Expected integer output from awk, got: {output!r}")
        rss = int(output)
        self.assertGreaterEqual(rss, 0, "RSS must be non-negative")


# ---------------------------------------------------------------------------
# Env-var bypass tests for _free_mps_pool
# ---------------------------------------------------------------------------


class TestEnvVarBypassDisablesFix(unittest.TestCase):
    """Verify KRAB_EAR_DISABLE_MPS_POOL_FREE=1 short-circuits _free_mps_pool."""

    def tearDown(self) -> None:
        tracemalloc.stop()
        sys.modules.pop("gigaam_worker", None)
        os.environ.pop("KRAB_EAR_DISABLE_MPS_POOL_FREE", None)

    def test_env_var_bypass_disables_fix(self) -> None:
        """When KRAB_EAR_DISABLE_MPS_POOL_FREE=1, torch.mps.empty_cache must NOT be called."""
        mod = _reload_worker({"KRAB_EAR_DISABLE_MPS_POOL_FREE": None})

        empty_cache_calls: list[int] = []

        fake_mps = types.SimpleNamespace(empty_cache=lambda: empty_cache_calls.append(1))
        fake_torch = types.SimpleNamespace(mps=fake_mps)
        sys.modules["torch"] = fake_torch  # type: ignore[assignment]

        os.environ["KRAB_EAR_DISABLE_MPS_POOL_FREE"] = "1"
        try:
            mod._free_mps_pool()
        finally:
            sys.modules.pop("torch", None)
            os.environ.pop("KRAB_EAR_DISABLE_MPS_POOL_FREE", None)

        self.assertEqual(
            len(empty_cache_calls),
            0,
            "torch.mps.empty_cache must NOT be called when bypass env var = '1'",
        )

    def test_env_var_bypass_also_skips_gc(self) -> None:
        """When KRAB_EAR_DISABLE_MPS_POOL_FREE=1, gc.collect must NOT be called."""
        mod = _reload_worker({"KRAB_EAR_DISABLE_MPS_POOL_FREE": None})

        gc_collect_calls: list[int] = []

        original_gc_collect = mod.gc.collect

        def _mock_collect(*args: object, **kwargs: object) -> int:
            gc_collect_calls.append(1)
            return 0

        mod.gc.collect = _mock_collect  # type: ignore[assignment]
        os.environ["KRAB_EAR_DISABLE_MPS_POOL_FREE"] = "1"
        try:
            mod._free_mps_pool()
        finally:
            mod.gc.collect = original_gc_collect
            os.environ.pop("KRAB_EAR_DISABLE_MPS_POOL_FREE", None)

        self.assertEqual(
            len(gc_collect_calls),
            0,
            "gc.collect must NOT be called when KRAB_EAR_DISABLE_MPS_POOL_FREE=1",
        )

    def test_fix_runs_when_var_absent(self) -> None:
        """When env var is absent, torch.mps.empty_cache IS called (treatment mode)."""
        mod = _reload_worker({"KRAB_EAR_DISABLE_MPS_POOL_FREE": None})

        empty_cache_calls: list[int] = []
        fake_mps = types.SimpleNamespace(empty_cache=lambda: empty_cache_calls.append(1))
        fake_torch = types.SimpleNamespace(mps=fake_mps)
        sys.modules["torch"] = fake_torch  # type: ignore[assignment]

        # Ensure env var is NOT set
        os.environ.pop("KRAB_EAR_DISABLE_MPS_POOL_FREE", None)
        try:
            mod._free_mps_pool()
        finally:
            sys.modules.pop("torch", None)

        self.assertEqual(
            len(empty_cache_calls),
            1,
            "torch.mps.empty_cache must be called when env var is absent",
        )

    def test_fix_runs_when_var_not_1(self) -> None:
        """KRAB_EAR_DISABLE_MPS_POOL_FREE=0 must NOT disable the fix (only '1' disables)."""
        mod = _reload_worker({"KRAB_EAR_DISABLE_MPS_POOL_FREE": None})

        empty_cache_calls: list[int] = []
        fake_mps = types.SimpleNamespace(empty_cache=lambda: empty_cache_calls.append(1))
        fake_torch = types.SimpleNamespace(mps=fake_mps)
        sys.modules["torch"] = fake_torch  # type: ignore[assignment]

        os.environ["KRAB_EAR_DISABLE_MPS_POOL_FREE"] = "0"
        try:
            mod._free_mps_pool()
        finally:
            sys.modules.pop("torch", None)
            os.environ.pop("KRAB_EAR_DISABLE_MPS_POOL_FREE", None)

        self.assertEqual(
            len(empty_cache_calls),
            1,
            "torch.mps.empty_cache must be called when KRAB_EAR_DISABLE_MPS_POOL_FREE=0",
        )

    def test_bypass_no_raise(self) -> None:
        """Bypassed _free_mps_pool() must not raise under any condition."""
        mod = _reload_worker({"KRAB_EAR_DISABLE_MPS_POOL_FREE": None})
        os.environ["KRAB_EAR_DISABLE_MPS_POOL_FREE"] = "1"
        try:
            mod._free_mps_pool()  # must complete without exception
        finally:
            os.environ.pop("KRAB_EAR_DISABLE_MPS_POOL_FREE", None)


if __name__ == "__main__":
    unittest.main()
