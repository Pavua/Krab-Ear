"""W1769 tests: BulkReprocessor in-progress guard + cancel-safe re-entry (MED concurrency bug).

Контекст бага:
    BackendService держит ОДИН общий singleton self._bulk_reprocessor (service.py:530),
    а IPC — thread-per-connection, причём bulk_reprocess_start НЕ в HEAVY_METHODS
    (light-лимит 120/мин). Без guard два клиента могли запустить два reprocess()-цикла по
    ОДНОМУ набору кандидатов: (a) дубль MLX-транскрибаций + гонка last-writer-wins на
    update_history_item_text для одного id; (b) сломанная отмена — re-entry второго запуска
    вызывал _reset_cancel()/clear() и стирал pending cancel первого, либо один cancel()
    обрывал ОБА прохода.

Покрытие:
    - Повторный reprocess() пока один «выполняется» → возвращает already-running error
      БЕЗ старта второго цикла (симулируется удержанием _run_lock / блокировкой work-loop).
    - cancel(), выданный активному запуску, чтится и НЕ стирается re-entry-попыткой.
    - _reset_cancel() — no-op при активном цикле; is_running() корректен.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.bulk_reprocess import BulkReprocessor


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_bulk_reprocess.py)
# ---------------------------------------------------------------------------

def _make_item_dict(
    item_id: str,
    text: str = "Привет мир",
    confidence: float = 0.4,
    audio_path: str = "",
    is_protected: bool = False,
    ts: str | None = None,
) -> dict:
    from datetime import datetime, timezone, timedelta
    if ts is None:
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    return {
        "id": item_id,
        "ts": ts,
        "text": text,
        "paste_status": "ok",
        "source_text": "",
        "translated_text": "",
        "translation_mode": "off",
        "source_lang": "ru",
        "target_lang": "",
        "translation_status": "not_requested",
        "translation_engine": "",
        "chat_id": "",
        "message_id": "",
        "cleaned_text": "",
        "llm_applied": False,
        "llm_latency_ms": 0,
        "diarization": None,
        "audio_duration_sec": None,
        "confidence": confidence,
        "tags": [],
        "favorite": False,
        "emotion": None,
        "word_timestamps": None,
        "speaker_turns": None,
        "reasoning": None,
        "audio_path": audio_path,
        "is_protected": is_protected,
    }


def _make_store_mock(items: list[dict]) -> MagicMock:
    from backend.models import HistoryItem
    store = MagicMock()
    history_items = [HistoryItem.from_dict(d) for d in items]
    store._load_active_items_unlocked = MagicMock(return_value=history_items)
    store._lock = MagicMock(return_value=contextlib.nullcontext())
    store.update_history_item_text = MagicMock(return_value=True)
    return store


def _make_transcriber_mock(text: str = "Улучшенный текст", confidence: float = 0.9) -> MagicMock:
    t = MagicMock()
    t.transcribe = MagicMock(return_value={"text": text, "confidence": confidence})
    return t


def _make_version_manager_mock() -> MagicMock:
    vm = MagicMock()
    vm.save_version = MagicMock(return_value={"version_num": 1})
    return vm


# ---------------------------------------------------------------------------
# 1. In-progress guard: second reprocess() bails out without starting a loop
# ---------------------------------------------------------------------------

class TestReprocessInProgressGuard(unittest.TestCase):
    """W1769: a second reprocess() while one is running returns already-running error."""

    def test_second_call_returns_already_running_error_when_lock_held(self):
        """Holding _run_lock (simulating an active run) makes reprocess() refuse instantly."""
        store = _make_store_mock([_make_item_dict("id1", confidence=0.3)])
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)

        # Simulate an in-progress run by holding the guard lock.
        acquired = br._run_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            result = br.reprocess(dry_run=True)
        finally:
            br._run_lock.release()

        # Must report the already-running error and start NO second loop.
        self.assertEqual(result.get("error"), "bulk_reprocess already running")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["reprocessed"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["errors"], [])
        self.assertFalse(result["cancelled"])
        # The second (refused) call must not touch the store or transcriber.
        store._load_active_items_unlocked.assert_not_called()
        transcriber.transcribe.assert_not_called()
        store.update_history_item_text.assert_not_called()

    def test_concurrent_runs_only_one_executes_loop(self):
        """When two reprocess() overlap on the singleton, exactly one runs the loop."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_make_item_dict("id1", confidence=0.3, audio_path=audio_path)]
            store = _make_store_mock(items)
            vm = _make_version_manager_mock()

            in_loop = threading.Event()
            release_loop = threading.Event()

            def blocking_transcribe(*a, **kw):
                # Signal we are inside the real loop, then hold until released so the
                # second reprocess() definitely overlaps with the first.
                in_loop.set()
                release_loop.wait(timeout=3.0)
                return {"text": "Новый текст", "confidence": 0.95}

            transcriber = MagicMock()
            # MagicMock(side_effect=...) keeps call_count while still blocking.
            transcriber.transcribe = MagicMock(side_effect=blocking_transcribe)

            results: dict[str, dict] = {}

            def _run_first():
                with patch.object(BulkReprocessor, "_load_audio", return_value="audio_array"):
                    results["first"] = br.reprocess(only_low_confidence=True, threshold=0.7)

            br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)

            t1 = threading.Thread(target=_run_first, daemon=True)
            t1.start()

            # Wait until run #1 is parked inside transcribe (loop active, lock held).
            self.assertTrue(in_loop.wait(timeout=3.0), "first run never entered loop")

            # Second concurrent call must be refused immediately.
            second = br.reprocess(dry_run=True)
            self.assertEqual(second.get("error"), "bulk_reprocess already running")

            # Let run #1 finish.
            release_loop.set()
            t1.join(timeout=5.0)
            self.assertFalse(t1.is_alive())

            first = results["first"]
            self.assertIsNone(first.get("error"))
            self.assertEqual(first["reprocessed"], 1)
            # Exactly one real transcription happened (no duplicate work).
            self.assertEqual(transcriber.transcribe.call_count, 1)
            store.update_history_item_text.assert_called_once()
        finally:
            os.unlink(audio_path)

    def test_lock_released_after_run_allows_subsequent_call(self):
        """After a run finishes the guard is released; a later reprocess() runs normally."""
        store = _make_store_mock([])  # empty — nothing to process
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)

        r1 = br.reprocess(dry_run=True)
        self.assertIsNone(r1.get("error"))
        self.assertFalse(br.is_running())

        # Second, sequential call must NOT be refused (lock was released in finally).
        r2 = br.reprocess(dry_run=True)
        self.assertIsNone(r2.get("error"))
        self.assertEqual(r2["total"], 0)

    def test_lock_released_even_if_run_raises(self):
        """If the run body raises, the guard lock is still released (finally)."""
        store = _make_store_mock([_make_item_dict("id1", confidence=0.3)])
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)

        with patch.object(BulkReprocessor, "_run_locked", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                br.reprocess(dry_run=True)

        # Lock must be free again — otherwise the singleton would be permanently jammed.
        self.assertFalse(br.is_running())
        self.assertTrue(br._run_lock.acquire(blocking=False))
        br._run_lock.release()


# ---------------------------------------------------------------------------
# 2. Cancel safety: active-run cancel is honored, not wiped by re-entry
# ---------------------------------------------------------------------------

class TestReprocessCancelSafetyUnderGuard(unittest.TestCase):
    """W1769: cancel for an active run survives a concurrent re-entry attempt."""

    def test_cancel_active_run_not_wiped_by_reentry(self):
        """cancel() mid-run is honored; a second reprocess() must not clear it."""
        tmpdir = tempfile.mkdtemp()
        try:
            n = 4
            paths = []
            for i in range(n):
                p = os.path.join(tmpdir, f"x{i}.wav")
                open(p, "wb").close()
                paths.append(p)

            items = [_make_item_dict(f"cid{i}", confidence=0.2, audio_path=paths[i]) for i in range(n)]
            store = _make_store_mock(items)
            vm = _make_version_manager_mock()

            in_loop = threading.Event()
            release_first_item = threading.Event()

            def slow_transcribe(*a, **kw):
                # Park inside the FIRST item so cancel + re-entry race the active run.
                in_loop.set()
                release_first_item.wait(timeout=3.0)
                return {"text": "OK", "confidence": 0.9}

            transcriber = MagicMock()
            transcriber.transcribe = slow_transcribe

            br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)

            results: dict[str, dict] = {}

            def _run_first():
                with patch.object(BulkReprocessor, "_load_audio", return_value="audio_array"):
                    results["first"] = br.reprocess(only_low_confidence=True, threshold=0.7)

            t1 = threading.Thread(target=_run_first, daemon=True)
            t1.start()
            self.assertTrue(in_loop.wait(timeout=3.0), "first run never entered loop")

            # (a) Request cancel for the still-active run #1.
            br.cancel()
            self.assertTrue(br._cancel_event.is_set())

            # (b) A concurrent second reprocess() must be refused AND must not clear
            #     run #1's pending cancel (the original broken-cancel bug).
            second = br.reprocess(only_low_confidence=True, threshold=0.7)
            self.assertEqual(second.get("error"), "bulk_reprocess already running")
            self.assertTrue(
                br._cancel_event.is_set(),
                "re-entry wiped the active run's pending cancel (regression)",
            )

            # Let run #1 continue; it must observe the cancel before the next item.
            release_first_item.set()
            t1.join(timeout=5.0)
            self.assertFalse(t1.is_alive())

            first = results["first"]
            self.assertTrue(first["cancelled"], "active run's cancel was not honored")
            # It stopped early: not all n items were processed.
            self.assertLess(first["reprocessed"], n)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_reset_cancel_is_noop_while_running(self):
        """_reset_cancel() must NOT clear cancel while a run holds _run_lock."""
        store = _make_store_mock([])
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)

        # Simulate active run by holding the lock, then set a pending cancel.
        self.assertTrue(br._run_lock.acquire(blocking=False))
        try:
            br.cancel()
            self.assertTrue(br._cancel_event.is_set())
            self.assertTrue(br.is_running())

            # A stray _reset_cancel() (e.g. from a re-entry path) must be a no-op now.
            br._reset_cancel()
            self.assertTrue(
                br._cancel_event.is_set(),
                "_reset_cancel cleared cancel of an active run",
            )
        finally:
            br._run_lock.release()

        # Once not running, _reset_cancel() clears as before (fresh-run semantics).
        self.assertFalse(br.is_running())
        br._reset_cancel()
        self.assertFalse(br._cancel_event.is_set())

    def test_is_running_reflects_lock_state(self):
        """is_running() is True only while the guard lock is held."""
        store = _make_store_mock([])
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)

        self.assertFalse(br.is_running())
        self.assertTrue(br._run_lock.acquire(blocking=False))
        try:
            self.assertTrue(br.is_running())
        finally:
            br._run_lock.release()
        self.assertFalse(br.is_running())


# ---------------------------------------------------------------------------
# 3. Status/cancel handler contract preserved
# ---------------------------------------------------------------------------

class TestReprocessStatusContractPreserved(unittest.TestCase):
    """_handle_bulk_reprocess_status reads _cancel_event.is_set() — keep it working."""

    def test_cancel_then_status_flag_visible(self):
        """cancel() sets _cancel_event; a fresh run resets it (status contract intact)."""
        store = _make_store_mock([])
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)

        # No run active: cancel sets the flag, status would report True.
        br.cancel()
        self.assertTrue(br._cancel_event.is_set())

        # A new (sequential) run resets it at start and finishes with cancelled=False.
        result = br.reprocess(dry_run=True)
        self.assertIsNone(result.get("error"))
        self.assertFalse(result["cancelled"])
        self.assertFalse(br._cancel_event.is_set())


if __name__ == "__main__":
    unittest.main()
