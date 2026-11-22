"""Изоляция A5.3: операции через dir_fd внутри fixture-профиля допустимы, вне него — нет.

`shutil.rmtree` (purge, `rmtree_strict`) по умолчанию идёт через dir_fd. Безусловный
запрет dir_fd (tests-09) ломал `test_purge_crash_recovery`; безусловное разрешение
открывало обход (путь относительно ЧУЖОГО каталога). Решение — по объекту дескриптора.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import _plaintext_export_integration_backend as backend


class DirFdTargetResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.root = base / "fixture"
        self.outside = base / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        (self.root / "child").mkdir()

    def _dir_fd(self, path: Path) -> int:
        fd = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        return fd

    def test_relative_name_under_fixture_dir_fd_is_inside(self) -> None:
        resolved = backend.resolve_mutation_target("child", self._dir_fd(self.root))
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.is_relative_to(self.root))

    def test_relative_name_under_external_dir_fd_is_outside(self) -> None:
        resolved = backend.resolve_mutation_target("victim", self._dir_fd(self.outside))
        self.assertIsNotNone(resolved)
        self.assertFalse(resolved.is_relative_to(self.root))

    def test_parent_escape_through_fixture_dir_fd_is_outside(self) -> None:
        resolved = backend.resolve_mutation_target("../outside/victim", self._dir_fd(self.root))
        self.assertIsNotNone(resolved)
        self.assertFalse(resolved.is_relative_to(self.root))

    def test_without_dir_fd_uses_path_as_is(self) -> None:
        resolved = backend.resolve_mutation_target(str(self.root / "child"), None)
        self.assertTrue(resolved.is_relative_to(self.root))

    def test_unresolvable_descriptor_fails_closed(self) -> None:
        self.assertIsNone(backend.resolve_mutation_target("x", 987654))


if __name__ == "__main__":
    unittest.main()
