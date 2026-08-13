"""W1765: purge_all_data — privacy-purge gap fix для speaker_manager + playback_tracker.

Покрывает:
  1. SpeakerManager.clear_all() очищает in-memory алиасы и отпечатки.
  2. SpeakerManager.clear_all() удаляет speaker_aliases.json + speaker_fingerprints.json с диска.
  3. SpeakerManager.clear_all() идемпотентен (повторный вызов не бросает исключение).
  4. PlaybackTracker.clear_all() очищает in-memory статистику.
  5. PlaybackTracker.clear_all() удаляет playback_stats.json с диска.
  6. PlaybackTracker.clear_all() идемпотентен.
  7. handle_purge_all_data вызывает speaker_manager.clear_all() при наличии wire.
  8. handle_purge_all_data вызывает playback_tracker.clear_all() при наличии wire.
  9. handle_purge_all_data без wire (speaker_manager=None) — нет краша, errors=[].
 10. handle_purge_all_data без wire (playback_tracker=None) — нет краша, errors=[].
 11. handle_purge_all_data: ошибка clear_all не прерывает остальные шаги.
 12. E2E (real tempdir): зарегистрировать спикера + воспроизведение →
     purge_all_data → speaker_aliases.json + speaker_fingerprints.json + playback_stats.json
     отсутствуют, in-memory дикты пусты.
 13. BackendService wiring: _speaker_manager и _playback_tracker заведены в HistoryService.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.speaker_manager import SpeakerManager  # noqa: E402
from backend.playback_tracker import PlaybackTracker  # noqa: E402
from backend.history_service import HistoryService  # noqa: E402


# ---------------------------------------------------------------------------
# Минимальные fakes (повторяют паттерн test_purge_all_data_w1730.py)
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str) -> None:
        self.id = item_id
        self.ts = "2024-01-01T00:00:00+00:00"

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "text": "secret text"}


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
# Вспомогательный embedding (маленький, чтобы не зависеть от pyannote)
# ---------------------------------------------------------------------------

def _fake_embedding(dim: int = 8) -> np.ndarray:
    """Возвращает нормированный fake embedding заданной размерности."""
    arr = np.ones(dim, dtype=np.float32)
    return arr / np.linalg.norm(arr)


# ---------------------------------------------------------------------------
# 1–3: SpeakerManager.clear_all()
# ---------------------------------------------------------------------------

class SpeakerManagerClearAllTestCase(unittest.TestCase):
    """W1765: SpeakerManager.clear_all() — полная очистка биометрики."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = SpeakerManager(data_dir=self._tmpdir)

    def _register(self, name: str = "Паша", dim: int = 8) -> str:
        """Регистрирует спикера с fake embedding."""
        emb = _fake_embedding(dim)
        return self._mgr.register_speaker(name, emb)

    def test_clear_all_empties_aliases_in_memory(self) -> None:
        """clear_all() должен очистить _aliases до пустого dict."""
        self._register("Мария")
        self._register("Иван")
        self.assertGreater(len(self._mgr.get_all_aliases()), 0)

        self._mgr.clear_all()

        self.assertEqual(self._mgr.get_all_aliases(), {},
                         "После clear_all() _aliases должен быть пустым")

    def test_clear_all_empties_fingerprints_in_memory(self) -> None:
        """clear_all() должен очистить _fingerprints до пустого dict."""
        self._register("Анна")
        self.assertGreater(len(self._mgr.get_all_fingerprints()), 0)

        self._mgr.clear_all()

        self.assertEqual(self._mgr.get_all_fingerprints(), {},
                         "После clear_all() _fingerprints должен быть пустым")

    def test_clear_all_deletes_aliases_json_from_disk(self) -> None:
        """clear_all() должен удалить speaker_aliases.json с диска."""
        self._register("Сергей")
        aliases_path = Path(self._tmpdir) / "speaker_aliases.json"
        self.assertTrue(aliases_path.exists(),
                        "speaker_aliases.json должен существовать после register_speaker")

        self._mgr.clear_all()

        self.assertFalse(aliases_path.exists(),
                         "speaker_aliases.json должен быть удалён после clear_all()")

    def test_clear_all_deletes_fingerprints_json_from_disk(self) -> None:
        """clear_all() должен удалить speaker_fingerprints.json с диска."""
        self._register("Ольга")
        fp_path = Path(self._tmpdir) / "speaker_fingerprints.json"
        self.assertTrue(fp_path.exists(),
                        "speaker_fingerprints.json должен существовать после register_speaker")

        self._mgr.clear_all()

        self.assertFalse(fp_path.exists(),
                         "speaker_fingerprints.json должен быть удалён после clear_all()")

    def test_clear_all_idempotent_no_files(self) -> None:
        """Повторный вызов clear_all() при отсутствии файлов не бросает исключений."""
        # Первый вызов без каких-либо данных — не должно быть исключения
        try:
            self._mgr.clear_all()
            self._mgr.clear_all()
        except Exception as exc:
            self.fail(f"clear_all() бросил исключение: {exc}")

    def test_clear_all_without_data_dir_no_crash(self) -> None:
        """clear_all() без data_dir не должен бросать исключений."""
        mgr = SpeakerManager(data_dir=None)
        mgr.set_alias("SPEAKER_00", "Тест")
        try:
            mgr.clear_all()
        except Exception as exc:
            self.fail(f"clear_all() без data_dir бросил исключение: {exc}")
        self.assertEqual(mgr.get_all_aliases(), {})

    def test_clear_all_reloaded_manager_sees_nothing(self) -> None:
        """После clear_all() новый SpeakerManager из того же tmpdir не видит данных."""
        self._register("Дмитрий")

        self._mgr.clear_all()

        mgr2 = SpeakerManager(data_dir=self._tmpdir)
        self.assertEqual(mgr2.get_all_aliases(), {},
                         "Перезагруженный SpeakerManager не должен видеть алиасы")
        self.assertEqual(mgr2.get_all_fingerprints(), {},
                         "Перезагруженный SpeakerManager не должен видеть отпечатки")


