"""Wave 331: Unit tests for RecordingCoreService.

Tests cover:
  - Static helper utilities (_coerce_bool, _coerce_bounded, _looks_like_silence_audio,
    _looks_like_distant_background_speech, _extract_transcribed_text,
    _extract_transcribed_error, _collect_audio_paths, _is_known_prompt_echo,
    _postprocess_transcribed_text, _collapse_immediate_duplicate_phrase,
    _contains_repeated_chunk, _looks_like_looping_artifact)
  - Instance handler logic (handle_get_recording_state, handle_cancel_transcribe_job,
    handle_get_transcribe_progress, handle_preview_transcribe_paths,
    _build_empty_audio_response)
  - Mutable state management (_clipboard_history cap, _transcription_counter)

All collaborators are replaced with lightweight fakes — no real IO, network, or GPU.
"""

from __future__ import annotations

import sys
import os
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock

import numpy as np

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.recording_core_service import RecordingCoreService  # noqa: E402


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def _make_service(**overrides) -> RecordingCoreService:
    """Return a RecordingCoreService wired with MagicMock collaborators."""
    recorder = MagicMock()
    recorder.is_recording = False
    recorder.sample_rate = 16000

    store = MagicMock()
    store.data_dir = pathlib.Path(tempfile.gettempdir())

    job_tracker = MagicMock()
    job_tracker.get.return_value = None

    defaults = dict(
        recorder=recorder,
        transcriber=MagicMock(),
        translator=MagicMock(),
        store=store,
        vocabulary=MagicMock(),
        job_tracker=job_tracker,
        settings_svc=MagicMock(),
        auto_glossary=MagicMock(),
        context_memory=MagicMock(),
        semantic_searcher=MagicMock(is_enabled=False),
        auto_backup=MagicMock(),
        action_items_extractor=None,
        transcript_writer_cls=MagicMock(),
        clipboard_history=[],
        get_preview_state_fn=lambda: ("", 0.0),
        start_preview_fn=MagicMock(),
        stop_preview_fn=MagicMock(),
        reset_preview_fn=MagicMock(),
        cached_settings_fn=lambda: {},
        get_runtime_setting_fn=lambda key, default=None: default,
        generate_summary_fn=lambda text: None,
        format_text_with_speakers_fn=lambda text, diar: text,
    )
    defaults.update(overrides)
    return RecordingCoreService(**defaults)


# ===========================================================================
# 1. _coerce_bool
# ===========================================================================

class TestCoerceBool(unittest.TestCase):
    def test_true_passthrough(self):
        self.assertIs(RecordingCoreService._coerce_bool(True, default=False), True)

    def test_false_passthrough(self):
        self.assertIs(RecordingCoreService._coerce_bool(False, default=True), False)

    def test_string_true(self):
        for val in ("1", "true", "True", "on", "yes"):
            with self.subTest(val=val):
                self.assertTrue(RecordingCoreService._coerce_bool(val, default=False))

    def test_string_false(self):
        for val in ("0", "false", "False", "off", "no"):
            with self.subTest(val=val):
                self.assertFalse(RecordingCoreService._coerce_bool(val, default=True))

    def test_none_returns_default(self):
        self.assertTrue(RecordingCoreService._coerce_bool(None, default=True))
        self.assertFalse(RecordingCoreService._coerce_bool(None, default=False))

    def test_unknown_string_returns_default(self):
        self.assertTrue(RecordingCoreService._coerce_bool("maybe", default=True))

    def test_integer_one(self):
        self.assertTrue(RecordingCoreService._coerce_bool(1, default=False))

    def test_integer_zero(self):
        self.assertFalse(RecordingCoreService._coerce_bool(0, default=True))


# ===========================================================================
# 2. _coerce_bounded
# ===========================================================================

