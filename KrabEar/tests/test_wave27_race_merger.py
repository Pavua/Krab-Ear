"""wave-27 MED — тесты для трёх исправлений:

A1 (race)    — RecordingCoreService: старт/стоп жизненного цикла
               RealtimePartialTranscriber / RealtimeSilenceFilter сериализован под
               self._rt_lock, поэтому конкурентные start_recording/stop_recording
               не оставляют осиротевший (started-but-never-stopped) daemon.
A2 (privacy) — RecordingMerger.merge_items / preview_merge возвращают
               {"ok": False, "reason": "privacy_mode_active"} БЕЗ чтения текста,
               когда privacy_mode_fn() == True.
A3 (DoS)     — RecordingMerger.merge_items отклоняет списки длиннее
               MAX_MERGE_ITEMS, не выполняя per-item store-lookup'ов.

Без mlx_whisper. Memory-safe (by path).
"""

from __future__ import annotations

import sys
import threading
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

# Настройка пути для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_merger import MAX_MERGE_ITEMS, RecordingMerger  # noqa: E402
import backend.recording_core_service as rcs_mod  # noqa: E402
from backend.recording_core_service import RecordingCoreService  # noqa: E402


# ===========================================================================
# A2 / A3 — RecordingMerger privacy gate + merge cap
# ===========================================================================


@dataclass
class FakeHistoryItem:
    id: str
    ts: str
    text: str
    paste_status: str = "success"
    source_text: str = ""
    translated_text: str = ""
    translation_mode: str = "off"
    source_lang: str = ""
    target_lang: str = ""
    translation_status: str = "not_requested"
    audio_duration_sec: float | None = None
    confidence: float | None = None
    diarization: dict | None = None
    tags: list = field(default_factory=list)
    favorite: bool = False

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


class CountingFakeStore:
    """Фейк StateStore, считающий обращения get_history_item_by_id."""

    def __init__(self) -> None:
        self._items: dict[str, FakeHistoryItem] = {}
        self._added: list[FakeHistoryItem] = []
        self.lookup_count = 0

    def add_fake_item(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00") -> FakeHistoryItem:
        item = FakeHistoryItem(id=item_id, ts=ts, text=text)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        self.lookup_count += 1
        return self._items.get(item_id)

    def add_history_item(self, text: str, **kwargs: Any) -> FakeHistoryItem:
        import uuid
        item = FakeHistoryItem(id=str(uuid.uuid4()), ts="2026-04-12T12:00:00", text=text)
        self._items[item.id] = item
        self._added.append(item)
        return item

    def delete_history_item(self, item_id: str) -> bool:
        return self._items.pop(item_id, None) is not None


class TestMergerPrivacyGate(unittest.TestCase):
    """A2: privacy_mode_fn гейт в merge_items / preview_merge."""

    def setUp(self) -> None:
        self.store = CountingFakeStore()
        self.store.add_fake_item("a1", "Секретный текст один", ts="2026-04-12T09:00:00")
        self.store.add_fake_item("a2", "Секретный текст два", ts="2026-04-12T09:05:00")

    def test_merge_blocked_when_privacy_on(self) -> None:
        merger = RecordingMerger(privacy_mode_fn=lambda: True)
        result = merger.merge_items(["a1", "a2"], self.store)
        self.assertEqual(result, {"ok": False, "reason": "privacy_mode_active"})

    def test_merge_does_not_read_store_when_privacy_on(self) -> None:
        # Гейт стоит ДО _load_items → ни одного store-lookup'а транскриптов.
        merger = RecordingMerger(privacy_mode_fn=lambda: True)
        merger.merge_items(["a1", "a2"], self.store)
        self.assertEqual(self.store.lookup_count, 0)
        self.assertEqual(len(self.store._added), 0)

    def test_preview_blocked_when_privacy_on(self) -> None:
        merger = RecordingMerger(privacy_mode_fn=lambda: True)
        result = merger.preview_merge(["a1", "a2"], self.store)
        self.assertEqual(result, {"ok": False, "reason": "privacy_mode_active"})

    def test_preview_does_not_read_store_when_privacy_on(self) -> None:
        merger = RecordingMerger(privacy_mode_fn=lambda: True)
        merger.preview_merge(["a1", "a2"], self.store)
        self.assertEqual(self.store.lookup_count, 0)

    def test_merge_proceeds_when_privacy_off(self) -> None:
        merger = RecordingMerger(privacy_mode_fn=lambda: False)
        result = merger.merge_items(["a1", "a2"], self.store)
        self.assertIn("text", result)
        self.assertIn("Секретный текст один", result["text"])

    def test_merge_proceeds_when_fn_none(self) -> None:
        # Дефолт (None) = гейт выключен → обратная совместимость.
        merger = RecordingMerger()
        result = merger.merge_items(["a1", "a2"], self.store)
        self.assertIn("text", result)

    def test_handle_merge_recordings_blocked_when_privacy_on(self) -> None:
        merger = RecordingMerger(privacy_mode_fn=lambda: True)
        result = merger.handle_merge_recordings({"item_ids": ["a1", "a2"]}, self.store)
        self.assertEqual(result, {"ok": False, "reason": "privacy_mode_active"})

    def test_handle_preview_merge_blocked_when_privacy_on(self) -> None:
        merger = RecordingMerger(privacy_mode_fn=lambda: True)
        result = merger.handle_preview_merge({"item_ids": ["a1", "a2"]}, self.store)
        self.assertEqual(result, {"ok": False, "reason": "privacy_mode_active"})


class TestMergerMergeCap(unittest.TestCase):
    """A3: MAX_MERGE_ITEMS DoS-крышка в merge_items."""

    def setUp(self) -> None:
        self.store = CountingFakeStore()

    def test_over_cap_returns_error(self) -> None:
        merger = RecordingMerger()
        ids = [f"id{i}" for i in range(MAX_MERGE_ITEMS + 1)]
        result = merger.merge_items(ids, self.store)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "too_many_items")
        self.assertEqual(result["max_items"], MAX_MERGE_ITEMS)
        self.assertEqual(result["requested"], MAX_MERGE_ITEMS + 1)

    def test_over_cap_does_no_store_lookups(self) -> None:
        # Крышка ДО _load_items → ни одного per-item lookup'а (это и есть DoS-защита).
        merger = RecordingMerger()
        ids = [f"id{i}" for i in range(MAX_MERGE_ITEMS + 50)]
        merger.merge_items(ids, self.store)
        self.assertEqual(self.store.lookup_count, 0)

    def test_at_cap_is_allowed(self) -> None:
        # Ровно MAX_MERGE_ITEMS записей — крышка не срабатывает (проходит к _load_items).
        merger = RecordingMerger()
        for i in range(MAX_MERGE_ITEMS):
            self.store.add_fake_item(f"c{i}", f"Текст {i}", ts=f"2026-04-12T09:{i:02d}:00")
        ids = [f"c{i}" for i in range(MAX_MERGE_ITEMS)]
        result = merger.merge_items(ids, self.store)
        self.assertIn("text", result)
        self.assertEqual(len(result["merged_from"]), MAX_MERGE_ITEMS)

    def test_privacy_gate_precedes_cap(self) -> None:
        # При privacy ON огромный список всё равно отклоняется как privacy, без lookup'ов.
        merger = RecordingMerger(privacy_mode_fn=lambda: True)
        ids = [f"id{i}" for i in range(MAX_MERGE_ITEMS + 10)]
        result = merger.merge_items(ids, self.store)
        self.assertEqual(result["reason"], "privacy_mode_active")
        self.assertEqual(self.store.lookup_count, 0)

    def test_cap_constant_value(self) -> None:
        self.assertEqual(MAX_MERGE_ITEMS, 50)


