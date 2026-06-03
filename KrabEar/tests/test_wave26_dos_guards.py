"""Тесты DoS-guards wave-26 (MED):

C1 — audio_quality.analyze_file: отказ при файле > _MAX_AUDIO_FILE_BYTES
C2 — data_migrator.migrate / _create_backup: без резервной копии когда
     current == target_version; лимит _MAX_MIGRATION_BACKUPS старых бэкапов
C3 — language_learning.get_learning_stats: cap на _MAX_ITEMS_FOR_STATS записей
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# C1 — audio_quality.analyze_file size guard
# ---------------------------------------------------------------------------

class TestAudioQualityFileSizeGuard(unittest.TestCase):
    """analyze_file должна отклонять файлы > _MAX_AUDIO_FILE_BYTES."""

    def _write_tmp_file(self, size_bytes: int) -> Path:
        """Создаёт временный файл нужного размера."""
        tmp = Path(tempfile.mktemp(suffix=".wav"))
        tmp.write_bytes(b"\x00" * size_bytes)
        return tmp

    def test_file_within_limit_calls_sf_read(self) -> None:
        """Файл меньше лимита → sf.read должен быть вызван."""
        from core.audio_quality import analyze_file
        import numpy as np

        small = self._write_tmp_file(1024)
        try:
            fake_audio = np.zeros(16000, dtype="float32")
            with patch("soundfile.read", return_value=(fake_audio, 16000)):
                report = analyze_file(small)
            self.assertEqual(report.quality_score, "poor")  # тишина → poor
        finally:
            small.unlink(missing_ok=True)

    def test_file_over_limit_raises_value_error(self) -> None:
        """Файл больше _MAX_AUDIO_FILE_BYTES → ValueError (не OOM-нагрузка)."""
        from core.audio_quality import analyze_file, _MAX_AUDIO_FILE_BYTES

        over_limit = self._write_tmp_file(_MAX_AUDIO_FILE_BYTES + 1)
        try:
            with self.assertRaises(ValueError) as ctx:
                analyze_file(over_limit)
            self.assertIn("МБ", str(ctx.exception))
        finally:
            over_limit.unlink(missing_ok=True)

    def test_file_exactly_at_limit_is_allowed(self) -> None:
        """Файл ровно на пределе → sf.read вызывается, исключение не бросается."""
        from core.audio_quality import analyze_file, _MAX_AUDIO_FILE_BYTES
        import numpy as np

        at_limit = self._write_tmp_file(_MAX_AUDIO_FILE_BYTES)
        try:
            fake_audio = np.zeros(16000, dtype="float32")
            with patch("soundfile.read", return_value=(fake_audio, 16000)):
                report = analyze_file(at_limit)
            self.assertIsNotNone(report)
        finally:
            at_limit.unlink(missing_ok=True)

    def test_nonexistent_file_raises_file_not_found(self) -> None:
        """Несуществующий файл → FileNotFoundError (не ValueError от guard)."""
        from core.audio_quality import analyze_file

        with self.assertRaises(FileNotFoundError):
            analyze_file("/tmp/nonexistent_krab_ear_test_file.wav")

    def test_max_audio_file_bytes_constant_is_reasonable(self) -> None:
        """Константа должна быть > 10 MB и < 2 GB (разумный диапазон)."""
        from core.audio_quality import _MAX_AUDIO_FILE_BYTES

        self.assertGreater(_MAX_AUDIO_FILE_BYTES, 10 * 1024 * 1024)
        self.assertLess(_MAX_AUDIO_FILE_BYTES, 2 * 1024 * 1024 * 1024)


# ---------------------------------------------------------------------------
# C2 — data_migrator: no backup when already at target; backup cap
# ---------------------------------------------------------------------------

def _write_v2_history(data_dir: Path) -> None:
    """Создаёт history.ndjson с одной v2.0 записью."""
    record = {"id": "1", "text": "привет", "tags": [], "favorite": False, "annotation": ""}
    (data_dir / "history.ndjson").write_text(json.dumps(record) + "\n", encoding="utf-8")


def _write_v1_history(data_dir: Path) -> None:
    """Создаёт history.ndjson с одной v1.0 записью (без tags/favorite)."""
    record = {"id": "1", "text": "привет"}
    (data_dir / "history.ndjson").write_text(json.dumps(record) + "\n", encoding="utf-8")


class TestDataMigratorNoBackupWhenUpToDate(unittest.TestCase):
    """migrate() не создаёт резервную копию когда текущая версия == целевой."""

    def test_migrate_already_at_target_no_backup_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_v2_history(data_dir)

            from backend.data_migrator import DataMigrator
            migrator = DataMigrator(data_dir)
            result = migrator.migrate(data_dir, "2.0")

            self.assertEqual(result.items_migrated, 0)
            self.assertEqual(result.backup_path, "")

            # backups/ директория не должна была быть создана
            backups_dir = data_dir / "backups"
            self.assertFalse(backups_dir.exists(), "backup directory must NOT exist when no migration needed")

    def test_migrate_already_at_target_idempotent(self) -> None:
        """Повторные вызовы когда уже v2.0 — никаких side-effects."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_v2_history(data_dir)

            from backend.data_migrator import DataMigrator
            migrator = DataMigrator(data_dir)

            for _ in range(5):
                result = migrator.migrate(data_dir, "2.0")
                self.assertEqual(result.backup_path, "")

            backups_dir = data_dir / "backups"
            self.assertFalse(backups_dir.exists())

    def test_migrate_v1_creates_backup(self) -> None:
        """Настоящая миграция v1→v2 должна создавать бэкап."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_v1_history(data_dir)

            from backend.data_migrator import DataMigrator
            migrator = DataMigrator(data_dir)
            result = migrator.migrate(data_dir, "2.0")

            self.assertEqual(result.items_migrated, 1)
            self.assertNotEqual(result.backup_path, "")
            self.assertTrue(Path(result.backup_path).is_dir())


class TestDataMigratorBackupCap(unittest.TestCase):
    """_create_backup должна удалять старые бэкапы сверх _MAX_MIGRATION_BACKUPS."""

    def test_backup_cap_enforced(self) -> None:
        """После N+1 бэкапов самый старый должен быть удалён."""
        from backend.data_migrator import DataMigrator, _MAX_MIGRATION_BACKUPS

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            backups_dir = data_dir / "backups"
            backups_dir.mkdir()
            _write_v2_history(data_dir)

            migrator = DataMigrator(data_dir)

            # Создаём _MAX_MIGRATION_BACKUPS + 2 бэкапа вручную, потом ещё один через _create_backup
            import time
            created: list[str] = []
            for i in range(_MAX_MIGRATION_BACKUPS + 1):
                ts = f"20260101_00000{i}"
                d = backups_dir / f"migration_backup_{ts}"
                d.mkdir()
                (d / "migration_meta.json").write_text("{}", encoding="utf-8")
                created.append(d.name)
                time.sleep(0.001)  # гарантируем разные mtime (для сортировки)

            # Вызываем _create_backup ещё раз — должно удалить лишние
            migrator._create_backup(data_dir)

            remaining = sorted([
                d.name for d in backups_dir.iterdir()
                if d.is_dir() and d.name.startswith("migration_backup_")
            ])
            self.assertLessEqual(len(remaining), _MAX_MIGRATION_BACKUPS)

    def test_max_migration_backups_constant_reasonable(self) -> None:
        """Константа должна быть в диапазоне [2, 20]."""
        from backend.data_migrator import _MAX_MIGRATION_BACKUPS
        self.assertGreaterEqual(_MAX_MIGRATION_BACKUPS, 2)
        self.assertLessEqual(_MAX_MIGRATION_BACKUPS, 20)


class TestDataMigratorHandleRunMigrationEarlyReturn(unittest.TestCase):
    """handle_run_migration возвращает backup_path='' когда миграция не нужна."""

    def test_handle_run_migration_already_v2_no_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_v2_history(data_dir)

            from backend.data_migrator import DataMigrator
            migrator = DataMigrator(data_dir)
            result = migrator.handle_run_migration({"target_version": "2.0"})

            self.assertEqual(result["items_migrated"], 0)
            self.assertEqual(result["backup_path"], "")


# ---------------------------------------------------------------------------
# C3 — language_learning.get_learning_stats items cap
# ---------------------------------------------------------------------------

class FakeItem:
    def __init__(self, text: str = "слово тест проверка", ts: str = "2026-01-01T00:00:00") -> None:
        self.text = text
        self.source_text = text
        self.translated_text = "word test check"
        self.source_lang = "ru"
        self.target_lang = "en"
        self.ts = ts


class TestLanguageLearningStatsCap(unittest.TestCase):
    """get_learning_stats должна обрабатывать только последние _MAX_ITEMS_FOR_STATS записей."""

    def test_get_learning_stats_cap_applied(self) -> None:
        """При превышении лимита items_scanned == _MAX_ITEMS_FOR_STATS."""
        from backend.language_learning import LanguageLearningManager
        from backend.language_learning import _MAX_ITEMS_FOR_STATS as _CAP

        mgr = LanguageLearningManager()
        items = [FakeItem(f"слово{i} тест{i} проверка{i}") for i in range(_CAP + 50)]

        stats = mgr.get_learning_stats(items, "ru", "en")
        self.assertLessEqual(stats["items_scanned"], _CAP)
        self.assertEqual(stats["items_scanned"], _CAP)

    def test_get_learning_stats_small_list_not_capped(self) -> None:
        """Список меньше лимита — items_scanned == len(items)."""
        from backend.language_learning import LanguageLearningManager

        mgr = LanguageLearningManager()
        items = [FakeItem() for _ in range(10)]

        stats = mgr.get_learning_stats(items, "ru", "en")
        self.assertEqual(stats["items_scanned"], 10)

    def test_get_learning_stats_uses_tail_items(self) -> None:
        """Когда список > лимита, обрабатываются ПОСЛЕДНИЕ _MAX_ITEMS_FOR_STATS записей."""
        from backend.language_learning import LanguageLearningManager, _MAX_ITEMS_FOR_STATS

        mgr = LanguageLearningManager()

        # Первые записи с нейтральным словом, последние — с уникальным
        unique_word = "уникальный"
        early = [FakeItem("нейтральный текст заполнитель")] * (_MAX_ITEMS_FOR_STATS + 10)
        tail = [FakeItem(f"{unique_word} тест проверка")]

        stats = mgr.get_learning_stats(early + tail, "ru", "en")
        top_words = [w["word"] for w in stats["top_words"]]
        # Уникальное слово должно быть в top_words (оно в хвосте)
        self.assertIn(unique_word, top_words)

    def test_get_learning_stats_empty_list(self) -> None:
        """Пустой список → items_scanned == 0, нет ошибок."""
        from backend.language_learning import LanguageLearningManager

        mgr = LanguageLearningManager()
        stats = mgr.get_learning_stats([], "ru", "en")
        self.assertEqual(stats["items_scanned"], 0)
        self.assertEqual(stats["unique_words"], 0)

    def test_max_items_for_stats_constant_reasonable(self) -> None:
        """Константа должна быть >= 100 и <= 10000."""
        from backend.language_learning import _MAX_ITEMS_FOR_STATS
        self.assertGreaterEqual(_MAX_ITEMS_FOR_STATS, 100)
        self.assertLessEqual(_MAX_ITEMS_FOR_STATS, 10000)

    def test_handle_get_learning_stats_returns_items_scanned(self) -> None:
        """IPC-handler handle_get_learning_stats должен возвращать items_scanned."""
        from backend.language_learning import LanguageLearningManager

        mgr = LanguageLearningManager()
        items = [FakeItem() for _ in range(5)]
        result = mgr.handle_get_learning_stats({
            "source_lang": "ru",
            "target_lang": "en",
            "items": items,
        })
        self.assertIn("items_scanned", result)
        self.assertEqual(result["items_scanned"], 5)


if __name__ == "__main__":
    unittest.main()
