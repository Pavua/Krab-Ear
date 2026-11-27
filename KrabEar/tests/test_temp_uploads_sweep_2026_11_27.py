"""Уборка осиротевших REST-загрузок: сырое аудио не должно жить в temp_uploads неделями.

Живой факт 27.11.2026: в рабочем temp_uploads лежали файлы с июля (177 шт., ~60 МБ) —
процесс умирал между записью и удалением в finally, а purge стирает только по команде.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend import temp_uploads_sweep as sweep  # noqa: E402

_NOW = 2_000_000_000.0


def _make(directory: Path, name: str, age_sec: float) -> Path:
    path = directory / name
    path.write_bytes(b"RIFFfake")
    os.utime(path, (_NOW - age_sec, _NOW - age_sec))
    return path


class SweepStaleUploadsTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="krab_ear_sweep_")
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name) / "temp_uploads"
        self.dir.mkdir()

    def test_old_file_removed_fresh_kept(self):
        old = _make(self.dir, "old.wav", 7200)
        fresh = _make(self.dir, "fresh.wav", 30)
        result = sweep.sweep_stale_uploads(self.dir, max_age_sec=3600, now=_NOW)
        self.assertEqual(result.deleted, 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_boundary_age_is_kept(self):
        edge = _make(self.dir, "edge.wav", 3599)
        sweep.sweep_stale_uploads(self.dir, max_age_sec=3600, now=_NOW)
        self.assertTrue(edge.exists())

    def test_symlink_and_dir_untouched(self):
        outside = Path(self._td.name) / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        os.utime(outside, (_NOW - 99999, _NOW - 99999))
        link = self.dir / "link.wav"
        link.symlink_to(outside)
        sub = self.dir / "subdir"
        sub.mkdir()
        result = sweep.sweep_stale_uploads(self.dir, max_age_sec=3600, now=_NOW)
        self.assertEqual(result.deleted, 0)
        self.assertTrue(outside.exists(), "симлинк не должен приводить к удалению цели")
        self.assertTrue(link.is_symlink())
        self.assertTrue(sub.is_dir())

    def test_symlinked_directory_is_not_walked(self):
        real = Path(self._td.name) / "real"
        real.mkdir()
        victim = _make(real, "victim.wav", 99999)
        link_dir = Path(self._td.name) / "link_dir"
        link_dir.symlink_to(real, target_is_directory=True)
        result = sweep.sweep_stale_uploads(link_dir, max_age_sec=3600, now=_NOW)
        self.assertEqual(result.deleted, 0)
        self.assertTrue(victim.exists())

    def test_missing_directory_is_not_an_error(self):
        result = sweep.sweep_stale_uploads(Path(self._td.name) / "nope", now=_NOW)
        self.assertEqual(result, sweep.SweepResult())

    def test_unlink_failure_counted_and_others_continue(self):
        a = _make(self.dir, "a.wav", 7200)
        b = _make(self.dir, "b.wav", 7200)
        real_unlink = os.unlink

        def flaky(path, *args, **kwargs):
            if str(path).endswith("a.wav"):
                raise PermissionError(13, "synthetic")
            return real_unlink(path, *args, **kwargs)

        orig = sweep.os.unlink
        sweep.os.unlink = flaky
        try:
            result = sweep.sweep_stale_uploads(self.dir, max_age_sec=3600, now=_NOW)
        finally:
            sweep.os.unlink = orig
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.deleted, 1)
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())

    def test_file_vanished_before_lstat_is_not_a_failure(self):
        """Штатный finally мог удалить файл между scandir и lstat — цель достигнута."""
        a = _make(self.dir, "a.wav", 7200)
        real_lstat = os.lstat

        def vanishing(path, *args, **kwargs):
            if str(path).endswith("a.wav"):
                raise FileNotFoundError(2, "synthetic")
            return real_lstat(path, *args, **kwargs)

        orig = sweep.os.lstat
        sweep.os.lstat = vanishing
        try:
            result = sweep.sweep_stale_uploads(self.dir, max_age_sec=3600, now=_NOW)
        finally:
            sweep.os.lstat = orig
        self.assertEqual(result.failed, 0)
        self.assertTrue(a.exists())


class ThrottleTest(unittest.TestCase):
    def setUp(self):
        sweep._last_run.clear()
        self.addCleanup(sweep._last_run.clear)
        self._td = tempfile.TemporaryDirectory(prefix="krab_ear_sweep_thr_")
        self.addCleanup(self._td.cleanup)

    def test_second_call_within_interval_is_skipped(self):
        first = sweep.maybe_sweep_stale_uploads(self._td.name, min_interval_sec=600, now=1000.0)
        second = sweep.maybe_sweep_stale_uploads(self._td.name, min_interval_sec=600, now=1100.0)
        third = sweep.maybe_sweep_stale_uploads(self._td.name, min_interval_sec=600, now=1700.0)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNotNone(third)


class RestServerWiringTest(unittest.TestCase):
    """Без вызова из rest_server модуль — декоративный код (урок audit_decorative_wiring)."""

    def _tree(self):
        src = (_ROOT / "backend" / "rest_server.py").read_text(encoding="utf-8")
        return ast.parse(src)

    def _calls(self, scope):
        names = set()
        for node in ast.walk(scope):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    names.add(func.attr)
                elif isinstance(func, ast.Name):
                    names.add(func.id)
        return names

    def test_sweep_not_run_at_import_but_at_server_start(self):
        """Импорт rest_server (его делают десятки тестов) не должен чистить реальный
        ~/.krab_ear_data/temp_uploads: стартовая уборка — только в ``__main__``."""
        tree = self._tree()
        unconditional = ast.Module(body=[n for n in tree.body if not isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If))], type_ignores=[])
        self.assertNotIn("sweep_stale_uploads", self._calls(unconditional))
        main_blocks = [
            n for n in tree.body
            if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
            and isinstance(n.test.left, ast.Name) and n.test.left.id == "__name__"
        ]
        self.assertEqual(len(main_blocks), 1, "блок if __name__ == '__main__' не найден")
        main = ast.Module(body=main_blocks[0].body, type_ignores=[])
        self.assertIn("sweep_stale_uploads", self._calls(main))

    def test_upload_handler_calls_throttled_sweep(self):
        tree = self._tree()
        handlers = [n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "transcribe_audio"]
        self.assertTrue(handlers, "обработчик transcribe_audio не найден")
        self.assertIn("maybe_sweep_stale_uploads", self._calls(handlers[0]))


if __name__ == "__main__":
    unittest.main()
