"""
test_audit_cherry_pick_scanner_W1694.py — regression test for Wave 1694 fix.

Verifies that _collect_top_level_names() correctly identifies ONLY module-level
symbols and does NOT count class methods or nested function defs as top-level.
"""
import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# Locate the scripts/ directory relative to this file's repo layout and add it
# so we can import the scanner without installing it.
_TESTS_DIR = Path(__file__).parent
_REPO_ROOT = _TESTS_DIR.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"


def _load_scanner():
    """Import audit_cherry_pick_regressions from scripts/ without side effects."""
    scanner_path = _SCRIPTS_DIR / "audit_cherry_pick_regressions.py"
    spec = importlib.util.spec_from_file_location(
        "audit_cherry_pick_regressions", scanner_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCollectTopLevelNames(unittest.TestCase):
    """Unit tests for the _collect_top_level_names() helper."""

    @classmethod
    def setUpClass(cls):
        cls.scanner = _load_scanner()
        # Store as a staticmethod wrapper so self is not injected on calls.
        cls._collect_fn = staticmethod(cls.scanner._collect_top_level_names)

    # ------------------------------------------------------------------
    # Core fixture: module with mixed top-level and nested defs
    # ------------------------------------------------------------------
    def _write_fixture(self, source: str) -> Path:
        """Write source to a temp .py file and return its Path."""
        tmp = tempfile.NamedTemporaryFile(suffix=".py", mode="w",
                                          delete=False, encoding="utf-8")
        tmp.write(textwrap.dedent(source))
        tmp.flush()
        tmp.close()
        return Path(tmp.name)

    def test_module_level_function_present(self):
        p = self._write_fixture("""
            def foo():
                pass
        """)
        names = self._collect_fn(p)
        self.assertIn("foo", names)

    def test_module_level_assignment_present(self):
        p = self._write_fixture("""
            bar = 1
        """)
        names = self._collect_fn(p)
        self.assertIn("bar", names)

    def test_module_level_class_present(self):
        p = self._write_fixture("""
            class C:
                pass
        """)
        names = self._collect_fn(p)
        self.assertIn("C", names)

    def test_conditional_name_present(self):
        """Names assigned inside an if-block ARE module-level in Python."""
        p = self._write_fixture("""
            if True:
                cond_name = 2
        """)
        names = self._collect_fn(p)
        self.assertIn("cond_name", names)

    def test_class_method_not_present(self):
        """A method defined inside a class must NOT appear as a module symbol."""
        p = self._write_fixture("""
            class C:
                def method_inside(self):
                    pass
        """)
        names = self._collect_fn(p)
        self.assertNotIn("method_inside", names)
        self.assertIn("C", names)

    def test_nested_function_not_present(self):
        """A function defined inside another function is not module-level."""
        p = self._write_fixture("""
            def outer():
                def inner():
                    pass
        """)
        names = self._collect_fn(p)
        self.assertIn("outer", names)
        self.assertNotIn("inner", names)

    def test_comprehensive_fixture(self):
        """All four cases from the task spec in one fixture."""
        p = self._write_fixture("""
            def foo():
                pass

            bar = 1

            class C:
                def method_inside(self):
                    pass

            if True:
                cond_name = 2
        """)
        names = self._collect_fn(p)
        # Must be present
        self.assertIn("foo", names)
        self.assertIn("bar", names)
        self.assertIn("C", names)
        self.assertIn("cond_name", names)
        # Must NOT be present (the core fix)
        self.assertNotIn("method_inside", names)

    def test_try_except_names_present(self):
        """Names defined in try/except blocks are module-level."""
        p = self._write_fixture("""
            try:
                import ujson as json
            except ImportError:
                import json
        """)
        names = self._collect_fn(p)
        self.assertIn("json", names)

    def test_for_loop_names_present(self):
        """Names assigned in a for-loop body are module-level."""
        p = self._write_fixture("""
            for _x in range(1):
                loop_var = _x
        """)
        names = self._collect_fn(p)
        self.assertIn("loop_var", names)

    def test_staticmethod_in_class_not_present(self):
        """Simulates the W1694 regression: _escape_as_str was a @staticmethod
        of BackendService and MUST NOT appear as a module-level symbol."""
        p = self._write_fixture("""
            class BackendService:
                @staticmethod
                def _escape_as_str(value):
                    return str(value)
        """)
        names = self._collect_fn(p)
        self.assertIn("BackendService", names)
        self.assertNotIn("_escape_as_str", names)


class TestScannerNoFalsePositives(unittest.TestCase):
    """Ensure the scanner reports 0 missing on the real repo (sanity check).

    This test is intentionally lightweight — it just checks the scanner's
    main() returns an empty list, proving the stricter logic does not cause
    false positives on legitimate module-level re-exports.
    """

    @classmethod
    def setUpClass(cls):
        cls.scanner = _load_scanner()

    def test_real_repo_zero_missing(self):
        missing = self.scanner.main(as_json=False)
        self.assertEqual(
            missing,
            [],
            msg=(
                f"Scanner found {len(missing)} missing symbol(s) in the real repo "
                "after the Wave-1694 fix — possible false positive in the new logic."
            ),
        )


if __name__ == "__main__":
    unittest.main()
