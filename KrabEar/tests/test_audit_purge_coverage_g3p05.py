"""G3-privacy-05: гейт audit_purge_coverage не засчитывает покрытие без удаления.

05-01 — путь ``base / "x.json"`` засчитывается, только если доходит до удаления
        (чтение/проверка существования — нет).
05-02 — ``glob()``/``iterdir()`` засчитываются, только если найденное удаляется.
05-05 — хелпер purge ищется по вызову (не по импорту) и в правильной области.
05-06 — коллаборатору засчитываются только вызванные purge-методы его класса.
05-10 — по умолчанию пробелы дают ненулевой код выхода.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _load_guard():
    path = _SCRIPTS_DIR / "audit_purge_coverage.py"
    spec = importlib.util.spec_from_file_location("audit_purge_coverage_g3p05", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = _load_guard()

    def removed(self, source: str, name: str = "purge") -> set[str]:
        tree = ast.parse(textwrap.dedent(source))
        consts = self.guard.collect_string_constants(tree)
        attrs = self.guard._module_attr_filenames(tree, consts)
        fn = self.guard._find_function(tree, name)
        return self.guard._collect_removed_names_in_function(fn, consts, attrs)


class PathMentionIsNotDeletion(_Base):  # 05-01
    def test_read_only_reference_is_not_coverage(self):
        got = self.removed("""
            def purge(self, base):
                data = (base / "secret.json").read_text()
                if (base / "other.json").exists():
                    pass
        """)
        self.assertNotIn("secret.json", got)
        self.assertNotIn("other.json", got)

    def test_direct_unlink_is_coverage(self):
        self.assertIn("secret.json", self.removed("""
            def purge(self, base):
                (base / "secret.json").unlink(missing_ok=True)
        """))

    def test_variable_then_unlink_is_coverage(self):
        self.assertIn("secret.json", self.removed("""
            def purge(self, base):
                p = base / "secret.json"
                if p.exists():
                    p.unlink()
        """))

    def test_named_delete_helper_is_coverage(self):
        self.assertIn("secret.json", self.removed("""
            def purge(self, base):
                _unlink_quiet(base / "secret.json")
        """))

    def test_tuple_of_paths_in_delete_loop_is_coverage(self):
        got = self.removed("""
            def purge(self, base):
                for p in (base / "a.json", base / "b.json"):
                    p.unlink(missing_ok=True)
        """)
        self.assertTrue({"a.json", "b.json"} <= got)

    def test_dir_named_but_not_emptied_is_not_coverage(self):
        self.assertNotIn("rescue/", self.removed("""
            def purge(self, base):
                d = base / "rescue"
                if d.is_dir():
                    pass
        """))

    def test_dir_fully_enumerated_and_deleted_is_coverage(self):
        self.assertIn("rescue/", self.removed("""
            def purge(self, base):
                d = base / "rescue"
                for f in d.iterdir():
                    f.unlink()
        """))

    def test_loop_carried_name_read_only_is_not_coverage(self):
        self.assertNotIn("x.json", self.removed("""
            def purge(self, base):
                for name in ("x.json",):
                    (base / name).read_text()
        """))

    def test_loop_carried_name_deleted_is_coverage(self):
        self.assertIn("x.json", self.removed("""
            def purge(self, base):
                for name in ("x.json",):
                    (base / name).unlink(missing_ok=True)
        """))


class EnumerationIsNotDeletion(_Base):  # 05-02
    def test_glob_without_delete_is_not_coverage(self):
        self.assertNotIn("cache_*.bin", self.removed("""
            def purge(self, base):
                found = list(base.glob("cache_*.bin"))
                return len(found)
        """))

    def test_glob_union_into_delete_loop_is_coverage(self):
        got = self.removed("""
            def purge(self, base):
                files = set(base.glob("cache_*.bin"))
                files |= {q for q in base.glob("spill_*.part") if q.is_file()}
                for p in files:
                    p.unlink()
        """)
        self.assertTrue({"cache_*.bin", "spill_*.part"} <= got)

    def test_dir_extension_iterdir_without_delete_is_not_wipe(self):
        tree = ast.parse(textwrap.dedent("""
            from pathlib import Path

            def purge(self):
                d = Path(self.store.data_dir) / "transcripts"
                names = [p.name for p in d.iterdir()]
                count = len(list(d.glob("*.html")))
        """))
        consts = self.guard.collect_string_constants(tree)
        resolver = self.guard._DataDirBaseResolver(tree, consts)
        fn = self.guard._find_function(tree, "purge")
        cov = self.guard._dir_extension_coverage(fn, resolver, consts)
        self.assertNotIn("transcripts/*", cov)
        self.assertNotIn("transcripts/*.html", cov)


class HelperResolution(_Base):  # 05-05
    SOURCE = """
        def module_wipe(base):
            (base / "a.json").unlink()

        def compact(base):
            (base / "c.json").unlink()

        def imported_wipe(base):
            (base / "d.json").unlink()

        class Store:
            def _purge_step(self, base):
                (base / "b.json").unlink()

        class Service:
            def handle_purge_all_data(self, base):
                from backend.other import imported_wipe  # noqa: F401 — импорт без вызова
                module_wipe(base)
                self._own_step(base)
                self.store.compact()

            def _own_step(self, base):
                (base / "e.json").unlink()

            def _purge_step(self, base):
                pass
    """

    def helpers(self):
        tree = ast.parse(textwrap.dedent(self.SOURCE))
        owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Service")
        fn = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == "handle_purge_all_data")
        fn._krab_module = tree
        fn._krab_owner = owner
        return {h.name for h in self.guard._reachable_helpers(fn)}

    def test_bare_call_and_own_method_are_helpers(self):
        self.assertTrue({"module_wipe", "_own_step"} <= self.helpers())

    def test_import_is_not_a_call(self):
        self.assertNotIn("imported_wipe", self.helpers())

    def test_foreign_attribute_call_does_not_match_module_function(self):
        self.assertNotIn("compact", self.helpers(), "self.store.compact() — не модульный compact()")


class CollaboratorClassScope(_Base):  # 05-06
    MODULE = """
        class Alpha:
            def clear(self, base):
                (base / "alpha.json").unlink()

            def purge_all(self, base):
                (base / "alpha_extra.json").unlink()

        class Beta:
            def purge_all(self, base):
                (base / "beta.json").unlink()
    """

    def cleared(self, *args):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Path(tmp.name, "collab_mod.py").write_text(textwrap.dedent(self.MODULE), "utf-8")
        with mock.patch.object(self.guard, "BACKEND_DIR", Path(tmp.name)):
            return self.guard._filenames_cleared_by_module("collab_mod", *args)

    def test_only_called_methods_of_collaborator_class(self):
        self.assertEqual(self.cleared("Alpha", {"clear"}), {"alpha.json"})

    def test_other_class_methods_not_credited(self):
        self.assertNotIn("beta.json", self.cleared("Alpha", {"clear", "purge_all"}))

    def test_unknown_class_credits_nothing(self):
        self.assertEqual(self.cleared("Gamma", {"clear"}), set())


class ExitCodeDefault(_Base):  # 05-10
    def _with_gap(self):
        gap = self.guard.StoreRef("leak.json", "mod", "KrabEar/backend/mod.py:1")
        return self.guard.AuditResult(discovered={"leak.json": [gap]}, gaps=[gap])

    def test_gaps_fail_by_default(self):
        with mock.patch.object(self.guard, "run_audit", return_value=self._with_gap()), \
                mock.patch("builtins.print"):
            self.assertEqual(self.guard.main([]), 1)
            self.assertEqual(self.guard.main(["--fail-on-found"]), 1)

    def test_report_only_exits_zero(self):
        with mock.patch.object(self.guard, "run_audit", return_value=self._with_gap()), \
                mock.patch("builtins.print"):
            self.assertEqual(self.guard.main(["--report-only"]), 0)

    def test_no_gaps_exit_zero(self):
        with mock.patch.object(self.guard, "run_audit", return_value=self.guard.AuditResult()), \
                mock.patch("builtins.print"):
            self.assertEqual(self.guard.main([]), 0)


if __name__ == "__main__":
    unittest.main()
