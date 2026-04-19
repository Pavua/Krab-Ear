"""Unit-тесты ObsidianSyncManager.

Проверяют:
- configure() с валидным/невалидным путём
- sync() полная и инкрементальная
- SyncResult: synced_count, skipped_count, errors, new_files, updated_files
- get_sync_status() до и после конфигурации
- Персистентность состояния (obsidian_sync.json)
- Формат .md файлов: YAML frontmatter, секции
- IPC-обработчики handle_configure / handle_sync / handle_get_status
- Обработка ошибок: vault не настроен, несуществующий путь
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.obsidian_sync import ObsidianSyncManager, SyncResult  # noqa: E402


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_item(
    text: str = "Тестовый текст.",
    ts: str | None = None,
    item_id: str = "abc12345-0000-0000-0000-000000000000",
    translated_text: str = "",
    translation_mode: str = "off",
    tags: list | None = None,
    diarization: dict | None = None,
    confidence: float | None = None,
) -> dict:
    """Создаёт dict-представление HistoryItem для тестов."""
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    return {
        "id": item_id,
        "ts": ts,
        "text": text,
        "translated_text": translated_text,
        "translation_mode": translation_mode,
        "source_lang": "ru",
        "target_lang": "",
        "tags": tags or [],
        "diarization": diarization,
        "confidence": confidence,
    }


class TestObsidianSyncConfigure(unittest.TestCase):
    """Тесты метода configure()."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault_dir.mkdir()
        self.mgr = ObsidianSyncManager(data_dir=self.data_dir)

    def test_configure_valid_vault(self) -> None:
        """configure() с существующим путём возвращает корректный dict."""
        result = self.mgr.configure(str(self.vault_dir))
        self.assertEqual(result["vault_path"], str(self.vault_dir.resolve()))
        self.assertEqual(result["folder"], "Transcriptions")
        self.assertTrue(Path(result["folder_full_path"]).exists())

    def test_configure_custom_folder(self) -> None:
        """configure() с кастомной папкой создаёт её внутри vault."""
        result = self.mgr.configure(str(self.vault_dir), folder="KrabEar")
        self.assertEqual(result["folder"], "KrabEar")
        self.assertTrue((self.vault_dir / "KrabEar").exists())

    def test_configure_nonexistent_vault_raises(self) -> None:
        """configure() с несуществующим путём вызывает ValueError."""
        with self.assertRaises(ValueError) as ctx:
            self.mgr.configure("/nonexistent/path/xyz")
        self.assertIn("не существует", str(ctx.exception))

    def test_configure_file_instead_of_dir_raises(self) -> None:
        """configure() с файлом (не директорией) вызывает ValueError."""
        file_path = Path(self.tmp.name) / "somefile.txt"
        file_path.write_text("data")
        with self.assertRaises(ValueError) as ctx:
            self.mgr.configure(str(file_path))
        self.assertIn("директорией", str(ctx.exception))

    def test_configure_persists_state(self) -> None:
        """После configure() состояние сохраняется в obsidian_sync.json."""
        self.mgr.configure(str(self.vault_dir))
        state_path = self.data_dir / "obsidian_sync.json"
        self.assertTrue(state_path.exists())
        state = json.loads(state_path.read_text())
        self.assertIsNotNone(state.get("vault_path"))
        self.assertEqual(state.get("folder"), "Transcriptions")


