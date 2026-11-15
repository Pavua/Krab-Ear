"""Real synthetic-source regressions for invocation-local scanner reuse."""
import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "audit_cherry_pick_regressions.py"


class ScannerSourceReuseTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("scanner_under_test", SOURCE)
        self.scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.scanner)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        backend = self.root / "KrabEar" / "backend"
        backend.mkdir(parents=True)
        tests = self.root / "KrabEar" / "tests"
        tests.mkdir()
        self.module = backend / "sample.py"
        self.test_file = tests / "test_imports.py"
        self.scanner.REPO_ROOT = self.root
        self.scanner.KRAB_EAR = self.root / "KrabEar"
        self.scanner.TESTS_DIR = tests

    def run_scanner(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            missing = self.scanner.main(**kwargs)
        return missing, stream.getvalue()

    def test_repeated_imports_reuse_one_real_module_parse(self):
        self.module.write_text("one = 1\ntwo = 2\n")
        self.test_file.write_text("from backend.sample import " + ", ".join(["one", "two"] * 30) + "\n")
        real_parse = ast.parse
        module_parses = []

        def counted_parse(source, filename="<unknown>", *args, **kwargs):
            if filename == str(self.module):
                module_parses.append(source)
            return real_parse(source, filename, *args, **kwargs)

        with patch.object(ast, "parse", counted_parse):
            missing, output = self.run_scanner(as_json=True)
        self.assertEqual(missing, [])
        self.assertEqual(json.loads(output), {"missing": [], "checked": 60})
        self.assertEqual(len(module_parses), 1, "Repeated symbols must not repeatedly parse identical module text")

    def test_each_main_call_has_its_own_parse_lifetime(self):
        self.module.write_text("one = 1\ntwo = 2\n")
        self.test_file.write_text("from backend.sample import one, two\n")
        real_parse = ast.parse
        parsed = []

        def counted_parse(source, filename="<unknown>", *args, **kwargs):
            if filename == str(self.module):
                parsed.append(source)
            return real_parse(source, filename, *args, **kwargs)

        with patch.object(ast, "parse", counted_parse):
            self.assertEqual(self.run_scanner()[0], [])
            self.assertEqual(self.run_scanner()[0], [])
        self.assertEqual(len(parsed), 2, "Each main() must own an independent parse cache")

    def test_changed_text_with_same_size_and_mtime_is_seen_during_scan(self):
        self.module.write_text("one = 1\n")
        stamp = self.module.stat()
        self.test_file.write_text("from backend.sample import one, two, one\n")
        real_read = Path.read_text
        reads = []

        def changing_read(path, *args, **kwargs):
            source = real_read(path, *args, **kwargs)
            if path == self.module:
                reads.append(source)
                if len(reads) == 1:
                    path.write_text("two = 1\n")
                    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            return source

        with patch.object(Path, "read_text", changing_read):
            missing, _ = self.run_scanner()
        self.assertEqual(missing, [{"test_file": "KrabEar/tests/test_imports.py", "module": "backend.sample", "symbol": "one", "source_file": "KrabEar/backend/sample.py"}])
        self.assertEqual(reads, ["one = 1\n", "two = 1\n", "two = 1\n"])

    def test_cache_does_not_leak_between_main_calls(self):
        self.module.write_text("one = 1\n")
        self.test_file.write_text("from backend.sample import one\n")
        self.assertEqual(self.run_scanner()[0], [])
        self.module.write_text("two = 1\n")
        self.assertEqual(self.run_scanner()[0], [{"test_file": "KrabEar/tests/test_imports.py", "module": "backend.sample", "symbol": "one", "source_file": "KrabEar/backend/sample.py"}])

    def test_read_error_after_successful_lookup_is_not_hidden(self):
        self.module.write_text("one = 1\ntwo = 2\n")
        self.test_file.write_text("from backend.sample import one, two\n")
        real_read = Path.read_text
        reads = 0

        def failing_read(path, *args, **kwargs):
            nonlocal reads
            if path == self.module:
                reads += 1
                if reads == 2:
                    raise PermissionError("synthetic read failure")
            return real_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", failing_read):
            with self.assertRaisesRegex(PermissionError, "synthetic read failure"):
                self.run_scanner()

    def test_syntax_error_after_cached_valid_text_is_still_missing(self):
        self.module.write_text("one = 1\ntwo = 2\n")
        self.test_file.write_text("from backend.sample import one, two\n")
        real_read = Path.read_text
        reads = 0

        def invalidating_read(path, *args, **kwargs):
            nonlocal reads
            source = real_read(path, *args, **kwargs)
            if path == self.module:
                reads += 1
                if reads == 1:
                    path.write_text("def ?\n")
            return source

        with patch.object(Path, "read_text", invalidating_read):
            missing, _ = self.run_scanner()
        self.assertEqual(missing, [{"test_file": "KrabEar/tests/test_imports.py", "module": "backend.sample", "symbol": "two", "source_file": "KrabEar/backend/sample.py"}])

    def test_true_missing_stays_nonzero_with_fail_on_found(self):
        self.module.write_text("one = 1\n")
        self.test_file.write_text("from backend.sample import one, absent\n")
        with self.assertRaises(SystemExit) as caught:
            self.run_scanner(fail_on_found=True)
        self.assertEqual(caught.exception.code, 1)

    def test_top_level_scope_and_missing_order_are_preserved(self):
        self.module.write_text("one = 1\nclass C:\n    def hidden(self): pass\ndef outer():\n    def inner(): pass\nif True:\n    conditional: int = 2\n")
        self.test_file.write_text("from backend.sample import one, C, hidden, outer, inner, conditional, hidden\n")
        missing, _ = self.run_scanner()
        self.assertEqual([m["symbol"] for m in missing], ["hidden", "inner", "hidden"])


if __name__ == "__main__":
    unittest.main()
