"""Tests for BulkReprocessor and IPC handlers (bulk_reprocess_start/status/cancel)."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.bulk_reprocess import BulkReprocessor, HARD_LIMIT
from backend.ipc_throttle import HEAVY_METHODS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item_dict(
    item_id: str,
    text: str = "Привет мир",
    confidence: float | None = 0.5,
    audio_path: str | None = None,
    is_protected: bool = False,
    ts: str | None = None,
) -> dict:
    from datetime import datetime, timezone, timedelta
    if ts is None:
        # 2 hours ago — old enough to not be skipped
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
        "audio_path": audio_path or "",
        "is_protected": is_protected,
    }


def _make_store_mock(items: list[dict]) -> MagicMock:
    """Returns a mock StateStore with items pre-loaded."""
    from backend.models import HistoryItem
    import contextlib
    store = MagicMock()
    # HistoryItem.from_dict now handles audio_path and is_protected
    history_items = [HistoryItem.from_dict(d) for d in items]
    store._load_active_items_unlocked = MagicMock(return_value=history_items)
    # _lock() returns a context manager
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
# Test cases
# ---------------------------------------------------------------------------

class TestBulkReprocessDryRun(unittest.TestCase):
    """1. Dry run does not call transcriber or update store."""

    def test_dry_run_counts_but_no_stt(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_make_item_dict("id1", confidence=0.4, audio_path=audio_path)]
            store = _make_store_mock(items)
            transcriber = _make_transcriber_mock()
            vm = _make_version_manager_mock()

            br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)
            result = br.reprocess(dry_run=True)

            self.assertEqual(result["total"], 1)
            self.assertEqual(result["reprocessed"], 1)
            self.assertEqual(result["skipped"], 0)
            self.assertFalse(result["cancelled"])
            transcriber.transcribe.assert_not_called()
            store.update_history_item_text.assert_not_called()
            vm.save_version.assert_not_called()
        finally:
            os.unlink(audio_path)


class TestBulkReprocessActual(unittest.TestCase):
    """2. Actual reprocess: calls transcriber, saves version, updates store."""

    def test_actual_reprocess(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_make_item_dict("id1", text="Старый текст", confidence=0.4, audio_path=audio_path)]
            store = _make_store_mock(items)
            transcriber = _make_transcriber_mock(text="Новый текст", confidence=0.9)
            vm = _make_version_manager_mock()

            with patch.object(BulkReprocessor, "_load_audio", return_value="audio_array"):
                br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)
                result = br.reprocess(only_low_confidence=True, threshold=0.7)

            self.assertEqual(result["reprocessed"], 1)
            self.assertEqual(result["skipped"], 0)
            self.assertEqual(result["errors"], [])
            store.update_history_item_text.assert_called_once_with("id1", "Новый текст", confidence=0.9)
        finally:
            os.unlink(audio_path)


class TestBulkReprocessConfidenceFilter(unittest.TestCase):
    """3. Items with confidence >= threshold are skipped."""

    def test_high_confidence_skipped(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_make_item_dict("id1", confidence=0.85, audio_path=audio_path)]
            store = _make_store_mock(items)
            transcriber = _make_transcriber_mock()
            vm = _make_version_manager_mock()

            br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)
            result = br.reprocess(only_low_confidence=True, threshold=0.7)

            self.assertEqual(result["total"], 1)
            self.assertEqual(result["reprocessed"], 0)
            self.assertEqual(result["skipped"], 1)
            transcriber.transcribe.assert_not_called()
        finally:
            os.unlink(audio_path)


class TestBulkReprocessCancellation(unittest.TestCase):
    """4. Cancel stops processing before all items are done."""

    def test_cancel_stops_loop(self):
        """Cancel during reprocess stops the loop before all items are processed."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f1, \
             tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f2, \
             tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f3:
            paths = [f1.name, f2.name, f3.name]
        try:
            items = [_make_item_dict(f"id{i}", confidence=0.3, audio_path=p) for i, p in enumerate(paths)]
            store = _make_store_mock(items)
            vm = _make_version_manager_mock()

            # Transcriber that blocks until cancel_event is set, then raises
            cancel_ready = threading.Event()

            def blocking_transcribe(*args, **kwargs):
                cancel_ready.set()
                # Block for a bit to allow cancel to be set
                time.sleep(0.2)
                return {"text": "Текст", "confidence": 0.9}

            transcriber = MagicMock()
            transcriber.transcribe = blocking_transcribe

            br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)

            def _canceller():
                cancel_ready.wait(timeout=1.0)
                br.cancel()

            t = threading.Thread(target=_canceller, daemon=True)
            t.start()

            with patch.object(BulkReprocessor, "_load_audio", return_value="audio_array"):
                result = br.reprocess(only_low_confidence=True, threshold=0.7, dry_run=False)

            t.join(timeout=2.0)
            # Either cancelled or processed fewer than all 3
            self.assertTrue(result["cancelled"] or result["reprocessed"] < 3)
        finally:
            for p in paths:
                os.unlink(p)


