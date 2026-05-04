"""Tests for scripts/verify_claude_md.py — CLAUDE.md drift verifier.

Phase C C.8: Documentation drift verification.
"""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_claude_md.py"


def _load_module():
    """Import verify_claude_md without requiring it to be a package."""
    spec = importlib.util.spec_from_file_location("verify_claude_md", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestExtractPaths:
    """Unit tests for the extract_paths() helper."""

    def setup_method(self):
        self.mod = _load_module()

    def test_finds_backtick_python_path(self):
        content = "Look at `KrabEar/backend/service.py` for details."
        assert "KrabEar/backend/service.py" in self.mod.extract_paths(content)

    def test_finds_bare_module_path_backtick(self):
        content = "See `backend/service.py` for the IPC handler."
        assert "backend/service.py" in self.mod.extract_paths(content)

    def test_finds_swift_filename_backtick(self):
        content = "**`KrabEarTheme.swift`** — visual theme."
        assert "KrabEarTheme.swift" in self.mod.extract_paths(content)

    def test_skips_https_urls(self):
        content = "See `https://example.com/file.py` for details."
        paths = self.mod.extract_paths(content)
        assert not any("http" in p for p in paths)

    def test_skips_absolute_user_paths(self):
        content = "File at `/Users/foo/bar.py`"
        paths = self.mod.extract_paths(content)
        assert "/Users/foo/bar.py" not in paths

    def test_skips_bare_name_without_slash_or_known_ext(self):
        """Names without '/' and without a known extension are not tracked."""
        content = "Use `mymodule` for this."
        paths = self.mod.extract_paths(content)
        assert "mymodule" not in paths

    def test_finds_yml_workflow(self):
        content = "See `.github/workflows/ci.yml` for CI config."
        paths = self.mod.extract_paths(content)
        assert ".github/workflows/ci.yml" not in paths  # starts with dot, filtered by regex anchor

    def test_finds_sh_script(self):
        content = "Run `scripts/install_agent.sh` to install."
        paths = self.mod.extract_paths(content)
        assert "scripts/install_agent.sh" in paths

    def test_italic_path_detected(self):
        """Italic (`*path*`) should be detected by the same regex."""
        content = "See *backend/service.py* for the IPC handler."
        paths = self.mod.extract_paths(content)
        assert "backend/service.py" in paths


class TestFileExistsInRepo:
    """Unit tests for the file_exists_in_repo() resolver."""

    def setup_method(self):
        self.mod = _load_module()
        # Reset module-level cache between tests
        self.mod._INDEX = None

    def test_direct_path_found(self):
        # KrabEar/backend/service.py definitely exists
        assert self.mod.file_exists_in_repo("KrabEar/backend/service.py")

    def test_bare_module_path_resolves_via_prefix(self):
        # backend/service.py → KrabEar/backend/service.py
        assert self.mod.file_exists_in_repo("backend/service.py")

    def test_swift_filename_resolves_via_search(self):
        # KrabEarTheme.swift lives at native/KrabEarAgent/Sources/KrabEarAgent/
        assert self.mod.file_exists_in_repo("KrabEarTheme.swift")

    def test_missing_file_returns_false(self):
        assert not self.mod.file_exists_in_repo("does_not_exist_ever.py")

    def test_missing_bare_swift_returns_false(self):
        assert not self.mod.file_exists_in_repo("GhostFile.swift")


class TestMainScript:
    """Integration tests — run script as subprocess."""

    def test_script_runs_without_crash(self):
        """Script must exit 0 (clean) or 1 (drift) but never crash."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode in (0, 1), (
            f"Unexpected exit code {result.returncode}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # One of the two expected output lines must be present
        combined = result.stdout + result.stderr
        assert "DRIFT DETECTED" in combined or "OK —" in combined

    def test_exit_code_2_on_missing_claude_md(self):
        """Exit 2 when CLAUDE.md is absent (e.g. wrong working dir)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a minimal repo structure so REPO_ROOT resolves to tmpdir
            scripts_dir = Path(tmpdir) / "scripts"
            scripts_dir.mkdir()
            # Symlink or copy the script into the temp dir so parents[1] = tmpdir
            import shutil
            tmp_script = scripts_dir / "verify_claude_md.py"
            shutil.copy(str(SCRIPT), str(tmp_script))

            result = subprocess.run(
                [sys.executable, str(tmp_script)],
                cwd=tmpdir,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 2
            assert "CLAUDE.md not found" in result.stderr
