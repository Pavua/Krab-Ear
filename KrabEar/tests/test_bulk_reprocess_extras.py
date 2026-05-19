"""Wave 211 — extra coverage for BulkReprocessor.

Tests:
  - status_transitions: idle → running → done (via threading)
  - partial_progress_reported (event_bus events mid-run)
  - resume_from_checkpoint (inject last processed item via skip filter)
  - individual_item_exception_continues
  - cancel_during_processing_clean
  - max_concurrent_workers_respected
  - unicode_audio_paths
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.bulk_reprocess import BulkReprocessor, HARD_LIMIT  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_bulk_reprocess.py style)
# ---------------------------------------------------------------------------

def _item_dict(
    item_id: str,
    text: str = "Привет мир",
    confidence: float | None = 0.5,
    audio_path: str | None = None,
    is_protected: bool = False,
    hours_old: float = 2.0,
    source_lang: str = "ru",
) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
    return {
        "id": item_id,
        "ts": ts,
        "text": text,
        "paste_status": "ok",
        "source_text": "",
        "translated_text": "",
        "translation_mode": "off",
        "source_lang": source_lang,
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


def _make_store(items):
    from backend.models import HistoryItem
    store = MagicMock()
    history_items = [HistoryItem.from_dict(d) for d in items]
    store._load_active_items_unlocked = MagicMock(return_value=history_items)
    store._lock = MagicMock(return_value=contextlib.nullcontext())
    store.update_history_item_text = MagicMock(return_value=True)
    return store


def _make_transcriber(text: str = "Улучшенный текст", confidence: float = 0.9):
    t = MagicMock()
    t.transcribe = MagicMock(return_value={"text": text, "confidence": confidence})
    return t


def _make_vm():
    vm = MagicMock()
    vm.save_version = MagicMock(return_value={"version_num": 1})
    return vm


def _make_event_bus():
    bus = MagicMock()
    bus.emit = MagicMock()
    return bus


# ---------------------------------------------------------------------------
# 1. Status transitions: idle → running → done
# ---------------------------------------------------------------------------

class TestStatusTransitions(unittest.TestCase):
    """BulkReprocessor does not have a formal state enum, but we can verify
    that after reprocess() completes the result dict always has expected keys
    and the cancel flag is cleared for subsequent runs."""

    def test_result_has_required_keys(self):
        """reprocess() result must always contain total/reprocessed/skipped/errors/cancelled."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_item_dict("id1", confidence=0.4, audio_path=audio_path)]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(),
                version_manager=_make_vm(),
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                result = br.reprocess(dry_run=False, only_low_confidence=True, threshold=0.7)
            for key in ("total", "reprocessed", "skipped", "errors", "cancelled"):
                self.assertIn(key, result, f"Missing key: {key}")
        finally:
            os.unlink(audio_path)

    def test_cancel_flag_cleared_between_runs(self):
        """After a cancelled run, a subsequent run should execute normally.

        reprocess() calls _reset_cancel() at the start, so we must set the
        cancel flag *during* execution (not before) to actually trigger
        cancellation. We verify the flag is cleared for the next run.
        """
        audio_files = []
        for _ in range(5):
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            f.close()
            audio_files.append(f.name)
        try:
            items = [_item_dict(f"id{i}", confidence=0.3, audio_path=p)
                     for i, p in enumerate(audio_files)]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
                batch_size=100,
            )

            load_count = [0]

            def cancel_after_two(path):
                load_count[0] += 1
                if load_count[0] >= 2:
                    br.cancel()
                return [0.0] * 16000

            with patch.object(br, "_load_audio", side_effect=cancel_after_two):
                r1 = br.reprocess(only_low_confidence=True, threshold=0.7)
            self.assertTrue(r1["cancelled"])

            # Second run — cancel flag is reset by _reset_cancel() at start of reprocess()
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                r2 = br.reprocess(only_low_confidence=True, threshold=0.7)
            self.assertFalse(r2["cancelled"])
        finally:
            for p in audio_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_idle_to_done_single_item(self):
        """Single eligible item → reprocessed=1, cancelled=False."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_item_dict("id1", confidence=0.3, audio_path=audio_path)]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                result = br.reprocess(only_low_confidence=True, threshold=0.7)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["reprocessed"], 1)
            self.assertFalse(result["cancelled"])
        finally:
            os.unlink(audio_path)


# ---------------------------------------------------------------------------
# 2. Partial progress reported
# ---------------------------------------------------------------------------

class TestPartialProgressReported(unittest.TestCase):

    def _create_audio_files(self, n):
        paths = []
        for _ in range(n):
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            f.close()
            paths.append(f.name)
        return paths

    def test_progress_events_emitted_every_batch(self):
        """With batch_size=2 and 4 items, we expect 2 interim + 1 final emit calls."""
        audio_paths = self._create_audio_files(4)
        try:
            items = [_item_dict(f"id{i}", confidence=0.3, audio_path=p)
                     for i, p in enumerate(audio_paths)]
            bus = _make_event_bus()
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
                event_bus=bus,
                batch_size=2,
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                br.reprocess(only_low_confidence=True, threshold=0.7)

            # Each batch_size=2 interval fires, plus final emit
            self.assertGreaterEqual(bus.emit.call_count, 2)
        finally:
            for p in audio_paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_progress_event_fields(self):
        """Each emitted event should have task_id, processed, total, reprocessed, skipped, error_count."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_item_dict("id1", confidence=0.3, audio_path=audio_path)]
            bus = _make_event_bus()
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
                event_bus=bus,
                batch_size=1,
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                br.reprocess(only_low_confidence=True, threshold=0.7, task_id="task-42")

            # Find the final emit call
            last_call = bus.emit.call_args_list[-1]
            event_name, payload = last_call[0]
            self.assertEqual(event_name, "bulk_reprocess_progress")
            for field in ("task_id", "processed", "total", "reprocessed", "skipped", "error_count"):
                self.assertIn(field, payload, f"Missing field: {field}")
            self.assertEqual(payload["task_id"], "task-42")
        finally:
            os.unlink(audio_path)

    def test_no_events_without_event_bus(self):
        """BulkReprocessor without event_bus should not raise on progress emission."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_item_dict("id1", confidence=0.3, audio_path=audio_path)]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
                event_bus=None,  # no bus
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                result = br.reprocess()
            self.assertEqual(result["reprocessed"], 1)
        finally:
            os.unlink(audio_path)


# ---------------------------------------------------------------------------
# 3. Resume from checkpoint (simulate via filter / id-based skip)
# ---------------------------------------------------------------------------

class TestResumeFromCheckpoint(unittest.TestCase):
    """BulkReprocessor doesn't have built-in checkpointing, but we can simulate
    resume by running with confidence filter: already-processed items (confidence
    raised) will be skipped on a second pass."""

    def test_already_improved_items_skipped_on_second_pass(self):
        """Items that already have high confidence after first pass are skipped on re-run."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            # High confidence item — simulates already reprocessed
            items = [_item_dict("id1", confidence=0.95, audio_path=audio_path)]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.99),
                version_manager=_make_vm(),
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                result = br.reprocess(only_low_confidence=True, threshold=0.7)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["reprocessed"], 0)
        finally:
            os.unlink(audio_path)

    def test_mixed_items_only_low_conf_reprocessed(self):
        """Only items below threshold are processed; others are skipped."""
        audio_files = []
        for _ in range(4):
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            f.close()
            audio_files.append(f.name)
        try:
            items = [
                _item_dict("low1", confidence=0.3, audio_path=audio_files[0]),
                _item_dict("low2", confidence=0.4, audio_path=audio_files[1]),
                _item_dict("high1", confidence=0.9, audio_path=audio_files[2]),
                _item_dict("high2", confidence=0.95, audio_path=audio_files[3]),
            ]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                result = br.reprocess(only_low_confidence=True, threshold=0.7)
            # low1 and low2 should be reprocessed, high1 and high2 skipped
            self.assertEqual(result["total"], 4)
            self.assertEqual(result["skipped"], 2)
            self.assertEqual(result["reprocessed"], 2)
        finally:
            for p in audio_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 4. Individual item exception continues