# ---------------------------------------------------------------------------
# 4–6: PlaybackTracker.clear_all()
# ---------------------------------------------------------------------------

class PlaybackTrackerClearAllTestCase(unittest.TestCase):
    """W1765: PlaybackTracker.clear_all() — полная очистка статистики воспроизведения."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._tracker = PlaybackTracker(data_dir=self._tmpdir)

    def test_clear_all_empties_stats_in_memory(self) -> None:
        """clear_all() должен очистить _stats до пустого dict."""
        self._tracker.record_playback("item-1", 30.0)
        self._tracker.record_playback("item-2", 15.5)
        self.assertGreater(len(self._tracker._stats), 0)

        self._tracker.clear_all()

        self.assertEqual(self._tracker._stats, {},
                         "После clear_all() _stats должен быть пустым")

    def test_clear_all_deletes_playback_stats_json_from_disk(self) -> None:
        """clear_all() должен удалить playback_stats.json с диска."""
        self._tracker.record_playback("item-x", 10.0)
        stats_path = Path(self._tmpdir) / "playback_stats.json"
        self.assertTrue(stats_path.exists(),
                        "playback_stats.json должен существовать после record_playback")

        self._tracker.clear_all()

        self.assertFalse(stats_path.exists(),
                         "playback_stats.json должен быть удалён после clear_all()")

    def test_clear_all_idempotent_no_file(self) -> None:
        """Повторный вызов clear_all() при отсутствии файла не бросает исключений."""
        try:
            self._tracker.clear_all()
            self._tracker.clear_all()
        except Exception as exc:
            self.fail(f"clear_all() бросил исключение: {exc}")

    def test_clear_all_without_data_dir_no_crash(self) -> None:
        """clear_all() без data_dir не должен бросать исключений."""
        tracker = PlaybackTracker(data_dir=None)
        tracker.record_playback("item-a", 5.0)
        try:
            tracker.clear_all()
        except Exception as exc:
            self.fail(f"clear_all() без data_dir бросил исключение: {exc}")
        self.assertEqual(tracker._stats, {})

    def test_clear_all_reloaded_tracker_sees_nothing(self) -> None:
        """После clear_all() новый PlaybackTracker из того же tmpdir не видит данных."""
        self._tracker.record_playback("item-z", 99.9)

        self._tracker.clear_all()

        tracker2 = PlaybackTracker(data_dir=self._tmpdir)
        result = tracker2.get_playback_stats("item-z")
        self.assertEqual(result["play_count"], 0,
                         "Перезагруженный PlaybackTracker не должен видеть данные")
        self.assertEqual(result["total_listened_sec"], 0.0)


# ---------------------------------------------------------------------------
# 7–11: HistoryService.handle_purge_all_data — wiring + error isolation
# ---------------------------------------------------------------------------

class PurgeAllDataSpeakerPlaybackTestCase(unittest.TestCase):
    """W1765: purge_all_data вызывает clear_all на обоих collaborators."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _make_svc(self) -> HistoryService:
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-1")
        return HistoryService(store=store)

    def test_purge_calls_speaker_manager_clear_all(self) -> None:
        """handle_purge_all_data должен вызвать speaker_manager.clear_all()."""
        svc = self._make_svc()

        calls: list[str] = []

        class SpySpeakerManager:
            def clear_all(self_inner) -> None:  # noqa: N805
                calls.append("clear_all")

        svc._speaker_manager = SpySpeakerManager()
        svc.handle_purge_all_data({"confirm": True})

        self.assertIn("clear_all", calls,
                      "speaker_manager.clear_all() должен быть вызван")

    def test_purge_calls_playback_tracker_clear_all(self) -> None:
        """handle_purge_all_data должен вызвать playback_tracker.clear_all()."""
        svc = self._make_svc()

        calls: list[str] = []

        class SpyPlaybackTracker:
            def clear_all(self_inner) -> None:  # noqa: N805
                calls.append("clear_all")

        svc._playback_tracker = SpyPlaybackTracker()
        svc.handle_purge_all_data({"confirm": True})

        self.assertIn("clear_all", calls,
                      "playback_tracker.clear_all() должен быть вызван")

    def test_purge_no_speaker_manager_no_crash(self) -> None:
        """purge_all_data без _speaker_manager не бросает исключений."""
        svc = self._make_svc()
        svc._speaker_manager = None
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"))
        self.assertNotIn("speaker_fingerprints", result.get("errors", []))

    def test_purge_no_playback_tracker_no_crash(self) -> None:
        """purge_all_data без _playback_tracker не бросает исключений."""
        svc = self._make_svc()
        svc._playback_tracker = None
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"))
        self.assertNotIn("playback", result.get("errors", []))

    def test_speaker_manager_error_does_not_abort_purge(self) -> None:
        """Ошибка speaker_manager.clear_all() не прерывает удаление истории."""

        class ErrorSpeakerManager:
            def clear_all(self) -> None:
                raise OSError("disk full")

        svc = self._make_svc()
        svc._speaker_manager = ErrorSpeakerManager()
        result = svc.handle_purge_all_data({"confirm": True})
        # История всё равно должна быть удалена
        self.assertEqual(result["history_deleted"], 1,
                         "История должна быть удалена даже при ошибке speaker_manager")
        self.assertFalse(result["complete"],
                         "complete должен быть False при ошибке secondary step")
        self.assertIn("speaker_fingerprints", result["errors"],
                      "'speaker_fingerprints' должен присутствовать в errors")

    def test_playback_tracker_error_does_not_abort_purge(self) -> None:
        """Ошибка playback_tracker.clear_all() не прерывает удаление истории."""

        class ErrorPlaybackTracker:
            def clear_all(self) -> None:
                raise RuntimeError("io error")

            def remove_stats(self, item_id: str) -> bool:
                return False

        svc = self._make_svc()
        svc._playback_tracker = ErrorPlaybackTracker()
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["history_deleted"], 1,
                         "История должна быть удалена даже при ошибке playback_tracker")
        self.assertFalse(result["complete"])
        self.assertIn("playback", result["errors"],
                      "'playback' должен присутствовать в errors")