class TestObsidianSyncSync(unittest.TestCase):
    """Тесты метода sync()."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault_dir.mkdir()
        self.mgr = ObsidianSyncManager(data_dir=self.data_dir)
        self.mgr.configure(str(self.vault_dir))

    def test_sync_without_configure_raises(self) -> None:
        """sync() без configure() вызывает RuntimeError."""
        # Используем отдельную data_dir без сохранённого состояния
        fresh_data_dir = Path(self.tmp.name) / "fresh_data"
        fresh_data_dir.mkdir(parents=True)
        mgr2 = ObsidianSyncManager(data_dir=fresh_data_dir)
        with self.assertRaises(RuntimeError) as ctx:
            mgr2.sync([_make_item()])
        self.assertIn("не настроен", str(ctx.exception))

    def test_sync_creates_md_files(self) -> None:
        """sync() создаёт .md файлы в папке vault/folder."""
        items = [
            _make_item(item_id="aaa00000-0000-0000-0000-000000000001"),
            _make_item(text="Второй текст.", item_id="bbb00000-0000-0000-0000-000000000002"),
        ]
        result = self.mgr.sync(items)
        self.assertEqual(result.synced_count, 2)
        self.assertEqual(len(result.new_files), 2)
        self.assertEqual(result.skipped_count, 0)
        self.assertEqual(result.errors, [])
        for f in result.new_files:
            self.assertTrue(Path(f).exists())

    def test_sync_returns_sync_result(self) -> None:
        """sync() возвращает экземпляр SyncResult."""
        result = self.mgr.sync([_make_item()])
        self.assertIsInstance(result, SyncResult)

    def test_sync_incremental_skips_old(self) -> None:
        """Инкрементальная синхронизация пропускает записи старше last_sync_ts."""
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        # Первый sync устанавливает last_sync_ts
        self.mgr.sync([_make_item(ts=old_ts)])

        # Второй sync: старая запись должна быть пропущена
        result = self.mgr.sync([_make_item(ts=old_ts)])
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.synced_count, 0)

        # new_ts захватываем ПОСЛЕ последнего sync, чтобы гарантировать,
        # что он новее last_sync_ts
        new_ts = datetime.now(timezone.utc).isoformat()

        # Новая запись не должна быть пропущена
        result2 = self.mgr.sync([_make_item(ts=new_ts)])
        self.assertEqual(result2.synced_count, 1)
        self.assertEqual(result2.skipped_count, 0)

    def test_sync_force_ignores_last_sync(self) -> None:
        """sync(force=True) синхронизирует все записи независимо от last_sync_ts."""
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        # Устанавливаем last_sync_ts первым sync'ом
        self.mgr.sync([_make_item(ts=old_ts)])
        # force=True должен синхронизировать даже старую запись
        result = self.mgr.sync([_make_item(ts=old_ts)], force=True)
        self.assertEqual(result.synced_count, 1)
        self.assertEqual(result.skipped_count, 0)

    def test_sync_updates_existing_file(self) -> None:
        """Повторный sync с force=True обновляет существующий файл."""
        item = _make_item()
        result1 = self.mgr.sync([item])
        self.assertEqual(len(result1.new_files), 1)
        self.assertEqual(len(result1.updated_files), 0)

        # Повторная синхронизация с force
        result2 = self.mgr.sync([item], force=True)
        self.assertEqual(len(result2.updated_files), 1)
        self.assertEqual(len(result2.new_files), 0)

    def test_sync_updates_last_sync_ts(self) -> None:
        """После sync() last_sync_ts в get_sync_status() обновляется."""
        status_before = self.mgr.get_sync_status()
        self.assertIsNone(status_before["last_sync_ts"])

        self.mgr.sync([_make_item()])
        status_after = self.mgr.get_sync_status()
        self.assertIsNotNone(status_after["last_sync_ts"])

    def test_sync_file_count_in_status(self) -> None:
        """get_sync_status() корректно возвращает количество .md файлов."""
        self.mgr.sync([
            _make_item(item_id="ccc00000-0000-0000-0000-000000000001"),
            _make_item(text="Ещё один.", item_id="ddd00000-0000-0000-0000-000000000002"),
        ])
        status = self.mgr.get_sync_status()
        self.assertEqual(status["file_count"], 2)


class TestObsidianSyncStatus(unittest.TestCase):
    """Тесты метода get_sync_status()."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.mgr = ObsidianSyncManager(data_dir=self.data_dir)

    def test_status_before_configure(self) -> None:
        """get_sync_status() до configure() возвращает configured=False."""
        status = self.mgr.get_sync_status()
        self.assertFalse(status["configured"])
        self.assertIsNone(status["vault_path"])
        self.assertEqual(status["file_count"], 0)

    def test_status_after_configure(self) -> None:
        """get_sync_status() после configure() возвращает configured=True."""
        vault = Path(self.tmp.name) / "vault"
        vault.mkdir()
        self.mgr.configure(str(vault))
        status = self.mgr.get_sync_status()
        self.assertTrue(status["configured"])
        self.assertIsNotNone(status["vault_path"])
        self.assertEqual(status["folder"], "Transcriptions")


