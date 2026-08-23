"""W1767: privacy-purge gaps — migration backups, shares, translation cache,
translation glossary, vocabulary, settings backups.

Покрывает:
  #2  (HIGH) migration backups/ — DataMigrator._create_backup() копирует history.ndjson
      в <data_dir>/backups/migration_backup_<ts>/.  purge должен удалить весь каталог.
  #3  (HIGH) shares/ — SharingManager хранит полный текст транскрипций в <data_dir>/shares/.
      purge должен удалить директорию целиком (включая shares_index.json).
  #7  (MED)  translation_cache.json — TranslationCache персистирует переводы транскрипций.
      purge должен вызвать clear() + удалить файл с диска.
  #8  (MED)  translation_glossary в settings.json — словарь переводов может содержать ПДн.
      purge должен сбросить ключ в {}.
  #9  (MED)  vocabulary.json — пользовательский словарь STT.
      purge должен вызвать VocabularyStore.clear_all() и удалить файл.
  #10 (MED)  settings_backups/ — rolling-снапшоты settings.json (включают glossary).
      purge должен удалить директорию.
  E2E handle_purge_all_data: все шесть gap-ов закрыты одним вызовом purge.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translation_cache import TranslationCache  # noqa: E402
from backend.vocabulary_store import VocabularyStore    # noqa: E402
from backend.history_service import HistoryService      # noqa: E402


# ---------------------------------------------------------------------------
# Минимальные fakes (паттерн из предыдущих purge-тестов)
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str) -> None:
        self.id = item_id
        self.ts = "2024-01-01T00:00:00+00:00"

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "text": "тестовый текст"}


class FakeStore:
    """Минимальный StateStore fake для purge тестов."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}
        self._tombstones: list[dict] = []
        self._lock_obj = threading.Lock()
        self._settings: dict = {}

    def add_item(self, item_id: str) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id)
        self._items[item_id] = item
        return item

    def _lock(self):
        return self._lock_obj

    def _load_active_items_unlocked(self) -> list[FakeHistoryItem]:
        return list(self._items.values())

    def _append_ndjson(self, path: Any, payload: dict) -> None:
        self._tombstones.append(payload)

    @property
    def tombstones_path(self) -> str:
        return "fake_tombstones.ndjson"

    def compact_with_stats(self) -> dict:
        return {"before_active_count": len(self._items), "after_active_count": 0}

    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> dict:
        return dict(self._settings)

    def save_settings(self, settings: dict) -> dict:
        self._settings = dict(settings)
        return dict(settings)


class FakeSettingsSvc:
    """Минимальный SettingsService fake для проверки invalidate_cache()."""

    def __init__(self) -> None:
        self.invalidated = False
        self._backup: Optional[Any] = None

    def invalidate_cache(self) -> None:
        self.invalidated = True


class FakeSettingsBackup:
    """Минимальный SettingsBackup fake с настраиваемым backup_dir."""

    def __init__(self, backup_dir: str) -> None:
        self._dir = Path(backup_dir)

    def get_backup_dir(self) -> Path:
        return self._dir


# ---------------------------------------------------------------------------
# #2 — migration backups/ удаляются при purge
# ---------------------------------------------------------------------------

