"""Coverage tests for ObsidianSyncManager (Wave 82).

Дополнительное покрытие:
- YAML frontmatter в созданных .md файлах
- Инкрементальная синхронизация (timestamp-based)
- Форсированный режим (force=True)
- Пропуск при отсутствующем vault_path
- Персистентность состояния в obsidian_sync.json
- Unicode в именах файлов
- Пустая история
- Спецсимволы YAML в содержимом
- Файлы вне namespace
- Маркировка ошибок в результате
- Продолжение после ошибки одного файла
- Восстановление после повреждённого state-файла
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path setup (standalone run без установленного пакета)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.obsidian_sync import ObsidianSyncManager  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(
    item_id: str = "test-id-0000-0000-0000-000000000001",
    ts: str = "2025-06-01T12:00:00+00:00",
    text: str = "Тестовая транскрипция.",
    translated_text: str = "",
    translation_mode: str = "off",
    source_lang: str = "ru",
    target_lang: str = "",
    tags: list | None = None,
    diarization: dict | None = None,
    confidence: float | None = 0.95,
) -> dict:
    return {
        "id": item_id,
        "ts": ts,
        "text": text,
        "translated_text": translated_text,
        "translation_mode": translation_mode,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "tags": tags or [],
        "diarization": diarization,
        "confidence": confidence,
    }


class _Base(unittest.TestCase):
    """Base class — создаёт tmp dirs + ObsidianSyncManager с настроенным vault."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault_dir.mkdir()
        self.mgr = ObsidianSyncManager(data_dir=self.data_dir)
        self.mgr.configure(str(self.vault_dir))

    def _target_dir(self) -> Path:
        return self.vault_dir / "Transcriptions"


# ===========================================================================
# 1. YAML frontmatter
# ===========================================================================

class TestSyncCreatesMdFilesWithYamlFrontmatter(_Base):
    """test_sync_creates_md_files_with_yaml_frontmatter."""

    def test_sync_creates_md_files_with_yaml_frontmatter(self) -> None:
        """Созданные .md файлы содержат YAML frontmatter (начинается с '---')."""
        result = self.mgr.sync([_item()])
        self.assertEqual(result.synced_count, 1)
        md_path = Path(result.new_files[0])
        self.assertTrue(md_path.exists(), "MD file must exist")
        content = md_path.read_text(encoding="utf-8")
        self.assertTrue(
            content.startswith("---"),
            "Файл должен начинаться с YAML frontmatter '---'",
        )
        # Должен быть закрывающий ---
        lines = content.splitlines()
        self.assertIn("---", lines[1:], "Frontmatter должен иметь закрывающий '---'")

    def test_frontmatter_contains_required_keys(self) -> None:
        """Frontmatter содержит ключи title, date, id, tags, source."""
        result = self.mgr.sync([_item()])
        content = Path(result.new_files[0]).read_text(encoding="utf-8")
        for key in ("title:", "date:", "id:", "tags:", "source:"):
            self.assertIn(key, content, f"Ключ '{key}' должен быть во frontmatter")

    def test_frontmatter_source_is_krab_ear(self) -> None:
        """Frontmatter.source = krab-ear."""
        result = self.mgr.sync([_item()])
        content = Path(result.new_files[0]).read_text(encoding="utf-8")
        self.assertIn("source: krab-ear", content)


# ===========================================================================
# 2. Incremental sync
# ===========================================================================