class TestObsidianSyncMdFormat(unittest.TestCase):
    """Тесты формата .md файлов (YAML frontmatter, секции)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault_dir.mkdir()
        self.mgr = ObsidianSyncManager(data_dir=self.data_dir)
        self.mgr.configure(str(self.vault_dir))

    def _get_content(self, item: dict) -> str:
        """Синхронизируем один item и читаем содержимое файла."""
        result = self.mgr.sync([item], force=True)
        files = result.new_files + result.updated_files
        self.assertGreater(len(files), 0, "Файл должен быть создан/обновлён")
        return Path(files[0]).read_text(encoding="utf-8")

    def test_md_has_yaml_frontmatter(self) -> None:
        """Файл начинается с YAML frontmatter (--- ... ---)."""
        content = self._get_content(_make_item())
        self.assertTrue(content.startswith("---"))
        lines = content.splitlines()
        closing = [i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"]
        self.assertGreater(len(closing), 0, "Должен быть закрывающий ---")

    def test_md_frontmatter_fields(self) -> None:
        """YAML frontmatter содержит поля title, date, id, tags."""
        content = self._get_content(_make_item())
        self.assertIn("title:", content)
        self.assertIn("date:", content)
        self.assertIn("id:", content)
        self.assertIn("tags:", content)
        self.assertIn("krab-ear", content)
        self.assertIn("transcript", content)

    def test_md_has_transcript_section(self) -> None:
        """Файл содержит секцию '## Улучшенная транскрибация' с текстом."""
        item = _make_item(text="Привет, мир!")
        content = self._get_content(item)
        self.assertIn("## Улучшенная транскрибация", content)
        self.assertIn("Привет, мир!", content)

    def test_md_has_speaker_tag(self) -> None:
        """В секции транскрипции присутствует [Спикер (timestamp)]."""
        content = self._get_content(_make_item())
        self.assertIn("[Спикер (", content)

    def test_md_has_summary_section(self) -> None:
        """Файл содержит секцию '## Краткое содержание (Summary)'."""
        content = self._get_content(_make_item())
        self.assertIn("## Краткое содержание (Summary)", content)

    def test_md_translation_section(self) -> None:
        """Перевод включается в файл если translation_mode != 'off'."""
        item = _make_item(
            translated_text="Hola mundo.",
            translation_mode="ru_es",
        )
        content = self._get_content(item)
        self.assertIn("## Перевод", content)
        self.assertIn("Hola mundo.", content)

    def test_md_no_translation_section_when_off(self) -> None:
        """Секция перевода не добавляется если translated_text пустой или translation_mode=off."""
        item = _make_item(translated_text="", translation_mode="off")
        content = self._get_content(item)
        self.assertNotIn("## Перевод", content)

    def test_md_diarization_speaker_turns(self) -> None:
        """При диаризации реплики форматируются как **[SPEAKER_XX (HH:MM:SS)]**."""
        diarization = {
            "enabled": True,
            "speaker_turns": [
                {"speaker": "SPEAKER_00", "text": "Добрый день.", "start": 0.0, "end": 2.0},
                {"speaker": "SPEAKER_01", "text": "Привет!", "start": 2.0, "end": 3.5},
            ],
        }
        item = _make_item(text="Добрый день. Привет!", diarization=diarization)
        content = self._get_content(item)
        self.assertIn("SPEAKER_00", content)
        self.assertIn("SPEAKER_01", content)
        self.assertIn("Добрый день.", content)
        self.assertIn("Привет!", content)

    def test_md_tags_in_frontmatter(self) -> None:
        """Пользовательские теги из item.tags попадают в frontmatter."""
        item = _make_item(tags=["важный", "звонок"])
        content = self._get_content(item)
        self.assertIn("важный", content)
        self.assertIn("звонок", content)

    def test_md_confidence_in_frontmatter(self) -> None:
        """Поле confidence попадает в frontmatter если передано."""
        item = _make_item(confidence=0.92)
        content = self._get_content(item)
        self.assertIn("confidence:", content)
        self.assertIn("0.920", content)


class TestObsidianSyncIpcHandlers(unittest.TestCase):
    """Тесты IPC-обработчиков handle_configure / handle_sync / handle_get_status."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault_dir.mkdir()
        self.mgr = ObsidianSyncManager(data_dir=self.data_dir)

    def test_handle_configure_valid(self) -> None:
        """handle_configure() с корректными параметрами возвращает dict."""
        result = self.mgr.handle_configure({"vault_path": str(self.vault_dir)})
        self.assertIn("vault_path", result)
        self.assertIn("folder", result)

    def test_handle_configure_missing_vault_path_raises(self) -> None:
        """handle_configure() без vault_path вызывает ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.handle_configure({})

    def test_handle_sync_valid(self) -> None:
        """handle_sync() с корректными items возвращает dict с полями SyncResult."""
        self.mgr.configure(str(self.vault_dir))
        item = _make_item()
        result = self.mgr.handle_sync({"items": [item]})
        self.assertIn("synced_count", result)
        self.assertIn("skipped_count", result)
        self.assertIn("errors", result)
        self.assertIn("new_files", result)
        self.assertIn("updated_files", result)

    def test_handle_sync_missing_items_raises(self) -> None:
        """handle_sync() без items вызывает ValueError."""
        self.mgr.configure(str(self.vault_dir))
        with self.assertRaises(ValueError):
            self.mgr.handle_sync({})

    def test_handle_sync_items_not_list_raises(self) -> None:
        """handle_sync() с items не-списком вызывает ValueError."""
        self.mgr.configure(str(self.vault_dir))
        with self.assertRaises(ValueError):
            self.mgr.handle_sync({"items": "не список"})

    def test_handle_get_status_returns_dict(self) -> None:
        """handle_get_status() всегда возвращает dict с ключами configured и file_count."""
        result = self.mgr.handle_get_status({})
        self.assertIn("configured", result)
        self.assertIn("file_count", result)
        self.assertIn("vault_path", result)
        self.assertIn("last_sync_ts", result)


class TestObsidianSyncPersistence(unittest.TestCase):
    """Тесты персистентности состояния (перезагрузка из obsidian_sync.json)."""

    def test_state_survives_reload(self) -> None:
        """Состояние (vault_path, folder, last_sync_ts) загружается при новом экземпляре."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name) / "data"
        data_dir.mkdir(parents=True)
        vault_dir = Path(tmp.name) / "vault"
        vault_dir.mkdir()

        mgr1 = ObsidianSyncManager(data_dir=data_dir)
        mgr1.configure(str(vault_dir), folder="MyNotes")
        mgr1.sync([_make_item()])

        # Создаём новый экземпляр — должен загрузить состояние из файла
        mgr2 = ObsidianSyncManager(data_dir=data_dir)
        status = mgr2.get_sync_status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["folder"], "MyNotes")
        self.assertIsNotNone(status["last_sync_ts"])

    def test_state_file_created_on_configure(self) -> None:
        """Файл obsidian_sync.json создаётся после configure()."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name) / "data"
        data_dir.mkdir(parents=True)
        vault_dir = Path(tmp.name) / "vault"
        vault_dir.mkdir()

        mgr = ObsidianSyncManager(data_dir=data_dir)
        state_file = data_dir / "obsidian_sync.json"
        self.assertFalse(state_file.exists())
        mgr.configure(str(vault_dir))
        self.assertTrue(state_file.exists())


class TestSyncResultDataclass(unittest.TestCase):
    """Тесты датакласса SyncResult."""

    def test_sync_result_defaults(self) -> None:
        """SyncResult создаётся с нулевыми счётчиками и пустыми списками."""
        r = SyncResult()
        self.assertEqual(r.synced_count, 0)
        self.assertEqual(r.skipped_count, 0)
        self.assertEqual(r.errors, [])
        self.assertEqual(r.new_files, [])
        self.assertEqual(r.updated_files, [])

    def test_sync_result_to_dict(self) -> None:
        """to_dict() возвращает все поля."""
        r = SyncResult(synced_count=3, skipped_count=1, errors=["e1"])
        d = r.to_dict()
        self.assertEqual(d["synced_count"], 3)
        self.assertEqual(d["skipped_count"], 1)
        self.assertEqual(d["errors"], ["e1"])
        self.assertIn("new_files", d)
        self.assertIn("updated_files", d)


class TestObsidianSyncEdgeCases(unittest.TestCase):
    """Тесты граничных случаев: пустая история, отсутствие vault, повреждённый state."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault_dir.mkdir()
        self.mgr = ObsidianSyncManager(data_dir=self.data_dir)
        self.mgr.configure(str(self.vault_dir))

    def test_sync_empty_history_returns_zero(self) -> None:
        """sync([]) без items возвращает SyncResult с нулевыми счётчиками и без ошибок."""
        result = self.mgr.sync([])
        self.assertEqual(result.synced_count, 0)
        self.assertEqual(result.skipped_count, 0)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.new_files, [])
        self.assertEqual(result.updated_files, [])

    def test_sync_empty_history_updates_last_sync_ts(self) -> None:
        """Даже sync([]) обновляет last_sync_ts в состоянии."""
        self.mgr.sync([])
        status = self.mgr.get_sync_status()
        self.assertIsNotNone(status["last_sync_ts"])

    def test_sync_creates_target_dir_if_missing(self) -> None:
        """sync() создаёт папку vault/folder если она была удалена после configure()."""
        target_dir = self.vault_dir / "Transcriptions"
        # Удаляем папку после configure()
        import shutil
        if target_dir.exists():
            shutil.rmtree(str(target_dir))
        self.assertFalse(target_dir.exists())

        result = self.mgr.sync([_make_item()])
        self.assertEqual(result.synced_count, 1)
        self.assertTrue(target_dir.exists())

    def test_corrupted_state_file_graceful_recovery(self) -> None:
        """Повреждённый obsidian_sync.json не ломает инициализацию — менеджер стартует без state."""
        state_path = self.data_dir / "obsidian_sync.json"
        state_path.write_text("{ NOT VALID JSON !!!", encoding="utf-8")

        # Новый экземпляр должен подняться без исключения
        mgr2 = ObsidianSyncManager(data_dir=self.data_dir)
        # Состояние не загружено — vault не настроен
        status = mgr2.get_sync_status()
        self.assertFalse(status["configured"])

    def test_corrupted_state_file_can_be_reconfigured(self) -> None:
        """После повреждённого state-файла можно вызвать configure() и sync() успешно."""
        state_path = self.data_dir / "obsidian_sync.json"
        state_path.write_text("null", encoding="utf-8")

        mgr2 = ObsidianSyncManager(data_dir=self.data_dir)
        mgr2.configure(str(self.vault_dir))
        result = mgr2.sync([_make_item()])
        self.assertEqual(result.synced_count, 1)
        self.assertEqual(result.errors, [])

    def test_missing_vault_dir_after_configure_raises(self) -> None:
        """configure() на несуществующей директории вызывает ValueError."""
        nonexistent = Path(self.tmp.name) / "does_not_exist"
        with self.assertRaises(ValueError):
            self.mgr.configure(str(nonexistent))

    def test_deleted_item_md_file_remains(self) -> None:
        """Удаление item из списка не удаляет соответствующий .md файл из vault.

        ObsidianSyncManager не реализует tombstone-удаление:
        файлы остаются в vault после исчезновения item из истории.
        """
        item = _make_item(item_id="keep-me-00-0000-0000-000000000001")
        result1 = self.mgr.sync([item])
        self.assertEqual(result1.synced_count, 1)
        created_path = Path(result1.new_files[0])
        self.assertTrue(created_path.exists())

        # Sync без этого item — файл должен остаться
        result2 = self.mgr.sync([], force=True)
        self.assertEqual(result2.synced_count, 0)
        self.assertTrue(created_path.exists(), "Файл должен остаться — tombstone не реализован")