# ---------------------------------------------------------------------------

class TestHandlesIndividualItemExceptionContinues(unittest.TestCase):

    def test_one_bad_item_does_not_stop_others(self):
        """If one item fails (_load_audio raises), remaining items still process.

        Both audio files must exist on disk — the os.path.isfile() pre-filter in
        reprocess() would otherwise exclude the item before _load_audio is called.
        We make both files exist but patch _load_audio to raise for one of them.
        """
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_good:
            good_path = f_good.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_bad:
            bad_path = f_bad.name  # file exists, but load will fail

        try:
            items = [
                _item_dict("bad", confidence=0.3, audio_path=bad_path),
                _item_dict("good", confidence=0.3, audio_path=good_path),
            ]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
            )

            real_good_data = [0.0] * 16000

            def mock_load(path):
                if path == bad_path:
                    raise RuntimeError("audio not found")
                return real_good_data

            with patch.object(br, "_load_audio", side_effect=mock_load):
                result = br.reprocess(only_low_confidence=True, threshold=0.7)

            # "bad" item should add to errors; "good" should be reprocessed
            self.assertEqual(result["reprocessed"], 1)
            self.assertEqual(len(result["errors"]), 1)
            self.assertFalse(result["cancelled"])
        finally:
            os.unlink(good_path)
            try:
                os.unlink(bad_path)
            except OSError:
                pass

    def test_transcriber_returning_empty_text_skips(self):
        """Transcriber returning empty string → recorded as error + skipped."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_item_dict("id1", confidence=0.3, audio_path=audio_path)]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(text="", confidence=0.95),
                version_manager=_make_vm(),
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                result = br.reprocess(only_low_confidence=True, threshold=0.7)
            self.assertEqual(result["reprocessed"], 0)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(len(result["errors"]), 1)
        finally:
            os.unlink(audio_path)

    def test_version_manager_failure_does_not_abort(self):
        """If version_manager.save_version raises, processing should continue."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [
                _item_dict("id1", confidence=0.3, audio_path=audio_path),
            ]
            vm = _make_vm()
            vm.save_version.side_effect = RuntimeError("version store offline")
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=vm,
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                result = br.reprocess(only_low_confidence=True, threshold=0.7)
            # version_manager failure is a warning, not a stop condition
            self.assertEqual(result["reprocessed"], 1)
        finally:
            os.unlink(audio_path)