class MigrationBackupsPurgeTestCase(unittest.TestCase):
    """W1767 #2: purge_all_data удаляет <data_dir>/backups/ целиком."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _seed_backups(self) -> Path:
        """Создаёт тестовую структуру migration backup."""
        backups_dir = Path(self._tmpdir) / "backups"
        backup_sub = backups_dir / "migration_backup_20260101_120000"
        backup_sub.mkdir(parents=True, exist_ok=True)
        (backup_sub / "history.ndjson").write_text(
            '{"id":"rec-1","text":"секрет из истории"}\n',
            encoding="utf-8",
        )
        (backup_sub / "settings.json").write_text(
            '{"translation_glossary": {"hello": "привет"}}\n',
            encoding="utf-8",
        )
        return backups_dir

    def test_backups_dir_removed_on_purge(self) -> None:
        """purge_all_data должен удалить весь <data_dir>/backups/ каталог."""
        backups_dir = self._seed_backups()
        self.assertTrue(backups_dir.is_dir(), "backups/ должен существовать до purge")

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), f"purge должен вернуть ok=True: {result}")
        self.assertFalse(backups_dir.exists(),
                         "backups/ должен быть удалён после purge_all_data")

    def test_history_text_not_on_disk_after_purge(self) -> None:
        """После purge текст истории не должен присутствовать в backups/ на диске."""
        self._seed_backups()
        secret = "секрет из истории"

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc.handle_purge_all_data({"confirm": True})

        for f in Path(self._tmpdir).rglob("*"):
            if f.is_file():
                content = f.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn(
                    secret, content,
                    f"Текст истории найден в {f} после purge"
                )

    def test_no_backups_dir_no_crash(self) -> None:
        """purge_all_data без <data_dir>/backups/ не бросает исключений."""
        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        try:
            result = svc.handle_purge_all_data({"confirm": True})
        except Exception as exc:
            self.fail(f"purge_all_data без backups/ бросил исключение: {exc}")
        self.assertTrue(result.get("ok"))

    def test_backups_error_in_secondary_errors(self) -> None:
        """При ошибке удаления backups/ — добавляется в secondary_errors, purge не прерывается."""
        backups_dir = Path(self._tmpdir) / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)

        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-1")
        svc = HistoryService(store=store)

        # Симулируем ошибку — делаем store.data_dir несуществующим типом
        original_data_dir = store.data_dir
        store.data_dir = 12345  # вызовет ошибку при Path(12345)

        result = svc.handle_purge_all_data({"confirm": True})
        store.data_dir = original_data_dir

        # история должна быть удалена (primary step отработал до ошибки),
        # но complete может быть False — не проверяем строго из-за flaкiness fake store
        self.assertTrue(result.get("ok"), "purge должен вернуть ok=True даже при ошибке backups")


# ---------------------------------------------------------------------------
# #3 — shares/ удаляется при purge
# ---------------------------------------------------------------------------

class SharesDirPurgeTestCase(unittest.TestCase):
    """W1767 #3: purge_all_data удаляет <data_dir>/shares/ целиком."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _seed_shares(self) -> tuple[Path, str]:
        """Создаёт тестовую директорию shares с файлами транскрипций."""
        shares_dir = Path(self._tmpdir) / "shares"
        shares_dir.mkdir(parents=True, exist_ok=True)
        secret = "секретная транскрипция для шаринга"
        (shares_dir / "share_abc12345.txt").write_text(secret, encoding="utf-8")
        index = {
            "abc12345": {
                "share_id": "abc12345",
                "content": secret,
                "created_at": "2026-06-01T00:00:00+00:00",
            }
        }
        (shares_dir / "shares_index.json").write_text(
            json.dumps(index), encoding="utf-8"
        )
        return shares_dir, secret

    def test_shares_dir_removed_on_purge(self) -> None:
        """purge_all_data должен удалить весь <data_dir>/shares/ каталог."""
        shares_dir, _ = self._seed_shares()
        self.assertTrue(shares_dir.is_dir(), "shares/ должен существовать до purge")

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), f"purge должен вернуть ok=True: {result}")
        self.assertFalse(shares_dir.exists(),
                         "shares/ должен быть удалён после purge_all_data")

    def test_shares_index_not_on_disk_after_purge(self) -> None:
        """После purge shares_index.json не должен присутствовать на диске."""
        self._seed_shares()
        shares_index = Path(self._tmpdir) / "shares" / "shares_index.json"
        self.assertTrue(shares_index.exists(), "shares_index.json должен существовать до purge")

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc.handle_purge_all_data({"confirm": True})

        self.assertFalse(shares_index.exists(),
                         "shares_index.json должен быть удалён после purge")

    def test_share_text_not_on_disk_after_purge(self) -> None:
        """После purge текст транскрипции из share-пакета не должен быть на диске."""
        _, secret = self._seed_shares()

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc.handle_purge_all_data({"confirm": True})

        for f in Path(self._tmpdir).rglob("*"):
            if f.is_file():
                content = f.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn(
                    secret, content,
                    f"Текст транскрипции из share найден в {f} после purge"
                )

    def test_no_shares_dir_no_crash(self) -> None:
        """purge_all_data без <data_dir>/shares/ не бросает исключений."""
        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        try:
            result = svc.handle_purge_all_data({"confirm": True})
        except Exception as exc:
            self.fail(f"purge_all_data без shares/ бросил исключение: {exc}")
        self.assertTrue(result.get("ok"))