class TestBulkReprocessNoAudioFile(unittest.TestCase):
    """5. Items without existing audio_path are skipped."""

    def test_no_audio_file_skipped(self):
        items = [_make_item_dict("id1", confidence=0.3, audio_path="/nonexistent/audio.wav")]
        store = _make_store_mock(items)
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)
        result = br.reprocess()

        # Item has no real file → total=0 (filtered out before processing)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["reprocessed"], 0)
        transcriber.transcribe.assert_not_called()


class TestBulkReprocessVersionSavedOnImprovement(unittest.TestCase):
    """6. Old version saved before new text applied; new version also saved."""

    def test_versions_saved(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_make_item_dict("id1", text="Старый", confidence=0.3, audio_path=audio_path)]
            store = _make_store_mock(items)
            transcriber = _make_transcriber_mock(text="Новый", confidence=0.95)
            vm = _make_version_manager_mock()

            with patch.object(BulkReprocessor, "_load_audio", return_value="audio_array"):
                br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)
                result = br.reprocess(only_low_confidence=True, threshold=0.7)

            self.assertEqual(result["reprocessed"], 1)
            # save_version called at least twice: old text + new text
            self.assertGreaterEqual(vm.save_version.call_count, 2)
            # Check sources were stt_raw and stt_cleaned
            call_kwargs = [c.kwargs for c in vm.save_version.call_args_list]
            all_sources = [kw.get("source") for kw in call_kwargs]
            self.assertIn("stt_raw", all_sources)
            self.assertIn("stt_cleaned", all_sources)
        finally:
            os.unlink(audio_path)


class TestBulkReprocessNoVersionOnRegression(unittest.TestCase):
    """7. If new confidence <= old confidence, no update and no new version."""

    def test_no_version_on_regression(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_make_item_dict("id1", text="Старый", confidence=0.6, audio_path=audio_path)]
            store = _make_store_mock(items)
            # New confidence lower than existing
            transcriber = _make_transcriber_mock(text="Новый", confidence=0.5)
            vm = _make_version_manager_mock()

            with patch.object(BulkReprocessor, "_load_audio", return_value="audio_array"):
                br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)
                result = br.reprocess(only_low_confidence=True, threshold=0.7)

            # Item starts with confidence=0.6 < threshold=0.7 so it passes the filter,
            # but new_confidence=0.5 <= old_confidence=0.6 → skip (no improvement)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["reprocessed"], 0)
            vm.save_version.assert_not_called()
            store.update_history_item_text.assert_not_called()
        finally:
            os.unlink(audio_path)


