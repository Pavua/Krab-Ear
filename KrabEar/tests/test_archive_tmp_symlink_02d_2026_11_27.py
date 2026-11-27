"""Файловая безопасность архива и бэкапа (ревью ChatGPT R-Krab-Ear-fix-01: RF10, RF12; 27.11.2026).

RF10: перезапись архива шла через ФИКСИРОВАННОЕ имя ``archive.ndjson.tmp``: подложенный заранее
симлинк заставлял ``open("w")`` перезаписать чужой файл, а ``replace`` превращал сам архив в симлинк.
RF12: копия settings.json в авто-бэкапе создавалась с правами по umask и только потом получала 0600.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.archive_manager import ArchiveManager  # noqa: E402
from backend.auto_backup import AutoBackupManager  # noqa: E402


class _Store:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir


def _manager_with_planted_symlink(tmp_path: Path):
    mgr = ArchiveManager(_Store(str(tmp_path)))
    mgr._archive_dir.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("VICTIM")
    # прежнее фиксированное имя временного файла — атакующий кладёт сюда симлинк заранее
    (mgr._archive_dir / "archive.ndjson.tmp").symlink_to(victim)
    return mgr, victim


def _assert_untouched(mgr, victim: Path) -> None:
    assert victim.read_text() == "VICTIM", "файл по подложенному симлинку перезаписан"
    assert not mgr._archive_path.is_symlink(), "архив стал симлинком"
    assert mgr._archive_path.is_file()
    assert stat.S_IMODE(mgr._archive_path.stat().st_mode) == 0o600
    leftovers = [p.name for p in mgr._archive_dir.iterdir() if p.name.startswith(".archive_")]
    assert not leftovers, f"остались временные файлы: {leftovers}"


def test_rewrite_archive_does_not_follow_planted_tmp_symlink(tmp_path):
    mgr, victim = _manager_with_planted_symlink(tmp_path)
    mgr._rewrite_archive([{"id": "a", "text": "t"}])
    _assert_untouched(mgr, victim)
    assert '"id": "a"' in mgr._archive_path.read_text()


def test_clear_all_does_not_follow_planted_tmp_symlink(tmp_path):
    mgr, victim = _manager_with_planted_symlink(tmp_path)
    mgr._rewrite_archive([{"id": "a", "text": "t"}])
    # второй заход: симлинк мог быть подложен и перед purge
    (mgr._archive_dir / "archive.ndjson.tmp").unlink(missing_ok=True)
    (mgr._archive_dir / "archive.ndjson.tmp").symlink_to(victim)
    assert mgr.clear_all() == 1
    _assert_untouched(mgr, victim)
    assert mgr._archive_path.read_text() == "", "purge не опустошил архив"


def test_failed_write_leaves_old_archive_and_no_temp(tmp_path, monkeypatch):
    mgr = ArchiveManager(_Store(str(tmp_path)))
    mgr._archive_dir.mkdir(parents=True, exist_ok=True)
    mgr._rewrite_archive([{"id": "old", "text": "keep"}])
    before = mgr._archive_path.read_text()

    def boom(*_a, **_k):
        raise OSError("replace failed")

    monkeypatch.setattr("backend.archive_manager.os.replace", boom)
    with pytest.raises(OSError):
        mgr._rewrite_archive([{"id": "new", "text": "x"}])
    assert mgr._archive_path.read_text() == before
    assert not [p for p in mgr._archive_dir.iterdir() if p.name.startswith(".archive_")]


def test_settings_backup_is_private_at_creation(tmp_path, monkeypatch):
    """chmod подавлен: итоговый режим = режим при СОЗДАНИИ (umask 0 дал бы 0666 у write_text)."""
    data = tmp_path / "data"
    data.mkdir()
    store = MagicMock()
    store.data_dir = str(data)
    store.history_path = data / "history.ndjson"
    store.tombstones_path = data / "tombstones.ndjson"
    store.status_path = data / "status.json"
    store.settings_path = data / "settings.json"
    store.count_active_items.return_value = 10
    for attr in ("history_path", "tombstones_path"):
        getattr(store, attr).write_text("{}", encoding="utf-8")
    store.settings_path.write_text('{"language": "ru"}', encoding="utf-8")
    old_umask = os.umask(0)
    try:
        mgr = AutoBackupManager(store=store, interval_hours=0, max_copies=5)
        monkeypatch.setattr("backend.auto_backup.os.chmod", lambda *a, **k: None)
        result = mgr._do_backup()
    finally:
        os.umask(old_umask)
    copies = list(mgr.backups_dir.glob("*/settings.json"))
    assert copies, f"settings.json не скопирован: {result}"
    mode = stat.S_IMODE(copies[0].stat().st_mode)
    assert mode == 0o600, f"settings.json создан с правами {oct(mode)}"
