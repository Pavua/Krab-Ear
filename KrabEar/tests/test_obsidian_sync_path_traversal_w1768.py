"""W1768 (MED, path-traversal) — регрессионные тесты ObsidianSyncManager.

Дыра: ``configure()`` делал ``target_dir = vault / folder`` без проверки
контейнмента. pathlib позволяет абсолютному RHS заменить базу, а ``..`` поднимает
вверх по дереву — поэтому ``folder='/private/tmp/evil'`` или
``folder='../../../private/tmp/evil'`` приводили к ``mkdir(parents=True)`` и записи
.md с контентом, контролируемым атакующим, ВНЕ vault. Достижимо через live IPC
handlers ``configure_obsidian_sync`` / ``run_obsidian_sync`` (Unix socket).

Проверяем:
- configure() отклоняет абсолютный folder (UnsafeFolderError / ValueError),
  директория НЕ создаётся;
- configure() отклоняет folder с ``..``, директория НЕ создаётся;
- configure() отклоняет folder со спецсимволами (не из whitelist);
- нормальный folder по-прежнему работает (включая вложенную подпапку);
- sync() повторно проверяет инвариант, когда _folder подделан в state-файле
  и перезагружен при рестарте (запись вне vault не происходит);
- _load_state() откатывает подделанный folder к безопасному значению по умолчанию;
- purge_all_synced_files() — безопасный no-op при подделанном folder.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.obsidian_sync import (  # noqa: E402
    ObsidianSyncManager,
    UnsafeFolderError,
    _DEFAULT_FOLDER,
)


def _make_item(text: str = "Секретный текст.") -> dict:
    return {
        "id": "abc12345-0000-0000-0000-000000000000",
        "ts": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "translated_text": "",
        "translation_mode": "off",
        "source_lang": "ru",
        "target_lang": "",
        "tags": [],
        "diarization": None,
        "confidence": None,
    }


class TestObsidianSyncPathTraversal(unittest.TestCase):
    """W1768: configure()/sync() не должны писать вне vault."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True)
        self.vault_dir = self.root / "vault"
        self.vault_dir.mkdir()
        # Директория-«жертва» ВНЕ vault, в которую атакующий пытается записать.
        self.outside_dir = self.root / "outside"
        self.mgr = ObsidianSyncManager(data_dir=self.data_dir)

    # ------------------------------------------------------------------
    # configure() — абсолютный folder
    # ------------------------------------------------------------------

    def test_configure_rejects_absolute_folder(self) -> None:
        """Абсолютный folder отклоняется, директория-жертва НЕ создаётся."""
        evil = str(self.outside_dir / "evil")
        with self.assertRaises(ValueError):
            self.mgr.configure(str(self.vault_dir), folder=evil)
        # Ни сама жертва, ни её родитель не должны быть созданы.
        self.assertFalse((self.outside_dir / "evil").exists())
        # Менеджер остаётся ненастроенным (vault не закоммичен).
        self.assertFalse(self.mgr.get_sync_status()["configured"])

    def test_configure_absolute_folder_raises_unsafe_subclass(self) -> None:
        """Тип ошибки — UnsafeFolderError (подкласс ValueError)."""
        with self.assertRaises(UnsafeFolderError):
            self.mgr.configure(str(self.vault_dir), folder="/private/tmp/evil")

    # ------------------------------------------------------------------
    # configure() — обход через ".."
    # ------------------------------------------------------------------

    def test_configure_rejects_parent_traversal_folder(self) -> None:
        """folder с '..' отклоняется, запись вне vault не происходит."""
        evil = "../../../" + self.outside_dir.name + "/evil"
        with self.assertRaises(ValueError):
            self.mgr.configure(str(self.vault_dir), folder=evil)
        self.assertFalse((self.outside_dir / "evil").exists())
        self.assertFalse(self.mgr.get_sync_status()["configured"])

    def test_configure_rejects_dotdot_component(self) -> None:
        """Любой компонент '..' отклоняется даже без выхода за пределы."""
        with self.assertRaises(UnsafeFolderError):
            self.mgr.configure(str(self.vault_dir), folder="sub/../../escape")

    # ------------------------------------------------------------------
    # configure() — спецсимволы вне whitelist
    # ------------------------------------------------------------------

    def test_configure_rejects_special_chars(self) -> None:
        """folder со спецсимволами (точка/двоеточие) отклоняется."""
        for bad in ("..hidden", "a:b", "x\nnewline", "tab\tname"):
            with self.subTest(folder=bad):
                with self.assertRaises(ValueError):
                    self.mgr.configure(str(self.vault_dir), folder=bad)

    # ------------------------------------------------------------------
    # configure() — нормальный путь по-прежнему работает
    # ------------------------------------------------------------------

    def test_configure_accepts_normal_folder(self) -> None:
        """Обычный folder создаётся внутри vault и регистрируется."""
        result = self.mgr.configure(str(self.vault_dir), folder="Transcriptions")
        self.assertEqual(result["folder"], "Transcriptions")
        target = self.vault_dir / "Transcriptions"
        self.assertTrue(target.is_dir())
        # folder_full_path остаётся внутри vault.
        self.assertTrue(
            Path(result["folder_full_path"]).resolve().is_relative_to(
                self.vault_dir.resolve()
            )
        )

    def test_configure_accepts_nested_subfolder(self) -> None:
        """Вложенная подпапка (со слэшем/пробелом) допустима и создаётся."""
        result = self.mgr.configure(
            str(self.vault_dir), folder="Krab Ear/Транскрипции"
        )
        self.assertEqual(result["folder"], "Krab Ear/Транскрипции")
        self.assertTrue((self.vault_dir / "Krab Ear" / "Транскрипции").is_dir())

    def test_configure_normal_folder_then_sync_writes_inside_vault(self) -> None:
        """После валидного configure() sync() пишет .md строго внутри vault."""
        self.mgr.configure(str(self.vault_dir), folder="Notes")
        result = self.mgr.sync([_make_item()])
        self.assertEqual(result.synced_count, 1)
        self.assertEqual(result.errors, [])
        for f in result.new_files:
            self.assertTrue(
                Path(f).resolve().is_relative_to(self.vault_dir.resolve())
            )

    # ------------------------------------------------------------------
    # sync() — повторная проверка инварианта при подделанном state
    # ------------------------------------------------------------------

    def test_sync_rejects_tampered_folder_from_state(self) -> None:
        """Если _folder подделан после рестарта — sync() не пишет вне vault.

        Моделируем перезагрузку: настраиваем валидно, затем напрямую подменяем
        внутренний _folder на traversal-значение (как если бы state-файл был
        изменён внешним процессом и перезагружен) и убеждаемся, что sync()
        отклоняет операцию вместо записи в outside_dir.
        """
        self.mgr.configure(str(self.vault_dir), folder="Transcriptions")
        # Подделка in-memory состояния (минуя _load_state-санацию).
        with self.mgr._lock:
            self.mgr._folder = "../../" + self.outside_dir.name + "/evil"

        with self.assertRaises(ValueError):
            self.mgr.sync([_make_item()])
        self.assertFalse((self.outside_dir / "evil").exists())

    # ------------------------------------------------------------------
    # _load_state() — санация подделанного folder
    # ------------------------------------------------------------------

    def test_load_state_sanitizes_tampered_folder(self) -> None:
        """Подделанный folder в obsidian_sync.json откатывается к дефолту."""
        state = {
            "vault_path": str(self.vault_dir),
            "folder": "../../../" + self.outside_dir.name,
            "last_sync_ts": None,
        }
        state_path = self.data_dir / "obsidian_sync.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        mgr2 = ObsidianSyncManager(data_dir=self.data_dir)
        # vault загружен, но небезопасный folder заменён на _DEFAULT_FOLDER.
        self.assertEqual(mgr2.get_sync_status()["folder"], _DEFAULT_FOLDER)

        # И последующий sync() пишет внутрь vault, а не в outside_dir.
        result = mgr2.sync([_make_item()])
        self.assertEqual(result.errors, [])
        self.assertFalse((self.outside_dir).exists())

    def test_load_state_keeps_valid_folder(self) -> None:
        """Валидный folder из state-файла сохраняется без изменений."""
        self.mgr.configure(str(self.vault_dir), folder="MyNotes")
        mgr2 = ObsidianSyncManager(data_dir=self.data_dir)
        self.assertEqual(mgr2.get_sync_status()["folder"], "MyNotes")

    # ------------------------------------------------------------------
    # purge_all_synced_files() — безопасный no-op при подделке
    # ------------------------------------------------------------------

    def test_purge_safe_noop_on_tampered_folder(self) -> None:
        """purge() при подделанном _folder — no-op, ничего не удаляет вне vault."""
        self.mgr.configure(str(self.vault_dir), folder="Transcriptions")
        # Кладём посторонний .md в outside_dir, который НЕ должен быть удалён.
        self.outside_dir.mkdir(parents=True, exist_ok=True)
        victim = self.outside_dir / "important.md"
        victim.write_text("keep me", encoding="utf-8")

        with self.mgr._lock:
            self.mgr._folder = "../../" + self.outside_dir.name

        deleted = self.mgr.purge_all_synced_files()
        self.assertEqual(deleted, 0)
        self.assertTrue(victim.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