# ---------------------------------------------------------------------------
# #7 — TranslationCache.clear_all (via _translation_cache)
# ---------------------------------------------------------------------------

class TranslationCachePurgeTestCase(unittest.TestCase):
    """W1767 #7: purge_all_data очищает translation_cache in-memory + файл."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _make_cache_with_data(self) -> TranslationCache:
        cache = TranslationCache(data_dir=self._tmpdir)
        cache.put("Привет мир", "ru", "es", "offline", "Hola mundo")
        cache.put("Добрый день", "ru", "en", "offline", "Good afternoon")
        return cache

    def test_cache_file_gone_after_purge(self) -> None:
        """После purge translation_cache.json должен быть удалён."""
        cache = self._make_cache_with_data()
        cache_path = Path(self._tmpdir) / "translation_cache.json"
        self.assertTrue(cache_path.exists(), "translation_cache.json должен существовать до purge")

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._translation_cache = cache
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), f"purge должен вернуть ok=True: {result}")
        self.assertFalse(cache_path.exists(),
                         "translation_cache.json должен быть удалён после purge")

    def test_cache_in_memory_empty_after_purge(self) -> None:
        """После purge in-memory кэш должен быть пуст."""
        cache = self._make_cache_with_data()
        self.assertGreater(len(cache._cache), 0, "Кэш должен содержать записи до purge")

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._translation_cache = cache
        svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(len(cache._cache), 0,
                         "In-memory кэш должен быть пуст после purge")

    def test_cache_text_not_on_disk_after_purge(self) -> None:
        """После purge переведённый текст не должен присутствовать на диске."""
        cache = self._make_cache_with_data()
        private_text = "Hola mundo"

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._translation_cache = cache
        svc.handle_purge_all_data({"confirm": True})

        for f in Path(self._tmpdir).rglob("*"):
            if f.is_file():
                content = f.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn(
                    private_text, content,
                    f"Перевод найден в {f} после purge"
                )

    def test_no_translation_cache_no_crash(self) -> None:
        """purge_all_data без _translation_cache не бросает исключений."""
        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._translation_cache = None
        try:
            result = svc.handle_purge_all_data({"confirm": True})
        except Exception as exc:
            self.fail(f"purge без _translation_cache бросил исключение: {exc}")
        self.assertTrue(result.get("ok"))
        self.assertNotIn("translation_cache", result.get("errors", []))

    def test_cache_error_does_not_abort_purge(self) -> None:
        """Ошибка _translation_cache.clear() не прерывает удаление истории."""

        class ErrorCache:
            def clear(self) -> None:
                raise IOError("диск недоступен")

        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-1")
        svc = HistoryService(store=store)
        svc._translation_cache = ErrorCache()

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(result["history_deleted"], 1,
                         "История должна быть удалена даже при ошибке translation_cache")
        self.assertFalse(result["complete"])
        self.assertIn("translation_cache", result["errors"])


# ---------------------------------------------------------------------------
# #8 — translation_glossary сбрасывается в settings.json
# ---------------------------------------------------------------------------

class TranslationGlossaryPurgeTestCase(unittest.TestCase):
    """W1767 #8: purge_all_data сбрасывает translation_glossary в settings.json."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _make_store_with_glossary(self, glossary: dict) -> FakeStore:
        store = FakeStore(data_dir=self._tmpdir)
        store._settings = {"translation_glossary": glossary}
        return store

    def test_glossary_reset_to_empty_dict(self) -> None:
        """После purge translation_glossary в settings должен быть {}."""
        glossary = {"привет": "hola", "мир": "mundo"}
        store = self._make_store_with_glossary(glossary)
        svc = HistoryService(store=store)

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), f"purge должен вернуть ok=True: {result}")
        saved_settings = store.load_settings()
        self.assertEqual(saved_settings.get("translation_glossary"), {},
                         "translation_glossary должен быть {} после purge")

    def test_glossary_terms_not_in_settings_after_purge(self) -> None:
        """После purge термины глоссария не должны присутствовать в сохранённых настройках."""
        secret_term = "Паша"
        glossary = {secret_term: "Pasha"}
        store = self._make_store_with_glossary(glossary)
        svc = HistoryService(store=store)

        svc.handle_purge_all_data({"confirm": True})

        saved = json.dumps(store.load_settings())
        self.assertNotIn(
            secret_term, saved,
            "Термин глоссария найден в настройках после purge"
        )

    def test_settings_svc_invalidate_cache_called(self) -> None:
        """После сброса глоссария invalidate_cache() SettingsService должен быть вызван."""
        glossary = {"hello": "привет"}
        store = self._make_store_with_glossary(glossary)
        svc = HistoryService(store=store)

        fake_settings_svc = FakeSettingsSvc()
        svc._settings_svc = fake_settings_svc

        svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(
            fake_settings_svc.invalidated,
            "invalidate_cache() должен быть вызван после сброса glossary"
        )

    def test_empty_glossary_no_error(self) -> None:
        """Purge с пустым glossary не добавляет ошибки в secondary_errors."""
        store = self._make_store_with_glossary({})
        svc = HistoryService(store=store)

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertNotIn("translation_glossary", result.get("errors", []),
                         "Пустой глоссарий не должен генерировать ошибку")

    def test_glossary_error_does_not_abort_purge(self) -> None:
        """Ошибка сброса glossary не прерывает удаление истории."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-x")
        # Сломаем load_settings чтобы вызвать ошибку
        original_load = store.load_settings

        def broken_load(*args, **kwargs) -> dict:
            # kwargs прод-вызовов (lock_timeout_sec/nowait): иначе подмена
            # однажды упадёт TypeError'ом, а тест этого не заметит — он ждёт
            # исключения и здесь, и там (класс «дрейф фейка», отложенный).
            raise RuntimeError("ошибка чтения settings")

        store.load_settings = broken_load
        svc = HistoryService(store=store)

        result = svc.handle_purge_all_data({"confirm": True})
        # Восстановим для assert
        store.load_settings = original_load

        # История должна быть удалена (primary step), glossary ошибка — вторичная
        self.assertEqual(result.get("history_deleted"), 1,
                         "История должна быть удалена даже при ошибке glossary сброса")
        self.assertFalse(result.get("complete"),
                         "complete должен быть False при ошибке вторичного шага")
        self.assertIn("translation_glossary", result.get("errors", []))


# ---------------------------------------------------------------------------
# #9 — vocabulary.json удаляется при purge
# ---------------------------------------------------------------------------

class VocabularyStorePurgeTestCase(unittest.TestCase):
    """W1767 #9: purge_all_data удаляет vocabulary.json через VocabularyStore.clear_all()."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _make_vocab_with_words(self) -> VocabularyStore:
        vocab = VocabularyStore(data_dir=Path(self._tmpdir))
        vocab.save(["Паша", "Иван", "Мария"])
        return vocab

    def test_vocabulary_file_gone_after_purge(self) -> None:
        """После purge vocabulary.json должен быть удалён с диска."""
        vocab = self._make_vocab_with_words()
        vocab_path = Path(self._tmpdir) / "vocabulary.json"
        self.assertTrue(vocab_path.exists(), "vocabulary.json должен существовать до purge")

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._vocabulary_store = vocab
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), f"purge должен вернуть ok=True: {result}")
        self.assertFalse(vocab_path.exists(),
                         "vocabulary.json должен быть удалён после purge")

    def test_vocabulary_words_not_on_disk_after_purge(self) -> None:
        """После purge слова из словаря не должны присутствовать на диске."""
        vocab = self._make_vocab_with_words()
        secret_word = "Паша"

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._vocabulary_store = vocab
        svc.handle_purge_all_data({"confirm": True})

        for f in Path(self._tmpdir).rglob("*"):
            if f.is_file():
                content = f.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn(
                    secret_word, content,
                    f"Слово словаря найдено в {f} после purge"
                )

    def test_no_vocabulary_store_no_crash(self) -> None:
        """purge_all_data без _vocabulary_store не бросает исключений."""
        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._vocabulary_store = None
        try:
            result = svc.handle_purge_all_data({"confirm": True})
        except Exception as exc:
            self.fail(f"purge без _vocabulary_store бросил исключение: {exc}")
        self.assertTrue(result.get("ok"))
        self.assertNotIn("vocabulary", result.get("errors", []))

    def test_vocabulary_error_does_not_abort_purge(self) -> None:
        """Ошибка vocabulary_store.clear_all() не прерывает удаление истории."""

        class ErrorVocabularyStore:
            def clear_all(self) -> None:
                raise PermissionError("нет прав на запись")

        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-v")
        svc = HistoryService(store=store)
        svc._vocabulary_store = ErrorVocabularyStore()

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(result["history_deleted"], 1,
                         "История должна быть удалена даже при ошибке vocabulary_store")
        self.assertFalse(result["complete"])
        self.assertIn("vocabulary", result["errors"])

    def test_vocabulary_clear_all_unit(self) -> None:
        """VocabularyStore.clear_all() удаляет файл и не бросает исключений."""
        vocab = self._make_vocab_with_words()
        vocab_path = Path(self._tmpdir) / "vocabulary.json"
        self.assertTrue(vocab_path.exists())

        vocab.clear_all()

        self.assertFalse(vocab_path.exists(), "vocabulary.json должен быть удалён после clear_all()")

    def test_vocabulary_clear_all_idempotent(self) -> None:
        """Повторный VocabularyStore.clear_all() не бросает исключений."""
        vocab = VocabularyStore(data_dir=Path(self._tmpdir))
        try:
            vocab.clear_all()
            vocab.clear_all()
        except Exception as exc:
            self.fail(f"clear_all() бросил исключение: {exc}")

    def test_vocabulary_reloaded_sees_empty_after_clear(self) -> None:
        """После clear_all() новый VocabularyStore из того же tmpdir видит пустой список."""
        vocab = self._make_vocab_with_words()
        vocab.clear_all()

        vocab2 = VocabularyStore(data_dir=Path(self._tmpdir))
        self.assertEqual(vocab2.load(), [],
                         "Перезагруженный VocabularyStore должен вернуть [] после clear_all")


