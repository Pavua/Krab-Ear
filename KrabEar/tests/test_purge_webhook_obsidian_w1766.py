"""W1766: privacy-purge gaps — webhooks.json секреты, .md.tmp, Obsidian vault.

Покрывает:
  #7  WebhookManager.purge_all() — удаляет webhooks.json и очищает in-memory
      реестр/статистику; секреты не остаются на диске.
  #9  handle_purge_all_data удаляет *.md.tmp из transcripts/ вместе с *.md.
  #10 ObsidianSyncManager.purge_all_synced_files() — удаляет .md из vault,
      сбрасывает last_sync_ts; no-op если vault не настроен.
  E2E handle_purge_all_data: все три gap-а закрыты одним вызовом purge.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.webhook_manager import WebhookManager         # noqa: E402
from backend.obsidian_sync import ObsidianSyncManager      # noqa: E402
from backend.history_service import HistoryService         # noqa: E402


# ---------------------------------------------------------------------------
# Минимальные fakes — повторяют паттерн test_purge_all_data_w1730.py
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str) -> None:
        self.id = item_id
        self.ts = "2024-01-01T00:00:00+00:00"

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "text": "секрет"}


class FakeStore:
    """Минимальный StateStore fake для purge тестов."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}
        self._tombstones: list[dict] = []
        self._lock_obj = threading.Lock()

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
        return {}

    def save_settings(self, settings: dict) -> dict:
        return settings


# ---------------------------------------------------------------------------
# #7 — WebhookManager.purge_all()
# ---------------------------------------------------------------------------

class WebhookManagerPurgeAllTestCase(unittest.TestCase):
    """W1766 #7: purge_all() уничтожает webhooks.json и in-memory секреты."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = WebhookManager(data_dir=self._tmpdir)

    def _register(self, secret: str = "s3cR3t-is-long-enough") -> str:
        """Регистрирует тестовый webhook с секретом через прямой Python-вызов."""
        return self._mgr.register_webhook(
            url="https://example.com/hook",
            events=["transcription.completed"],
            secret=secret,
            allow_local=False,
        )

    def test_purge_all_removes_webhooks_json(self) -> None:
        """purge_all() должен удалить webhooks.json с диска."""
        self._register()
        wfile = Path(self._tmpdir) / "webhooks.json"
        self.assertTrue(wfile.exists(), "webhooks.json должен существовать до purge_all")

        self._mgr.purge_all()

        self.assertFalse(wfile.exists(), "webhooks.json должен быть удалён после purge_all")

    def test_purge_all_secret_not_on_disk(self) -> None:
        """После purge_all() секрет НЕ должен присутствовать на диске ни в каком файле."""
        secret = "my-super-secret-hmac-key"
        self._register(secret=secret)

        self._mgr.purge_all()

        # Проверяем что ни одного файла с секретом нет
        for f in Path(self._tmpdir).iterdir():
            content = f.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn(
                secret,
                content,
                f"Секрет найден в файле {f.name} после purge_all",
            )

    def test_purge_all_clears_in_memory_registry(self) -> None:
        """purge_all() должен очистить in-memory реестр webhook-ов."""
        self._register()
        self.assertGreater(len(self._mgr._webhooks), 0)

        self._mgr.purge_all()

        self.assertEqual(self._mgr._webhooks, {},
                         "In-memory реестр webhook-ов должен быть пуст после purge_all")

    def test_purge_all_clears_in_memory_stats(self) -> None:
        """purge_all() должен очистить in-memory статистику."""
        wid = self._register()
        # Добавляем данные в статистику напрямую
        self._mgr._stats[wid] = {"deliveries": 5, "failures": 1}

        self._mgr.purge_all()

        self.assertEqual(self._mgr._stats, {},
                         "In-memory статистика должна быть пуста после purge_all")

    def test_purge_all_idempotent_no_file(self) -> None:
        """Повторный purge_all() без файла не бросает исключений."""
        try:
            self._mgr.purge_all()
            self._mgr.purge_all()
        except Exception as exc:
            self.fail(f"purge_all() бросил исключение: {exc}")

    def test_purge_all_list_webhooks_empty_after(self) -> None:
        """После purge_all() list_webhooks возвращает пустой список."""
        self._register()
        self.assertGreater(len(self._mgr.list_webhooks()), 0)

        self._mgr.purge_all()

        self.assertEqual(self._mgr.list_webhooks(), [],
                         "list_webhooks() должен вернуть [] после purge_all")

    def test_purge_all_reloaded_manager_sees_empty(self) -> None:
        """После purge_all() новый WebhookManager из того же tmpdir не видит вебхуков."""
        self._register()

        self._mgr.purge_all()

        mgr2 = WebhookManager(data_dir=self._tmpdir)
        self.assertEqual(mgr2.list_webhooks(), [],
                         "Перезагруженный WebhookManager не должен видеть вебхуки")


# ---------------------------------------------------------------------------
# #9 — .md.tmp glob в purge_all_data (транскрипционный in-flight файл)
# ---------------------------------------------------------------------------

class PurgeTranscriptTmpTestCase(unittest.TestCase):
    """W1766 #9: handle_purge_all_data удаляет *.md.tmp вместе с *.md."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _make_svc(self) -> HistoryService:
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-1")
        return HistoryService(store=store)

    def test_md_tmp_removed_on_purge(self) -> None:
        """*.md.tmp в transcripts/ должен быть удалён при purge_all_data."""
        transcripts_dir = Path(self._tmpdir) / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)

        # Создаём in-flight временный файл (как TranscriptWriter при записи)
        tmp_file = transcripts_dir / "transcript_2026-06-02_12-00-00_abc12345.md.tmp"
        tmp_file.write_text("# Секретный транскрипт\n\nСодержимое", encoding="utf-8")
        self.assertTrue(tmp_file.exists(), ".md.tmp должен существовать до purge")

        svc = self._make_svc()
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"))
        self.assertFalse(tmp_file.exists(),
                         "*.md.tmp должен быть удалён при purge_all_data")

    def test_md_and_md_tmp_both_removed(self) -> None:
        """И *.md, и *.md.tmp должны быть удалены при purge_all_data."""
        transcripts_dir = Path(self._tmpdir) / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)

        md_file = transcripts_dir / "transcript_2026-06-02_10-00-00_aabbccdd.md"
        tmp_file = transcripts_dir / "transcript_2026-06-02_10-00-01_eeff0011.md.tmp"
        md_file.write_text("# Запись 1", encoding="utf-8")
        tmp_file.write_text("# Запись 2 (незаконченная)", encoding="utf-8")

        svc = self._make_svc()
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"))
        self.assertFalse(md_file.exists(), "*.md должен быть удалён")
        self.assertFalse(tmp_file.exists(), "*.md.tmp должен быть удалён")
        # transcripts_deleted считает оба файла
        self.assertEqual(result.get("transcripts_deleted", 0), 2,
                         "transcripts_deleted должен считать и .md, и .md.tmp")


