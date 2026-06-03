"""wave-33 security regression tests for SharingManager.

Покрывает 4 фикса в backend/sharing_manager.py + 1 строку purge в history_service.py:

A1 (HIGH) — in-memory _index переживал privacy-purge: rmtree(shares/) удалял файлы,
            но RAM-копия с полным текстом транскрипций продолжала отдавать данные
            через get_shared/list_shared. clear() сбрасывает _index.
A2 (HIGH) — arbitrary file deletion: подделанный filename='../../x' в shares_index.json
            заставлял revoke_share/purge_all удалить произвольный файл вне shares/.
            Containment-guard через Path.is_relative_to отклоняет такие пути.
A3 (MED)  — handle_get_shared/handle_list_shared не проверяли privacy_mode и отдавали
            транскрипцию даже в privacy mode. Добавлен privacy gate → ok:False.
A4 (MED)  — _render_json экспортировал ПОЛНЫЙ to_dict() (audio_path, chat_id, message_id,
            privacy_mode, reasoning). Перешли на строгий allowlist безопасных полей.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sharing_manager import SharingManager  # noqa: E402


# ---------------------------------------------------------------------------
# Фейки
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    """HistoryItem-заглушка с потенциально чувствительными полями (A4)."""

    def __init__(
        self,
        item_id: str,
        text: str,
        *,
        ts: str = "2024-01-01T10:00:00+00:00",
        translated_text: str = "",
        source_lang: str = "ru",
        target_lang: str = "es",
        audio_path: str = "/Users/secret/audio/call.wav",
        chat_id: str = "telegram_chat_999",
        message_id: str = "msg_555",
        privacy_mode: bool = True,
        reasoning: str = "internal chain-of-thought leak",
        audio_duration_sec: float = 12.5,
        confidence: float = 0.93,
    ) -> None:
        self.id = item_id
        self.text = text
        self.ts = ts
        self.translated_text = translated_text
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.audio_path = audio_path
        self.chat_id = chat_id
        self.message_id = message_id
        self.privacy_mode = privacy_mode
        self.reasoning = reasoning
        self.audio_duration_sec = audio_duration_sec
        self.confidence = confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "ts": self.ts,
            "translated_text": self.translated_text,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "audio_path": self.audio_path,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "privacy_mode": self.privacy_mode,
            "reasoning": self.reasoning,
            "audio_duration_sec": self.audio_duration_sec,
            "confidence": self.confidence,
        }


class FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}

    def add(self, item_id: str, text: str, **kwargs: Any) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text, **kwargs)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        return self._items.get(item_id)


def _make_mgr(tmpdir: str, *, privacy: bool = False) -> SharingManager:
    return SharingManager(
        store=FakeStore(tmpdir),
        privacy_mode_fn=(lambda: privacy),
    )


# ---------------------------------------------------------------------------
# A1 — in-memory index cleared on purge
# ---------------------------------------------------------------------------

class InMemoryPurgeTestCase(unittest.TestCase):
    """A1: clear() сбрасывает _index, get_shared больше не отдаёт данные."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(self._tmpdir)
        self._mgr = SharingManager(store=self._store)
        self._store._items["i1"] = FakeHistoryItem("i1", "секретная транскрипция")

    def test_clear_empties_in_memory_index(self) -> None:
        pkg = self._mgr.prepare_share(["i1"], format="json")
        # До clear: пакет доступен через get_shared
        self.assertIsNotNone(self._mgr.get_shared(pkg.share_id))
        self.assertEqual(len(self._mgr.list_shared()), 1)

        self._mgr.clear()

        # После clear: in-memory индекс пуст, данные не отдаются
        self.assertIsNone(self._mgr.get_shared(pkg.share_id))
        self.assertEqual(self._mgr.list_shared(), [])
        self.assertEqual(self._mgr._index, {})

    def test_clear_is_idempotent_on_empty(self) -> None:
        # clear() на пустом менеджере не должен бросать
        self._mgr.clear()
        self._mgr.clear()
        self.assertEqual(self._mgr._index, {})

    def test_purge_simulation_file_gone_and_index_cleared(self) -> None:
        """Симуляция purge-шага 13: rmtree(shares/) + clear()."""
        import shutil

        pkg = self._mgr.prepare_share(["i1"], format="json")
        shares_dir = Path(self._tmpdir) / "shares"
        file_path = shares_dir / pkg.filename
        self.assertTrue(file_path.exists())

        # purge step 13 в history_service.py: rmtree затем clear()
        shutil.rmtree(shares_dir, ignore_errors=True)
        self._mgr.clear()

        self.assertFalse(file_path.exists())
        self.assertIsNone(self._mgr.get_shared(pkg.share_id))