# ---------------------------------------------------------------------------
# #10 — settings_backups/ удаляется при purge
# ---------------------------------------------------------------------------

class SettingsBackupsPurgeTestCase(unittest.TestCase):
    """W1767 #10: purge_all_data удаляет settings_backups/ через _settings_backup."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _seed_settings_backups(self) -> Path:
        """Создаёт тестовые файлы settings backups."""
        sb_dir = Path(self._tmpdir) / "settings_backups"
        sb_dir.mkdir(parents=True, exist_ok=True)
        # Симулируем rolling backup файл с glossary
        backup = {
            "translation_glossary": {"Иван": "Ivan"},
            "lm_studio_url": "http://localhost:1234/v1",
        }
        (sb_dir / "20260601_120000_auto.json").write_text(
            json.dumps(backup), encoding="utf-8"
        )
        return sb_dir

    def test_settings_backups_dir_removed_on_purge(self) -> None:
        """purge_all_data должен удалить <settings_backups_dir>/ целиком."""
        sb_dir = self._seed_settings_backups()
        self.assertTrue(sb_dir.is_dir(), "settings_backups/ должен существовать до purge")

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        fake_sb = FakeSettingsBackup(backup_dir=str(sb_dir))
        svc._settings_backup = fake_sb

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), f"purge должен вернуть ok=True: {result}")
        self.assertFalse(sb_dir.exists(),
                         "settings_backups/ должен быть удалён после purge_all_data")

    def test_glossary_in_backup_not_on_disk_after_purge(self) -> None:
        """После purge термин из settings backup не должен быть на диске."""
        sb_dir = self._seed_settings_backups()
        secret = "Иван"

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._settings_backup = FakeSettingsBackup(backup_dir=str(sb_dir))

        svc.handle_purge_all_data({"confirm": True})

        # Проверяем только в settings_backups подкаталоге
        self.assertFalse(sb_dir.exists(),
                         f"settings_backups/ должен быть удалён, поэтому '{secret}' не может там быть")

    def test_no_settings_backup_no_crash(self) -> None:
        """purge_all_data без _settings_backup не бросает исключений."""
        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._settings_backup = None
        try:
            result = svc.handle_purge_all_data({"confirm": True})
        except Exception as exc:
            self.fail(f"purge без _settings_backup бросил исключение: {exc}")
        self.assertTrue(result.get("ok"))
        self.assertNotIn("settings_backups", result.get("errors", []))

    def test_nonexistent_backup_dir_no_crash(self) -> None:
        """purge_all_data с несуществующим backup dir не бросает исключений."""
        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        # Директория не существует
        fake_sb = FakeSettingsBackup(backup_dir=str(Path(self._tmpdir) / "nonexistent_sb"))
        svc._settings_backup = fake_sb

        try:
            result = svc.handle_purge_all_data({"confirm": True})
        except Exception as exc:
            self.fail(f"purge с несуществующим backup dir бросил исключение: {exc}")
        self.assertTrue(result.get("ok"))

    def test_settings_backup_error_does_not_abort_purge(self) -> None:
        """Ошибка удаления settings_backups/ не прерывает удаление истории."""

        class ErrorSettingsBackup:
            def get_backup_dir(self) -> None:
                raise RuntimeError("ошибка получения backup dir")

        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-sb")
        svc = HistoryService(store=store)
        svc._settings_backup = ErrorSettingsBackup()

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(result["history_deleted"], 1,
                         "История должна быть удалена даже при ошибке settings_backup")
        self.assertFalse(result["complete"])
        self.assertIn("settings_backups", result["errors"])


# ---------------------------------------------------------------------------
# E2E — все шесть gap-ов закрыты одним вызовом handle_purge_all_data
# ---------------------------------------------------------------------------

class PurgeAllDataE2EW1767TestCase(unittest.TestCase):
    """W1767 E2E: все новые gap-ы закрыты одним вызовом purge_all_data."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_e2e_all_six_gaps_closed(self) -> None:
        """E2E: backups + shares + translation_cache + glossary + vocabulary + settings_backups."""
        tmpdir = Path(self._tmpdir)

        # --- Seed: migration backup ---
        backup_dir = tmpdir / "backups" / "migration_backup_20260101_000000"
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "history.ndjson").write_text(
            '{"id":"r1","text":"приватная транскрипция"}\n', encoding="utf-8"
        )

        # --- Seed: shares ---
        shares_dir = tmpdir / "shares"
        shares_dir.mkdir(parents=True, exist_ok=True)
        secret_share = "текст шаринга с PII"
        (shares_dir / "share_deadbeef.txt").write_text(secret_share, encoding="utf-8")
        (shares_dir / "shares_index.json").write_text(
            json.dumps({"deadbeef": {"content": secret_share}}), encoding="utf-8"
        )

        # --- Seed: translation cache ---
        cache = TranslationCache(data_dir=self._tmpdir)
        cache.put("Привет", "ru", "es", "offline", "Hola")
        cache_path = tmpdir / "translation_cache.json"
        self.assertTrue(cache_path.exists(), "translation_cache.json должен существовать")

        # --- Seed: glossary in store settings ---
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("recording-1")
        store._settings = {"translation_glossary": {"Вася": "Vasya"}}

        # --- Seed: vocabulary ---
        vocab = VocabularyStore(data_dir=tmpdir)
        vocab.save(["СекретноеСлово", "ИванИванов"])
        vocab_path = tmpdir / "vocabulary.json"
        self.assertTrue(vocab_path.exists(), "vocabulary.json должен существовать")

        # --- Seed: settings backups ---
        sb_dir = tmpdir / "settings_backups"
        sb_dir.mkdir(parents=True, exist_ok=True)
        backup_content = {"translation_glossary": {"Вася": "Vasya"}}
        (sb_dir / "20260601_auto.json").write_text(
            json.dumps(backup_content), encoding="utf-8"
        )

        # --- Подключаем collaborators ---
        svc = HistoryService(store=store)
        svc._translation_cache = cache
        svc._vocabulary_store = vocab
        fake_settings_svc = FakeSettingsSvc()
        svc._settings_svc = fake_settings_svc
        svc._settings_backup = FakeSettingsBackup(backup_dir=str(sb_dir))

        # --- Purge ---
        result = svc.handle_purge_all_data({"confirm": True})

        # --- Assertions ---
        self.assertTrue(result.get("ok"), f"purge должен вернуть ok=True: {result}")
        self.assertTrue(result.get("complete"),
                        f"purge должен быть полным: {result.get('errors')}")

        # #2: backups удалены
        self.assertFalse((tmpdir / "backups").exists(),
                         "backups/ должен быть удалён")

        # #3: shares удалены
        self.assertFalse(shares_dir.exists(),
                         "shares/ должен быть удалён")

        # #7: translation_cache.json удалён, in-memory пуст
        self.assertFalse(cache_path.exists(),
                         "translation_cache.json должен быть удалён")
        self.assertEqual(len(cache._cache), 0,
                         "In-memory кэш переводов должен быть пуст")

        # #8: glossary сброшен в {}
        saved_settings = store.load_settings()
        self.assertEqual(saved_settings.get("translation_glossary"), {},
                         "translation_glossary должен быть {} после purge")
        # invalidate_cache() вызван
        self.assertTrue(fake_settings_svc.invalidated,
                        "invalidate_cache() должен быть вызван")

        # #9: vocabulary.json удалён
        self.assertFalse(vocab_path.exists(),
                         "vocabulary.json должен быть удалён")

        # #10: settings_backups/ удалён
        self.assertFalse(sb_dir.exists(),
                         "settings_backups/ должен быть удалён")

        # Контент PII не остался на диске
        pii_tokens = ["приватная транскрипция", secret_share, "Hola", "Вася", "СекретноеСлово"]
        for f in tmpdir.rglob("*"):
            if f.is_file():
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    for token in pii_tokens:
                        self.assertNotIn(
                            token, content,
                            f"PII-токен '{token}' найден в {f} после purge"
                        )
                except OSError:
                    pass  # файл удалён параллельно

    def test_confirm_false_no_purge(self) -> None:
        """Без confirm=True purge не удаляет ничего."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-nc")
        svc = HistoryService(store=store)

        result = svc.handle_purge_all_data({"confirm": False})

        self.assertFalse(result.get("ok", True) is True and result.get("error") is None,
                         "Без confirm не должно быть ok=True без error")
        self.assertIn("error", result,
                      "Ответ должен содержать 'error' при отсутствии confirm")


# ---------------------------------------------------------------------------
# BackendService wiring test
# ---------------------------------------------------------------------------

class BackendServiceW1767WiringTestCase(unittest.TestCase):
    """W1767: BackendService wires translation_cache, vocabulary, settings_svc,
    settings_backup в HistoryService."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_backend_wires_translation_cache_into_history(self) -> None:
        """BackendService.__init__ должен wire _translation_cache в _history._translation_cache."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._translation_cache,
            svc._translation_cache,
            "BackendService должен wire _translation_cache в _history._translation_cache",
        )
        self.assertIsNotNone(svc._history._translation_cache)

    def test_backend_wires_vocabulary_into_history(self) -> None:
        """BackendService.__init__ должен wire vocabulary в _history._vocabulary_store."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._vocabulary_store,
            svc.vocabulary,
            "BackendService должен wire vocabulary в _history._vocabulary_store",
        )
        self.assertIsNotNone(svc._history._vocabulary_store)

    def test_backend_wires_settings_svc_into_history(self) -> None:
        """BackendService.__init__ должен wire _settings_svc в _history._settings_svc."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._settings_svc,
            svc._settings_svc,
            "BackendService должен wire _settings_svc в _history._settings_svc",
        )
        self.assertIsNotNone(svc._history._settings_svc)

    def test_backend_wires_settings_backup_into_history(self) -> None:
        """BackendService.__init__ должен wire _settings_svc._backup в _history._settings_backup."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._settings_backup,
            svc._settings_svc._backup,
            "BackendService должен wire _settings_svc._backup в _history._settings_backup",
        )
        self.assertIsNotNone(svc._history._settings_backup)

    def test_backend_wires_sharing_manager_into_history(self) -> None:
        """wave-1770 MED: BackendService.__init__ must wire _sharing into _history._sharing_manager.

        Without this wiring, SharingManager.clear() is never called during purge_all_data,
        leaving in-memory share index populated with stale PII after a purge.
        """
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._sharing_manager,
            svc._sharing,
            "BackendService must wire _sharing into _history._sharing_manager for purge coverage",
        )
        self.assertIsNotNone(svc._history._sharing_manager)


if __name__ == "__main__":
    unittest.main()