class TestSyncIncrementalOnlyNewItems(_Base):
    """test_sync_incremental_only_new_items."""

    def test_sync_incremental_only_new_items(self) -> None:
        """Инкрементальная синхронизация не трогает записи старше last_sync_ts."""
        old_item = _item(
            item_id="old-item-000-0000-0000-000000000001",
            ts="2025-01-01T00:00:00+00:00",
            text="Старая запись.",
        )
        new_item = _item(
            item_id="new-item-000-0000-0000-000000000002",
            ts="2030-01-01T00:00:00+00:00",
            text="Новая запись.",
        )

        # First sync — сохраняет old_item, обновляет last_sync_ts
        r1 = self.mgr.sync([old_item], force=True)
        self.assertEqual(r1.synced_count, 1)

        # Second sync — old_item должен быть пропущен, new_item синхронизирован
        r2 = self.mgr.sync([old_item, new_item])
        self.assertEqual(r2.synced_count, 1, "Только новый item должен быть синхронизирован")
        self.assertEqual(r2.skipped_count, 1, "Старый item должен быть пропущен")
        self.assertIn("new-item", r2.new_files[0] + r2.updated_files[:1][0] if r2.updated_files else r2.new_files[0])


# ===========================================================================
# 3. Forced mode
# ===========================================================================

class TestSyncForcedModeOverwritesAll(_Base):
    """test_sync_forced_mode_overwrites_all."""

    def test_sync_forced_mode_overwrites_all(self) -> None:
        """force=True синхронизирует все записи независимо от last_sync_ts."""
        old_item = _item(
            item_id="force-test-00-0000-0000-000000000001",
            ts="2020-01-01T00:00:00+00:00",
            text="Очень старая запись.",
        )

        # Первый sync — устанавливает last_sync_ts в будущее
        self.mgr.sync([old_item], force=True)

        # Второй sync — без force пропустил бы old_item
        r_incremental = self.mgr.sync([old_item])
        self.assertEqual(r_incremental.skipped_count, 1)

        # Третий sync с force=True — должен синхронизировать
        r_forced = self.mgr.sync([old_item], force=True)
        self.assertEqual(r_forced.synced_count, 1)
        self.assertEqual(r_forced.skipped_count, 0)


# ===========================================================================
# 4. Skip when vault path missing
# ===========================================================================

