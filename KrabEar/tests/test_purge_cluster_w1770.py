"""W1770: privacy-purge cluster — закрытие оставшихся пробелов покрытия
(audit_purge_coverage.py --fail-on-found → 0).

Покрывает удаление при handle_purge_all_data всех новых PII/секрет-хранилищ:
  - audio/, failed_recordings/, debug_duration_wav/ — сырое аудио (голос пользователя)
  - exports/, auto_exports/, timeline/ — экспортированные транскрипции (STT-текст)
  - export_schedule.json              — конфиг авто-экспорта PII-истории
  - sessions.ndjson                   — метаданные сессий (usage-pattern ПДн), #1605
  - collections.json                  — имена коллекций (free-text PII), #1613
  - event_replay.ndjson               — payload-ы событий (могут содержать текст)
  - audit_*.ndjson                    — IPC usage-trail (косвенные ПДн)
  - auto_glossary.json                — имена/термины из истории (transcript-derived)
  - search_history.json               — поисковые запросы пользователя
  - hotwords.json, vocabulary.txt     — пользовательские слова (имена/термины)
  - usage_stats.json, recap_state.json, scheduled_recordings.json — usage-pattern
  - api_tokens.json                   — Bearer-токены REST (СЕКРЕТЫ)
  - transcript_versions.ndjson        — полный текст версий + ЛАТЕНТНЫЙ БАГ (wire)

Дополнительно: BackendService.__init__ wiring — _transcript_versions / _collection_manager
/ _session_tracker действительно подключены в _history (ранее _transcript_versions был
мёртвым None → каскадная очистка версий не работала).
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

from backend.history_service import HistoryService            # noqa: E402
from backend.transcript_versioning import TranscriptVersionManager  # noqa: E402
from backend.collection_manager import CollectionManager      # noqa: E402
from backend.session_tracker import SessionTracker            # noqa: E402
from backend.template_manager import TemplateManager          # noqa: E402
from backend.event_replay import EventReplayManager           # noqa: E402
from backend.live_subs_service import LiveSubsService         # noqa: E402


# ---------------------------------------------------------------------------
# Минимальный StateStore fake (паттерн из W1767 purge-тестов)
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str) -> None:
        self.id = item_id
        self.ts = "2024-01-01T00:00:00+00:00"

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "text": "тестовый текст"}


class FakeStore:
    """Минимальный StateStore fake для purge-тестов."""

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


# ---------------------------------------------------------------------------
# Прямые файловые/директорные хранилища (rmtree / unlink в purge)
# ---------------------------------------------------------------------------

class DirectStorePurgeTestCase(unittest.TestCase):
    """W1770: каждое прямое файловое/директорное PII-хранилище удаляется при purge."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._dir = Path(self._tmpdir)

    def _purge(self) -> dict:
        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        return svc.handle_purge_all_data({"confirm": True})

    def test_audio_dir_removed(self) -> None:
        """audio/ (сырое аудио) удаляется целиком."""
        audio = self._dir / "audio"
        audio.mkdir(parents=True, exist_ok=True)
        (audio / "rec_001.wav").write_bytes(b"RIFF....fake-pcm....")
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(audio.exists(), "audio/ должен быть удалён")

    def test_failed_recordings_dir_removed(self) -> None:
        """failed_recordings/ (сырое аудио сорванных записей) удаляется."""
        failed = self._dir / "failed_recordings"
        failed.mkdir(parents=True, exist_ok=True)
        (failed / "broken.wav").write_bytes(b"PCM")
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(failed.exists(), "failed_recordings/ должен быть удалён")

    def test_debug_duration_wav_dir_removed(self) -> None:
        """debug_duration_wav/ (opt-in keep успешных диктовок, W2b) удаляется."""
        kept = self._dir / "debug_duration_wav"
        kept.mkdir(parents=True, exist_ok=True)
        (kept / "keep.wav").write_bytes(b"PCM")
        (kept / "keep.json").write_text('{"sample_rate": 16000}', encoding="utf-8")
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(kept.exists(), "debug_duration_wav/ должен быть удалён")

    def test_export_dirs_removed(self) -> None:
        """exports/, auto_exports/, timeline/ (экспорт транскрипций) удаляются."""
        for name in ("exports", "auto_exports", "timeline"):
            d = self._dir / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "out.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nприватный текст\n", encoding="utf-8")
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        for name in ("exports", "auto_exports", "timeline"):
            self.assertFalse((self._dir / name).exists(), f"{name}/ должен быть удалён")

    def test_export_schedule_removed(self) -> None:
        """export_schedule.json удаляется."""
        f = self._dir / "export_schedule.json"
        f.write_text('{"enabled": true, "format": "srt", "output_dir": "/Users/me/Documents"}', encoding="utf-8")
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(f.exists(), "export_schedule.json должен быть удалён")

    def test_event_replay_removed(self) -> None:
        """event_replay.ndjson удаляется."""
        f = self._dir / "event_replay.ndjson"
        f.write_text('{"type":"stt.result","data":{"text":"приватная транскрипция"}}\n', encoding="utf-8")
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(f.exists(), "event_replay.ndjson должен быть удалён")

    def test_audit_ndjson_glob_removed(self) -> None:
        """Все audit_*.ndjson удаляются (glob-семейство)."""
        for date in ("2026-06-01", "2026-06-02"):
            (self._dir / f"audit_{date}.ndjson").write_text(
                '{"ts":"x","method":"translate_text","params_keys":[],"success":true}\n',
                encoding="utf-8",
            )
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(list(self._dir.glob("audit_*.ndjson")), [], "audit_*.ndjson должны быть удалены")

    def test_auto_glossary_removed(self) -> None:
        """auto_glossary.json (имена из истории) удаляется."""
        f = self._dir / "auto_glossary.json"
        f.write_text('{"terms": ["Пашка", "РонгФа"]}', encoding="utf-8")
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(f.exists(), "auto_glossary.json должен быть удалён")

    def test_search_history_removed(self) -> None:
        """search_history.json (поисковые запросы) удаляется."""
        f = self._dir / "search_history.json"
        f.write_text('{"queries": [{"q": "секретный запрос", "ts": "x"}]}', encoding="utf-8")
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(f.exists(), "search_history.json должен быть удалён")

    def test_hotwords_and_vocabulary_txt_removed(self) -> None:
        """hotwords.json и legacy vocabulary.txt удаляются."""
        hw = self._dir / "hotwords.json"
        hw.write_text('{"hotwords": ["ИмяСобственное"]}', encoding="utf-8")
        vt = self._dir / "vocabulary.txt"
        vt.write_text("ИванИванов\nМарияПетрова\n", encoding="utf-8")
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(hw.exists(), "hotwords.json должен быть удалён")
        self.assertFalse(vt.exists(), "vocabulary.txt должен быть удалён")

    def test_usage_recap_scheduler_removed(self) -> None:
        """usage_stats.json, recap_state.json, scheduled_recordings.json удаляются."""
        (self._dir / "usage_stats.json").write_text('{"2026-06-01": {"recordings": 5}}', encoding="utf-8")
        (self._dir / "recap_state.json").write_text('{"last_sent_date": "2026-06-01"}', encoding="utf-8")
        (self._dir / "scheduled_recordings.json").write_text('[{"id": "x", "start": "2026-06-02T10:00"}]', encoding="utf-8")
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        for name in ("usage_stats.json", "recap_state.json", "scheduled_recordings.json"):
            self.assertFalse((self._dir / name).exists(), f"{name} должен быть удалён")

    def test_api_tokens_removed(self) -> None:
        """api_tokens.json (Bearer secrets) удаляется."""
        f = self._dir / "api_tokens.json"
        f.write_text('{"tokens": {"deadbeef": {"hash": "sha256:...", "label": "ci"}}}', encoding="utf-8")
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(f.exists(), "api_tokens.json (secrets) должен быть удалён")

    def test_purge_without_any_stores_no_crash(self) -> None:
        """purge на пустом data_dir не бросает и не помечает новые шаги ошибками."""
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        for step in (
            "audio", "failed_recordings", "debug_duration_wav", "exports", "auto_exports", "timeline",
            "export_schedule", "event_replay", "audit_logs", "auto_glossary",
            "search_history", "hotwords", "vocabulary_txt", "usage_stats",
            "recap_state", "scheduled_recordings", "api_tokens",
        ):
            self.assertNotIn(step, result.get("errors", []), f"{step} не должен попасть в errors на пустом dir")