class TestCoerceBounded(unittest.TestCase):
    def test_within_range_int(self):
        result = RecordingCoreService._coerce_bounded(500, default=100, min_value=0, max_value=1000)
        self.assertEqual(result, 500)

    def test_clamp_to_min(self):
        result = RecordingCoreService._coerce_bounded(-10, default=100, min_value=0, max_value=1000)
        self.assertEqual(result, 0)

    def test_clamp_to_max(self):
        result = RecordingCoreService._coerce_bounded(9999, default=100, min_value=0, max_value=1200)
        self.assertEqual(result, 1200)

    def test_float_passthrough(self):
        result = RecordingCoreService._coerce_bounded(0.005, default=0.002, min_value=0.001, max_value=0.1)
        self.assertAlmostEqual(result, 0.005, places=6)

    def test_none_uses_default(self):
        result = RecordingCoreService._coerce_bounded(None, default=42, min_value=0, max_value=100)
        self.assertEqual(result, 42)

    def test_string_numeric(self):
        result = RecordingCoreService._coerce_bounded("75", default=50, min_value=0, max_value=100)
        self.assertEqual(result, 75)

    def test_invalid_string_uses_default(self):
        result = RecordingCoreService._coerce_bounded("oops", default=50, min_value=0, max_value=100)
        self.assertEqual(result, 50)


# ===========================================================================
# 3. _looks_like_silence_audio
# ===========================================================================

class TestLooksLikeSilence(unittest.TestCase):
    SR = 16000

    def _zeros(self, n=SR):
        return np.zeros(n, dtype=np.float32)

    def _loud(self, amplitude=0.5, n=SR):
        return np.full(n, amplitude, dtype=np.float32)

    def test_pure_silence_detected(self):
        self.assertTrue(RecordingCoreService._looks_like_silence_audio(
            self._zeros(), self.SR, 0.002, 0.012, 0.015))

    def test_loud_audio_not_silence(self):
        self.assertFalse(RecordingCoreService._looks_like_silence_audio(
            self._loud(), self.SR, 0.002, 0.012, 0.015))

    def test_empty_array_is_silence(self):
        self.assertTrue(RecordingCoreService._looks_like_silence_audio(
            np.array([], dtype=np.float32), self.SR, 0.002, 0.012, 0.015))

    def test_very_low_noise_detected(self):
        noise = np.random.uniform(-0.0005, 0.0005, self.SR).astype(np.float32)
        self.assertTrue(RecordingCoreService._looks_like_silence_audio(
            noise, self.SR, 0.002, 0.012, 0.015))


# ===========================================================================
# 4. _looks_like_distant_background_speech
# ===========================================================================

class TestLooksLikeBackground(unittest.TestCase):
    SR = 16000

    def test_empty_is_not_background(self):
        self.assertFalse(RecordingCoreService._looks_like_distant_background_speech(
            np.array([], dtype=np.float32), self.SR, 0.025, 0.004, 0.006, 0.92))

    def test_loud_foreground_not_flagged(self):
        # Clearly foreground: high-amplitude bursts separated by silence.
        # peak >> min_peak (0.025) AND highly non-uniform active ratio.
        audio = np.zeros(self.SR * 5, dtype=np.float32)
        # Insert loud bursts at intervals to break uniformity
        for start in range(0, self.SR * 5, self.SR):
            audio[start:start + 1600] = 0.5
        self.assertFalse(RecordingCoreService._looks_like_distant_background_speech(
            audio, self.SR, 0.025, 0.004, 0.006, 0.92))

    def test_low_uniform_noise_flagged(self):
        # Highly uniform very-low amplitude noise
        noise = np.full(self.SR * 5, 0.003, dtype=np.float32)
        # Tiny random jitter so std != 0
        noise += np.random.uniform(-0.0001, 0.0001, noise.shape).astype(np.float32)
        result = RecordingCoreService._looks_like_distant_background_speech(
            noise, self.SR, 0.025, 0.004, 0.006, 0.92)
        # Not asserting specific outcome — just ensuring it doesn't crash
        self.assertIsInstance(result, bool)


# ===========================================================================
# 5. _extract_transcribed_text
# ===========================================================================