# ---------------------------------------------------------------------------
# #10 — ObsidianSyncManager.purge_all_synced_files()
# ---------------------------------------------------------------------------

class ObsidianPurgeAllSyncedFilesTestCase(unittest.TestCase):
    """W1766 #10: purge_all_synced_files() удаляет .md из vault и сбрасывает timestamp."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _make_mgr(self) -> ObsidianSyncManager:
        return ObsidianSyncManager(data_dir=self._tmpdir)

    def _configure_and_create_files(self, mgr: ObsidianSyncManager) -> Path:
        """Настраивает vault, создаёт тестовые .md файлы, возвращает target_dir."""
        vault_dir = Path(self._tmpdir) / "vault"
        vault_dir.mkdir(parents=True, exist_ok=True)

        mgr.configure(str(vault_dir), folder="Transcriptions")

        target_dir = vault_dir / "Transcriptions"
        (target_dir / "transcript_2026-06-01_12-00-00_aa.md").write_text(
            "# Транскрипция 1", encoding="utf-8"
        )
        (target_dir / "transcript_2026-06-01_13-00-00_bb.md").write_text(
            "# Транскрипция 2", encoding="utf-8"
        )
        return target_dir

    def test_purge_removes_synced_md_files(self) -> None:
        """purge_all_synced_files() должен удалить все .md из vault/folder."""
        mgr = self._make_mgr()
        target_dir = self._configure_and_create_files(mgr)

        md_files = list(target_dir.glob("*.md"))
        self.assertGreater(len(md_files), 0, ".md файлы должны существовать до purge")

        deleted = mgr.purge_all_synced_files()

        self.assertEqual(deleted, len(md_files),
                         "purge_all_synced_files должен вернуть количество удалённых файлов")
        remaining = list(target_dir.glob("*.md"))
        self.assertEqual(remaining, [], ".md файлы должны быть удалены")

    def test_purge_resets_last_sync_ts(self) -> None:
        """purge_all_synced_files() должен сбросить last_sync_ts."""
        mgr = self._make_mgr()
        self._configure_and_create_files(mgr)
        # Устанавливаем timestamp вручную
        mgr._last_sync_ts = "2026-06-01T12:00:00+00:00"

        mgr.purge_all_synced_files()

        self.assertIsNone(mgr._last_sync_ts,
                          "last_sync_ts должен быть сброшен до None после purge")

    def test_purge_saves_state_with_null_ts(self) -> None:
        """После purge state-файл должен содержать last_sync_ts: null."""
        mgr = self._make_mgr()
        self._configure_and_create_files(mgr)
        mgr._last_sync_ts = "2026-06-01T12:00:00+00:00"

        mgr.purge_all_synced_files()

        state_path = Path(self._tmpdir) / "obsidian_sync.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIsNone(state.get("last_sync_ts"),
                              "last_sync_ts в obsidian_sync.json должен быть null после purge")

    def test_purge_noop_when_vault_not_configured(self) -> None:
        """purge_all_synced_files() — no-op (возвращает 0) если vault не настроен."""
        mgr = self._make_mgr()  # vault не настроен

        try:
            deleted = mgr.purge_all_synced_files()
        except Exception as exc:
            self.fail(f"purge_all_synced_files() бросил исключение: {exc}")

        self.assertEqual(deleted, 0, "Должен вернуть 0 при vault=None")

    def test_purge_noop_vault_path_missing(self) -> None:
        """purge_all_synced_files() — no-op если директория vault не существует."""
        mgr = self._make_mgr()
        # Устанавливаем несуществующий vault напрямую
        mgr._vault_path = Path(self._tmpdir) / "nonexistent_vault"

        deleted = mgr.purge_all_synced_files()
        self.assertEqual(deleted, 0, "Должен вернуть 0 если vault dir не существует")

    def test_purge_idempotent(self) -> None:
        """Повторный purge_all_synced_files() не бросает исключений."""
        mgr = self._make_mgr()
        self._configure_and_create_files(mgr)

        try:
            mgr.purge_all_synced_files()
            mgr.purge_all_synced_files()
        except Exception as exc:
            self.fail(f"Повторный purge_all_synced_files() бросил исключение: {exc}")

    def test_purge_vault_config_preserved(self) -> None:
        """После purge vault_path и folder остаются в конфигурации."""
        mgr = self._make_mgr()
        vault_dir = Path(self._tmpdir) / "vault"
        vault_dir.mkdir(parents=True, exist_ok=True)
        # configure() вызывает Path.resolve() внутри, поэтому сравниваем resolved-путь
        vault_dir_resolved = vault_dir.resolve()
        mgr.configure(str(vault_dir), folder="MyNotes")

        mgr.purge_all_synced_files()

        status = mgr.get_sync_status()
        self.assertEqual(status["vault_path"], str(vault_dir_resolved),
                         "vault_path должен сохраниться после purge")
        self.assertEqual(status["folder"], "MyNotes",
                         "folder должен сохраниться после purge")


# ---------------------------------------------------------------------------
# E2E — handle_purge_all_data закрывает все три gap-а одним вызовом
# ---------------------------------------------------------------------------

class PurgeAllDataE2EW1766TestCase(unittest.TestCase):
    """W1766 E2E: purge_all_data удаляет webhook-секреты, .md.tmp и vault .md."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_e2e_all_three_gaps_closed(self) -> None:
        """E2E: webhook с секретом + .md.tmp + Obsidian vault файл → purge → всё стёрто."""
        # --- Подготовка ---
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-1")
        svc = HistoryService(store=store)

        # #7: webhook с HMAC-секретом
        wh_mgr = WebhookManager(data_dir=self._tmpdir)
        secret = "top-secret-key-abc123"
        wh_mgr.register_webhook(
            url="https://example.com/hook",
            events=[],
            secret=secret,
            allow_local=False,
        )
        webhooks_path = Path(self._tmpdir) / "webhooks.json"
        self.assertTrue(webhooks_path.exists(), "webhooks.json должен существовать")
        self.assertIn(secret, webhooks_path.read_text(encoding="utf-8"),
                      "Секрет должен быть в webhooks.json до purge")

        # #9: in-flight .md.tmp файл
        transcripts_dir = Path(self._tmpdir) / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = transcripts_dir / "transcript_2026-06-02_00-00-00_deadbeef.md.tmp"
        tmp_file.write_text("# Секрет в tmp", encoding="utf-8")

        # #10: Obsidian vault с .md
        vault_dir = Path(self._tmpdir) / "vault"
        vault_dir.mkdir(parents=True, exist_ok=True)
        obs_mgr = ObsidianSyncManager(data_dir=self._tmpdir)
        obs_mgr.configure(str(vault_dir), folder="Transcriptions")
        target_dir = vault_dir / "Transcriptions"
        vault_md = target_dir / "transcript_2026-06-01_10-00-00_cafe0000.md"
        vault_md.write_text("# Vault транскрипция", encoding="utf-8")
        obs_mgr._last_sync_ts = "2026-06-01T10:00:00+00:00"
        obs_mgr._save_state()

        # Подключаем collaborators к HistoryService (имитируем late-inject service.py)
        svc._webhook_manager = wh_mgr
        svc._obsidian_sync = obs_mgr

        # --- Purge ---
        result = svc.handle_purge_all_data({"confirm": True})

        # --- Проверки ---
        self.assertTrue(result.get("ok"), f"purge должен вернуть ok=True: {result}")
        self.assertTrue(result.get("complete"),
                        f"purge должен быть полным: {result.get('errors')}")

        # #7: webhooks.json удалён, секрет не на диске
        self.assertFalse(webhooks_path.exists(),
                         "webhooks.json должен быть удалён")
        self.assertEqual(wh_mgr._webhooks, {},
                         "In-memory реестр webhook-ов должен быть пуст")
        # Секрет не должен присутствовать ни в одном файле tmpdir
        for f in Path(self._tmpdir).rglob("*"):
            if f.is_file():
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    self.assertNotIn(
                        secret, content,
                        f"Секрет найден в {f} после purge",
                    )
                except OSError:
                    pass  # файл удалён параллельно — нормально

        # #9: .md.tmp удалён
        self.assertFalse(tmp_file.exists(), "*.md.tmp должен быть удалён")

        # #10: Obsidian vault .md удалён
        self.assertFalse(vault_md.exists(), "Vault .md должен быть удалён")
        self.assertIsNone(obs_mgr._last_sync_ts,
                          "last_sync_ts должен быть сброшен")
        # Результат содержит obsidian_deleted
        self.assertGreaterEqual(result.get("obsidian_deleted", 0), 1,
                                "obsidian_deleted должен быть >= 1")

    def test_webhook_error_does_not_abort_purge(self) -> None:
        """Ошибка webhook_manager.purge_all() не прерывает удаление истории."""

        class ErrorWebhookManager:
            def purge_all(self) -> None:
                raise RuntimeError("сетевая ошибка")

        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-x")
        svc = HistoryService(store=store)
        svc._webhook_manager = ErrorWebhookManager()

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(result["history_deleted"], 1,
                         "История должна быть удалена даже при ошибке webhook_manager")
        self.assertFalse(result["complete"])
        self.assertIn("webhooks", result["errors"])

    def test_obsidian_error_does_not_abort_purge(self) -> None:
        """Ошибка obsidian_sync.purge_all_synced_files() не прерывает удаление истории."""

        class ErrorObsidianSync:
            def purge_all_synced_files(self) -> int:
                raise OSError("vault недоступен")

        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-y")
        svc = HistoryService(store=store)
        svc._obsidian_sync = ErrorObsidianSync()

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(result["history_deleted"], 1)
        self.assertFalse(result["complete"])
        self.assertIn("obsidian", result["errors"])

    def test_no_webhook_manager_no_crash(self) -> None:
        """purge_all_data без _webhook_manager не бросает исключений."""
        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._webhook_manager = None

        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"))
        self.assertNotIn("webhooks", result.get("errors", []))

    def test_no_obsidian_sync_no_crash(self) -> None:
        """purge_all_data без _obsidian_sync не бросает исключений."""
        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._obsidian_sync = None

        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"))
        self.assertNotIn("obsidian", result.get("errors", []))


# ---------------------------------------------------------------------------
# BackendService wiring test
# ---------------------------------------------------------------------------

class BackendServiceW1766WiringTestCase(unittest.TestCase):
    """W1766: BackendService wires _webhook_manager + _obsidian_sync в HistoryService."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_backend_wires_webhook_manager_into_history(self) -> None:
        """BackendService.__init__ должен wire _webhook_manager в _history._webhook_manager."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._webhook_manager,
            svc._webhook_manager,
            "BackendService должен wire _webhook_manager в _history._webhook_manager",
        )
        self.assertIsNotNone(svc._history._webhook_manager)

    def test_backend_wires_obsidian_sync_into_history(self) -> None:
        """BackendService.__init__ должен wire _obsidian_sync в _history._obsidian_sync."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._obsidian_sync,
            svc._obsidian_sync,
            "BackendService должен wire _obsidian_sync в _history._obsidian_sync",
        )
        self.assertIsNotNone(svc._history._obsidian_sync)


if __name__ == "__main__":
    unittest.main()