# ---------------------------------------------------------------------------
# Collaborator-хранилища (in-memory + диск): sessions / collections / versions
# ---------------------------------------------------------------------------

class CollaboratorStorePurgeTestCase(unittest.TestCase):
    """W1770: sessions.ndjson, collections.json, transcript_versions.ndjson —
    очищаются через коллабораторов (in-memory) + физически с диска."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._dir = Path(self._tmpdir)

    def test_sessions_cleared_via_session_tracker(self) -> None:
        """SessionTracker.clear_all() + явный unlink удаляют sessions.ndjson."""
        tracker = SessionTracker(data_dir=self._tmpdir)
        tracker.start_session(audio_device="MacBook", stt_model="balanced")
        tracker.end_session({"duration_sec": 3.0, "text": "session text"})
        sessions_path = self._dir / "sessions.ndjson"
        self.assertTrue(sessions_path.exists(), "sessions.ndjson должен существовать до purge")

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._session_tracker = tracker
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), result)
        self.assertFalse(sessions_path.exists(), "sessions.ndjson должен быть удалён")

    def test_collections_cleared_via_collection_manager(self) -> None:
        """CollectionManager.purge_all() + явный unlink удаляют collections.json."""
        store = FakeStore(data_dir=self._tmpdir)
        cm = CollectionManager(store=store)
        cm.create_collection("Личные звонки", "приватная папка")
        collections_path = self._dir / "collections.json"
        self.assertTrue(collections_path.exists(), "collections.json должен существовать до purge")

        svc = HistoryService(store=store)
        svc._collection_manager = cm
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), result)
        self.assertFalse(collections_path.exists(), "collections.json должен быть удалён")
        # in-memory тоже очищен
        self.assertEqual(cm.list_collections(), [])

    def test_collection_name_not_on_disk_after_purge(self) -> None:
        """Имя коллекции (free-text PII) не остаётся на диске после purge."""
        store = FakeStore(data_dir=self._tmpdir)
        cm = CollectionManager(store=store)
        secret_name = "СовершенноСекретнаяПапка"
        cm.create_collection(secret_name, "")

        svc = HistoryService(store=store)
        svc._collection_manager = cm
        svc.handle_purge_all_data({"confirm": True})

        for f in self._dir.rglob("*"):
            if f.is_file():
                content = f.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn(secret_name, content, f"Имя коллекции найдено в {f} после purge")

    def test_transcript_versions_cleared_via_collaborator(self) -> None:
        """Каскадная очистка версий: cleanup_for_ids стирает версии активных записей."""
        tvm = TranscriptVersionManager(data_dir=self._tmpdir)
        tvm.save_version("recording-1", "приватный текст версии 1", source="stt_raw")
        tvm.save_version("recording-1", "приватный текст версии 2", source="manual")
        versions_path = self._dir / "transcript_versions.ndjson"
        self.assertTrue(tvm.get_versions("recording-1"), "версии должны существовать до purge")

        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("recording-1")
        svc = HistoryService(store=store)
        svc._transcript_versions = tvm
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(tvm.get_versions("recording-1"), [], "версии должны быть очищены после purge")
        # текст версий не остаётся на диске
        content = versions_path.read_text(encoding="utf-8") if versions_path.exists() else ""
        self.assertNotIn("приватный текст версии", content, "текст версии остался на диске после purge")

    def test_collaborator_error_does_not_abort_purge(self) -> None:
        """Ошибка collection_manager.purge_all() не прерывает удаление истории."""

        class ErrorCM:
            def purge_all(self) -> None:
                raise PermissionError("нет прав")

        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-c")
        svc = HistoryService(store=store)
        svc._collection_manager = ErrorCM()

        result = svc.handle_purge_all_data({"confirm": True})

        self.assertEqual(result["history_deleted"], 1)
        self.assertFalse(result["complete"])
        self.assertIn("collections", result["errors"])


# ---------------------------------------------------------------------------
# E2E — каждый PII-store удаляется одним вызовом handle_purge_all_data
# ---------------------------------------------------------------------------

class PurgeAllDataE2EW1770TestCase(unittest.TestCase):
    """W1770 E2E: посеять все новые хранилища на диске и проверить, что после
    одного purge с confirm=True ВСЕ PII-stores исчезли."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._dir = Path(self._tmpdir)

    def test_e2e_all_new_pii_stores_gone(self) -> None:
        d = self._dir

        # --- сырое аудио ---
        (d / "audio").mkdir(parents=True, exist_ok=True)
        (d / "audio" / "rec.wav").write_bytes(b"PCMDATA voice biometric")
        (d / "failed_recordings").mkdir(parents=True, exist_ok=True)
        (d / "failed_recordings" / "fail.wav").write_bytes(b"PCM")
        (d / "debug_duration_wav").mkdir(parents=True, exist_ok=True)
        (d / "debug_duration_wav" / "keep.wav").write_bytes(b"PCM")

        # --- экспорт транскрипций ---
        for name in ("exports", "auto_exports", "timeline"):
            (d / name).mkdir(parents=True, exist_ok=True)
            (d / name / "out.txt").write_text("экспортированный приватный текст", encoding="utf-8")
        (d / "export_schedule.json").write_text('{"format": "srt"}', encoding="utf-8")

        # --- usage/replay/audit ---
        (d / "event_replay.ndjson").write_text('{"data":{"text":"replayed приват"}}\n', encoding="utf-8")
        (d / "audit_2026-06-02.ndjson").write_text('{"method":"x"}\n', encoding="utf-8")
        (d / "usage_stats.json").write_text('{"2026-06-02":{"recordings":1}}', encoding="utf-8")
        (d / "recap_state.json").write_text('{"last_sent_date":"2026-06-02"}', encoding="utf-8")
        (d / "scheduled_recordings.json").write_text('[]', encoding="utf-8")

        # --- словари / поиск ---
        (d / "auto_glossary.json").write_text('{"terms":["ГлоссарийПриват"]}', encoding="utf-8")
        (d / "search_history.json").write_text('{"queries":["ПоискПриват"]}', encoding="utf-8")
        (d / "hotwords.json").write_text('{"hotwords":["ХотвордПриват"]}', encoding="utf-8")
        (d / "vocabulary.txt").write_text("ЛегаСловоПриват\n", encoding="utf-8")

        # --- REST secrets ---
        (d / "api_tokens.json").write_text('{"tokens":{"k":{"hash":"СекретТокен"}}}', encoding="utf-8")

        # --- collaborators: sessions / collections / transcript versions ---
        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("recording-1")
        tracker = SessionTracker(data_dir=self._tmpdir)
        tracker.start_session(audio_device="Устройство", stt_model="call")
        tracker.end_session({"duration_sec": 5.0})
        cm = CollectionManager(store=store)
        cm.create_collection("КоллекцияПриват", "")
        tvm = TranscriptVersionManager(data_dir=self._tmpdir)
        tvm.save_version("recording-1", "ВерсияПриватныйТекст", source="manual")

        svc = HistoryService(store=store)
        svc._session_tracker = tracker
        svc._collection_manager = cm
        svc._transcript_versions = tvm

        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("complete"), f"purge должен быть полным: {result.get('errors')}")

        # все файлы/директории исчезли
        gone_dirs = ["audio", "failed_recordings", "debug_duration_wav", "exports", "auto_exports", "timeline"]
        for name in gone_dirs:
            self.assertFalse((d / name).exists(), f"{name}/ должен быть удалён")
        gone_files = [
            "export_schedule.json", "event_replay.ndjson", "usage_stats.json",
            "recap_state.json", "scheduled_recordings.json", "auto_glossary.json",
            "search_history.json", "hotwords.json", "vocabulary.txt",
            "api_tokens.json", "collections.json", "sessions.ndjson",
        ]
        for name in gone_files:
            self.assertFalse((d / name).exists(), f"{name} должен быть удалён")
        self.assertEqual(list(d.glob("audit_*.ndjson")), [], "audit_*.ndjson должны быть удалены")

        # ни один PII-токен не остаётся на диске
        pii_tokens = [
            "voice biometric", "экспортированный приватный текст", "replayed приват",
            "ГлоссарийПриват", "ПоискПриват", "ХотвордПриват", "ЛегаСловоПриват",
            "СекретТокен", "КоллекцияПриват", "ВерсияПриватныйТекст",
        ]
        for f in d.rglob("*"):
            if f.is_file():
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for token in pii_tokens:
                    self.assertNotIn(token, content, f"PII-токен '{token}' найден в {f} после purge")


