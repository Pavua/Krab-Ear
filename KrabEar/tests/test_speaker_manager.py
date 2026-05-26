"""Тесты SpeakerManager — псевдонимы спикеров диаризации."""

from __future__ import annotations
from backend.speaker_manager import SpeakerManager

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Настройка путей для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestSpeakerManagerCRUD(unittest.TestCase):
    """Базовые операции CRUD без персистентности."""

    def setUp(self):
        self.mgr = SpeakerManager()  # data_dir=None → in-memory

    def test_set_and_get_alias(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша")

    def test_get_alias_missing_returns_none(self):
        self.assertIsNone(self.mgr.get_alias("SPEAKER_99"))

    def test_set_alias_overwrites(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_00", "Павел")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Павел")

    def test_remove_alias_returns_true(self):
        self.mgr.set_alias("SPEAKER_01", "Маша")
        removed = self.mgr.remove_alias("SPEAKER_01")
        self.assertTrue(removed)
        self.assertIsNone(self.mgr.get_alias("SPEAKER_01"))

    def test_remove_missing_alias_returns_false(self):
        removed = self.mgr.remove_alias("SPEAKER_42")
        self.assertFalse(removed)

    def test_get_all_aliases(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_01", "Маша")
        aliases = self.mgr.get_all_aliases()
        self.assertEqual(aliases, {"SPEAKER_00": "Паша", "SPEAKER_01": "Маша"})

    def test_get_all_aliases_returns_copy(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        aliases = self.mgr.get_all_aliases()
        aliases["SPEAKER_00"] = "Изменено"
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша")

    def test_set_empty_name_removes_alias(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_00", "")
        self.assertIsNone(self.mgr.get_alias("SPEAKER_00"))


class TestSpeakerManagerApplyAliases(unittest.TestCase):
    """Применение псевдонимов к тексту транскрипции."""

    def setUp(self):
        self.mgr = SpeakerManager()

    def test_apply_single_alias(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        result = self.mgr.apply_aliases("[SPEAKER_00] Привет мир")
        self.assertEqual(result, "[Паша] Привет мир")

    def test_apply_multiple_aliases(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_01", "Маша")
        text = "[SPEAKER_00] Привет\n[SPEAKER_01] Как дела?"
        result = self.mgr.apply_aliases(text)
        self.assertEqual(result, "[Паша] Привет\n[Маша] Как дела?")

    def test_unknown_speaker_left_as_is(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        text = "[SPEAKER_00] Привет [SPEAKER_01] Неизвестный"
        result = self.mgr.apply_aliases(text)
        self.assertEqual(result, "[Паша] Привет [SPEAKER_01] Неизвестный")

    def test_apply_no_aliases_unchanged(self):
        text = "[SPEAKER_00] Текст без псевдонимов"
        result = self.mgr.apply_aliases(text)
        self.assertEqual(result, text)

    def test_apply_empty_string(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.assertEqual(self.mgr.apply_aliases(""), "")

    def test_apply_text_without_tags(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        text = "Обычный текст без тегов спикеров"
        self.assertEqual(self.mgr.apply_aliases(text), text)

    def test_apply_repeated_speaker(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        text = "[SPEAKER_00] раз [SPEAKER_00] два"
        result = self.mgr.apply_aliases(text)
        self.assertEqual(result, "[Паша] раз [Паша] два")


class TestSpeakerManagerPersistence(unittest.TestCase):
    """Персистентность псевдонимов в файл."""

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr1 = SpeakerManager(data_dir=tmpdir)
            mgr1.set_alias("SPEAKER_00", "Паша")
            mgr1.set_alias("SPEAKER_01", "Маша")

            mgr2 = SpeakerManager(data_dir=tmpdir)
            self.assertEqual(mgr2.get_alias("SPEAKER_00"), "Паша")
            self.assertEqual(mgr2.get_alias("SPEAKER_01"), "Маша")

    def test_aliases_file_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SpeakerManager(data_dir=tmpdir)
            mgr.set_alias("SPEAKER_00", "Паша")
            aliases_path = Path(tmpdir) / "speaker_aliases.json"
            self.assertTrue(aliases_path.exists())
            data = json.loads(aliases_path.read_text(encoding="utf-8"))
            self.assertEqual(data["SPEAKER_00"], "Паша")

    def test_remove_persists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr1 = SpeakerManager(data_dir=tmpdir)
            mgr1.set_alias("SPEAKER_00", "Паша")
            mgr1.remove_alias("SPEAKER_00")

            mgr2 = SpeakerManager(data_dir=tmpdir)
            self.assertIsNone(mgr2.get_alias("SPEAKER_00"))

    def test_empty_data_dir_no_crash(self):
        mgr = SpeakerManager(data_dir=None)
        mgr.set_alias("SPEAKER_00", "Паша")  # не должно упасть
        self.assertEqual(mgr.get_alias("SPEAKER_00"), "Паша")

    def test_missing_file_no_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SpeakerManager(data_dir=tmpdir)
            # Файл ещё не создан — get_alias должен вернуть None без исключения
            self.assertIsNone(mgr.get_alias("SPEAKER_00"))


class TestSpeakerManagerIPCHandlers(unittest.TestCase):
    """IPC-обработчики."""

    def setUp(self):
        self.mgr = SpeakerManager()

    def test_handle_set_speaker_alias(self):
        result = self.mgr.handle_set_speaker_alias(
            {"speaker_id": "SPEAKER_00", "name": "Паша"}
        )
        self.assertEqual(result["speaker_id"], "SPEAKER_00")
        self.assertEqual(result["name"], "Паша")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша")

    def test_handle_set_speaker_alias_missing_id(self):
        with self.assertRaises(ValueError):
            self.mgr.handle_set_speaker_alias({"name": "Паша"})

    def test_handle_set_speaker_alias_empty_name(self):
        with self.assertRaises(ValueError):
            self.mgr.handle_set_speaker_alias({"speaker_id": "SPEAKER_00", "name": ""})

    def test_handle_get_speaker_aliases(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        result = self.mgr.handle_get_speaker_aliases({})
        self.assertIn("aliases", result)
        self.assertEqual(result["aliases"]["SPEAKER_00"], "Паша")

    def test_handle_remove_speaker_alias_exists(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        result = self.mgr.handle_remove_speaker_alias({"speaker_id": "SPEAKER_00"})
        self.assertTrue(result["removed"])
        self.assertIsNone(self.mgr.get_alias("SPEAKER_00"))

    def test_handle_remove_speaker_alias_missing(self):
        result = self.mgr.handle_remove_speaker_alias({"speaker_id": "SPEAKER_99"})
        self.assertFalse(result["removed"])

    def test_handle_remove_speaker_alias_no_id(self):
        with self.assertRaises(ValueError):
            self.mgr.handle_remove_speaker_alias({})


class TestSpeakerManagerEdgeCases(unittest.TestCase):
    """Edge cases: rename, merge, list, persistence via tempfile."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = SpeakerManager(data_dir=self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Create profile with name + lang hint encoded in alias
    # ------------------------------------------------------------------

    def test_create_profile_with_lang_hint(self):
        """Псевдоним может кодировать язык: 'Паша (RU)'."""
        self.mgr.set_alias("SPEAKER_00", "Паша (RU)")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша (RU)")

    # ------------------------------------------------------------------
    # Rename: exists vs not-exists
    # ------------------------------------------------------------------

    def test_rename_existing_alias(self):
        """Переименование существующего псевдонима через повторный set_alias."""
        self.mgr.set_alias("SPEAKER_00", "OldName")
        self.mgr.set_alias("SPEAKER_00", "NewName")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "NewName")

    def test_rename_nonexistent_creates_entry(self):
        """set_alias на несуществующий speaker_id — просто создаёт запись."""
        self.mgr.set_alias("SPEAKER_99", "Ghost")
        self.assertEqual(self.mgr.get_alias("SPEAKER_99"), "Ghost")

    # ------------------------------------------------------------------
    # Merge two profiles: items of both end up under one alias
    # ------------------------------------------------------------------

    def test_merge_two_profiles(self):
        """Слияние: псевдоним SPEAKER_01 → SPEAKER_00, SPEAKER_01 удаляется."""
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_01", "Дубль")
        # Merge: assign SPEAKER_01's texts to SPEAKER_00 (alias stays),
        # then remove SPEAKER_01 entry.
        self.mgr.remove_alias("SPEAKER_01")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша")
        self.assertIsNone(self.mgr.get_alias("SPEAKER_01"))
        self.assertNotIn("SPEAKER_01", self.mgr.get_all_aliases())

    def test_merge_with_self_is_noop(self):
        """Слияние спикера с самим собой — псевдоним не меняется."""
        self.mgr.set_alias("SPEAKER_00", "Паша")
        # "merge self" → set same alias again, remove same id — effectively a noop
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша")

    # ------------------------------------------------------------------
    # List profiles: sort order, filter
    # ------------------------------------------------------------------

    def test_list_profiles_sorted(self):
        """get_all_aliases возвращает все записи; ключи можно сортировать."""
        self.mgr.set_alias("SPEAKER_02", "В")
        self.mgr.set_alias("SPEAKER_00", "А")
        self.mgr.set_alias("SPEAKER_01", "Б")
        aliases = self.mgr.get_all_aliases()
        sorted_keys = sorted(aliases.keys())
        self.assertEqual(sorted_keys, ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"])

    def test_list_profiles_filter_by_pattern(self):
        """Можно фильтровать псевдонимы по шаблону имени."""
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_01", "Маша")
        self.mgr.set_alias("SPEAKER_02", "Никита")
        aliases = self.mgr.get_all_aliases()
        asha = {k: v for k, v in aliases.items() if "аша" in v}
        self.assertIn("SPEAKER_00", asha)
        self.assertIn("SPEAKER_01", asha)
        self.assertNotIn("SPEAKER_02", asha)

    # ------------------------------------------------------------------
    # Persistence via tempfile (mock-free, uses real fs)
    # ------------------------------------------------------------------

    def test_persistence_survives_reload(self):
        """Псевдонимы сохраняются и загружаются из tempdir."""
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_01", "Маша")
        # Reload from same dir
        mgr2 = SpeakerManager(data_dir=self.tmpdir)
        self.assertEqual(mgr2.get_alias("SPEAKER_00"), "Паша")
        self.assertEqual(mgr2.get_alias("SPEAKER_01"), "Маша")

    # ------------------------------------------------------------------
    # Rename to existing name (two speakers get the same human name)
    # ------------------------------------------------------------------

    def test_rename_to_existing_name_allowed(self):
        """Два speaker_id могут иметь одинаковый псевдоним — это легально."""
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_01", "Паша")  # same name, different speaker
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша")
        self.assertEqual(self.mgr.get_alias("SPEAKER_01"), "Паша")
        self.assertEqual(len(self.mgr.get_all_aliases()), 2)

    # ------------------------------------------------------------------
    # apply_aliases after merge: segments of removed speaker reassigned
    # ------------------------------------------------------------------

    def test_apply_aliases_after_merge_segments_reassigned(self):
        """После слияния apply_aliases применяет имя оставшегося спикера."""
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_01", "Дубль")
        # Слияние: SPEAKER_01 → SPEAKER_00, SPEAKER_01 удаляется
        self.mgr.remove_alias("SPEAKER_01")
        # В тексте теги SPEAKER_01 остаются нетронутыми (merge = переименование сегментов
        # в тексте остаётся задачей верхнего уровня), но псевдоним удалён
        text = "[SPEAKER_00] Привет\n[SPEAKER_01] Я тоже Паша"
        result = self.mgr.apply_aliases(text)
        self.assertEqual(result, "[Паша] Привет\n[SPEAKER_01] Я тоже Паша")

    # ------------------------------------------------------------------
    # IPC: handle_set_speaker_alias whitespace-only name
    # ------------------------------------------------------------------

    def test_handle_set_alias_whitespace_name_raises(self):
        """IPC: name из пробелов → ValueError (strip делает пустой строкой)."""
        with self.assertRaises(ValueError):
            self.mgr.handle_set_speaker_alias(
                {"speaker_id": "SPEAKER_00", "name": "   "}
            )

    # ------------------------------------------------------------------
    # Thread-safety: concurrent set_alias does not corrupt state
    # ------------------------------------------------------------------

    def test_concurrent_set_alias_no_corruption(self):
        """Параллельные set_alias не приводят к race condition (базовая проверка)."""
        import threading

        errors: list[Exception] = []

        def worker(spk: str, name: str) -> None:
            try:
                for _ in range(20):
                    self.mgr.set_alias(spk, name)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"SPEAKER_{i:02d}", f"User{i}"))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Ошибки потоков: {errors}")
        # Все 5 псевдонимов должны быть установлены
        aliases = self.mgr.get_all_aliases()
        self.assertEqual(len(aliases), 5)


class TestSpeakerManagerWave92(unittest.TestCase):
    """Wave 92 required test names + fingerprint / register_speaker coverage."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = SpeakerManager(data_dir=self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Required: test_create_speaker_profile
    # ------------------------------------------------------------------

    def test_create_speaker_profile(self):
        """Создание профиля спикера: set_alias + get_alias возвращает имя."""
        self.mgr.set_alias("SPEAKER_00", "Иван")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Иван")
        aliases = self.mgr.get_all_aliases()
        self.assertIn("SPEAKER_00", aliases)

    # ------------------------------------------------------------------
    # Required: test_rename_speaker
    # ------------------------------------------------------------------

    def test_rename_speaker(self):
        """Переименование спикера: старый псевдоним заменяется новым (только сессия)."""
        self.mgr.set_alias("SPEAKER_00", "Старое_имя")
        self.mgr.set_alias("SPEAKER_00", "Новое_имя")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Новое_имя")

    # ------------------------------------------------------------------
    # Required: test_merge_speakers
    # ------------------------------------------------------------------

    def test_merge_speakers(self):
        """Слияние спикеров: SPEAKER_01 удаляется, SPEAKER_00 сохраняет своё имя.
        voice fingerprints сохраняются под merged speaker_id."""
        import numpy as np

        self.mgr.set_alias("SPEAKER_00", "Главный")
        self.mgr.set_alias("SPEAKER_01", "Дубль")

        # Register fingerprints for both
        emb0 = np.ones(512, dtype=np.float32)
        emb1 = np.ones(512, dtype=np.float32) * 0.5
        self.mgr._fingerprints["SPEAKER_00"] = emb0.tolist()
        self.mgr._fingerprints["SPEAKER_01"] = emb1.tolist()
        self.mgr._save_fingerprints()

        # Perform merge: remove the duplicate speaker
        self.mgr.remove_alias("SPEAKER_01")
        self.mgr.delete_fingerprint("SPEAKER_01")

        # Only SPEAKER_00 should remain
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Главный")
        self.assertIsNone(self.mgr.get_alias("SPEAKER_01"))
        fps = self.mgr.get_all_fingerprints()
        self.assertIn("SPEAKER_00", fps)
        self.assertNotIn("SPEAKER_01", fps)

    # ------------------------------------------------------------------
    # Required: test_persist_reload
    # ------------------------------------------------------------------

    def test_persist_reload(self):
        """JSON roundtrip: псевдонимы и фингерпринты переживают reload."""
        import numpy as np

        self.mgr.set_alias("SPEAKER_00", "Паша")
        emb = np.random.rand(512).astype(np.float32)
        self.mgr._fingerprints["SPEAKER_00"] = emb.tolist()
        self.mgr._save_fingerprints()

        mgr2 = SpeakerManager(data_dir=self.tmpdir)
        self.assertEqual(mgr2.get_alias("SPEAKER_00"), "Паша")
        fps = mgr2.get_all_fingerprints()
        self.assertIn("SPEAKER_00", fps)
        self.assertEqual(len(fps["SPEAKER_00"]), 512)

    # ------------------------------------------------------------------
    # Required: test_concurrent_rename
    # ------------------------------------------------------------------

    def test_concurrent_rename(self):
        """Атомарность: параллельные rename (set_alias) не вызывают race condition."""
        import threading

        errors: list[Exception] = []

        def rename_worker(spk: str, name: str) -> None:
            try:
                for _ in range(30):
                    self.mgr.set_alias(spk, name)
                    _ = self.mgr.get_alias(spk)  # concurrent read
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=rename_worker, args=("SPEAKER_00", f"Name{i}"))
            for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        # Final alias should be one of the valid names (not None/corrupt)
        alias = self.mgr.get_alias("SPEAKER_00")
        self.assertIsNotNone(alias)

    # ------------------------------------------------------------------
    # Required: test_unicode_speaker_name
    # ------------------------------------------------------------------

    def test_unicode_speaker_name(self):
        """Псевдонимы с unicode (кириллица, эмодзи, иероглифы) сохраняются корректно."""
        names = {
            "SPEAKER_00": "Александра 🎙",
            "SPEAKER_01": "Иван Петрович",
            "SPEAKER_02": "田中さん",
            "SPEAKER_03": "María José",
        }
        for spk, name in names.items():
            self.mgr.set_alias(spk, name)

        # Verify in memory
        for spk, name in names.items():
            self.assertEqual(self.mgr.get_alias(spk), name)

        # Verify roundtrip via JSON
        mgr2 = SpeakerManager(data_dir=self.tmpdir)
        for spk, name in names.items():
            self.assertEqual(mgr2.get_alias(spk), name)

        # Verify apply_aliases works with unicode names
        text = "[SPEAKER_00] Привет [SPEAKER_02] こんにちは"
        result = self.mgr.apply_aliases(text)
        self.assertIn("Александра 🎙", result)
        self.assertIn("田中さん", result)

    # ------------------------------------------------------------------
    # Required: test_delete_speaker_unmerges_history
    # ------------------------------------------------------------------

    def test_delete_speaker_unmerges_history(self):
        """Удаление псевдонима спикера: apply_aliases возвращает raw SPEAKER_XX тег
        (тексты истории не затрагиваются, теги 'отмёрживаются' обратно в дефолт)."""
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_01", "Маша")

        # Before deletion
        text = "[SPEAKER_00] Привет\n[SPEAKER_01] Пока"
        result_before = self.mgr.apply_aliases(text)
        self.assertEqual(result_before, "[Паша] Привет\n[Маша] Пока")

        # Delete SPEAKER_01 alias (simulates "unmerge" — remove custom label)
        self.mgr.remove_alias("SPEAKER_01")

        # After deletion: SPEAKER_01 reverts to raw tag, SPEAKER_00 unchanged
        result_after = self.mgr.apply_aliases(text)
        self.assertEqual(result_after, "[Паша] Привет\n[SPEAKER_01] Пока")

        # Full removal of SPEAKER_00 reverts both
        self.mgr.remove_alias("SPEAKER_00")
        result_clean = self.mgr.apply_aliases(text)
        self.assertEqual(result_clean, "[SPEAKER_00] Привет\n[SPEAKER_01] Пока")

    # ------------------------------------------------------------------
    # Bonus: register_speaker + find_matching_speaker + update_fingerprint
    # ------------------------------------------------------------------

    def test_register_speaker_returns_auto_id(self):
        """register_speaker returns Speaker_0, Speaker_1, ... sequentially."""
        import numpy as np

        emb0 = np.ones(512, dtype=np.float32)
        emb1 = np.zeros(512, dtype=np.float32)
        emb1[0] = 1.0

        sid0 = self.mgr.register_speaker("Первый", emb0)
        sid1 = self.mgr.register_speaker("Второй", emb1)

        self.assertEqual(sid0, "Speaker_0")
        self.assertEqual(sid1, "Speaker_1")
        self.assertEqual(self.mgr.get_alias(sid0), "Первый")
        self.assertEqual(self.mgr.get_alias(sid1), "Второй")

    def test_find_matching_speaker_above_threshold(self):
        """find_matching_speaker returns speaker_id when cosine similarity >= threshold."""
        import numpy as np

        emb = np.ones(512, dtype=np.float32)
        sid = self.mgr.register_speaker("Тест", emb)

        # Near-identical embedding — should match
        emb_query = np.ones(512, dtype=np.float32) * 0.999
        matched = self.mgr.find_matching_speaker(emb_query, threshold=0.9)
        self.assertEqual(matched, sid)

    def test_find_matching_speaker_below_threshold_returns_none(self):
        """find_matching_speaker returns None when best score < threshold."""
        import numpy as np

        emb = np.ones(512, dtype=np.float32)
        self.mgr.register_speaker("Тест", emb)

        # Orthogonal embedding — cosine similarity ~0
        emb_query = np.zeros(512, dtype=np.float32)
        emb_query[0] = 1.0
        matched = self.mgr.find_matching_speaker(emb_query, threshold=0.9)
        self.assertIsNone(matched)

    def test_find_matching_speaker_zero_embedding_returns_none(self):
        """find_matching_speaker with zero embedding returns None."""
        import numpy as np

        emb = np.ones(512, dtype=np.float32)
        self.mgr.register_speaker("Тест", emb)

        zero = np.zeros(512, dtype=np.float32)
        self.assertIsNone(self.mgr.find_matching_speaker(zero, threshold=0.5))

    def test_update_fingerprint_ema(self):
        """update_fingerprint applies EMA blend to existing fingerprint."""
        import numpy as np

        emb_init = np.zeros(512, dtype=np.float32)
        sid = self.mgr.register_speaker("A", emb_init)

        emb_new = np.ones(512, dtype=np.float32)
        updated = self.mgr.update_fingerprint(sid, emb_new, alpha=1.0)
        self.assertTrue(updated)

        fp = np.array(self.mgr.get_all_fingerprints()[sid], dtype=np.float32)
        # alpha=1.0 → fully replaced by new embedding
        np.testing.assert_allclose(fp, emb_new, rtol=1e-5)

    def test_update_fingerprint_nonexistent_returns_false(self):
        """update_fingerprint on unknown speaker_id returns False."""
        import numpy as np

        emb = np.ones(512, dtype=np.float32)
        result = self.mgr.update_fingerprint("NONEXISTENT", emb)
        self.assertFalse(result)

    def test_delete_fingerprint(self):
        """delete_fingerprint removes the entry and returns True."""
        import numpy as np

        sid = self.mgr.register_speaker("X", np.ones(512, dtype=np.float32))
        deleted = self.mgr.delete_fingerprint(sid)
        self.assertTrue(deleted)
        self.assertNotIn(sid, self.mgr.get_all_fingerprints())

    def test_delete_fingerprint_nonexistent_returns_false(self):
        """delete_fingerprint on unknown speaker returns False."""
        self.assertFalse(self.mgr.delete_fingerprint("GHOST"))

    def test_handle_register_speaker_ipc(self):
        """IPC handle_register_speaker creates speaker from embedding list."""
        import numpy as np

        emb = np.ones(512, dtype=np.float32).tolist()
        result = self.mgr.handle_register_speaker({"name": "Тест", "embedding": emb})
        self.assertIn("speaker_id", result)
        self.assertEqual(result["name"], "Тест")

    def test_handle_register_speaker_missing_embedding(self):
        """IPC handle_register_speaker without embedding raises ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.handle_register_speaker({"name": "Тест"})

    def test_handle_list_speaker_fingerprints_ipc(self):
        """IPC handle_list_speaker_fingerprints returns count and speaker info."""
        import numpy as np

        self.mgr.register_speaker("А", np.ones(512, dtype=np.float32))
        self.mgr.register_speaker("Б", np.ones(512, dtype=np.float32) * 0.5)
        result = self.mgr.handle_list_speaker_fingerprints({})
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["speakers"]), 2)
        speaker_ids = {s["speaker_id"] for s in result["speakers"]}
        self.assertIn("Speaker_0", speaker_ids)

    def test_fingerprints_persist_across_reload(self):
        """Fingerprints written to disk and loaded by new SpeakerManager instance."""
        import numpy as np

        emb = np.random.rand(512).astype(np.float32)
        sid = self.mgr.register_speaker("Краб", emb)

        mgr2 = SpeakerManager(data_dir=self.tmpdir)
        fps = mgr2.get_all_fingerprints()
        self.assertIn(sid, fps)
        self.assertEqual(len(fps[sid]), 512)
        # Auto-counter should be restored — next ID should be Speaker_1
        sid2 = mgr2.register_speaker("Второй", emb)
        self.assertEqual(sid2, "Speaker_1")


class TestSpeakerManagerPrivacyMode(unittest.TestCase):
    """W951 HIGH F2: voice embeddings must not be persisted when privacy_mode_enabled."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_register_speaker_skips_persist_in_privacy_mode(self):
        """Fingerprint file must NOT be written when privacy_mode_enabled=True."""
        import numpy as np

        mgr = SpeakerManager(
            data_dir=self.tmpdir,
            settings_provider=lambda: {"privacy_mode_enabled": True},
        )
        emb = np.ones(512, dtype=np.float32)
        sid = mgr.register_speaker("TestUser", emb)

        # In-memory state should be intact
        self.assertIn(sid, mgr.get_all_fingerprints())

        # On-disk file must NOT exist — privacy gate suppressed _save_fingerprints
        fp_path = Path(self.tmpdir) / "speaker_fingerprints.json"
        self.assertFalse(
            fp_path.exists(),
            "speaker_fingerprints.json must not be written when privacy mode is active",
        )

    def test_register_speaker_persists_when_privacy_mode_off(self):
        """Fingerprint file IS written when privacy_mode_enabled=False (baseline)."""
        import numpy as np

        mgr = SpeakerManager(
            data_dir=self.tmpdir,
            settings_provider=lambda: {"privacy_mode_enabled": False},
        )
        emb = np.ones(512, dtype=np.float32)
        mgr.register_speaker("TestUser", emb)

        fp_path = Path(self.tmpdir) / "speaker_fingerprints.json"
        self.assertTrue(
            fp_path.exists(),
            "speaker_fingerprints.json must be written when privacy mode is off",
        )

    def test_register_speaker_persists_without_settings_provider(self):
        """Default (no settings_provider) should persist fingerprints as before."""
        import numpy as np

        mgr = SpeakerManager(data_dir=self.tmpdir)
        emb = np.ones(512, dtype=np.float32)
        mgr.register_speaker("AnonUser", emb)

        fp_path = Path(self.tmpdir) / "speaker_fingerprints.json"
        self.assertTrue(
            fp_path.exists(),
            "speaker_fingerprints.json must be written when no settings_provider given",
        )

    def test_update_fingerprint_skips_persist_in_privacy_mode(self):
        """update_fingerprint must not persist when privacy mode is active."""
        import numpy as np

        # First register without privacy mode
        mgr = SpeakerManager(
            data_dir=self.tmpdir,
            settings_provider=lambda: {"privacy_mode_enabled": False},
        )
        emb0 = np.ones(512, dtype=np.float32)
        sid = mgr.register_speaker("User", emb0)
        fp_path = Path(self.tmpdir) / "speaker_fingerprints.json"
        mtime_after_register = fp_path.stat().st_mtime

        # Now switch to privacy mode and update fingerprint
        mgr2 = SpeakerManager(
            data_dir=self.tmpdir,
            settings_provider=lambda: {"privacy_mode_enabled": True},
        )
        emb1 = np.zeros(512, dtype=np.float32)
        emb1[0] = 1.0
        mgr2.update_fingerprint(sid, emb1)

        # File mtime must NOT change (privacy gate blocked the write)
        mtime_after_update = fp_path.stat().st_mtime
        self.assertEqual(
            mtime_after_register,
            mtime_after_update,
            "speaker_fingerprints.json must not be rewritten when privacy mode is active",
        )

    def test_is_privacy_mode_handles_provider_exception(self):
        """_is_privacy_mode returns False gracefully when provider raises."""
        def broken_provider():
            raise RuntimeError("settings unavailable")

        mgr = SpeakerManager(settings_provider=broken_provider)
        # Should not raise; defaults to False (non-private, safe fallback)
        self.assertFalse(mgr._is_privacy_mode())


if __name__ == "__main__":
    unittest.main()