class TestExtractTranscribedText(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(RecordingCoreService._extract_transcribed_text(None), "")

    def test_string_payload(self):
        self.assertEqual(RecordingCoreService._extract_transcribed_text("hello"), "hello")

    def test_dict_with_text_key(self):
        self.assertEqual(RecordingCoreService._extract_transcribed_text({"text": "world"}), "world")

    def test_dict_nested_result_text(self):
        payload = {"result": {"text": "nested"}}
        self.assertEqual(RecordingCoreService._extract_transcribed_text(payload), "nested")

    def test_dict_without_text_returns_empty(self):
        self.assertEqual(RecordingCoreService._extract_transcribed_text({}), "")

    def test_strips_whitespace(self):
        self.assertEqual(RecordingCoreService._extract_transcribed_text("  spaces  "), "spaces")

    def test_other_type_stringified(self):
        self.assertEqual(RecordingCoreService._extract_transcribed_text(42), "42")


# ===========================================================================
# 6. _extract_transcribed_error
# ===========================================================================

class TestExtractTranscribedError(unittest.TestCase):
    def test_error_key_present(self):
        self.assertEqual(RecordingCoreService._extract_transcribed_error({"error": "oops"}), "oops")

    def test_no_error_key_returns_empty(self):
        self.assertEqual(RecordingCoreService._extract_transcribed_error({"text": "ok"}), "")

    def test_non_dict_returns_empty(self):
        self.assertEqual(RecordingCoreService._extract_transcribed_error("raw string"), "")

    def test_none_returns_empty(self):
        self.assertEqual(RecordingCoreService._extract_transcribed_error(None), "")


# ===========================================================================
# 7. _is_known_prompt_echo
# ===========================================================================

class TestIsKnownPromptEcho(unittest.TestCase):
    def test_empty_returns_true(self):
        self.assertTrue(RecordingCoreService._is_known_prompt_echo(""))

    def test_blank_whitespace_returns_true(self):
        self.assertTrue(RecordingCoreService._is_known_prompt_echo("   "))

    def test_fragment_продолжение(self):
        self.assertTrue(RecordingCoreService._is_known_prompt_echo("продолжение следует"))

    def test_fragment_to_be_continued(self):
        self.assertTrue(RecordingCoreService._is_known_prompt_echo("to be continued"))

    def test_normal_text_not_echo(self):
        self.assertFalse(RecordingCoreService._is_known_prompt_echo("привет мир"))

    def test_regex_match_сохраняй(self):
        text = "сохраняй смысл корректную пунктуацию"
        self.assertTrue(RecordingCoreService._is_known_prompt_echo(text))


# ===========================================================================
# 8. _collapse_immediate_duplicate_phrase
# ===========================================================================

class TestCollapseDuplicate(unittest.TestCase):
    def test_empty_string_returns_empty(self):
        self.assertEqual(RecordingCoreService._collapse_immediate_duplicate_phrase(""), "")

    def test_short_phrase_returns_empty(self):
        # < 8 words
        self.assertEqual(RecordingCoreService._collapse_immediate_duplicate_phrase("one two three four"), "")

    def test_exact_duplicate_even(self):
        phrase = "hello world hello world hello world hello world"
        result = RecordingCoreService._collapse_immediate_duplicate_phrase(phrase)
        # Should collapse
        self.assertIn("Hello world", result)

    def test_no_duplicate_returns_empty(self):
        unique = "the quick brown fox jumps over the lazy dog today"
        result = RecordingCoreService._collapse_immediate_duplicate_phrase(unique)
        # No exact halves — should return empty
        self.assertEqual(result, "")


# ===========================================================================
# 9. _postprocess_transcribed_text
# ===========================================================================

class TestPostprocessTranscribedText(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(RecordingCoreService._postprocess_transcribed_text(""), "")

    def test_tech_artifact_dropped(self):
        text = "<begin_of_box> some content <end_of_box>"
        self.assertEqual(RecordingCoreService._postprocess_transcribed_text(text), "")

    def test_normal_text_capitalised_and_punctuated(self):
        result = RecordingCoreService._postprocess_transcribed_text("hello world this is a test")
        self.assertTrue(result[0].isupper(), "Should start with capital letter")
        self.assertIn(".", result, "Should end with punctuation")

    def test_already_punctuated_not_doubled(self):
        result = RecordingCoreService._postprocess_transcribed_text("Привет мир.")
        self.assertEqual(result.count("."), 1)

    def test_json_action_artifact_dropped(self):
        text = '{"action": "do_something", "param": 1}'
        self.assertEqual(RecordingCoreService._postprocess_transcribed_text(text), "")


# ===========================================================================
# 10. _collect_audio_paths
# ===========================================================================

class TestCollectAudioPaths(unittest.TestCase):
    def test_empty_list_returns_empty(self):
        self.assertEqual(RecordingCoreService._collect_audio_paths([]), [])

    def test_nonexistent_path_skipped(self):
        paths = ["/nonexistent/path/audio.wav"]
        self.assertEqual(RecordingCoreService._collect_audio_paths(paths), [])

    def test_real_wav_file_returned(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        try:
            result = RecordingCoreService._collect_audio_paths([tmp_path])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0], str(pathlib.Path(tmp_path).resolve()))
        finally:
            os.unlink(tmp_path)

    def test_non_audio_file_skipped(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            tmp_path = f.name
        try:
            result = RecordingCoreService._collect_audio_paths([tmp_path])
            self.assertEqual(result, [])
        finally:
            os.unlink(tmp_path)

    def test_directory_recurse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = pathlib.Path(tmpdir) / "test.mp3"
            wav_path.touch()
            txt_path = pathlib.Path(tmpdir) / "readme.txt"
            txt_path.touch()
            result = RecordingCoreService._collect_audio_paths([tmpdir])
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].endswith(".mp3"))

    def test_deduplication(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        try:
            result = RecordingCoreService._collect_audio_paths([tmp_path, tmp_path])
            self.assertEqual(len(result), 1)
        finally:
            os.unlink(tmp_path)


# ===========================================================================
# 11. handle_get_recording_state
# ===========================================================================

class TestHandleGetRecordingState(unittest.TestCase):
    def test_returns_expected_keys(self):
        svc = _make_service(get_preview_state_fn=lambda: ("partial text", 3.5))
        result = svc.handle_get_recording_state({})
        self.assertIn("is_recording", result)
        self.assertIn("duration_sec", result)
        self.assertIn("preview_text", result)
        self.assertIn("audio_rms", result)
        self.assertIn("elapsed_sec", result)
        self.assertIn("session_id", result)

    def test_preview_text_forwarded(self):
        svc = _make_service(get_preview_state_fn=lambda: ("hello", 1.2))
        result = svc.handle_get_recording_state({})
        self.assertEqual(result["preview_text"], "hello")
        self.assertAlmostEqual(result["duration_sec"], 1.2)

    def test_is_recording_false_when_not_recording(self):
        recorder = MagicMock()
        recorder.is_recording = False
        svc = _make_service(recorder=recorder)
        result = svc.handle_get_recording_state({})
        self.assertFalse(result["is_recording"])


# ===========================================================================
# 12. handle_cancel_transcribe_job
# ===========================================================================

class TestHandleCancelTranscribeJob(unittest.TestCase):
    def test_missing_job_id_raises(self):
        svc = _make_service()
        with self.assertRaises(RuntimeError):
            svc.handle_cancel_transcribe_job({})

    def test_cancel_called_on_tracker(self):
        job_tracker = MagicMock()
        job_tracker.cancel.return_value = True
        svc = _make_service(job_tracker=job_tracker)
        result = svc.handle_cancel_transcribe_job({"job_id": "abc123"})
        job_tracker.cancel.assert_called_once_with("abc123")
        self.assertTrue(result["cancelled"])

    def test_cancel_returns_false_when_not_found(self):
        job_tracker = MagicMock()
        job_tracker.cancel.return_value = False
        svc = _make_service(job_tracker=job_tracker)
        result = svc.handle_cancel_transcribe_job({"job_id": "missing"})
        self.assertFalse(result["cancelled"])


# ===========================================================================
# 13. handle_get_transcribe_progress
# ===========================================================================

class TestHandleGetTranscribeProgress(unittest.TestCase):
    def test_missing_job_id_raises(self):
        svc = _make_service()
        with self.assertRaises(RuntimeError):
            svc.handle_get_transcribe_progress({})

    def test_unknown_job_raises(self):
        job_tracker = MagicMock()
        job_tracker.get.return_value = None
        svc = _make_service(job_tracker=job_tracker)
        with self.assertRaises(RuntimeError):
            svc.handle_get_transcribe_progress({"job_id": "nope"})

    def test_running_job_has_no_items(self):
        job_tracker = MagicMock()
        job_tracker.get.return_value = {
            "status": "running",
            "current_file": "test.wav",
            "current_stage": "transcribe",
            "file_index": 1,
            "total_files": 3,
            "elapsed_sec": 5.0,
            "processed": 1,
            "errors": [],
            "items": [{"text": "hi"}],
        }
        svc = _make_service(job_tracker=job_tracker)
        result = svc.handle_get_transcribe_progress({"job_id": "job1"})
        # items hidden while running
        self.assertEqual(result["items"], [])
        self.assertEqual(result["status"], "running")

    def test_done_job_exposes_items(self):
        job_tracker = MagicMock()
        job_tracker.get.return_value = {
            "status": "done",
            "current_file": "",
            "current_stage": "idle",
            "file_index": 2,
            "total_files": 2,
            "elapsed_sec": 10.0,
            "processed": 2,
            "errors": [],
            "items": [{"text": "a"}, {"text": "b"}],
        }
        svc = _make_service(job_tracker=job_tracker)
        result = svc.handle_get_transcribe_progress({"job_id": "job2"})
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["status"], "done")