# ---------------------------------------------------------------------------
# BackendService wiring — латентный баг transcript_versions + новые collaborators
# ---------------------------------------------------------------------------

class BackendServiceW1770WiringTestCase(unittest.TestCase):
    """W1770: BackendService.__init__ реально подключает _transcript_versions
    (ранее None → мёртвый), _collection_manager, _session_tracker в _history."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _make_backend(self):
        from backend.state_store import StateStore
        from backend.service import BackendService
        store = StateStore(data_dir=Path(self._tmpdir))
        return BackendService(store=store)

    def test_transcript_versions_wired_not_none(self) -> None:
        """ИСПРАВЛЕНИЕ ЛАТЕНТНОГО БАГА: _history._transcript_versions не None и
        указывает на BackendService._transcript_versioning."""
        svc = self._make_backend()
        self.assertIsNotNone(
            svc._history._transcript_versions,
            "_history._transcript_versions должен быть подключён (ранее оставался None → dead)",
        )
        self.assertIs(
            svc._history._transcript_versions,
            svc._transcript_versioning,
            "_history._transcript_versions должен указывать на _transcript_versioning",
        )

    def test_compact_hook_wired(self) -> None:
        """_on_compact_hook fallback подключён к purge_orphaned_versions."""
        svc = self._make_backend()
        hook = getattr(svc.store, "_on_compact_hook", None)
        self.assertIsNotNone(hook, "_on_compact_hook должен быть подключён")

    def test_collection_manager_wired(self) -> None:
        """_history._collection_manager указывает на BackendService._collections."""
        svc = self._make_backend()
        self.assertIs(
            svc._history._collection_manager,
            svc._collections,
            "_history._collection_manager должен указывать на _collections",
        )
        self.assertIsNotNone(svc._history._collection_manager)

    def test_session_tracker_wired(self) -> None:
        """_history._session_tracker указывает на BackendService._session_tracker."""
        svc = self._make_backend()
        self.assertIs(
            svc._history._session_tracker,
            svc._session_tracker,
            "_history._session_tracker должен указывать на _session_tracker",
        )
        self.assertIsNotNone(svc._history._session_tracker)


# ---------------------------------------------------------------------------
# W1771 supplement — gaps the W1770 cluster missed + guard blind-spot
# ---------------------------------------------------------------------------

class _FakeBuffer:
    """Минимальный stand-in для LiveSubsService с in-memory PCM-буфером."""

    def __init__(self) -> None:
        # «сырой голос» — имитируем накопленный PCM (любой непустой объект).
        self._buffer = [b"\x00\x01" * 16000]
        self._buffer_samples = 16000

    def reset(self) -> None:
        self._buffer = []
        self._buffer_samples = 0


class PurgeW1771GapTestCase(unittest.TestCase):
    """W1771: report_*.html и сиблинг-экспорты, templates.json, безусловный
    clear версий, in-memory clear-хуки (event_replay/live_subs)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._dir = Path(self._tmpdir)

    def test_export_report_html_removed(self) -> None:
        """GAP-1: report_*.html (самый PII-плотный экспорт) удаляется из transcripts/."""
        tdir = self._dir / "transcripts"
        tdir.mkdir(parents=True, exist_ok=True)
        report = tdir / "report_20260602_101010.html"
        report.write_text("<html><body>ПОЛНЫЙ СЕКРЕТНЫЙ ТРАНСКРИПТ</body></html>", encoding="utf-8")
        store = FakeStore(data_dir=self._tmpdir)
        result = HistoryService(store=store).handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(report.exists(), "report_*.html должен быть удалён")

    def test_all_sibling_export_extensions_removed(self) -> None:
        """GAP-1: *.html, *.srt, *.json, *.csv (и *.md) — все экспортные расширения удаляются."""
        tdir = self._dir / "transcripts"
        tdir.mkdir(parents=True, exist_ok=True)
        files = {
            "report_x.html": "<html>СЕКРЕТ-HTML</html>",
            "srt_x.srt": "1\n00:00:00,000 --> 00:00:01,000\nСЕКРЕТ-SRT\n",
            "export_x.json": '{"text": "СЕКРЕТ-JSON"}',
            "export_x.csv": "text\nСЕКРЕТ-CSV\n",
            "export_x.md": "# СЕКРЕТ-MD",
            "report_x.html.tmp": "<html>СЕКРЕТ-TMP</html>",  # in-flight temp тоже
        }
        for name, content in files.items():
            (tdir / name).write_text(content, encoding="utf-8")
        store = FakeStore(data_dir=self._tmpdir)
        result = HistoryService(store=store).handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"), result)
        survivors = [p.name for p in tdir.iterdir() if p.is_file()]
        self.assertEqual(survivors, [], f"экспортные артефакты пережили purge: {survivors}")
        # ни один секрет-токен не остался на диске
        for f in self._dir.rglob("*"):
            if f.is_file():
                content = f.read_text(encoding="utf-8", errors="replace")
                for token in ("СЕКРЕТ-HTML", "СЕКРЕТ-SRT", "СЕКРЕТ-JSON", "СЕКРЕТ-CSV", "СЕКРЕТ-MD", "СЕКРЕТ-TMP"):
                    self.assertNotIn(token, content, f"{token} найден в {f}")

    def test_templates_json_purged_via_collaborator(self) -> None:
        """GAP-2: templates.json (free-text PII) удаляется через TemplateManager.purge_all()."""
        tmgr = TemplateManager(data_dir=self._tmpdir)
        tmgr.add_template(
            name="my_sig",
            text="С уважением, Павел Ронгфа, +7 999 123-45-67",  # реальное имя + телефон
            category="email",
        )
        templates_path = self._dir / "templates.json"
        self.assertTrue(templates_path.exists(), "templates.json должен существовать до purge")

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._template_manager = tmgr
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), result)
        self.assertFalse(templates_path.exists(), "templates.json должен быть удалён")
        # пользовательский шаблон исчез (builtin остаются)
        names = {t["name"] for t in tmgr.get_templates()}
        self.assertNotIn("my_sig", names)
        # PII не осталась на диске
        for f in self._dir.rglob("*"):
            if f.is_file():
                content = f.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn("+7 999 123-45-67", content, f"телефон найден в {f}")

    def test_template_manager_purge_all_idempotent(self) -> None:
        """TemplateManager.purge_all() идемпотентен и возвращает existed-флаг."""
        tmgr = TemplateManager(data_dir=self._tmpdir)
        tmgr.add_template(name="t", text="text", category="general")
        self.assertTrue(tmgr.purge_all(), "первый purge_all → True (файл был)")
        self.assertFalse(tmgr.purge_all(), "второй purge_all → False (файла нет)")

    def test_event_replay_cleared_via_collaborator(self) -> None:
        """GAP-3: event_replay усекается через clear() (файл + in-memory кольцо)."""
        replay_path = self._dir / "event_replay.ndjson"
        replay = EventReplayManager(persist_path=replay_path)
        replay.record_event("stt.result", {"text": "СЕКРЕТНАЯ ТРАНСКРИПЦИЯ В RING"})
        # файл содержит cleartext до purge
        self.assertIn("СЕКРЕТНАЯ", replay_path.read_text(encoding="utf-8"))

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._event_replay = replay
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), result)
        # файл усечён до пустого (дескриптор остаётся валидным)
        self.assertEqual(replay_path.read_text(encoding="utf-8"), "", "event_replay.ndjson должен быть пуст")
        # in-memory кольцо очищено
        self.assertEqual(replay.get_events(), [])

    def test_live_subs_buffer_reset_via_collaborator(self) -> None:
        """GAP-3: live_subs in-memory PCM-буфер (сырой голос) сбрасывается через reset()."""
        buf = _FakeBuffer()
        self.assertTrue(buf._buffer, "буфер должен быть непуст до purge")

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._live_subs_service = buf
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(buf._buffer, [], "PCM-буфер должен быть очищен")
        self.assertEqual(buf._buffer_samples, 0)

    def test_transcript_versions_unconditional_clear_removes_orphans(self) -> None:
        """GAP-3: clear_all() БЕЗУСЛОВНО стирает версии, включая orphan (записи,
        которых нет в active) — раньше cleanup_for_ids(active_ids) их оставлял."""
        tvm = TranscriptVersionManager(data_dir=self._tmpdir)
        # версия orphan-записи: её item_id НЕ будет в active store
        tvm.save_version("orphan-deleted-long-ago", "ORPHAN ПРИВАТНЫЙ ТЕКСТ", source="manual")
        # версия активной записи
        tvm.save_version("active-1", "ACTIVE ПРИВАТНЫЙ ТЕКСТ", source="stt_raw")
        versions_path = self._dir / "transcript_versions.ndjson"
        self.assertTrue(tvm.get_versions("orphan-deleted-long-ago"))

        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("active-1")  # только active-1 в истории; orphan отсутствует
        svc = HistoryService(store=store)
        svc._transcript_versions = tvm
        result = svc.handle_purge_all_data({"confirm": True})

        self.assertTrue(result.get("ok"), result)
        # ОБЕ версии стёрты (включая orphan, который cleanup_for_ids бы пропустил)
        self.assertEqual(tvm.get_versions("orphan-deleted-long-ago"), [], "orphan-версия должна быть стёрта")
        self.assertEqual(tvm.get_versions("active-1"), [])
        content = versions_path.read_text(encoding="utf-8") if versions_path.exists() else ""
        self.assertNotIn("ORPHAN ПРИВАТНЫЙ ТЕКСТ", content, "orphan cleartext остался на диске")
        self.assertNotIn("ACTIVE ПРИВАТНЫЙ ТЕКСТ", content)

    def test_collaborator_clear_error_does_not_abort_purge(self) -> None:
        """Ошибка clear-хука коллаборатора не прерывает purge истории."""

        class ErrorReplay:
            def clear(self) -> None:
                raise PermissionError("нет прав")

        store = FakeStore(data_dir=self._tmpdir)
        store.add_item("item-x")
        svc = HistoryService(store=store)
        svc._event_replay = ErrorReplay()
        result = svc.handle_purge_all_data({"confirm": True})
        self.assertEqual(result["history_deleted"], 1)
        self.assertFalse(result["complete"])
        self.assertIn("event_replay", result["errors"])

    def test_w1771_e2e_all_new_artifacts_gone(self) -> None:
        """W1771 E2E: посеять report_*.html + templates.json + event_replay +
        live_subs + transcript_versions, один purge — всё PII исчезло."""
        d = self._dir
        tdir = d / "transcripts"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "report_e2e.html").write_text("<html>E2E-HTML-СЕКРЕТ</html>", encoding="utf-8")
        (tdir / "srt_e2e.srt").write_text("E2E-SRT-СЕКРЕТ", encoding="utf-8")

        tmgr = TemplateManager(data_dir=self._tmpdir)
        tmgr.add_template(name="sig", text="E2E-ШАБЛОН-СЕКРЕТ +7 000", category="email")

        replay_path = d / "event_replay.ndjson"
        replay = EventReplayManager(persist_path=replay_path)
        replay.record_event("translation.result", {"text": "E2E-REPLAY-СЕКРЕТ"})

        buf = _FakeBuffer()

        tvm = TranscriptVersionManager(data_dir=self._tmpdir)
        tvm.save_version("orphan-x", "E2E-VERSION-СЕКРЕТ", source="manual")

        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        svc._template_manager = tmgr
        svc._event_replay = replay
        svc._live_subs_service = buf
        svc._transcript_versions = tvm

        result = svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("complete"), f"purge должен быть полным: {result.get('errors')}")

        # файлы исчезли
        self.assertFalse((tdir / "report_e2e.html").exists(), "report_*.html должен быть удалён")
        self.assertFalse((tdir / "srt_e2e.srt").exists(), "srt_*.srt должен быть удалён")
        self.assertFalse((d / "templates.json").exists(), "templates.json должен быть удалён")
        # event_replay усечён, in-memory очищено
        self.assertEqual(replay_path.read_text(encoding="utf-8"), "")
        self.assertEqual(replay.get_events(), [])
        self.assertEqual(buf._buffer, [])
        self.assertEqual(tvm.get_versions("orphan-x"), [])

        # ни один E2E-секрет не остался на диске
        secrets = [
            "E2E-HTML-СЕКРЕТ", "E2E-SRT-СЕКРЕТ", "E2E-ШАБЛОН-СЕКРЕТ",
            "E2E-REPLAY-СЕКРЕТ", "E2E-VERSION-СЕКРЕТ",
        ]
        for f in d.rglob("*"):
            if f.is_file():
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for token in secrets:
                    self.assertNotIn(token, content, f"PII-секрет '{token}' найден в {f} после purge")


if __name__ == "__main__":
    unittest.main()
