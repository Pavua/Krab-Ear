"""Права и защита от симлинков для temp_uploads (ревью: каталог 0755 / файлы 0644)."""

import ast
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from backend.private_upload import ensure_private_dir, save_private


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


class _FakeFileStorage:
    """Как werkzeug FileStorage: save(dst) принимает файловый объект."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def save(self, dst) -> None:
        dst.write(self._data)


class PrivateUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_umask = os.umask(0o022)  # типичный umask, дающий 0755/0644

    def tearDown(self) -> None:
        os.umask(self._old_umask)
        self._tmp.cleanup()

    def test_new_dir_is_0700_despite_umask(self) -> None:
        d = self.root / "temp_uploads"
        ensure_private_dir(d)
        self.assertEqual(_mode(d), 0o700)

    def test_existing_loose_dir_is_tightened(self) -> None:
        d = self.root / "temp_uploads"
        d.mkdir(mode=0o755)
        os.chmod(d, 0o755)
        ensure_private_dir(d)
        self.assertEqual(_mode(d), 0o700)

    def test_symlink_dir_is_refused_and_target_untouched(self) -> None:
        target = self.root / "target"
        target.mkdir()
        os.chmod(target, 0o755)
        link = self.root / "temp_uploads"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            ensure_private_dir(link)
        self.assertEqual(_mode(target), 0o755)

    def test_file_is_0600_bytes_and_filestorage(self) -> None:
        d = self.root / "u"
        ensure_private_dir(d)
        save_private(b"abc", d / "a.wav")
        save_private(_FakeFileStorage(b"xyz"), d / "b.wav")
        self.assertEqual((d / "a.wav").read_bytes(), b"abc")
        self.assertEqual((d / "b.wav").read_bytes(), b"xyz")
        self.assertEqual(_mode(d / "a.wav"), 0o600)
        self.assertEqual(_mode(d / "b.wav"), 0o600)

    def test_file_never_visible_with_loose_mode(self) -> None:
        """Атомарность: режим 0600 задан при создании, а не chmod после записи."""
        d = self.root / "u"
        ensure_private_dir(d)
        seen: list[int] = []

        class _Probe:
            def save(self, dst) -> None:
                seen.append(stat.S_IMODE(os.fstat(dst.fileno()).st_mode))
                dst.write(b"1")

        save_private(_Probe(), d / "p.wav")
        self.assertEqual(seen, [0o600])

    def test_existing_symlink_target_is_not_followed(self) -> None:
        d = self.root / "u"
        ensure_private_dir(d)
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        (d / "x.wav").symlink_to(victim)
        with self.assertRaises(OSError):
            save_private(b"evil", d / "x.wav")
        self.assertEqual(victim.read_bytes(), b"keep")

    def test_existing_file_is_not_overwritten(self) -> None:
        d = self.root / "u"
        ensure_private_dir(d)
        (d / "e.wav").write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            save_private(b"new", d / "e.wav")
        self.assertEqual((d / "e.wav").read_bytes(), b"old")

    def test_parent_replaced_by_symlink_is_refused(self) -> None:
        real = self.root / "real"
        real.mkdir()
        link = self.root / "u"
        link.symlink_to(real)
        with self.assertRaises(OSError):
            save_private(b"x", link / "f.wav")
        self.assertEqual(list(real.iterdir()), [])

    def test_save_from_other_thread(self) -> None:
        d = self.root / "u"
        ensure_private_dir(d)
        errs: list[BaseException] = []

        def run() -> None:
            try:
                save_private(b"t", d / "t.wav")
            except BaseException as exc:  # noqa: BLE001
                errs.append(exc)

        t = threading.Thread(target=run)
        t.start()
        t.join()
        self.assertEqual(errs, [])
        self.assertEqual(_mode(d / "t.wav"), 0o600)


class RestServerWiringTests(unittest.TestCase):
    """AST: загрузка идёт через private_upload, а не голый file.save/write_bytes."""

    @classmethod
    def setUpClass(cls) -> None:
        src = (Path(__file__).resolve().parents[1] / "backend" / "rest_server.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(src)

    def _calls(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                yield node

    def test_upload_uses_save_private_and_ensure_private_dir(self) -> None:
        names = {c.func.id for c in self._calls() if isinstance(c.func, ast.Name)}
        self.assertIn("save_private", names)
        self.assertIn("ensure_private_dir", names)

    def test_no_bare_save_or_write_bytes_on_temp_path(self) -> None:
        for c in self._calls():
            f = c.func
            if isinstance(f, ast.Attribute) and f.attr in {"save", "write_bytes"}:
                if isinstance(f.value, ast.Name) and f.value.id in {"file", "temp_path"}:
                    self.fail(f"голая запись {f.value.id}.{f.attr} в rest_server (строка {c.lineno})")

    def test_no_bare_temp_dir_mkdir(self) -> None:
        for c in self._calls():
            f = c.func
            if isinstance(f, ast.Attribute) and f.attr == "mkdir":
                if isinstance(f.value, ast.Name) and f.value.id == "TEMP_DIR":
                    self.fail(f"TEMP_DIR.mkdir без 0700 (строка {c.lineno})")


if __name__ == "__main__":
    unittest.main()