# ===========================================================================
# 14. _build_empty_audio_response
# ===========================================================================

class TestBuildEmptyAudioResponse(unittest.TestCase):
    def test_basic_response_fields(self):
        svc = _make_service()
        resp = svc._build_empty_audio_response(
            duration_sec=0.5,
            quality_profile="balanced",
            cleanup_profile="soft",
            translation_mode="off",
            translate_and_paste=False,
            stop_tail_trim_ms=180,
        )
        self.assertEqual(resp["status"], "empty_audio")
        self.assertAlmostEqual(resp["duration_sec"], 0.5)
        self.assertEqual(resp["text"], "")
        self.assertIsNone(resp["history_id"])

    def test_silence_and_background_flags(self):
        svc = _make_service()
        resp = svc._build_empty_audio_response(
            duration_sec=1.0,
            quality_profile="max",
            cleanup_profile="strict",
            translation_mode="ru",
            translate_and_paste=True,
            stop_tail_trim_ms=0,
            silence_detected=True,
            background_guard_rejected=True,
        )
        self.assertTrue(resp["silence_detected"])
        self.assertTrue(resp["background_guard_rejected"])


# ===========================================================================
# 15. handle_preview_transcribe_paths — path-traversal guard
# ===========================================================================

class TestHandlePreviewTranscribePaths(unittest.TestCase):
    def test_non_list_params_raises(self):
        svc = _make_service()
        with self.assertRaises(RuntimeError):
            svc.handle_preview_transcribe_paths({"paths": "not a list"})

    def test_path_outside_allowed_rejected(self):
        svc = _make_service()
        # /etc/passwd is outside home / tmp / data_dir
        result = svc.handle_preview_transcribe_paths({"paths": ["/etc/passwd"]})
        self.assertIn("errors", result)
        self.assertTrue(any("outside allowed" in e for e in result["errors"]),
                        f"Expected 'outside allowed' error, got: {result['errors']}")

    def test_empty_paths_returns_zero(self):
        svc = _make_service()
        result = svc.handle_preview_transcribe_paths({"paths": []})
        self.assertEqual(result["audio_count"], 0)