class TestObsidianSyncFilename(unittest.TestCase):
    """Тесты формирования имени файла _make_filename()."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault_dir.mkdir()
        self.mgr = ObsidianSyncManager(data_dir=self.data_dir)
        self.mgr.configure(str(self.vault_dir))

    def test_filename_has_md_extension(self) -> None:
        """Имя файла заканчивается на .md."""
        item = _make_item()
        result = self.mgr.sync([item], force=True)
        files = result.new_files + result.updated_files
        self.assertTrue(files[0].endswith(".md"))

    def test_filename_contains_timestamp(self) -> None:
        """Имя файла содержит дату из ts (YYYY-MM-DD формат)."""
        ts = "2025-03-15T10:30:00+00:00"
        item = _make_item(ts=ts, item_id="ts-test-00-0000-0000-000000000001")
        result = self.mgr.sync([item], force=True)
        files = result.new_files + result.updated_files
        filename = Path(files[0]).name
        self.assertIn("2025-03-15", filename)

    def test_filename_contains_id_prefix(self) -> None:
        """Имя файла содержит первые 8 символов item_id."""
        item = _make_item(item_id="abcdef12-0000-0000-0000-000000000001")
        result = self.mgr.sync([item], force=True)
        files = result.new_files + result.updated_files
        filename = Path(files[0]).name
        self.assertIn("abcdef12", filename)

    def test_filename_safe_for_filesystem(self) -> None:
        """Имя файла не содержит запрещённых символов (/, \\, :, *, ?, <, >, |)."""
        # Item с id содержащим спецсимволы — _make_filename должен их заменить
        item = _make_item(
            item_id="ab:cd/ef*",
            ts="2025-06-01T12:00:00+00:00",
        )
        result = self.mgr.sync([item], force=True)
        files = result.new_files + result.updated_files
        filename = Path(files[0]).name
        forbidden = set('/\\:*?<>|')
        self.assertFalse(
            forbidden & set(filename),
            f"Имя файла содержит запрещённые символы: {filename!r}",
        )

    def test_filename_starts_with_transcript_prefix(self) -> None:
        """Имя файла начинается с 'transcript_'."""
        item = _make_item()
        result = self.mgr.sync([item], force=True)
        files = result.new_files + result.updated_files
        filename = Path(files[0]).name
        self.assertTrue(filename.startswith("transcript_"))


class TestObsidianSyncYamlFrontmatter(unittest.TestCase):
    """Тесты разбора YAML frontmatter из сгенерированных .md файлов."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault_dir.mkdir()
        self.mgr = ObsidianSyncManager(data_dir=self.data_dir)
        self.mgr.configure(str(self.vault_dir))

    def _parse_frontmatter(self, content: str) -> dict:
        """Вручную парсит YAML frontmatter без внешних зависимостей."""
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        end = None
        for i, ln in enumerate(lines[1:], 1):
            if ln.strip() == "---":
                end = i
                break
        if end is None:
            return {}
        fm_lines = lines[1:end]
        result: dict = {}
        current_list_key: str | None = None
        current_list: list = []
        for ln in fm_lines:
            if ln.startswith("  - "):
                if current_list_key:
                    current_list.append(ln[4:].strip())
            elif ":" in ln and not ln.startswith(" "):
                if current_list_key:
                    result[current_list_key] = current_list
                    current_list = []
                    current_list_key = None
                key, _, val = ln.partition(":")
                key = key.strip()
                val = val.strip()
                if val == "":
                    current_list_key = key
                    current_list = []
                else:
                    result[key] = val
        if current_list_key:
            result[current_list_key] = current_list
        return result

    def test_frontmatter_title_field(self) -> None:
        """Поле title содержит строку с датой записи."""
        ts = "2025-04-10T09:00:00+00:00"
        item = _make_item(ts=ts, item_id="fm-title-0-0000-0000-000000000001")
        result = self.mgr.sync([item], force=True)
        content = Path(result.new_files[0]).read_text(encoding="utf-8")
        fm = self._parse_frontmatter(content)
        self.assertIn("title", fm)
        self.assertIn("2025-04-10", fm["title"])

    def test_frontmatter_id_field(self) -> None:
        """Поле id в frontmatter совпадает с item_id."""
        item = _make_item(item_id="myid-001-0000-0000-000000000001")
        result = self.mgr.sync([item], force=True)
        content = Path(result.new_files[0]).read_text(encoding="utf-8")
        fm = self._parse_frontmatter(content)
        self.assertIn("id", fm)
        self.assertEqual(fm["id"], "myid-001-0000-0000-000000000001")

    def test_frontmatter_source_field(self) -> None:
        """Поле source в frontmatter равно 'krab-ear'."""
        item = _make_item()
        result = self.mgr.sync([item], force=True)
        content = Path(result.new_files[0]).read_text(encoding="utf-8")
        fm = self._parse_frontmatter(content)
        self.assertEqual(fm.get("source"), "krab-ear")

    def test_frontmatter_tags_include_krab_ear(self) -> None:
        """Список tags в frontmatter содержит 'krab-ear' и 'transcript'."""
        item = _make_item()
        result = self.mgr.sync([item], force=True)
        content = Path(result.new_files[0]).read_text(encoding="utf-8")
        fm = self._parse_frontmatter(content)
        tags = fm.get("tags", [])
        self.assertIn("krab-ear", tags)
        self.assertIn("transcript", tags)

    def test_frontmatter_confidence_format(self) -> None:
        """Поле confidence форматируется с тремя знаками после запятой."""
        item = _make_item(confidence=0.876543)
        result = self.mgr.sync([item], force=True)
        content = Path(result.new_files[0]).read_text(encoding="utf-8")
        fm = self._parse_frontmatter(content)
        self.assertIn("confidence", fm)
        # Должно быть "0.877" (округление до 3 знаков)
        self.assertEqual(fm["confidence"], "0.877")


if __name__ == "__main__":
    unittest.main()