# ---------------------------------------------------------------------------
# 12: E2E — real SpeakerManager + PlaybackTracker, real purge
# ---------------------------------------------------------------------------

class PurgeAllDataE2ESpeakerPlaybackTestCase(unittest.TestCase):
    """W1765 E2E: реальный tmpdir — после purge_all_data все файлы ПДн удалены."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_speaker_and_playback_files_gone_after_purge(self) -> None:
        """E2E: зарегистрировать спикера + воспроизведение → purge → всё стёрто."""
        # --- Подготовка ---
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("recording-1")
        svc = HistoryService(store=store)

        # Создаём реальные collaborators
        speaker_mgr = SpeakerManager(data_dir=self._tmpdir)
        playback = PlaybackTracker(data_dir=self._tmpdir)

        # Подключаем к HistoryService
        svc._speaker_manager = speaker_mgr
        svc._playback_tracker = playback

        # Регистрируем спикера (создаёт оба JSON)
        emb = _fake_embedding(dim=8)
        speaker_mgr.register_speaker("Иван Иванов", emb)

        # Записываем воспроизведение (создаёт playback_stats.json)
        playback.record_playback("recording-1", duration_listened_sec=60.0)

        # Проверяем что файлы созданы
        aliases_path = Path(self._tmpdir) / "speaker_aliases.json"
        fp_path = Path(self._tmpdir) / "speaker_fingerprints.json"
        playback_path = Path(self._tmpdir) / "playback_stats.json"

        self.assertTrue(aliases_path.exists(), "speaker_aliases.json должен существовать")
        self.assertTrue(fp_path.exists(), "speaker_fingerprints.json должен существовать")
        self.assertTrue(playback_path.exists(), "playback_stats.json должен существовать")

        # Проверяем что имя присутствует в файле (PII-sanity check)
        aliases_content = aliases_path.read_text(encoding="utf-8")
        self.assertIn("Иван Иванов", aliases_content)

        # --- Purge ---
        result = svc.handle_purge_all_data({"confirm": True})

        # --- Assertions ---
        self.assertTrue(result.get("ok"), f"purge должен вернуть ok=True: {result}")
        self.assertTrue(result.get("complete"),
                        f"purge должен завершиться без ошибок: {result.get('errors')}")

        # Файлы с диска удалены
        self.assertFalse(aliases_path.exists(),
                         "speaker_aliases.json должен быть удалён после purge")
        self.assertFalse(fp_path.exists(),
                         "speaker_fingerprints.json должен быть удалён после purge")
        self.assertFalse(playback_path.exists(),
                         "playback_stats.json должен быть удалён после purge")

        # In-memory состояние очищено
        self.assertEqual(speaker_mgr.get_all_aliases(), {},
                         "In-memory алиасы должны быть пусты после purge")
        self.assertEqual(speaker_mgr.get_all_fingerprints(), {},
                         "In-memory отпечатки должны быть пусты после purge")
        stats = playback.get_playback_stats("recording-1")
        self.assertEqual(stats["play_count"], 0,
                         "In-memory статистика воспроизведения должна быть пуста после purge")

    def test_aliases_json_contains_no_pii_after_purge(self) -> None:
        """После purge_all_data реальные имена НЕ должны присутствовать на диске."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("rec-2")
        svc = HistoryService(store=store)

        speaker_mgr = SpeakerManager(data_dir=self._tmpdir)
        svc._speaker_manager = speaker_mgr

        emb = _fake_embedding(dim=8)
        speaker_mgr.register_speaker("Мария Петрова", emb)

        aliases_path = Path(self._tmpdir) / "speaker_aliases.json"
        before = aliases_path.read_text(encoding="utf-8")
        self.assertIn("Мария Петрова", before, "Имя должно быть в файле до purge")

        svc.handle_purge_all_data({"confirm": True})

        # Файл должен быть удалён — имя недоступно на диске
        self.assertFalse(aliases_path.exists(),
                         "Файл с именами должен быть удалён, чтобы ПДн не оставались на диске")

    def test_fingerprints_json_contains_no_voiceprint_after_purge(self) -> None:
        """После purge_all_data голосовые отпечатки НЕ должны присутствовать на диске."""
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("rec-3")
        svc = HistoryService(store=store)

        speaker_mgr = SpeakerManager(data_dir=self._tmpdir)
        svc._speaker_manager = speaker_mgr

        emb = _fake_embedding(dim=8)
        speaker_mgr.register_speaker("Тестовый спикер", emb)

        fp_path = Path(self._tmpdir) / "speaker_fingerprints.json"
        self.assertTrue(fp_path.exists(), "speaker_fingerprints.json должен существовать")

        svc.handle_purge_all_data({"confirm": True})

        self.assertFalse(fp_path.exists(),
                         "speaker_fingerprints.json должен быть удалён после purge")


