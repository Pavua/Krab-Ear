"""W1043 tests: BulkReprocessor recording guard + explicit mlx_lock (W1037 F1+F2 HIGH).

Tests:
    - test_reprocess_refused_while_recording   (F1: RuntimeError when recording active)
    - test_reprocess_allowed_when_not_recording (F1: passes through when not recording)
    - test_reprocess_no_guard_fn_proceeds       (F1: backward compat — no fn injected)
    - F2 пересмотрен 02.09.2026: внешний захват mlx_lock снят как самоблокировка
      (см. tests/test_bulk_reprocess_mlx_self_block_2026_09_02.py)
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

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
# F2 (пересмотрен 02.09.2026): внешнего захвата mlx_lock больше НЕТ
#
# Исходные тесты закрепляли ИМЕННО место захвата — `with mlx_inter_process_lock(),
# mlx_lock():` вокруг transcriber.transcribe(). Этот захват оказался
# самоблокировкой: engine.transcribe отдаёт работу в ThreadPoolExecutor, а поток
# пула берёт ТОТ ЖЕ mlx_lock (RLock реентерабелен только для своего потока).
# Инвариант «MLX-инференс под локом» держат сами MLX-пути (_transcribe_model,
# GigaAM-MLX/parakeet/voxtral-адаптеры). Разбор и регресс-тесты:
# tests/test_bulk_reprocess_mlx_self_block_2026_09_02.py.
# ---------------------------------------------------------------------------

class TestDryRunDoesNotTranscribe(unittest.TestCase):
    """dry_run планирует работу, но не запускает STT."""

    def test_dry_run_skips_transcription(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name

        try:
            items = [_make_item_dict("id1", confidence=0.4, audio_path=audio_path)]
            store = _make_store_mock(items)
            transcriber = _make_transcriber_mock()
            vm = _make_version_manager_mock()

            br = BulkReprocessor(store=store, transcriber=transcriber, version_manager=vm)
            result = br.reprocess(dry_run=True)

            transcriber.transcribe.assert_not_called()
            self.assertEqual(result["reprocessed"], 1)
        finally:
            os.unlink(audio_path)


# ---------------------------------------------------------------------------
# Guard срабатывает раньше любой работы
# ---------------------------------------------------------------------------

class TestRecordingGuardTakesPrecedenceOverWork(unittest.TestCase):
    """Проверка записи выполняется до любого доступа к хранилищу и к STT."""

    def test_guard_fires_before_store_access(self):
        """When recording active, store is never accessed (no partial state)."""
        store = _make_store_mock([])
        transcriber = _make_transcriber_mock()
        vm = _make_version_manager_mock()

        br = BulkReprocessor(
            store=store,
            transcriber=transcriber,
            version_manager=vm,
            is_recording_fn=lambda: True,
        )
        with self.assertRaises(RuntimeError):
            br.reprocess()

        transcriber.transcribe.assert_not_called()
        # Store must not have been accessed
        store._lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