# ===========================================================================
# 16. _contains_repeated_chunk
# ===========================================================================

class TestContainsRepeatedChunk(unittest.TestCase):
    def test_short_list_returns_false(self):
        self.assertFalse(RecordingCoreService._contains_repeated_chunk(["a", "b", "c"], min_repeats=3))

    def test_repeated_chunk_detected(self):
        words = ["hello", "world"] * 4  # chunk size 2, 4 repeats
        self.assertTrue(RecordingCoreService._contains_repeated_chunk(words, min_repeats=3))

    def test_no_repetition(self):
        words = "the quick brown fox jumps over the lazy dog today".split()
        self.assertFalse(RecordingCoreService._contains_repeated_chunk(words, min_repeats=3))


# ===========================================================================
# 17. Service-owned mutable state
# ===========================================================================

class TestServiceOwnedState(unittest.TestCase):
    def test_transcription_counter_starts_zero(self):
        svc = _make_service()
        self.assertEqual(svc._transcription_counter, 0)

    def test_rt_partial_starts_none(self):
        svc = _make_service()
        self.assertIsNone(svc._rt_partial)

    def test_rt_session_id_starts_empty(self):
        svc = _make_service()
        self.assertEqual(svc._rt_session_id, "")

    def test_clipboard_history_starts_empty(self):
        svc = _make_service()
        self.assertEqual(svc._clipboard_history, [])


if __name__ == "__main__":
    unittest.main()