# ---------------------------------------------------------------------------
# 13: BackendService wiring
# ---------------------------------------------------------------------------

class BackendServiceW1765WiringTestCase(unittest.TestCase):
    """W1765: BackendService wires _speaker_manager + _playback_tracker в HistoryService."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_backend_wires_speaker_manager_into_history(self) -> None:
        """BackendService.__init__ должен wire _speaker_manager в _history._speaker_manager."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._speaker_manager,
            svc._speaker_manager,
            "BackendService должен wire _speaker_manager в _history._speaker_manager",
        )
        self.assertIsNotNone(
            svc._history._speaker_manager,
            "_history._speaker_manager не должен быть None после инициализации BackendService",
        )

    def test_backend_wires_playback_tracker_into_history(self) -> None:
        """BackendService.__init__ должен wire _playback_tracker в _history._playback_tracker."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)

        self.assertIs(
            svc._history._playback_tracker,
            svc._playback_tracker,
            "BackendService должен wire _playback_tracker в _history._playback_tracker",
        )
        self.assertIsNotNone(
            svc._history._playback_tracker,
            "_history._playback_tracker не должен быть None после инициализации BackendService",
        )

    def test_e2e_backend_service_purge_clears_speaker_and_playback(self) -> None:
        """E2E через handle_request: данные спикера и воспроизведения стёрты после purge."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        store = StateStore(data_dir=Path(self._tmpdir))
        svc = BackendService(store=store)

        # Добавляем запись в историю
        store.add_history_item(text="тестовая запись для purge E2E")

        # Регистрируем спикера
        emb = _fake_embedding(dim=8)
        svc._speaker_manager.register_speaker("Тест Пользователь", emb)

        # Записываем воспроизведение
        svc._playback_tracker.record_playback("any-item", 45.0)

        aliases_before = svc._speaker_manager.get_all_aliases()
        self.assertGreater(len(aliases_before), 0, "Алиасы должны быть до purge")
        stats_before = svc._playback_tracker.get_playback_stats("any-item")
        self.assertGreater(stats_before["play_count"], 0, "Статистика должна быть до purge")

        # Purge через полный BackendService dispatch
        response = svc.handle_request({
            "id": "purge-w1765",
            "method": "purge_all_data",
            "params": {"confirm": True},
        })
        self.assertTrue(response.get("ok"), f"purge должен вернуть ok=True: {response}")

        # После purge: in-memory очищено
        aliases_after = svc._speaker_manager.get_all_aliases()
        self.assertEqual(aliases_after, {},
                         "Алиасы спикеров должны быть пусты после purge")
        fp_after = svc._speaker_manager.get_all_fingerprints()
        self.assertEqual(fp_after, {},
                         "Голосовые отпечатки должны быть пусты после purge")
        stats_after = svc._playback_tracker.get_playback_stats("any-item")
        self.assertEqual(stats_after["play_count"], 0,
                         "Статистика воспроизведения должна быть пуста после purge")

        # После purge: файлы удалены
        aliases_path = Path(self._tmpdir) / "speaker_aliases.json"
        fp_path = Path(self._tmpdir) / "speaker_fingerprints.json"
        playback_path = Path(self._tmpdir) / "playback_stats.json"
        self.assertFalse(aliases_path.exists(),
                         "speaker_aliases.json должен быть удалён после purge")
        self.assertFalse(fp_path.exists(),
                         "speaker_fingerprints.json должен быть удалён после purge")
        self.assertFalse(playback_path.exists(),
                         "playback_stats.json должен быть удалён после purge")


if __name__ == "__main__":
    unittest.main()