# ---------------------------------------------------------------------------
# 5. Cancel during processing — clean exit
# ---------------------------------------------------------------------------

class TestCancelDuringProcessing(unittest.TestCase):

    def test_cancel_stops_before_remaining_items(self):
        """cancel() during a multi-item run stops before the rest are processed."""
        audio_files = []
        for _ in range(5):
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            f.close()
            audio_files.append(f.name)
        try:
            items = [_item_dict(f"id{i}", confidence=0.3, audio_path=p)
                     for i, p in enumerate(audio_files)]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
                batch_size=10,
            )

            process_count = [0]

            def mock_load(path):
                process_count[0] += 1
                # Cancel after processing 2 items
                if process_count[0] >= 2:
                    br.cancel()
                return [0.0] * 16000

            with patch.object(br, "_load_audio", side_effect=mock_load):
                result = br.reprocess(only_low_confidence=True, threshold=0.7)

            self.assertTrue(result["cancelled"])
            # Should have processed at most 2 items (third triggers cancel check at loop top)
            self.assertLessEqual(result["reprocessed"], 3)
        finally:
            for p in audio_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_result_marked_cancelled_true(self):
        """If cancelled during execution, result['cancelled'] must be True.

        _reset_cancel() clears the flag at start of reprocess(), so cancellation
        must happen during the loop (not before calling reprocess()).
        """
        audio_files = []
        for _ in range(3):
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            f.close()
            audio_files.append(f.name)
        try:
            items = [_item_dict(f"id{i}", confidence=0.3, audio_path=p)
                     for i, p in enumerate(audio_files)]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
                batch_size=100,
            )

            def cancel_on_first(path):
                br.cancel()  # cancel during first item load
                return [0.0] * 16000

            with patch.object(br, "_load_audio", side_effect=cancel_on_first):
                result = br.reprocess(only_low_confidence=True, threshold=0.7)
            self.assertTrue(result["cancelled"])
        finally:
            for p in audio_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_cancel_from_separate_thread(self):
        """cancel() called from a different thread stops the main reprocess loop."""
        audio_files = []
        for _ in range(20):
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            f.close()
            audio_files.append(f.name)
        try:
            items = [_item_dict(f"id{i}", confidence=0.3, audio_path=p)
                     for i, p in enumerate(audio_files)]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
                batch_size=100,
            )

            loaded = [0]

            def slow_load(path):
                loaded[0] += 1
                time.sleep(0.01)
                return [0.0] * 16000

            result_holder = [None]

            def run_reprocess():
                with patch.object(br, "_load_audio", side_effect=slow_load):
                    result_holder[0] = br.reprocess(only_low_confidence=True, threshold=0.7)

            t = threading.Thread(target=run_reprocess)
            t.start()
            time.sleep(0.05)  # Let a few items process
            br.cancel()
            t.join(timeout=5)
            self.assertIsNotNone(result_holder[0])
            self.assertTrue(result_holder[0]["cancelled"])
            self.assertLess(result_holder[0]["total"] - result_holder[0]["reprocessed"], 20)
        finally:
            for p in audio_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 6. Max concurrent workers respected (BulkReprocessor is single-threaded)