# ---------------------------------------------------------------------------
# A2 — path traversal in revoke_share / purge_all
# ---------------------------------------------------------------------------

class PathTraversalTestCase(unittest.TestCase):
    """A2: подделанный filename='../../x' не должен удалить файл вне shares/."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_mgr(self._tmpdir)
        self._shares_dir = Path(self._tmpdir) / "shares"

    def _plant_victim(self) -> Path:
        """Создаёт файл-жертву ВНЕ shares/ (в data_dir)."""
        victim = Path(self._tmpdir) / "important_file"
        victim.write_text("DO NOT DELETE", encoding="utf-8")
        return victim

    def test_revoke_share_rejects_traversal_filename(self) -> None:
        victim = self._plant_victim()
        token = "EVILTOK1"
        # Внедряем вредоносную запись в индекс (как при подделанном shares_index.json)
        self._mgr._index[token] = {
            "share_id": token,
            "filename": "../important_file",
            "is_revoked": False,
        }
        # revoke_share не должен удалить victim
        result = self._mgr.revoke_share(token)
        self.assertTrue(result)  # запись существовала → True
        self.assertTrue(victim.exists(), "Файл-жертва вне shares/ НЕ должен быть удалён")

    def test_revoke_share_rejects_absolute_path_filename(self) -> None:
        victim = self._plant_victim()
        token = "EVILTOK2"
        self._mgr._index[token] = {
            "share_id": token,
            "filename": str(victim),  # абсолютный путь вне shares/
            "is_revoked": False,
        }
        self._mgr.revoke_share(token)
        self.assertTrue(victim.exists(), "Абсолютный путь вне shares/ НЕ должен быть удалён")

    def test_purge_all_rejects_traversal_filename(self) -> None:
        victim = self._plant_victim()
        self._mgr._index["EVIL"] = {
            "share_id": "EVIL",
            "filename": "../../important_file",
            "is_revoked": False,
        }
        # Двойной traversal: data_dir/shares/../../important_file
        deep_victim = Path(self._tmpdir).parent / "important_file"
        # (deep_victim может не существовать — ключевая проверка ниже про victim)
        stats = self._mgr.purge_all()
        self.assertTrue(victim.exists(), "purge_all НЕ должен удалять файлы вне shares/")
        # traversal-запись засчитана как ошибка, файл не тронут
        self.assertGreaterEqual(stats["errors"], 1)
        _ = deep_victim  # noqa: F841

    def test_purge_all_still_deletes_legit_share_file(self) -> None:
        """Containment-guard не ломает удаление нормальных пакетов."""
        store = self._mgr._store
        store._items["ok1"] = FakeHistoryItem("ok1", "обычный текст")
        pkg = self._mgr.prepare_share(["ok1"], format="text")
        legit_file = self._shares_dir / pkg.filename
        self.assertTrue(legit_file.exists())

        stats = self._mgr.purge_all()
        self.assertFalse(legit_file.exists())
        self.assertGreaterEqual(stats["deleted"], 1)

    def test_revoke_share_still_deletes_legit_file(self) -> None:
        store = self._mgr._store
        store._items["ok2"] = FakeHistoryItem("ok2", "ещё текст")
        pkg = self._mgr.prepare_share(["ok2"], format="text")
        legit_file = self._shares_dir / pkg.filename
        self.assertTrue(legit_file.exists())

        self.assertTrue(self._mgr.revoke_share(pkg.share_id))
        self.assertFalse(legit_file.exists())


# ---------------------------------------------------------------------------
# A3 — privacy gate on read/list handlers
# ---------------------------------------------------------------------------

class PrivacyGateTestCase(unittest.TestCase):
    """A3: handle_get_shared/handle_list_shared/handle_prepare_share → ok:False в privacy mode."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_get_shared_blocked_in_privacy_mode(self) -> None:
        # Готовим пакет БЕЗ privacy (чтобы он реально лежал в индексе)
        mgr = _make_mgr(self._tmpdir, privacy=False)
        mgr._store._items["s1"] = FakeHistoryItem("s1", "приватный текст")
        pkg = mgr.handle_prepare_share({"item_ids": ["s1"], "format": "json"})
        share_id = pkg["share_id"]

        # Теперь включаем privacy mode на том же индексе
        mgr._privacy_mode_fn = lambda: True
        res = mgr.handle_get_shared({"share_id": share_id})
        self.assertFalse(res.get("ok", True))
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        # Транскрипция НЕ должна утечь в ответе
        self.assertNotIn("content", res)
        self.assertNotIn("filename", res)

    def test_list_shared_blocked_in_privacy_mode(self) -> None:
        mgr = _make_mgr(self._tmpdir, privacy=False)
        mgr._store._items["s2"] = FakeHistoryItem("s2", "ещё приватный текст")
        mgr.handle_prepare_share({"item_ids": ["s2"], "format": "json"})

        mgr._privacy_mode_fn = lambda: True
        res = mgr.handle_list_shared({})
        self.assertFalse(res.get("ok", True))
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        self.assertNotIn("shares", res)

    def test_prepare_share_blocked_in_privacy_mode(self) -> None:
        mgr = _make_mgr(self._tmpdir, privacy=True)
        mgr._store._items["s3"] = FakeHistoryItem("s3", "не должно записаться")
        res = mgr.handle_prepare_share({"item_ids": ["s3"], "format": "json"})
        self.assertFalse(res.get("ok", True))
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        # Файл не должен быть записан на диск
        shares_dir = Path(self._tmpdir) / "shares"
        if shares_dir.exists():
            files = [p for p in shares_dir.iterdir() if p.name.startswith("krabear_share_")]
            self.assertEqual(files, [])

    def test_handlers_work_when_privacy_off(self) -> None:
        """Без privacy mode все обработчики работают нормально."""
        mgr = _make_mgr(self._tmpdir, privacy=False)
        mgr._store._items["s4"] = FakeHistoryItem("s4", "обычный текст")
        prep = mgr.handle_prepare_share({"item_ids": ["s4"], "format": "json"})
        self.assertIn("share_id", prep)
        listed = mgr.handle_list_shared({})
        self.assertIn("shares", listed)
        got = mgr.handle_get_shared({"share_id": prep["share_id"]})
        self.assertEqual(got["share_id"], prep["share_id"])