class TestSyncSkipsWhenVaultPathMissing(unittest.TestCase):
    """test_sync_skips_when_vault_path_missing."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)

    def test_sync_skips_when_vault_path_missing(self) -> None:
        """sync() вызывает RuntimeError когда vault не настроен."""
        mgr = ObsidianSyncManager(data_dir=self.data_dir)
        with self.assertRaises(RuntimeError):
            mgr.sync([_item()])

    def test_sync_without_configure_raises_runtime_error(self) -> None:
        """get_sync_status() возвращает configured=False без configure()."""
        mgr = ObsidianSyncManager(data_dir=self.data_dir)
        status = mgr.get_sync_status()
        self.assertFalse(status["configured"])


# ===========================================================================
# 5. State persistence
# ===========================================================================

class TestSyncStatePersistedToObsidianSyncJson(_Base):
    """test_sync_state_persisted_to_obsidian_sync_json."""

    def test_sync_state_persisted_to_obsidian_sync_json(self) -> None:
        """После sync() состояние last_sync_ts сохраняется в obsidian_sync.json."""
        self.mgr.sync([_item()])
        state_path = self.data_dir / "obsidian_sync.json"
        self.assertTrue(state_path.exists(), "obsidian_sync.json должен быть создан")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(state.get("last_sync_ts"))
        self.assertIsNotNone(state.get("vault_path"))

    def test_state_reloaded_on_new_instance(self) -> None:
        """Новый экземпляр загружает vault_path и last_sync_ts из JSON."""
        self.mgr.sync([_item()])
        original_status = self.mgr.get_sync_status()

        # Создаём новый экземпляр с тем же data_dir
        mgr2 = ObsidianSyncManager(data_dir=self.data_dir)
        reloaded_status = mgr2.get_sync_status()

        self.assertTrue(reloaded_status["configured"])
        self.assertEqual(reloaded_status["last_sync_ts"], original_status["last_sync_ts"])


# ===========================================================================
# 6. Unicode filenames
# ===========================================================================

class TestSyncHandlesUnicodeFilenames(_Base):
    """test_sync_handles_unicode_filenames."""

    def test_sync_handles_unicode_filenames(self) -> None:
        """ID с unicode-символами создаёт файл с безопасным именем."""
        unicode_id = "тест-запись-☆-001"
        result = self.mgr.sync([_item(item_id=unicode_id)], force=True)
        self.assertEqual(result.synced_count, 1, f"Errors: {result.errors}")
        md_path = Path(result.new_files[0])
        self.assertTrue(md_path.exists())
        # Имя файла должно оканчиваться на .md
        self.assertTrue(md_path.name.endswith(".md"))

    def test_sync_unicode_text_preserved_in_file(self) -> None:
        """Unicode-текст транскрипции сохраняется корректно в .md файле."""
        unicode_text = "Привет мир! 日本語 Ñoño"
        result = self.mgr.sync([_item(text=unicode_text)], force=True)
        content = Path(result.new_files[0]).read_text(encoding="utf-8")
        self.assertIn(unicode_text, content)


# ===========================================================================
# 7. Empty history
# ===========================================================================

class TestSyncHandlesEmptyHistory(_Base):
    """test_sync_handles_empty_history."""

    def test_sync_handles_empty_history(self) -> None:
        """sync([]) возвращает SyncResult с нулевыми счётчиками."""
        result = self.mgr.sync([])
        self.assertEqual(result.synced_count, 0)
        self.assertEqual(result.skipped_count, 0)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.new_files, [])
        self.assertEqual(result.updated_files, [])

    def test_sync_empty_still_updates_last_sync_ts(self) -> None:
        """sync([]) обновляет last_sync_ts."""
        self.mgr.sync([])
        status = self.mgr.get_sync_status()
        self.assertIsNotNone(status["last_sync_ts"])


# ===========================================================================
# 8. Invalid YAML chars in content
# ===========================================================================

class TestSyncWithInvalidYamlCharsInContent(_Base):
    """test_sync_with_invalid_yaml_chars_in_content."""

    def test_sync_with_invalid_yaml_chars_in_content(self) -> None:
        """Текст с YAML-спецсимволами не ломает создание файла."""
        tricky_text = 'Это: текст "с кавычками" и --- и *** и \x00 символ'
        result = self.mgr.sync([_item(text=tricky_text)], force=True)
        self.assertEqual(result.synced_count, 1, f"Errors: {result.errors}")
        md_path = Path(result.new_files[0])
        self.assertTrue(md_path.exists())
        # Файл должен быть читаем
        content = md_path.read_text(encoding="utf-8")
        self.assertTrue(len(content) > 0)

    def test_sync_with_colon_in_tags(self) -> None:
        """Теги с двоеточием нормализуются без исключений."""
        result = self.mgr.sync([_item(tags=["key: value", "a:b:c"])], force=True)
        self.assertEqual(result.synced_count, 1)
        self.assertEqual(result.errors, [])


# ===========================================================================
# 9. Files outside namespace preserved
# ===========================================================================

class TestSyncPreservesExistingMdFilesOutsideNamespace(_Base):
    """test_sync_preserves_existing_md_files_outside_namespace."""

    def test_sync_preserves_existing_md_files_outside_namespace(self) -> None:
        """Sync не трогает .md файлы в vault за пределами папки Transcriptions."""
        # Создаём файл непосредственно в vault (не в Transcriptions)
        external_md = self.vault_dir / "my_notes.md"
        external_md.write_text("# Мои заметки\nНе трогать!", encoding="utf-8")

        self.mgr.sync([_item()], force=True)

        # Внешний файл должен остаться нетронутым
        self.assertTrue(external_md.exists(), "Файл вне namespace должен остаться")
        content = external_md.read_text(encoding="utf-8")
        self.assertIn("Не трогать!", content)

    def test_sync_does_not_delete_other_md_in_target_folder(self) -> None:
        """Sync не удаляет .md файлы в Transcriptions, которые он не создавал."""
        target_dir = self._target_dir()
        manual_md = target_dir / "manual_note.md"
        manual_md.write_text("# Ручная заметка", encoding="utf-8")

        self.mgr.sync([_item()], force=True)

        self.assertTrue(manual_md.exists(), "Ручной .md файл должен остаться")


# ===========================================================================
# 10. Marks failed files in state (errors in SyncResult)
# ===========================================================================

class TestSyncMarksFailed(_Base):
    """test_sync_marks_failed_files_in_state."""

    def test_sync_marks_failed_files_in_state(self) -> None:
        """Ошибки записи в файл попадают в result.errors с ID записи."""
        with patch.object(
            ObsidianSyncManager,
            "_build_md_content",
            side_effect=RuntimeError("disk full"),
        ):
            result = self.mgr.sync([_item(item_id="fail-item-00001")], force=True)

        self.assertEqual(result.synced_count, 0)
        self.assertEqual(len(result.errors), 1)
        # Ошибка должна содержать ID записи
        self.assertIn("fail-item", result.errors[0])


# ===========================================================================
# 11. Continues after single file error
# ===========================================================================

class TestSyncContinuesAfterSingleFileError(_Base):
    """test_sync_continues_after_single_file_error."""

    def test_sync_continues_after_single_file_error(self) -> None:
        """Ошибка при синхронизации одного item не останавливает обработку остальных."""
        items = [
            _item(item_id=f"item-{i:03d}-0000-0000-0000-000000000001", ts=f"2025-06-0{i+1}T12:00:00+00:00")
            for i in range(3)
        ]

        call_count = [0]
        original_build = ObsidianSyncManager._build_md_content

        def _flaky_build(self_inner, item):
            call_count[0] += 1
            if call_count[0] == 2:  # Второй item падает
                raise OSError("write error")
            return original_build(self_inner, item)

        with patch.object(ObsidianSyncManager, "_build_md_content", _flaky_build):
            result = self.mgr.sync(items, force=True)

        # 2 успешных, 1 ошибка
        self.assertEqual(result.synced_count, 2)
        self.assertEqual(len(result.errors), 1)


# ===========================================================================
# 12. Corrupted state recovered to empty
# ===========================================================================

class TestStateFileCorruptedRecoveredToEmpty(unittest.TestCase):
    """test_state_file_corrupted_recovered_to_empty."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)

    def test_state_file_corrupted_recovered_to_empty(self) -> None:
        """Повреждённый obsidian_sync.json не ломает __init__ — менеджер стартует без state."""
        state_path = self.data_dir / "obsidian_sync.json"
        state_path.write_text("{ this is NOT valid JSON !!!", encoding="utf-8")

        mgr = ObsidianSyncManager(data_dir=self.data_dir)
        status = mgr.get_sync_status()

        self.assertFalse(status["configured"], "Vault не должен быть настроен после corrupt state")
        self.assertIsNone(status["last_sync_ts"])

    def test_partially_valid_state_ignores_nonexistent_vault(self) -> None:
        """State с несуществующим vault_path не настраивает vault."""
        state = {
            "vault_path": "/nonexistent/path/to/vault",
            "folder": "Transcriptions",
            "last_sync_ts": "2025-01-01T00:00:00+00:00",
        }
        state_path = self.data_dir / "obsidian_sync.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        mgr = ObsidianSyncManager(data_dir=self.data_dir)
        status = mgr.get_sync_status()

        # vault_path не существует → не должен быть загружен
        self.assertFalse(status["configured"])

    def test_valid_state_with_existing_vault_restores(self) -> None:
        """State с существующим vault_path восстанавливает конфигурацию."""
        vault_dir = Path(self.tmp.name) / "vault"
        vault_dir.mkdir()

        state = {
            "vault_path": str(vault_dir),
            "folder": "Notes",
            "last_sync_ts": "2025-06-01T10:00:00+00:00",
        }
        state_path = self.data_dir / "obsidian_sync.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        mgr = ObsidianSyncManager(data_dir=self.data_dir)
        status = mgr.get_sync_status()

        self.assertTrue(status["configured"])
        self.assertEqual(status["last_sync_ts"], "2025-06-01T10:00:00+00:00")
        self.assertEqual(status["folder"], "Notes")


if __name__ == "__main__":
    unittest.main()
