"""G3-Krab-Ear-privacy-03: тестовые пробелы 04, 05, 06, 08 (27.11.2026).

- P03-04: внешний Obsidian vault вне data_dir — очистка, сохранность чужих файлов,
  отказ на недоступном и подменённом пути.
- P03-05: FakeStore-ы purge-тестов совпадают со StateStore по сигнатурам и
  действительно уничтожают записи при compact; плюс реальный StateStore без
  открытого текста на диске после purge.
- P03-06: граница purge для hardlink и симлинков, указывающих за пределы профиля.
- P03-08: E2E перевода существующего открытого профиля в зашифрованный —
  сканирование остатков, перезапуск читателя, сбой fsync, нет ключа, сбой после
  replace и повторный запуск.

Все данные синтетические, каталоги — TemporaryDirectory. Keychain не трогается:
conftest нейтрализует `security`, а провайдер ключа подменяется явно.
"""

from __future__ import annotations

import base64
import importlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _plaintext_export_test_helpers import off_authorizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService  # noqa: E402
from backend.obsidian_sync import ObsidianSyncManager  # noqa: E402
from backend.state_store import StateStore  # noqa: E402

PII = "СинтетическийСекретП03"


def _tmpdir(case: unittest.TestCase) -> Path:
    td = tempfile.TemporaryDirectory()
    case.addCleanup(td.cleanup)
    return Path(td.name).resolve()


def _run_purge(svc: HistoryService) -> dict:
    with patch("backend.crypto_keystore.delete_history_key", return_value=True):
        return svc.handle_purge_all_data({"confirm": True})


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()
    }


# ---------------------------------------------------------------------------
# P03-04 — внешний Obsidian vault
# ---------------------------------------------------------------------------