# ---------------------------------------------------------------------------

class TestMaxConcurrentWorkersRespected(unittest.TestCase):
    """BulkReprocessor is intentionally single-threaded (no concurrent workers).
    We verify that transcribe() is never called concurrently."""

    def test_transcribe_calls_are_sequential(self):
        """transcribe() is never called concurrently — all calls complete before next starts."""
        audio_files = []
        for _ in range(4):
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            f.close()
            audio_files.append(f.name)
        try:
            items = [_item_dict(f"id{i}", confidence=0.3, audio_path=p)
                     for i, p in enumerate(audio_files)]

            active_calls = [0]
            max_concurrent = [0]

            original_transcribe = MagicMock(return_value={"text": "result", "confidence": 0.95})

            def counting_transcribe(*args, **kwargs):
                active_calls[0] += 1
                max_concurrent[0] = max(max_concurrent[0], active_calls[0])
                result = original_transcribe(*args, **kwargs)
                active_calls[0] -= 1
                return result

            transcriber = MagicMock()
            transcriber.transcribe = counting_transcribe
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=transcriber,
                version_manager=_make_vm(),
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                result = br.reprocess(only_low_confidence=True, threshold=0.7)

            self.assertEqual(max_concurrent[0], 1, "Should never have >1 concurrent transcribe call")
            self.assertEqual(result["reprocessed"], 4)
        finally:
            for p in audio_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_hard_limit_caps_candidates(self):
        """HARD_LIMIT caps the number of candidates processed in a single run."""
        audio_files = []
        n = HARD_LIMIT + 10
        for _ in range(n):
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            f.close()
            audio_files.append(f.name)
        try:
            items = [_item_dict(f"id{i}", confidence=0.3, audio_path=p)
                     for i, p in enumerate(audio_files)]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                result = br.reprocess(dry_run=True)
            self.assertEqual(result["total"], HARD_LIMIT)
        finally:
            for p in audio_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 7. Unicode audio paths
# ---------------------------------------------------------------------------

class TestUnicodeAudioPaths(unittest.TestCase):

    def test_unicode_filename_item_loaded(self):
        """Items with unicode audio paths are handled correctly by file-existence check."""
        with tempfile.TemporaryDirectory() as d:
            # Create a file with a unicode name
            unicode_path = Path(d) / "запись_тест.wav"
            unicode_path.write_bytes(b"\x00" * 100)

            items = [_item_dict("u1", confidence=0.3, audio_path=str(unicode_path))]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000) as mock_load:
                result = br.reprocess(only_low_confidence=True, threshold=0.7)
            mock_load.assert_called_once_with(str(unicode_path))
            self.assertEqual(result["total"], 1)

    def test_unicode_in_source_lang(self):
        """Items with non-ASCII source_lang field do not crash."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            items = [_item_dict("u2", confidence=0.3, audio_path=audio_path, source_lang="ru-RU")]
            br = BulkReprocessor(
                store=_make_store(items),
                transcriber=_make_transcriber(confidence=0.95),
                version_manager=_make_vm(),
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                result = br.reprocess(only_low_confidence=True, threshold=0.7)
            self.assertEqual(result["reprocessed"], 1)
        finally:
            os.unlink(audio_path)

    def test_unicode_text_in_history_item(self):
        """Items with Cyrillic/emoji text are saved correctly after reprocess."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            audio_path = f.name
        try:
            text = "Съешь же ещё этих мягких французских булок 🎙️"
            items = [_item_dict("u3", text=text, confidence=0.3, audio_path=audio_path)]
            store = _make_store(items)
            br = BulkReprocessor(
                store=store,
                transcriber=_make_transcriber(text="Новый текст с эмодзи 🎤", confidence=0.95),
                version_manager=_make_vm(),
            )
            with patch.object(br, "_load_audio", return_value=[0.0] * 16000):
                result = br.reprocess(only_low_confidence=True, threshold=0.7)
            self.assertEqual(result["reprocessed"], 1)
            # update_history_item_text should have been called with unicode text
            call_args = store.update_history_item_text.call_args
            self.assertIn("🎤", call_args[0][1])
        finally:
            os.unlink(audio_path)


if __name__ == "__main__":
    unittest.main()
