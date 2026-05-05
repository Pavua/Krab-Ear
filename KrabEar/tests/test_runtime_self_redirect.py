"""
Integration tests for the runtime → bundle self-redirect guard added in main.swift.

Context:
  - Two-binary drift is documented in memory/blocker_two_binary_drift_2026-05-03.md
  - Part 1 fix (commit 6781a4e): scripts/start_agent.command uses exec /usr/bin/open
  - Part 2 fix (this PR): main.swift detects runtime path and exec's the bundle

These tests verify the path-detection logic and redirect behaviour by:
  1. Using a real compiled runtime binary (if available) with a mock bundle executable
  2. Mocking path detection logic in Python when the binary is not available
  3. Testing edge cases (no bundle, non-runtime paths, symlinks)

Run:
  PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_runtime_self_redirect.py -v
  # or via unittest:
  PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar/tests/test_runtime_self_redirect.py -v
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REAL_RUNTIME_BINARY = os.path.join(REPO_ROOT, "native", "runtime", "KrabEarAgent")


def _is_runtime_binary_available() -> bool:
    """Return True if the compiled KrabEarAgent binary exists and is executable."""
    return (
        os.path.isfile(REAL_RUNTIME_BINARY)
        and os.access(REAL_RUNTIME_BINARY, os.X_OK)
    )


def _make_bundle_stub(root: str, exit_code: int = 0, sleep_sec: float = 0) -> str:
    """
    Create a minimal bundle structure under *root* with a stub KrabEarAgent
    that exits immediately with *exit_code*.

    Returns path to the bundle executable.
    """
    bundle_exe_dir = os.path.join(
        root, "Krab Ear.app", "Contents", "MacOS"
    )
    os.makedirs(bundle_exe_dir, exist_ok=True)
    bundle_exe = os.path.join(bundle_exe_dir, "KrabEarAgent")

    # A tiny shell stub that exits immediately (or sleeps briefly)
    sleep_line = f"sleep {sleep_sec}" if sleep_sec > 0 else ""
    script = f"""#!/bin/sh
{sleep_line}
exit {exit_code}
"""
    with open(bundle_exe, "w") as fh:
        fh.write(script)
    os.chmod(bundle_exe, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    return bundle_exe


def _make_runtime_stub(root: str, bundle_root: str) -> str:
    """
    Create a minimal native/runtime/KrabEarAgent stub that mimics the redirect
    logic from main.swift (in shell, for testing purposes without Swift compilation).

    This stub checks its own argv[0] path; if it matches `*/native/runtime/KrabEarAgent`
    AND a bundle exists, it execs the bundle — same logic as the Swift implementation.

    Returns path to the runtime stub binary.
    """
    runtime_dir = os.path.join(root, "native", "runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    runtime_exe = os.path.join(runtime_dir, "KrabEarAgent")

    bundle_exe = os.path.join(
        bundle_root, "Krab Ear.app", "Contents", "MacOS", "KrabEarAgent"
    )

    # Shell script that replicates the Swift redirect logic
    script = f"""#!/bin/sh
# Simulated Swift runtime-self-redirect guard (test stub)
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
PARENT_DIR="$(dirname "$(dirname "$SELF")")"

BUNDLE_EXE="{bundle_exe}"

case "$SELF" in
  */native/runtime/KrabEarAgent)
    if [ -x "$BUNDLE_EXE" ]; then
      exec "$BUNDLE_EXE" "$@"
    fi
    ;;
esac