class ExternalObsidianVaultPurgeTestCase(unittest.TestCase):
    """Vault лежит в отдельном каталоге, не внутри профиля."""

    def setUp(self) -> None:
        self.profile = _tmpdir(self)
        self.vault = _tmpdir(self)
        self.assertFalse(self.vault.is_relative_to(self.profile))
        self.store = StateStore(data_dir=self.profile)
        self.store.add_history_item(text=f"{PII} история")
        self.obs = ObsidianSyncManager(
            data_dir=str(self.profile), plaintext_export_authorizer=off_authorizer(),
        )
        self.obs.configure(str(self.vault), folder="Transcriptions")
        self.target = self.vault / "Transcriptions"
        self.synced = [
            self.target / "transcript_2026-11-27_10-00-00_aa.md",
            self.target / "transcript_2026-11-27_11-00-00_bb.md",
        ]
        for path in self.synced:
            path.write_text(f"# {PII}", encoding="utf-8")
        # Чужие заметки владельца: purge не вправе их трогать.
        self.foreign = {
            self.target / "Моя заметка.md": "личное, не транскрипт",
            self.vault / "transcript_вне_папки.md": "лежит вне папки синхронизации",
            self.vault / "Daily" / "2026-11-27.md": "дневник",
        }
        for path, text in self.foreign.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.svc = HistoryService(store=self.store)
        self.svc._obsidian_sync = self.obs

    def test_external_vault_cleaned_foreign_notes_kept(self) -> None:
        result = _run_purge(self.svc)

        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("complete"), result.get("errors"))
        self.assertEqual(result.get("obsidian_deleted"), len(self.synced))
        for path in self.synced:
            self.assertFalse(path.exists(), f"{path.name} пережил purge во внешнем vault")
        for path, text in self.foreign.items():
            self.assertEqual(path.read_text(encoding="utf-8"), text, f"purge задел чужой {path}")
        self.assertFalse((self.profile / "obsidian_sync.json").exists(),
                         "адрес внешнего vault пережил purge в профиле")
        for root in (self.profile, self.vault):
            for name, data in _snapshot(root).items():
                self.assertNotIn(PII.encode("utf-8"), data, f"PII осталась в {root.name}/{name}")

    def test_unavailable_external_vault_is_reported_not_silent(self) -> None:
        shutil.rmtree(self.vault)

        result = _run_purge(self.svc)

        self.assertTrue(result.get("ok"), result)
        self.assertFalse(result.get("complete"), "очистка недоступного vault выдана за полную")
        self.assertIn("obsidian", result.get("errors", []))
        self.assertEqual(result.get("history_deleted"), 1, "история должна очищаться независимо")

    def test_sync_folder_swapped_to_symlink_outside_vault_is_refused(self) -> None:
        elsewhere = _tmpdir(self)
        bait = elsewhere / "transcript_2026-11-27_12-00-00_cc.md"
        bait.write_text("чужой файл с подходящим именем", encoding="utf-8")
        shutil.rmtree(self.target)
        self.target.symlink_to(elsewhere, target_is_directory=True)

        result = _run_purge(self.svc)

        self.assertTrue(bait.exists(), "purge удалил файл за пределами vault по симлинку")
        self.assertFalse(result.get("complete"))
        self.assertIn("obsidian", result.get("errors", []))

    def test_vault_root_swapped_to_symlink_is_refused(self) -> None:
        elsewhere = _tmpdir(self)
        (elsewhere / "Transcriptions").mkdir()
        bait = elsewhere / "Transcriptions" / "transcript_2026-11-27_13-00-00_dd.md"
        bait.write_text("чужой файл с подходящим именем", encoding="utf-8")
        shutil.rmtree(self.vault)
        self.vault.symlink_to(elsewhere, target_is_directory=True)

        result = _run_purge(self.svc)

        self.assertTrue(bait.exists(), "purge удалил файл по подменённому корню vault")
        self.assertFalse(result.get("complete"))
        self.assertIn("obsidian", result.get("errors", []))

    def _restart_manager(self) -> ObsidianSyncManager:
        """Новый менеджер читает state-файл, как после перезапуска backend.

        Проверка «vault под $HOME» из _load_state здесь мешала бы: временные
        каталоги лежат вне $HOME, и менеджер просто отверг бы vault. Корнем
        считаем общий временный каталог — тогда проверяется именно подмена.
        """
        tmp_root = Path(tempfile.gettempdir()).resolve()
        with patch("pathlib.Path.home", return_value=tmp_root):
            restarted = ObsidianSyncManager(
                data_dir=str(self.profile), plaintext_export_authorizer=off_authorizer(),
            )
        self.svc._obsidian_sync = restarted
        return restarted

    def test_vault_swapped_to_symlink_before_restart_is_refused(self) -> None:
        """Подмена ДО перезапуска: загрузка state не должна «отмывать» путь через resolve().

        Если _load_state хранит resolve()-путь, после рестарта vault указывает
        на чужой каталог, проверка resolve()==vault_path проходит, и purge
        удаляет чужие transcript_*.md.
        """
        elsewhere = _tmpdir(self)
        (elsewhere / "Transcriptions").mkdir()
        bait = elsewhere / "Transcriptions" / "transcript_2026-11-27_14-00-00_ee.md"
        bait.write_text("чужой файл с подходящим именем", encoding="utf-8")
        shutil.rmtree(self.vault)
        self.vault.symlink_to(elsewhere, target_is_directory=True)
        self._restart_manager()

        result = _run_purge(self.svc)

        self.assertTrue(bait.exists(), "purge удалил чужой файл после рестарта по подменённому vault")
        self.assertFalse(result.get("complete"), "очистка по подменённому vault выдана за полную")
        self.assertIn("obsidian", result.get("errors", []))
        self.assertEqual(result.get("history_deleted"), 1, "история должна очищаться независимо")

    def test_legacy_state_with_symlink_component_refused_until_reconfigured(self) -> None:
        """Старый state хранил путь как есть, с симлинком внутри — purge не подтверждает очистку."""
        alias_root = _tmpdir(self)
        alias = alias_root / "vault_alias"
        alias.symlink_to(self.vault, target_is_directory=True)
        state_path = self.profile / "obsidian_sync.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["vault_path"] = str(alias)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        restarted = self._restart_manager()

        refused = _run_purge(self.svc)

        self.assertFalse(refused.get("complete"), "purge по пути с симлинком выдан за полный")
        self.assertIn("obsidian", refused.get("errors", []))
        for path in self.synced:
            self.assertTrue(path.exists(), f"{path.name} удалён по непроверенному пути")

        restarted.configure(str(alias), folder="Transcriptions")
        retried = _run_purge(self.svc)

        self.assertTrue(retried.get("complete"), retried.get("errors"))
        for path in self.synced:
            self.assertFalse(path.exists(), f"{path.name} пережил purge после перенастройки")
        for path, text in self.foreign.items():
            self.assertEqual(path.read_text(encoding="utf-8"), text, f"purge задел чужой {path}")