# ===========================================================================
# A1 — RecordingCoreService start/stop daemon-lifecycle race
# ===========================================================================


class _LifecycleRegistry:
    """Потокобезопасный учёт start()/stop() фейковых daemon'ов.

    Осиротевший daemon = тот, у кого был вызван start(), но stop() — никогда.
    После того как все циклы отработали, ``live`` ДОЛЖЕН быть пуст.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started: list[int] = []
        self.stopped: list[int] = []
        self.live: set[int] = set()
        self._seq = 0

    def next_id(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def mark_start(self, obj_id: int) -> None:
        with self._lock:
            self.started.append(obj_id)
            self.live.add(obj_id)

    def mark_stop(self, obj_id: int) -> None:
        with self._lock:
            self.stopped.append(obj_id)
            self.live.discard(obj_id)


class _FakeRTPartial:
    def __init__(self, registry: _LifecycleRegistry, **kwargs: Any) -> None:
        self._registry = registry
        self._id = registry.next_id()
        self._started = False

    def start(self, session_id: str = "", sample_rate: int = 16000) -> None:
        # Небольшая задержка расширяет окно гонки публикации handle'а.
        self._registry.mark_start(self._id)
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._registry.mark_stop(self._id)
            self._started = False


class _FakeRSF:
    def __init__(self, registry: _LifecycleRegistry, **kwargs: Any) -> None:
        self._registry = registry
        self._id = registry.next_id()
        self._started = False

    def start(self) -> None:
        self._registry.mark_start(self._id)
        self._started = True

    def stop(self) -> list:
        if self._started:
            self._registry.mark_stop(self._id)
            self._started = False
        return []

    @property
    def is_running(self) -> bool:
        # hardening 2026-07-20: phase_a проверяет is_running после stop();
        # без атрибута AttributeError трактовался как «не остановился» и
        # фильтр возвращался в слот — ассерты «cleared after stop» краснели.
        return self._started


class _FakeRecorder:
    """Старт всегда успешен; стоп возвращает None → phase_a берёт early_return
    сразу после locked rt/rsf-stop, минуя тяжёлый STT-пайплайн."""

    sample_rate = 16000

    def __init__(self) -> None:
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> bool:
        self._recording = True
        return True

    def stop(self, trim_tail_ms: int = 0):
        self._recording = False
        return None  # → already_stopped early_return


class _FakeSettingsSvc:
    def __init__(self, settings: dict[str, Any]) -> None:
        self._settings = settings

    def cached_settings(self) -> dict[str, Any]:
        return self._settings


class _FakeSessionTracker:
    _active_session = None

    def start_session(self, **kwargs: Any) -> None:
        return None

    def end_session(self, **kwargs: Any) -> None:
        return None


def _make_service(settings: dict[str, Any]) -> RecordingCoreService:
    return RecordingCoreService(
        recorder=_FakeRecorder(),
        transcriber=mock.Mock(),
        translator=mock.Mock(),
        store=mock.Mock(),
        vocabulary=mock.Mock(),
        settings_svc=_FakeSettingsSvc(settings),
        llm_rewriter=mock.Mock(),
        auto_glossary=mock.Mock(),
        semantic_searcher=mock.Mock(),
        context_memory=mock.Mock(),
        clipboard_history=[],
        auto_backup=mock.Mock(),
        session_tracker=_FakeSessionTracker(),
        action_items_extractor=mock.Mock(),
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )


class TestRecordingLifecycleRace(unittest.TestCase):
    """A1: конкурентные start/stop не оставляют осиротевших daemon'ов."""

    def _settings(self) -> dict[str, Any]:
        return {
            # включаем оба daemon'а, выключаем preview-воркер (чтобы не плодить треды),
            # privacy OFF (иначе rt_partial не стартует вовсе).
            "realtime_partial_enabled": True,
            "realtime_silence_filter_enabled": True,
            "realtime_preview_enabled": False,
            "privacy_mode_enabled": False,
            "llm_brain_model": "",  # отключает LM Studio unload/preload hook
            "rt_partial_interval_sec": 3.0,
            "rt_partial_buffer_sec": 8.0,
        }

    def test_rt_lock_exists(self) -> None:
        svc = _make_service(self._settings())
        # Реальный lock-объект (не None) — общий guard для _rt_partial/_rsf.
        self.assertTrue(hasattr(svc, "_rt_lock"))
        self.assertTrue(hasattr(svc._rt_lock, "acquire"))
        self.assertTrue(hasattr(svc._rt_lock, "release"))

    def test_no_orphaned_daemon_under_concurrency(self) -> None:
        registry = _LifecycleRegistry()
        svc = _make_service(self._settings())

        def _rt_factory(**kwargs: Any) -> _FakeRTPartial:
            return _FakeRTPartial(registry, **kwargs)

        def _rsf_factory(**kwargs: Any) -> _FakeRSF:
            return _FakeRSF(registry, **kwargs)

        errors: list[BaseException] = []
        err_lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker() -> None:
            try:
                barrier.wait()
                for _ in range(40):
                    svc.handle_start_recording({})
                    svc._stop_recording_phase_a({}, svc._settings_svc.cached_settings())
            except BaseException as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(exc)

        with mock.patch.object(rcs_mod, "RealtimePartialTranscriber", _rt_factory), \
                mock.patch.object(rcs_mod, "RealtimeSilenceFilter", _rsf_factory):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        self.assertEqual(errors, [], msg=f"worker errors: {errors}")
        # Финальный стоп уже отработал в каждом цикле → ни одного живого daemon'а.
        self.assertEqual(
            registry.live,
            set(),
            msg=f"осиротевшие (started-but-never-stopped) daemon'ы: {registry.live}",
        )
        # Инвариант баланса: сколько стартовали — столько и остановили.
        self.assertEqual(len(registry.started), len(registry.stopped))
        # Поля очищены после последнего стопа.
        self.assertIsNone(svc._rt_partial)
        self.assertIsNone(svc._rsf)

    def test_start_failure_clears_handle_under_lock(self) -> None:
        # Если start() падает, handle обнуляется (в except-ветке под локом) — не публикуется.
        svc = _make_service(self._settings())

        class _BrokenRT:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def start(self, **kwargs: Any) -> None:
                raise RuntimeError("boom")

            def stop(self) -> None:
                pass

        with mock.patch.object(rcs_mod, "RealtimePartialTranscriber", _BrokenRT), \
                mock.patch.object(rcs_mod, "RealtimeSilenceFilter", lambda **kw: _FakeRSF(_LifecycleRegistry(), **kw)):
            svc.handle_start_recording({})

        # rt_partial не опубликован (остался None), несмотря на сбой старта.
        self.assertIsNone(svc._rt_partial)


if __name__ == "__main__":
    unittest.main()