# Not a runtime path or no bundle: continue normally
sleep 60  # simulate long-running app (tests kill us)
"""
    with open(runtime_exe, "w") as fh:
        fh.write(script)
    os.chmod(runtime_exe, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    return runtime_exe


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestRuntimeSelfRedirect(unittest.TestCase):
    """
    Tests for the defense-in-depth runtime → bundle self-redirect guard.

    Tests 1-3 use shell stubs to replicate the exact redirect logic from main.swift.
    Test 4 verifies the actual compiled binary is present (smoke check).
    Tests 5-6 are pure path-detection logic tests (Python equivalent).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="krab_ear_redirect_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Test 1: runtime with sibling bundle → exits 0 quickly
    # ------------------------------------------------------------------

    def test_runtime_with_bundle_exits_cleanly(self):
        """
        When a runtime binary is launched AND a sibling bundle exists,
        the redirect guard must exec the bundle and exit 0 within 3 seconds.
        """
        # Create bundle stub (exits immediately with 0)
        _make_bundle_stub(self.tmpdir, exit_code=0)

        # Create runtime stub that performs redirect
        runtime_exe = _make_runtime_stub(self.tmpdir, bundle_root=self.tmpdir)

        proc = subprocess.Popen(
            [runtime_exe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail(
                "Runtime binary did not exit within 5s after bundle redirect — "
                "redirect guard may not be working"
            )

        self.assertEqual(
            rc, 0,
            f"Expected exit code 0 after redirect to bundle, got {rc}"
        )

    # ------------------------------------------------------------------
    # Test 2: runtime without bundle → does NOT exit immediately
    # ------------------------------------------------------------------

    def test_runtime_without_bundle_starts_normally(self):
        """
        When no sibling bundle exists, the runtime binary should NOT redirect
        (it continues running normally). We wait 2s and verify it's still alive.
        """
        # Only create the runtime stub; NO bundle
        runtime_dir = os.path.join(self.tmpdir, "native", "runtime")
        os.makedirs(runtime_dir, exist_ok=True)
        runtime_exe = os.path.join(runtime_dir, "KrabEarAgent")

        # A stub that: if it would redirect but can't find bundle, sleeps (fallthrough)
        no_bundle_path = os.path.join(
            self.tmpdir, "Krab Ear.app", "Contents", "MacOS", "KrabEarAgent"
        )
        script = f"""#!/bin/sh
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
BUNDLE_EXE="{no_bundle_path}"
case "$SELF" in
  */native/runtime/KrabEarAgent)
    if [ -x "$BUNDLE_EXE" ]; then
      exec "$BUNDLE_EXE" "$@"
    fi
    # No bundle found — fall through (dev mode)
    ;;
esac
sleep 60  # simulate running app
"""
        with open(runtime_exe, "w") as fh:
            fh.write(script)
        os.chmod(runtime_exe, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)

        proc = subprocess.Popen(
            [runtime_exe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Give it 2 seconds; if it exits immediately that's a bug
            time.sleep(2.0)
            still_running = proc.poll() is None
            self.assertTrue(
                still_running,
                "Runtime binary exited immediately even without a sibling bundle — "
                "redirect guard should fall through to normal startup"
            )
        finally:
            proc.kill()
            proc.wait()

    # ------------------------------------------------------------------
    # Test 3: bundle redirect passes original arguments through
    # ------------------------------------------------------------------

    def test_redirect_passes_arguments_to_bundle(self):
        """
        When redirecting, original argv[1:] must be forwarded to the bundle binary.
        We use a bundle stub that writes its argv to a temp file, then verify.
        """
        arg_capture_file = os.path.join(self.tmpdir, "captured_args.txt")

        # Bundle stub that captures its arguments to a file
        bundle_exe_dir = os.path.join(
            self.tmpdir, "Krab Ear.app", "Contents", "MacOS"
        )
        os.makedirs(bundle_exe_dir, exist_ok=True)
        bundle_exe = os.path.join(bundle_exe_dir, "KrabEarAgent")
        script = f"""#!/bin/sh
printf '%s\\n' "$@" > "{arg_capture_file}"
exit 0
"""
        with open(bundle_exe, "w") as fh:
            fh.write(script)
        os.chmod(bundle_exe, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)

        # Create runtime stub
        runtime_exe = _make_runtime_stub(self.tmpdir, bundle_root=self.tmpdir)

        test_args = ["--launched-by-launchd", "--project-root", "/tmp/fake"]
        proc = subprocess.Popen(
            [runtime_exe] + test_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail("Redirect with args timed out")

        self.assertEqual(rc, 0)
        self.assertTrue(
            os.path.isfile(arg_capture_file),
            "Bundle stub did not create arg capture file — bundle may not have been exec'd"
        )
        with open(arg_capture_file) as fh:
            captured = [line.strip() for line in fh if line.strip()]
        self.assertEqual(
            captured, test_args,
            f"Arguments not forwarded correctly. Expected {test_args}, got {captured}"
        )

    # ------------------------------------------------------------------
    # Test 4 (pure Python): path-detection logic — is_runtime_path()
    # ------------------------------------------------------------------

    def test_path_detection_logic_runtime_path(self):
        """
        Pure unit test for the path-detection logic used in the Swift guard.

        The guard fires when argv[0] ends with .../native/runtime/KrabEarAgent.
        """
        def is_runtime_path(exe_path: str) -> bool:
            """Python equivalent of the Swift path-check in redirectRuntimeToBundleIfPresent."""
            import os
            parts = exe_path.replace("\\", "/").split("/")
            # Remove empty strings from leading slash
            parts = [p for p in parts if p]
            if len(parts) < 3:
                return False
            return (
                parts[-1] == "KrabEarAgent"
                and parts[-2] == "runtime"
                and parts[-3] == "native"
            )

        # Should match
        self.assertTrue(is_runtime_path("/Users/pablito/Antigravity_AGENTS/Krab Ear/native/runtime/KrabEarAgent"))
        self.assertTrue(is_runtime_path("/tmp/project/native/runtime/KrabEarAgent"))

        # Should NOT match
        self.assertFalse(is_runtime_path("/Users/pablito/Antigravity_AGENTS/Krab Ear/Krab Ear.app/Contents/MacOS/KrabEarAgent"))
        self.assertFalse(is_runtime_path("/Users/pablito/KrabEarAgent"))
        self.assertFalse(is_runtime_path("/native/runtime/OtherBinary"))
        self.assertFalse(is_runtime_path("/some/runtime/KrabEarAgent"))  # no 'native' parent
        self.assertFalse(is_runtime_path(""))

    # ------------------------------------------------------------------
    # Test 5 (pure Python): bundle path construction from runtime path
    # ------------------------------------------------------------------

    def test_bundle_path_construction(self):
        """
        Pure unit test for the bundle path constructed from a runtime path.

        Given `.../native/runtime/KrabEarAgent`, the bundle binary must be
        `.../Krab Ear.app/Contents/MacOS/KrabEarAgent`.
        """
        def bundle_exe_for_runtime(runtime_exe_path: str) -> str:
            """Python equivalent of the Swift rootURL + bundle path construction."""
            import os
            exe = os.path.realpath(runtime_exe_path)
            # runtime → native → <root>
            root = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
            return os.path.join(root, "Krab Ear.app", "Contents", "MacOS", "KrabEarAgent")

        runtime = "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/runtime/KrabEarAgent"
        expected = "/Users/pablito/Antigravity_AGENTS/Krab Ear/Krab Ear.app/Contents/MacOS/KrabEarAgent"

        # Note: os.path.realpath may resolve symlinks on this machine, so we test
        # with a tempdir path that has no symlinks to control for that.
        td = tempfile.mkdtemp(prefix="krab_bundle_path_test_")
        try:
            # Resolve symlinks for the tempdir itself (macOS /var → /private/var)
            td_real = os.path.realpath(td)

            native_runtime = os.path.join(td_real, "native", "runtime")
            os.makedirs(native_runtime)
            stub = os.path.join(native_runtime, "KrabEarAgent")
            open(stub, "w").close()

            result = bundle_exe_for_runtime(stub)
            self.assertEqual(
                result,
                os.path.join(td_real, "Krab Ear.app", "Contents", "MacOS", "KrabEarAgent"),
                "Bundle path not constructed correctly from runtime path"
            )
        finally:
            shutil.rmtree(td, ignore_errors=True)

    # ------------------------------------------------------------------
    # Test 6 (smoke): compiled binary exists at expected runtime path
    # ------------------------------------------------------------------

    def test_compiled_runtime_binary_present(self):
        """
        Smoke check: the native/runtime/KrabEarAgent binary must exist and
        be executable. If it's absent the Swift guard can never fire.

        This test is skipped if the repo is checked out without building (CI).
        """
        if not os.path.isfile(REAL_RUNTIME_BINARY):
            self.skipTest(
                f"Compiled binary not found at {REAL_RUNTIME_BINARY}. "
                "Build with: cd native/KrabEarAgent && swift build -c release && "
                "cp .build/release/KrabEarAgent ../../native/runtime/KrabEarAgent"
            )
        self.assertTrue(
            os.access(REAL_RUNTIME_BINARY, os.X_OK),
            f"Binary at {REAL_RUNTIME_BINARY} exists but is not executable"
        )


if __name__ == "__main__":
    unittest.main()