# ---------------------------------------------------------------------------
# P03-05 — FakeStore-ы не расходятся со StateStore
# ---------------------------------------------------------------------------

_FAKE_STORE_MODULES = (
    ("test_purge_cluster_w1770", "FakeStore"),
    ("test_purge_all_data_w1730", "FakeStore"),
    ("test_purge_privacy_gaps_w1767", "FakeStore"),
    ("test_purge_rescue_forensics_r1", "FakeStore"),
    ("test_purge_temp_uploads_S3", "FakeStore"),
    ("test_purge_speaker_playback_w1765", "FakeStore"),
    ("test_purge_webhook_obsidian_w1766", "FakeStore"),
    ("test_purge_tmp_meeting_W_C2b", "_FakeStore"),
)
_SHARED_METHODS = ("_lock", "load_settings", "_append_ndjson", "_append_ndjson_raw",
                   "_load_active_items_unlocked", "compact_with_stats")


def _accepts_call_of(fake_fn, real_fn) -> list[str]:
    """Имена параметров реального метода, которые fake не примет."""
    fake_sig = inspect.signature(fake_fn)
    if any(p.kind is p.VAR_KEYWORD for p in fake_sig.parameters.values()):
        return []
    missing = []
    for name, param in inspect.signature(real_fn).parameters.items():
        if name == "self" or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if name not in fake_sig.parameters:
            missing.append(name)
    return missing


class FakeStoreParityTestCase(unittest.TestCase):

    def _fakes(self):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        for module_name, cls_name in _FAKE_STORE_MODULES:
            yield module_name, getattr(importlib.import_module(module_name), cls_name)

    def test_fake_signatures_accept_every_real_parameter(self) -> None:
        for module_name, fake_cls in self._fakes():
            for method in _SHARED_METHODS:
                fake_fn = getattr(fake_cls, method, None)
                if fake_fn is None:
                    continue
                real_fn = getattr(StateStore, method)
                with self.subTest(module=module_name, method=method):
                    self.assertEqual(_accepts_call_of(fake_fn, real_fn), [],
                                     f"{module_name}.{fake_cls.__name__}.{method} отстал от StateStore")

    def test_fake_compact_physically_drops_tombstoned_items(self) -> None:
        for module_name, fake_cls in self._fakes():
            if not hasattr(fake_cls, "add_item"):
                continue
            with self.subTest(module=module_name), tempfile.TemporaryDirectory() as td:
                store = fake_cls(data_dir=td)
                store.add_item("keep")
                store.add_item("gone")
                store._append_ndjson_raw(store.tombstones_path, json.dumps({"id": "gone"}))
                stats = store.compact_with_stats()
                self.assertEqual(set(store._items), {"keep"},
                                 "compact отчитался, но запись с томбстоуном осталась")
                self.assertEqual(stats["after_active_count"], 1)

    def test_real_store_purge_leaves_no_transcript_text_on_disk(self) -> None:
        profile = _tmpdir(self)
        store = StateStore(data_dir=profile)
        for i in range(3):
            store.add_history_item(text=f"{PII} запись {i}")
        self.assertTrue(any(PII.encode("utf-8") in data for data in _snapshot(profile).values()))

        result = _run_purge(HistoryService(store=store))

        self.assertTrue(result.get("complete"), result.get("errors"))
        self.assertEqual(result.get("history_deleted"), 3)
        for name, data in _snapshot(profile).items():
            self.assertNotIn(PII.encode("utf-8"), data, f"открытый текст истории остался в {name}")
        self.assertEqual(StateStore(data_dir=profile)._load_active_items_with_lock(), [])


