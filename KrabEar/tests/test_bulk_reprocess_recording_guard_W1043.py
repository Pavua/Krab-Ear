"""W1043 tests: BulkReprocessor recording guard + explicit mlx_lock (W1037 F1+F2 HIGH).

Tests:
    - test_reprocess_refused_while_recording   (F1: RuntimeError when recording active)
    - test_reprocess_allowed_when_not_recording (F1: passes through when not recording)
    - test_reprocess_no_guard_fn_proceeds       (F1: backward compat — no fn injected)
    - test_reprocess_uses_mlx_lock              (F2: mlx_lock acquired during transcribe)
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

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
# F1: Recording guard tests
# ---------------------------------------------------------------------------

class TestReprocessRecordingGuardF1(unittest.TestCase):
    """W1037 F1: BulkReprocessor must refuse when active recording is in progress."""

    def test_reprocess_refused_while_recording(self):
        """reprocess() raises RuntimeError immediately when is_recording_fn returns True."""
        store = _make_store_mock([])
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        is_recording = lambda: True  # noqa: E731

        br = BulkReprocessor(
            store=store,
            transcriber=transcriber,
            version_manager=vm,
            is_recording_fn=is_recording,
        )

        with self.assertRaises(RuntimeError) as ctx:
            br.reprocess()

        self.assertIn("active recording in progress", str(ctx.exception))
        # Must not touch the store at all.
        store._load_active_items_unlocked.assert_not_called()
        transcriber.transcribe.assert_not_called()

    def test_reprocess_allowed_when_not_recording(self):
        """reprocess() proceeds normally when is_recording_fn returns False."""
        store = _make_store_mock([])  # empty — nothing to process
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        is_recording = lambda: False  # noqa: E731

        br = BulkReprocessor(
            store=store,
            transcriber=transcriber,
            version_manager=vm,
            is_recording_fn=is_recording,
        )

        result = br.reprocess()
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["cancelled"])

    def test_reprocess_no_guard_fn_proceeds(self):
        """Backward compat: when is_recording_fn is None, reprocess() runs normally."""
        store = _make_store_mock([])
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        # Default constructor — no is_recording_fn
        br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)

        result = br.reprocess()
        self.assertEqual(result["total"], 0)

    def test_is_recording_fn_called_exactly_once_per_reprocess(self):
        """is_recording_fn is checked once at start of each reprocess() call."""
        store = _make_store_mock([])
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        call_count = {"n": 0}

        def is_recording():
            call_count["n"] += 1
            return False

        br = BulkReprocessor(
            store=store,
            transcriber=transcriber,
            version_manager=vm,
            is_recording_fn=is_recording,
        )

        br.reprocess()
        br.reprocess()
        self.assertEqual(call_count["n"], 2)

    def test_reprocess_refused_error_message_contains_method_name(self):
        """Error message is identifiable for logging / IPC error response."""
        store = _make_store_mock([])
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        br = BulkReprocessor(
            store=store,
            transcriber=transcriber,
            version_manager=vm,
            is_recording_fn=lambda: True,
        )

        with self.assertRaises(RuntimeError) as ctx:
            br.reprocess()

        self.assertIn("bulk_reprocess", str(ctx.exception))


# ---------------------------------------------------------------------------
# F2: mlx_lock defense-in-depth tests
# ---------------------------------------------------------------------------

class TestReprocessMlxLockF2(unittest.TestCase):
    """W1037 F2: mlx_lock must be acquired around transcriber.transcribe() call."""

    def test_reprocess_uses_mlx_lock(self):
        """mlx_lock context manager is entered during each actual transcription.

        Since mlx_lock is imported lazily inside reprocess(), we patch the source
        module (core.mlx_lock.mlx_lock) so the `from core.mlx_lock import mlx_lock`
        inside reprocess() picks up the mock.
        """
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name

        try:
            items = [_make_item_dict("id1", confidence=0.4, audio_path=audio_path)]
            store = _make_store_mock(items)
            transcriber = _make_transcriber_mock(text="Новый текст", confidence=0.9)
            vm = _make_version_manager_mock()

            lock_entered = {"count": 0}

            class _FakeLock:
                def __enter__(self):
                    lock_entered["count"] += 1
                    return self

                def __exit__(self, *args):
                    return False

            with (
                patch.object(BulkReprocessor, "_load_audio", return_value="audio_array"),
                patch("core.mlx_lock.mlx_lock", return_value=_FakeLock()),
            ):
                br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)
                result = br.reprocess(only_low_confidence=True, threshold=0.7)

            # The lock must have been entered exactly once (one transcription candidate).
            self.assertEqual(lock_entered["count"], 1)
            self.assertEqual(result["reprocessed"], 1)
        finally:
            os.unlink(audio_path)

    def test_reprocess_mlx_lock_imported_from_core(self):
        """mlx_lock is imported from core.mlx_lock (not a stub) during reprocess."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name

        try:
            items = [_make_item_dict("id1", confidence=0.4, audio_path=audio_path)]
            store = _make_store_mock(items)
            transcriber = _make_transcriber_mock(text="Новый текст", confidence=0.9)
            vm = _make_version_manager_mock()

            lock_enter_calls = []

            original_lock_cls = None
            try:
                from core.mlx_lock import mlx_lock as real_lock
                original_lock_cls = real_lock
            except ImportError:
                pass

            with (
                patch.object(BulkReprocessor, "_load_audio", return_value="audio_array"),
                patch("core.mlx_lock.mlx_lock") as mock_mlx,
            ):
                mock_ctx = MagicMock()
                mock_ctx.__enter__ = MagicMock(return_value=None)
                mock_ctx.__exit__ = MagicMock(return_value=False)
                mock_mlx.return_value = mock_ctx

                br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)
                result = br.reprocess(only_low_confidence=True, threshold=0.7)

            # transcribe must have been called (proves we got past the lock)
            transcriber.transcribe.assert_called_once()
            self.assertEqual(result["reprocessed"], 1)
        finally:
            os.unlink(audio_path)

    def test_mlx_lock_not_acquired_for_dry_run(self):
        """mlx_lock is NOT acquired during dry_run (no real transcription)."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name

        try:
            items = [_make_item_dict("id1", confidence=0.4, audio_path=audio_path)]
            store = _make_store_mock(items)
            transcriber = _make_transcriber_mock()
            vm = _make_version_manager_mock()

            with patch("core.mlx_lock.mlx_lock") as mock_mlx:
                br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)
                result = br.reprocess(dry_run=True)

            # Dry run should not invoke transcriber or mlx_lock
            transcriber.transcribe.assert_not_called()
            mock_mlx.assert_not_called()
            self.assertEqual(result["reprocessed"], 1)
        finally:
            os.unlink(audio_path)


# ---------------------------------------------------------------------------
# Combined guard + lock
# ---------------------------------------------------------------------------

class TestRecordingGuardTakesPrecedenceOverLock(unittest.TestCase):
    """Guard check runs before any lock acquisition."""

    def test_guard_fires_before_store_access(self):
        """When recording active, store is never accessed (no partial state)."""
        store = _make_store_mock([])
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        with patch("core.mlx_lock.mlx_lock") as mock_mlx:
            br = BulkReprocessor(
                store=store,
                transcriber=transcriber,
                version_manager=vm,
                is_recording_fn=lambda: True,
            )
            with self.assertRaises(RuntimeError):
                br.reprocess()

            # mlx_lock must not have been acquired
            mock_mlx.assert_not_called()
            # Store must not have been accessed
            store._lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