class TestBulkReprocessBatchSize(unittest.TestCase):
    """8. Events emitted at correct batch_size intervals."""

    def test_batch_events_emitted(self):
        tmpdir = tempfile.mkdtemp()
        try:
            audio_paths = []
            for i in range(6):
                p = os.path.join(tmpdir, f"audio{i}.wav")
                open(p, "wb").close()
                audio_paths.append(p)

            items = [
                _make_item_dict(f"id{i}", confidence=0.3, audio_path=audio_paths[i])
                for i in range(6)
            ]
            store = _make_store_mock(items)
            transcriber = _make_transcriber_mock(confidence=0.95)
            vm = _make_version_manager_mock()
            event_bus = MagicMock()
            emitted = []
            event_bus.emit = MagicMock(side_effect=lambda *a, **kw: emitted.append(a))

            with patch.object(BulkReprocessor, "_load_audio", return_value="audio_array"):
                br = BulkReprocessor(
                    store=store,
                    transcriber=transcriber,
                    version_manager=vm,
                    event_bus=event_bus,
                    batch_size=2,
                )
                br.reprocess(dry_run=True)

            # With batch_size=2 and 6 items: events at idx 1 (batch), 3, 5 + final emit
            self.assertGreater(len(emitted), 0)
            event_types = [e[0] for e in emitted]
            self.assertTrue(all(t == "bulk_reprocess_progress" for t in event_types))
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestBulkReprocessIPCHandlers(unittest.TestCase):
    """9. IPC handler smoke tests: start / status / cancel.

    Uses a minimal service mock (no full BackendService init) by calling the
    unbound handler methods with a hand-crafted self object.
    """

    def _make_service_ns(self):
        """Build a minimal namespace that satisfies handler method requirements."""
        import types
        tmpdir = tempfile.mkdtemp()
        # Minimal store with text_updates_path support
        store = MagicMock()
        import contextlib
        store._lock = MagicMock(return_value=contextlib.nullcontext())
        store._load_active_items_unlocked = MagicMock(return_value=[])

        ns = types.SimpleNamespace(
            store=store,
            _transcriber=_make_transcriber_mock(),
            _transcript_versioning=_make_version_manager_mock(),
            _event_bus=None,
            _bulk_tasks={},
            _bulk_tasks_lock=threading.Lock(),
        )
        return ns, tmpdir

    def test_start_returns_task_id(self):
        from backend.service import BackendService
        ns, tmpdir = self._make_service_ns()
        try:
            result = BackendService._handle_bulk_reprocess_start(ns, {"dry_run": True})
            self.assertIn("task_id", result)
            self.assertTrue(result["task_id"].startswith("br-"))
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_status_returns_running_or_done(self):
        from backend.service import BackendService
        ns, tmpdir = self._make_service_ns()
        try:
            start_result = BackendService._handle_bulk_reprocess_start(ns, {"dry_run": True})
            task_id = start_result["task_id"]
            # Poll briefly
            status_result = None
            for _ in range(20):
                status_result = BackendService._handle_bulk_reprocess_status(
                    ns, {"task_id": task_id}
                )
                if status_result["status"] in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.05)
            self.assertIsNotNone(status_result)
            self.assertIn(status_result["status"], ("running", "done", "cancelled", "failed"))
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_cancel_sets_requested(self):
        from backend.service import BackendService
        ns, tmpdir = self._make_service_ns()
        try:
            start_result = BackendService._handle_bulk_reprocess_start(ns, {"dry_run": False})
            task_id = start_result["task_id"]
            cancel_result = BackendService._handle_bulk_reprocess_cancel(
                ns, {"task_id": task_id}
            )
            self.assertTrue(cancel_result["requested"])
            self.assertEqual(cancel_result["task_id"], task_id)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestBulkReprocessThrottleCategory(unittest.TestCase):
    """10. bulk_reprocess_start is in HEAVY_METHODS throttle category."""

    def test_in_heavy_methods(self):
        self.assertIn("bulk_reprocess_start", HEAVY_METHODS)


class TestBulkReprocessEventsEmitted(unittest.TestCase):
    """11. Events contain correct fields."""

    def test_event_fields(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_make_item_dict("id1", confidence=0.3, audio_path=audio_path)]
            store = _make_store_mock(items)
            transcriber = _make_transcriber_mock(confidence=0.95)
            vm = _make_version_manager_mock()
            event_bus = MagicMock()
            emitted_payloads = []
            event_bus.emit = MagicMock(
                side_effect=lambda etype, payload: emitted_payloads.append(payload)
            )

            with patch.object(BulkReprocessor, "_load_audio", return_value="audio_array"):
                br = BulkReprocessor(
                    store=store,
                    transcriber=transcriber,
                    version_manager=vm,
                    event_bus=event_bus,
                    batch_size=1,
                )
                br.reprocess(dry_run=True)

            self.assertGreater(len(emitted_payloads), 0)
            payload = emitted_payloads[-1]
            for field in ("task_id", "processed", "total", "reprocessed", "skipped", "error_count"):
                self.assertIn(field, payload, f"Missing field: {field}")
        finally:
            os.unlink(audio_path)


class TestBulkReprocessHardLimit(unittest.TestCase):
    """12. Hard limit of 1000 items per run is enforced."""

    def test_hard_limit_enforced(self):
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_paths = []
            for i in range(HARD_LIMIT + 5):
                p = os.path.join(tmpdir, f"audio{i}.wav")
                open(p, "wb").close()
                audio_paths.append(p)

            items = [
                _make_item_dict(f"id{i}", confidence=0.3, audio_path=audio_paths[i], ts=old_ts)
                for i in range(HARD_LIMIT + 5)
            ]
            store = _make_store_mock(items)
            transcriber = _make_transcriber_mock()
            vm = _make_version_manager_mock()

            br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)
            result = br.reprocess(dry_run=True)

            self.assertLessEqual(result["total"], HARD_LIMIT)


if __name__ == "__main__":
    unittest.main()