# ---------------------------------------------------------------------------
# P03-06 — hardlink и симлинки за пределы профиля
# ---------------------------------------------------------------------------

class PurgeLinkBoundaryTestCase(unittest.TestCase):
    """Граница purge: удаляются ИМЕНА внутри профиля.

    Объект вне профиля, на который ведёт hardlink или симлинк, purge не
    переписывает и не удаляет: это чужой файл того же пользователя, а не копия
    данных Krab Ear. Статус отражает очистку профиля.
    """

    def setUp(self) -> None:
        self.profile = _tmpdir(self)
        self.outside = _tmpdir(self)
        self.external = self.outside / "external_note.md"
        self.external.write_text(f"{PII} внешний объект", encoding="utf-8")
        self.external_bytes = self.external.read_bytes()

    def _link_or_skip(self, link: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(self.external, link)
        except OSError as exc:  # разные тома
            self.skipTest(f"hardlink недоступен: {exc}")

    def test_hardlinked_files_removed_from_profile_external_inode_untouched(self) -> None:
        links = [
            self.profile / "transcripts" / "transcript_2026-11-27_10-00-00_aa.md",
            self.profile / "audio" / "rec_001.wav",
            self.profile / "rescue" / "spill.f32.part",
        ]
        for link in links:
            self._link_or_skip(link)
        self.assertEqual(os.stat(self.external).st_nlink, 1 + len(links))

        result = _run_purge(HistoryService(store=StateStore(data_dir=self.profile)))

        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("complete"), result.get("errors"))
        for link in links:
            self.assertFalse(os.path.lexists(link), f"{link.relative_to(self.profile)} пережил purge")
        self.assertEqual(self.external.read_bytes(), self.external_bytes,
                         "purge переписал общий inode через hardlink")
        self.assertEqual(os.stat(self.external).st_nlink, 1, "в профиле остались ссылки на inode")
        for name, data in _snapshot(self.profile).items():
            self.assertNotIn(PII.encode("utf-8"), data, f"PII осталась в профиле: {name}")

    def test_symlinked_store_dirs_never_followed(self) -> None:
        """Каталог хранилища подменён симлинком на чужой каталог.

        До фикса purge шёл по симлинку rescue/ и temp_uploads/ через iterdir() и
        удалял файлы ВНЕ профиля. Контракт: цель не трогается никогда;
        transcripts/ — ссылка снимается (G3-MEM-04), audio/, rescue/ и
        temp_uploads/ — ссылка остаётся, шаг честно отчитывается ошибкой.
        """
        refused = ("audio", "rescue", "temp_uploads")
        for name in ("transcripts",) + refused:
            (self.profile / name).symlink_to(self.outside, target_is_directory=True)
        before = _snapshot(self.outside)

        result = _run_purge(HistoryService(store=StateStore(data_dir=self.profile)))

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(_snapshot(self.outside), before, "purge прошёл по симлинку за профиль")
        self.assertFalse(os.path.lexists(self.profile / "transcripts"))
        self.assertFalse(result.get("complete"), "очистка с отказанными шагами выдана за полную")
        for name in refused:
            self.assertIn(name, result.get("errors", []), f"отказ по симлинку {name} не отражён")

    def test_symlinked_file_inside_store_dir_unlinked_target_untouched(self) -> None:
        link = self.profile / "transcripts" / "transcript_2026-11-27_10-00-00_aa.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.external)

        result = _run_purge(HistoryService(store=StateStore(data_dir=self.profile)))

        self.assertTrue(result.get("ok"), result)
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(self.external.read_bytes(), self.external_bytes)


