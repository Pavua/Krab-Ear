"""Внешнее ревью E2 (2026-11-15): purge не должен врать об успехе.

Боевой purge звал ``shutil.rmtree(..., ignore_errors=True)`` и писал в лог
«удалено», а ``purge_pending_restore_staging`` добавляла путь в ``removed``
независимо от результата. Для ``shares/`` это открытый текст пакетов
транскрипций: владелец видел ``complete: true``, а данные оставались.

Файловая неудача моделируется настоящей файловой системой: каталог без права
записи не даёт удалить файлы внутри. Под root такое невозможно, тесты
пропускаются.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _make_undeletable_dir(path: Path) -> Path:
    """Каталог с файлом внутри, который нельзя удалить (родитель r-x)."""
    path.mkdir(parents=True)
    (path / "secret.txt").write_text("транскрипция", encoding="utf-8")
    os.chmod(path, 0o500)
    return path


@unittest.skipIf(_IS_ROOT, "root обходит права файловой системы")
class RmtreeStrictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            if p.is_dir():
                os.chmod(p, 0o700)
        os.chmod(self.tmp, 0o700)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_raises_when_files_cannot_be_removed(self):
        from backend.purge_fs import rmtree_strict

        target = _make_undeletable_dir(self.tmp / "shares")

        with self.assertRaises(OSError):
            rmtree_strict(target)

        self.assertTrue((target / "secret.txt").exists())

    def test_removes_normal_tree(self):
        from backend.purge_fs import rmtree_strict

        target = self.tmp / "ok"
        (target / "sub").mkdir(parents=True)
        (target / "sub" / "a.txt").write_text("x", encoding="utf-8")

        rmtree_strict(target)

        self.assertFalse(target.exists())

    def test_missing_directory_is_not_an_error(self):
        from backend.purge_fs import rmtree_strict

        rmtree_strict(self.tmp / "never-existed")


@unittest.skipIf(_IS_ROOT, "root обходит права файловой системы")
class PurgeAllDataReportsFailedRmtree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        for p in Path(self.tmp).rglob("*"):
            if p.is_dir():
                os.chmod(p, 0o700)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _svc(self):
        from tests.test_purge_all_data_w1730 import _make_svc

        return _make_svc(self.tmp, item_ids=["a"])

    def test_undeletable_shares_dir_is_reported_not_complete(self):
        _make_undeletable_dir(Path(self.tmp) / "shares")

        result = self._svc().handle_purge_all_data({"confirm": True})

        self.assertIn("shares", result["errors"], result)
        self.assertFalse(result["complete"], result)

    def test_undeletable_backups_dir_is_reported_not_complete(self):
        _make_undeletable_dir(Path(self.tmp) / "backups" / "migration_backup_1")
        os.chmod(Path(self.tmp) / "backups", 0o500)

        result = self._svc().handle_purge_all_data({"confirm": True})

        self.assertIn("backups", result["errors"], result)
        self.assertFalse(result["complete"], result)

    def test_clean_purge_still_complete(self):
        (Path(self.tmp) / "shares").mkdir()
        (Path(self.tmp) / "shares" / "p.json").write_text("{}", encoding="utf-8")

        result = self._svc().handle_purge_all_data({"confirm": True})

        self.assertNotIn("shares", result["errors"], result)
        self.assertFalse((Path(self.tmp) / "shares").exists())


@unittest.skipIf(_IS_ROOT, "root обходит права файловой системы")
class PurgeRestoreStagingIsHonest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            if p.is_dir():
                os.chmod(p, 0o700)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_undeletable_staging_raises_instead_of_claiming_removed(self):
        from backend.encrypted_snapshot import (
            RESTORE_STAGING_PREFIX,
            purge_pending_restore_staging,
        )

        _make_undeletable_dir(self.tmp / f"{RESTORE_STAGING_PREFIX}stuck")

        with self.assertRaises(OSError):
            purge_pending_restore_staging(self.tmp)

    def test_removable_artifacts_are_still_removed_before_raising(self):
        from backend.encrypted_snapshot import (
            RESTORE_STAGING_PREFIX,
            purge_pending_restore_staging,
        )

        _make_undeletable_dir(self.tmp / f"{RESTORE_STAGING_PREFIX}a-stuck")
        ok = self.tmp / f"{RESTORE_STAGING_PREFIX}z-ok"
        ok.mkdir()
        (ok / "f").write_text("x", encoding="utf-8")

        with self.assertRaises(OSError):
            purge_pending_restore_staging(self.tmp)

        self.assertFalse(ok.exists(), "purge обязан дойти до конца")


if __name__ == "__main__":
    unittest.main()