# ---------------------------------------------------------------------------
# A4 — safe JSON subset
# ---------------------------------------------------------------------------

class SafeJsonExportTestCase(unittest.TestCase):
    """A4: JSON-экспорт не должен содержать audio_path/chat_id/message_id/privacy_mode/reasoning."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_mgr(self._tmpdir, privacy=False)
        self._mgr._store._items["x1"] = FakeHistoryItem(
            "x1",
            "текст транскрипции",
            translated_text="texto traducido",
        )

    def test_sensitive_fields_excluded(self) -> None:
        pkg = self._mgr.prepare_share(["x1"], format="json")
        data = json.loads(pkg.content)
        row = data[0]
        for forbidden in ("audio_path", "chat_id", "message_id", "privacy_mode", "reasoning"):
            self.assertNotIn(forbidden, row, f"{forbidden} не должен попасть в JSON-экспорт")

    def test_sensitive_values_not_present_anywhere(self) -> None:
        """Чувствительные значения не должны попасть в сериализованный текст вообще."""
        pkg = self._mgr.prepare_share(["x1"], format="json")
        self.assertNotIn("/Users/secret/audio/call.wav", pkg.content)
        self.assertNotIn("telegram_chat_999", pkg.content)
        self.assertNotIn("internal chain-of-thought leak", pkg.content)

    def test_safe_fields_present(self) -> None:
        pkg = self._mgr.prepare_share(["x1"], format="json")
        row = json.loads(pkg.content)[0]
        self.assertEqual(row["text"], "текст транскрипции")
        self.assertEqual(row["source_lang"], "ru")
        self.assertEqual(row["created_at"], "2024-01-01T10:00:00+00:00")
        self.assertEqual(row["duration_sec"], 12.5)
        self.assertEqual(row["confidence"], 0.93)
        self.assertEqual(row["translation"], "texto traducido")
        self.assertEqual(row["target_lang"], "es")

    def test_translation_excluded_when_flag_off(self) -> None:
        pkg = self._mgr.prepare_share(["x1"], format="json", include_translation=False)
        row = json.loads(pkg.content)[0]
        self.assertNotIn("translation", row)
        self.assertNotIn("translated_text", row)
        self.assertNotIn("target_lang", row)

    def test_speaker_count_derived_from_diarization(self) -> None:
        class DiarItem(FakeHistoryItem):
            def to_dict(self) -> dict[str, Any]:
                d = super().to_dict()
                d["diarization"] = [
                    {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0, "text": "a"},
                    {"speaker": "SPEAKER_01", "start": 1.0, "end": 2.0, "text": "b"},
                    {"speaker": "SPEAKER_00", "start": 2.0, "end": 3.0, "text": "c"},
                ]
                return d

        self._mgr._store._items["d1"] = DiarItem("d1", "диаризованный текст")
        pkg = self._mgr.prepare_share(["d1"], format="json")
        row = json.loads(pkg.content)[0]
        self.assertEqual(row["speaker_count"], 2)
        # diarization сохраняется (это шаренный транскрипт, не метаданные-утечка)
        self.assertIn("diarization", row)


if __name__ == "__main__":
    unittest.main()