class FlatStoreDirPurgeTestCase(unittest.TestCase):
    """rescue/ и temp_uploads/ — плоские каталоги; purge чистит их через fd каталога.

    Ошибка, которую pathlib прячет (на 3.14 is_dir()/is_symlink() отвечают False
    на PermissionError), обязана стать ошибкой шага, а не «каталога нет».
    """

    STORE_DIRS = ("rescue", "temp_uploads")

    def setUp(self) -> None:
        self.profile = _tmpdir(self)
        self.outside = _tmpdir(self)
        self.external = self.outside / "external.f32.part"
        self.external.write_bytes(b"outside")

    def _purge(self) -> dict:
        return _run_purge(HistoryService(store=StateStore(data_dir=self.profile)))

    def test_regular_files_and_symlinks_removed_targets_kept(self) -> None:
        for name in self.STORE_DIRS:
            store_dir = self.profile / name
            store_dir.mkdir()
            (store_dir / "a.f32.part").write_bytes(PII.encode("utf-8"))
            (store_dir / "b.meta.json").write_text("{}", encoding="utf-8")
            (store_dir / "link.f32.part").symlink_to(self.external)
            (store_dir / "dir_link").symlink_to(self.outside, target_is_directory=True)
            (store_dir / "dangling.f32.part").symlink_to(self.outside / "нет_такого")

        result = self._purge()

        self.assertTrue(result.get("complete"), result.get("errors"))
        self.assertEqual(result.get("rescue_deleted"), 5)
        for name in self.STORE_DIRS:
            store_dir = self.profile / name
            self.assertTrue(store_dir.is_dir(), f"{name}/ должен остаться пустым каталогом")
            self.assertEqual(os.listdir(store_dir), [], f"{name}/ не очищен")
        self.assertEqual(self.external.read_bytes(), b"outside", "purge задел цель симлинка")
        self.assertEqual(os.listdir(self.outside), ["external.f32.part"], "purge зашёл в каталог по симлинку")

    def test_non_directory_store_entry_is_reported(self) -> None:
        for name in self.STORE_DIRS:
            (self.profile / name).write_text("не каталог", encoding="utf-8")

        result = self._purge()

        self.assertFalse(result.get("complete"), "файл на месте каталога выдан за пустой каталог")
        for name in self.STORE_DIRS:
            self.assertIn(name, result.get("errors", []))

    def test_subdirectory_inside_store_dir_is_reported_and_untouched(self) -> None:
        for name in self.STORE_DIRS:
            nested = self.profile / name / "nested"
            nested.mkdir(parents=True)
            (nested / "inner.f32.part").write_bytes(b"inner")
            (self.profile / name / "top.f32.part").write_bytes(b"top")

        result = self._purge()

        self.assertFalse(result.get("complete"))
        for name in self.STORE_DIRS:
            self.assertIn(name, result.get("errors", []))
            self.assertFalse((self.profile / name / "top.f32.part").exists(),
                             f"обычный файл {name}/ пережил purge")
            self.assertEqual((self.profile / name / "nested" / "inner.f32.part").read_bytes(), b"inner")

    def test_missing_store_dirs_are_benign(self) -> None:
        result = self._purge()

        self.assertTrue(result.get("complete"), result.get("errors"))
        self.assertEqual(result.get("rescue_deleted"), 0)

    def test_helper_unreadable_parent_raises_not_absent(self) -> None:
        from backend.purge_fs import purge_flat_dir_contents

        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root игнорирует права каталога")
        parent = self.profile / "locked"
        (parent / "rescue").mkdir(parents=True)
        (parent / "rescue" / "a.f32.part").write_bytes(b"x")
        os.chmod(parent, 0)
        try:
            with self.assertRaises(PermissionError):
                purge_flat_dir_contents(parent / "rescue")
        finally:
            os.chmod(parent, 0o700)
        self.assertTrue((parent / "rescue" / "a.f32.part").exists())

    def test_helper_missing_dir_is_noop(self) -> None:
        from backend.purge_fs import purge_flat_dir_contents

        self.assertEqual(purge_flat_dir_contents(self.profile / "absent"), (0, []))

    def test_helper_symlinked_dir_raises(self) -> None:
        from backend.purge_fs import purge_flat_dir_contents

        link = self.profile / "rescue"
        link.symlink_to(self.outside, target_is_directory=True)

        with self.assertRaises(OSError):
            purge_flat_dir_contents(link)
        self.assertEqual(self.external.read_bytes(), b"outside")

    def test_helper_dir_swapped_after_open_never_reaches_new_target(self) -> None:
        """TOCTOU: каталог подменили симлинком после открытия — удаление идёт по fd старого."""
        import backend.purge_fs as purge_fs

        store_dir = self.profile / "rescue"
        store_dir.mkdir()
        (store_dir / "a.f32.part").write_bytes(b"a")
        moved = self.profile / "rescue.moved"
        real_listdir = os.listdir

        def _swap_then_list(fd):
            store_dir.rename(moved)
            store_dir.symlink_to(self.outside, target_is_directory=True)
            return real_listdir(fd)

        with patch.object(purge_fs.os, "listdir", side_effect=_swap_then_list):
            deleted, problems = purge_fs.purge_flat_dir_contents(store_dir)

        self.assertEqual((deleted, problems), (1, []))
        self.assertEqual(os.listdir(moved), [], "исходный каталог не очищен")
        self.assertEqual(self.external.read_bytes(), b"outside", "удаление ушло по подменённому пути")


# ---------------------------------------------------------------------------
# P03-08 — E2E миграции открытого профиля в зашифрованный
# ---------------------------------------------------------------------------

class PlaintextToEncryptedMigrationE2ETestCase(unittest.TestCase):

    def setUp(self) -> None:
        self.data_dir = _tmpdir(self)
        self.key: bytes | None = None
        self.allow_create = True
        self.commands: list[tuple] = []
        provider = patch("backend.crypto_keystore._run_security", side_effect=self._security)
        provider.start()
        self.addCleanup(provider.stop)

        # Старый открытый профиль: флаг выключен, три записи, одна удалена.
        store = StateStore(self.data_dir)
        store.initialize_startup_plaintext_policy(new_profile=True)
        store.save_settings({"history_encryption_enabled": False})
        self.items = [store.add_history_item(text=f"{PII} запись {i}") for i in range(3)]
        store.delete_history_item(self.items[0].id)
        self.history = store.history_path
        self.assertIn(PII, self.history.read_text(encoding="utf-8"))
        self.assertFalse(any(self.commands), "открытый профиль не должен трогать Keychain")
        self._enable_flag()

    def _security(self, args):
        self.commands.append(tuple(args))
        if args[0] == "find-generic-password":
            if self.key is None:
                return subprocess.CompletedProcess(args, 44, "", "not found")
            return subprocess.CompletedProcess(
                args, 0, base64.b64encode(self.key).decode("ascii") + "\n", "",
            )
        if args[0] == "add-generic-password":
            if not self.allow_create:
                return subprocess.CompletedProcess(args, 1, "", "denied")
            self.key = base64.b64decode(args[args.index("-w") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"неожиданная операция security: {args[0]}")

    def _enable_flag(self) -> None:
        settings = json.loads((self.data_dir / "settings.json").read_text(encoding="utf-8"))
        settings["history_encryption_enabled"] = True
        (self.data_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    def _leftovers(self) -> list[str]:
        return sorted(p.name for p in self.data_dir.iterdir()
                      if p.name.startswith("history.ndjson.") and p.name != "history.ndjson")

    def _assert_no_plaintext(self) -> None:
        for name, data in _snapshot(self.data_dir).items():
            self.assertNotIn(PII.encode("utf-8"), data, f"открытый текст остался в {name}")

    def _assert_reader_restart_sees_history(self) -> None:
        items, _ = StateStore(self.data_dir).get_history_page(None, 100)
        self.assertEqual({it["id"] for it in items}, {self.items[1].id, self.items[2].id})
        self.assertEqual({it["text"] for it in items}, {f"{PII} запись 1", f"{PII} запись 2"})

    def test_full_migration_encrypts_every_line_and_leaves_no_plaintext(self) -> None:
        result = StateStore(self.data_dir).migrate_history_encryption()

        self.assertTrue(result.get("ok"), result)
        lines = [ln for ln in self.history.read_text(encoding="utf-8").splitlines() if ln]
        self.assertTrue(lines)
        self.assertTrue(all(ln.startswith("ENC1:") for ln in lines), "в history.ndjson осталась открытая строка")
        self.assertEqual(self._leftovers(), [], "после миграции остались .bak/.migration_tmp")
        self._assert_no_plaintext()
        self.assertEqual(sum(c[0] == "add-generic-password" for c in self.commands), 1)
        self._assert_reader_restart_sees_history()
        # Повторный запуск идемпотентен: ничего не перешифровывает и не оставляет следов.
        again = StateStore(self.data_dir).migrate_history_encryption()
        self.assertTrue(again.get("ok"), again)
        self.assertEqual(again.get("encrypted"), 0)
        self.assertEqual(self._leftovers(), [])

    def test_fsync_failure_keeps_live_file_and_removes_sidecars(self) -> None:
        before = self.history.read_bytes()
        with patch("backend.state_store.os.fsync", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(OSError):
                StateStore(self.data_dir).migrate_history_encryption()

        self.assertEqual(self.history.read_bytes(), before, "live-файл изменён несмотря на сбой")
        self.assertEqual(self._leftovers(), [], "сбой fsync оставил открытую копию рядом")
        self._assert_reader_restart_sees_history()

    def test_missing_key_that_cannot_be_created_changes_nothing(self) -> None:
        self.allow_create = False
        before = _snapshot(self.data_dir)

        result = StateStore(self.data_dir).migrate_history_encryption()

        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result.get("reason"), "encryption_unavailable")
        self.assertEqual(_snapshot(self.data_dir), before)

    def test_crash_after_replace_then_rerun_leaves_no_plaintext(self) -> None:
        """Процесс убит после replace, до затирания .bak: открытая копия на диске.

        Повторный запуск миграции обязан её убрать: в .bak копируется уже
        зашифрованный live-файл, затем он затирается. Без повторного запуска
        копию убирает только purge (history.ndjson.bak* в списке purge).
        """
        real_open = Path.open

        def crash_on_bak_wipe(path, mode="r", *args, **kwargs):
            if mode == "r+b" and path.name == "history.ndjson.bak":
                raise KeyboardInterrupt("симуляция убитого процесса")
            return real_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", crash_on_bak_wipe):
            with self.assertRaises(KeyboardInterrupt):
                StateStore(self.data_dir).migrate_history_encryption()
        self.assertEqual(self._leftovers(), ["history.ndjson.bak"])
        self.assertIn(PII.encode("utf-8"), (self.data_dir / "history.ndjson.bak").read_bytes())

        result = StateStore(self.data_dir).migrate_history_encryption()

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(self._leftovers(), [])
        self._assert_no_plaintext()
        self._assert_reader_restart_sees_history()


if __name__ == "__main__":
    unittest.main()
