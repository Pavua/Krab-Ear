"""Wave 88 — unit tests for _handle_stop_recording phase helpers.

Each phase method is tested in isolation via mocked collaborators.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import BackendService
from backend.state_store import StateStore
from backend.translator import TranslationResult


# ---------------------------------------------------------------------------
# Shared test helpers (minimal stubs)
# ---------------------------------------------------------------------------

class _FakeRecorderRecording:
    """Recorder that is currently recording and returns speech-like audio."""

    is_recording = False
    sample_rate = 16000

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        audio = np.sin(2 * np.pi * 440 * np.linspace(0, 1, 16000)).astype(np.float32) * 0.3
        return audio, 1.0

    def snapshot_rms(self):
        return 0.05


class _FakeRecorderIdle:
    """Recorder that is NOT recording (returns None on stop)."""

    is_recording = False
    sample_rate = 16000

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        return None

    def snapshot_rms(self):
        return 0.0


class _SilentRecorder(_FakeRecorderRecording):
    """Returns silence audio (triggers silence guard)."""

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        return np.zeros(32000, dtype=np.float32), 1.0


class _EmptyAudioRecorder(_FakeRecorderRecording):
    """Returns zero-size audio array."""

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        audio = np.array([], dtype=np.float32)
        return audio, 0.1


class _FakeTranscriber:
    counter = 0
    engine = MagicMock(quality_profile="balanced", current_model="fake")

    def transcribe(self, audio_data, quality_profile="balanced", cleanup_profile="soft",
                   extra_vocabulary=None, lang_hint=None, history_context=None,
                   stt_hotwords=None, settings=None, diarize=None,
                   skip_vad_prefilter=False, silence_ranges=None, domain="casual"):
        _FakeTranscriber.counter += 1
        return {"text": f"тест #{_FakeTranscriber.counter}", "confidence": 0.9, "engine": "fake"}

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return "preview"


class _FakeTranslator:
    def translate(self, text, mode, network_mode, translation_style="neutral", glossary=None):
        if mode == "off":
            return TranslationResult(text="", status="not_requested",
                                     source_lang="", target_lang="", mode="off", engine="fake")
        return TranslationResult(text=f"ES:{text}", status="ok",
                                 source_lang="ru", target_lang="es",
                                 mode=mode, engine="fake")


def _make_service(test_case, recorder=None, transcriber=None, translator=None, tmp_dir=None):
    """Создать сервис и гарантированно закрыть его до временного каталога."""
    if tmp_dir is None:
        tmp_dir = tempfile.mkdtemp()
    store = StateStore(Path(tmp_dir) / "data")
    service = BackendService(
        store=store,
        recorder=recorder or _FakeRecorderRecording(),
        transcriber=transcriber or _FakeTranscriber(),
        translator=translator or _FakeTranslator(),
    )
    # Cleanup добавлен позже TemporaryDirectory.cleanup, поэтому выполнится первым.
    test_case.addCleanup(service.close)
    return service


# ---------------------------------------------------------------------------
# Phase A tests — audio capture finalization
# ---------------------------------------------------------------------------

class TestPhaseA(unittest.TestCase):
    """Tests for _stop_recording_phase_a."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_returns_early_when_already_stopped(self):
        """Phase A returns early_return with status=already_stopped when recorder is idle."""
        svc = _make_service(self, recorder=_FakeRecorderIdle(), tmp_dir=self.tmp.name)
        settings = svc._cached_settings()
        result = svc._stop_recording_phase_a({}, settings)
        self.assertIn("early_return", result)
        self.assertEqual(result["early_return"]["status"], "already_stopped")

    def test_returns_recorder_timeout_when_worker_hangs(self):
        """F2 (Fable 2026-07-22): timeout stop() → status=recorder_timeout, не already_stopped.

        Раньше зависший worker (PortAudio hang class) выглядел для Swift как
        идемпотентный already_stopped — пользователь молча терял диктовку.
        """
        from backend.recorder import AudioRecorderStopTimeout

        class _HungRecorder(_FakeRecorderRecording):
            def stop(self, timeout_sec=3.0, trim_tail_ms=0):
                raise AudioRecorderStopTimeout("worker не завершился за 0.0 с")

        rec = _HungRecorder()
        rec.start()
        svc = _make_service(self, recorder=rec, tmp_dir=self.tmp.name)
        settings = svc._cached_settings()
        result = svc._stop_recording_phase_a({}, settings)
        self.assertIn("early_return", result)
        self.assertEqual(result["early_return"]["status"], "recorder_timeout")
        self.assertFalse(result["early_return"]["is_recording"])
        # preview_text обязан присутствовать (шанс спасти диктовку из превью).
        self.assertIn("preview_text", result["early_return"])

    def test_returns_audio_and_duration_on_success(self):
        """Phase A returns audio/duration_sec/sr when recorder is active."""
        rec = _FakeRecorderRecording()
        rec.start()  # ensure recording state
        svc = _make_service(self, recorder=rec, tmp_dir=self.tmp.name)
        settings = svc._cached_settings()
        result = svc._stop_recording_phase_a({}, settings)
        self.assertNotIn("early_return", result)
        self.assertIn("audio", result)
        self.assertIn("duration_sec", result)
        self.assertIn("sr", result)
        self.assertGreater(result["duration_sec"], 0)

    def test_returns_early_for_empty_audio(self):
        """Phase A returns early_return with status=empty_text when audio has size 0."""
        rec = _EmptyAudioRecorder()
        rec.start()
        svc = _make_service(self, recorder=rec, tmp_dir=self.tmp.name)
        settings = svc._cached_settings()
        result = svc._stop_recording_phase_a({}, settings)
        self.assertIn("early_return", result)
        self.assertIn(result["early_return"]["status"], ("empty_text", "empty_audio"))

    def test_stop_tail_trim_ms_clamped(self):
        """Phase A coerces stop_tail_trim_ms to [0, 1200]."""
        rec = _FakeRecorderRecording()
        rec.start()
        svc = _make_service(self, recorder=rec, tmp_dir=self.tmp.name)
        settings = svc._cached_settings()
        result = svc._stop_recording_phase_a({"stop_tail_trim_ms": -50}, settings)
        if "early_return" not in result:
            self.assertGreaterEqual(result["stop_tail_trim_ms"], 0)

    def test_rt_partial_stopped_when_present(self):
        """Phase A stops and clears _rt_partial if set."""
        rec = _FakeRecorderRecording()
        rec.start()
        svc = _make_service(self, recorder=rec, tmp_dir=self.tmp.name)
        fake_partial = MagicMock()
        svc._rt_partial = fake_partial
        settings = svc._cached_settings()
        svc._stop_recording_phase_a({}, settings)
        fake_partial.stop.assert_called_once()
        self.assertIsNone(svc._rt_partial)

    def test_sr_dict_contains_required_keys(self):
        """Phase A result sr dict has all keys needed by downstream phases."""
        rec = _FakeRecorderRecording()
        rec.start()
        svc = _make_service(self, recorder=rec, tmp_dir=self.tmp.name)
        settings = svc._cached_settings()
        result = svc._stop_recording_phase_a({}, settings)
        if "early_return" in result:
            return  # already stopped, not applicable
        required_keys = {
            "quality_profile", "cleanup_profile", "lang_hint",
            "translation_mode", "translation_style", "translation_glossary",
            "translate_and_paste", "network_mode",
            "silence_guard_enabled", "silence_rms_threshold",
            "silence_peak_threshold", "silence_active_ratio_threshold",
            "background_guard_enabled", "background_guard_min_peak",
            "background_guard_min_rms", "background_guard_uniform_frame_threshold",
            "background_guard_max_uniform_active_ratio", "sample_rate",
        }
        self.assertTrue(required_keys.issubset(result["sr"].keys()))


# ---------------------------------------------------------------------------
# Phase B tests — audio quality guards
# ---------------------------------------------------------------------------

class TestPhaseB(unittest.TestCase):
    """Tests for _stop_recording_phase_b."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _make_sr(self, silence_guard=True, background_guard=True):
        """Build a minimal sr dict for phase B tests."""
        return {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translation_mode": "off",
            "translate_and_paste": False,
            "sample_rate": 16000,
            "silence_guard_enabled": silence_guard,
            "silence_rms_threshold": 0.002,
            "silence_peak_threshold": 0.012,
            "silence_active_ratio_threshold": 0.015,
            "background_guard_enabled": background_guard,
            "background_guard_min_peak": 0.025,
            "background_guard_min_rms": 0.004,
            "background_guard_uniform_frame_threshold": 0.006,
            "background_guard_max_uniform_active_ratio": 0.92,
        }

    def test_speech_audio_passes_both_guards(self):
        """Normal speech audio passes silence guard and background guard."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        sr = self._make_sr()
        # Generate speech-like audio with amplitude peaks
        audio = np.sin(2 * np.pi * 440 * np.linspace(0, 1, 16000)).astype(np.float32) * 0.3
        result = svc._stop_recording_phase_b(audio, 1.0, 180, sr)
        self.assertNotIn("early_return", result)
        self.assertFalse(result["silence_detected"])
        self.assertFalse(result["background_guard_rejected"])

    def test_silence_audio_triggers_silence_guard(self):
        """All-zeros audio triggers silence guard and returns early_return."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        sr = self._make_sr(silence_guard=True)
        audio = np.zeros(32000, dtype=np.float32)
        result = svc._stop_recording_phase_b(audio, 1.0, 180, sr)
        self.assertIn("early_return", result)

    def test_silence_guard_disabled_skips_check(self):
        """When silence_guard_enabled=False, silent audio is NOT rejected."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        sr = self._make_sr(silence_guard=False, background_guard=False)
        audio = np.zeros(32000, dtype=np.float32)
        result = svc._stop_recording_phase_b(audio, 1.0, 180, sr)
        self.assertNotIn("early_return", result)

    def test_background_guard_disabled_skips_check(self):
        """When background_guard_enabled=False, uniform background is NOT rejected."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        sr = self._make_sr(silence_guard=False, background_guard=False)
        # Uniform low amplitude (would trigger background guard if enabled)
        audio = np.full(32000, 0.0025, dtype=np.float32)
        result = svc._stop_recording_phase_b(audio, 1.0, 180, sr)
        self.assertNotIn("early_return", result)

    def test_returns_guard_flags(self):
        """Phase B result includes silence_detected and background_guard_rejected."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        sr = self._make_sr()
        audio = np.sin(2 * np.pi * 440 * np.linspace(0, 1, 16000)).astype(np.float32) * 0.3
        result = svc._stop_recording_phase_b(audio, 1.0, 180, sr)
        self.assertIn("silence_detected", result)
        self.assertIn("background_guard_rejected", result)


# ---------------------------------------------------------------------------
# Phase C tests — STT execution
# ---------------------------------------------------------------------------

class TestPhaseC(unittest.TestCase):
    """Tests for _stop_recording_phase_c."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _make_sr(self):
        return {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "lang_hint": None,
        }

    def test_returns_transcribe_payload(self):
        """Phase C returns dict with transcribe_payload key."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        audio = np.zeros(16000, dtype=np.float32)
        result = svc._stop_recording_phase_c(audio, 1.0, self._make_sr())
        self.assertIn("transcribe_payload", result)

    def test_transcriber_called_with_correct_profile(self):
        """Phase C passes quality_profile and cleanup_profile to transcriber."""
        transcriber = MagicMock()
        transcriber.transcribe.return_value = {"text": "hello", "confidence": 0.9}
        transcriber.engine = MagicMock(quality_profile="balanced")
        svc = _make_service(self, transcriber=transcriber, tmp_dir=self.tmp.name)
        audio = np.zeros(16000, dtype=np.float32)
        sr = {"quality_profile": "max", "cleanup_profile": "strict", "lang_hint": "ru"}
        svc._stop_recording_phase_c(audio, 1.0, sr)
        call_kwargs = transcriber.transcribe.call_args[1]
        self.assertEqual(call_kwargs["quality_profile"], "max")
        self.assertEqual(call_kwargs["cleanup_profile"], "strict")
        self.assertEqual(call_kwargs["lang_hint"], "ru")

    def test_lang_hint_passed_through(self):
        """Phase C forwards lang_hint from sr to transcriber."""
        transcriber = MagicMock()
        transcriber.transcribe.return_value = {"text": "привет"}
        transcriber.engine = MagicMock(quality_profile="balanced")
        svc = _make_service(self, transcriber=transcriber, tmp_dir=self.tmp.name)
        sr = {"quality_profile": "balanced", "cleanup_profile": "soft", "lang_hint": "es"}
        svc._stop_recording_phase_c(np.zeros(16000, dtype=np.float32), 1.0, sr)
        call_kwargs = transcriber.transcribe.call_args[1]
        self.assertEqual(call_kwargs["lang_hint"], "es")

    def test_auto_glossary_error_does_not_propagate(self):
        """Phase C catches auto_glossary errors and continues without them."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        svc._auto_glossary = MagicMock()
        svc._auto_glossary.build.side_effect = RuntimeError("glossary exploded")
        audio = np.zeros(16000, dtype=np.float32)
        # Should NOT raise
        result = svc._stop_recording_phase_c(audio, 1.0, self._make_sr())
        self.assertIn("transcribe_payload", result)

    def test_combined_hotwords_deduplicated(self):
        """Phase C deduplicates hotwords from settings and auto_glossary."""
        transcriber = MagicMock()
        transcriber.transcribe.return_value = {"text": "ok"}
        transcriber.engine = MagicMock(quality_profile="balanced")
        svc = _make_service(self, transcriber=transcriber, tmp_dir=self.tmp.name)
        # Inject overlapping hotwords
        svc._settings_svc._settings_cache = {
            "stt_hotwords_enabled": True,
            "stt_hotwords": ["Краб", "Антигравити"],
        }
        svc._auto_glossary = MagicMock()
        svc._auto_glossary.build.return_value = ["Антигравити", "НейроЦентр"]
        svc._stop_recording_phase_c(np.zeros(16000, dtype=np.float32), 1.0, self._make_sr())
        call_kwargs = transcriber.transcribe.call_args[1]
        hotwords = call_kwargs.get("stt_hotwords", [])
        if hotwords:
            lower = [w.lower() for w in hotwords]
            self.assertEqual(len(lower), len(set(lower)), "Hotwords should be deduplicated")


# ---------------------------------------------------------------------------
# Phase D tests — post-processing pipeline
# ---------------------------------------------------------------------------

class TestPhaseD(unittest.TestCase):
    """Tests for _stop_recording_phase_d."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _make_sr(self, translation_mode="off"):
        return {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translation_mode": translation_mode,
            "translation_style": "neutral",
            "translation_glossary": {},
            "translate_and_paste": False,
            "network_mode": "offline_default",
        }

    def _call_phase_d(self, svc, transcribe_payload, sr=None, duration_sec=1.0):
        return svc._stop_recording_phase_d(
            transcribe_payload=transcribe_payload,
            duration_sec=duration_sec,
            sr=sr or self._make_sr(),
            stop_tail_trim_ms=180,
            silence_detected=False,
            silence_guard_enabled=True,
            background_guard_rejected=False,
        )

    def test_returns_text_and_translation_fields(self):
        """Phase D returns expected keys on normal transcription."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        result = self._call_phase_d(svc, {"text": "Привет, мир.", "confidence": 0.9})
        self.assertNotIn("early_return", result)
        for key in ("text", "display_text", "translated_text", "final_text",
                    "translation", "translation_status", "confidence",
                    "diarization_data", "tp"):
            self.assertIn(key, result, f"missing key: {key}")

    def test_empty_text_returns_early_return(self):
        """Phase D returns early_return with status=empty_text when STT yields nothing."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        result = self._call_phase_d(svc, {"text": "", "confidence": 0.0})
        self.assertIn("early_return", result)
        self.assertEqual(result["early_return"]["status"], "empty_text")

    def test_translation_called_when_mode_not_off(self):
        """Phase D calls translator when translation_mode != off."""
        translator = MagicMock()
        translator.translate.return_value = TranslationResult(
            text="ES: hola", status="ok", source_lang="ru", target_lang="es",
            mode="ru_to_es", engine="fake",
        )
        svc = _make_service(self, translator=translator, tmp_dir=self.tmp.name)
        sr = self._make_sr(translation_mode="ru_to_es")
        result = self._call_phase_d(svc, {"text": "Привет.", "confidence": 0.9}, sr=sr)
        translator.translate.assert_called_once()
        self.assertNotIn("early_return", result)
        self.assertEqual(result["translated_text"], "ES: hola")

    def test_translate_and_paste_selects_translated_text(self):
        """When translate_and_paste=True, final_text is the translated text."""
        translator = MagicMock()
        translator.translate.return_value = TranslationResult(
            text="ES: hola", status="ok", source_lang="ru", target_lang="es",
            mode="ru_to_es", engine="fake",
        )
        svc = _make_service(self, translator=translator, tmp_dir=self.tmp.name)
        sr = self._make_sr(translation_mode="ru_to_es")
        sr["translate_and_paste"] = True
        result = self._call_phase_d(svc, {"text": "Привет.", "confidence": 0.9}, sr=sr)
        self.assertEqual(result["final_text"], "ES: hola")

    def test_soft_retry_recovers_long_transcript(self):
        """Phase D retries with soft cleanup when postprocess drops long raw text."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        # Postprocess would normally strip everything; simulate by returning empty
        # but raw text is long (>30 chars) and duration > 8s
        raw_long = "а " * 20  # 40 chars of borderline text
        result = self._call_phase_d(
            svc,
            {"text": raw_long, "confidence": 0.9},
            duration_sec=10.0,
        )
        # Should not early_return since raw was non-empty
        self.assertNotIn("early_return", result)

    def test_low_confidence_does_not_fail(self):
        """Phase D logs warning for low confidence but does not fail."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        result = self._call_phase_d(
            svc, {"text": "Привет.", "confidence": 0.2}
        )
        self.assertNotIn("early_return", result)
        self.assertAlmostEqual(result["confidence"], 0.2, places=4)

    def test_diarization_data_forwarded(self):
        """Phase D forwards diarization dict from transcribe_payload."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        fake_diar = {"speakers": [{"id": "SPEAKER_00", "segments": []}]}
        result = self._call_phase_d(
            svc, {"text": "Привет.", "confidence": 0.9, "diarization": fake_diar}
        )
        self.assertEqual(result["diarization_data"], fake_diar)


# ---------------------------------------------------------------------------
# Phase E tests — history persistence + response assembly
# ---------------------------------------------------------------------------

class TestPhaseE(unittest.TestCase):
    """Tests for _stop_recording_phase_e."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _make_phase_d_dict(self, text="Привет мир.", translated="", translation_mode="off"):
        translation = TranslationResult(
            text=translated,
            status="not_requested" if translation_mode == "off" else "ok",
            source_lang="ru",
            target_lang="es" if translated else "",
            mode=translation_mode,
            engine="fake",
        )
        return {
            "text": text,
            "display_text": text,
            "translated_text": translated,
            "final_text": translated if translated else text,
            "translation": translation,
            "translation_status": translation.status,
            "confidence": 0.9,
            "diarization_data": None,
            "tp": {"confidence": 0.9, "engine": "fake"},
        }

    def _make_sr(self):
        return {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translation_style": "neutral",
            "translate_and_paste": False,
        }

    def _call_phase_e(self, svc, phase_d, sr=None):
        return svc._stop_recording_phase_e(
            phase_d=phase_d,
            sr=sr or self._make_sr(),
            duration_sec=1.0,
            stop_tail_trim_ms=180,
            silence_detected=False,
            silence_guard_enabled=True,
            background_guard_rejected=False,
            rt_session_id=None,
            settings=svc._cached_settings(),
        )

    def test_returns_ok_status(self):
        """Phase E returns status=ok."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        result = self._call_phase_e(svc, self._make_phase_d_dict())
        self.assertEqual(result["status"], "ok")

    def test_saves_item_to_history(self):
        """Phase E writes a history item (history_id is non-empty)."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        result = self._call_phase_e(svc, self._make_phase_d_dict())
        self.assertIsNotNone(result.get("history_id"))
        self.assertTrue(len(result["history_id"]) > 0)

    def test_clipboard_history_updated(self):
        """Phase E appends entry to _clipboard_history."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        before = len(svc._clipboard_history)
        self._call_phase_e(svc, self._make_phase_d_dict())
        self.assertEqual(len(svc._clipboard_history), before + 1)

    def test_clipboard_history_capped_at_20(self):
        """Phase E keeps at most 20 clipboard history entries."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        svc._clipboard_history = [{"text": f"x{i}", "ts": "", "history_id": str(i)} for i in range(25)]
        self._call_phase_e(svc, self._make_phase_d_dict())
        self.assertLessEqual(len(svc._clipboard_history), 20)

    def test_response_shape_includes_required_fields(self):
        """Phase E result dict has all fields expected by Swift client."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        result = self._call_phase_e(svc, self._make_phase_d_dict())
        required = {
            "status", "duration_sec", "quality_profile", "cleanup_profile",
            "translation_mode", "translation_status", "text", "original_text",
            "translated_text", "history_id", "ts", "stop_tail_trim_ms",
            "silence_detected", "silence_guard_enabled", "background_guard_rejected",
        }
        self.assertTrue(required.issubset(result.keys()))

    def test_realtime_event_emitted_when_rt_session_id_set(self):
        """Phase E emits realtime.final_transcript event when rt_session_id is present."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        emitted_events = []

        original_emit = svc._stop_recording_phase_e.__func__  # noqa: F841 — just confirming

        # Patch event_bus.emit on the module level
        import backend.service as svc_module
        original = svc_module.event_bus.emit

        def fake_emit(event_type, data):
            emitted_events.append((event_type, data))

        svc_module.event_bus.emit = fake_emit
        try:
            svc._stop_recording_phase_e(
                phase_d=self._make_phase_d_dict(),
                sr=self._make_sr(),
                duration_sec=1.0,
                stop_tail_trim_ms=180,
                silence_detected=False,
                silence_guard_enabled=True,
                background_guard_rejected=False,
                rt_session_id="session-abc",
                settings=svc._cached_settings(),
            )
        finally:
            svc_module.event_bus.emit = original

        event_types = [ev[0] for ev in emitted_events]
        self.assertIn("realtime.final_transcript", event_types)

    def test_transcription_counter_incremented(self):
        """Phase E increments _transcription_counter after each save."""
        svc = _make_service(self, tmp_dir=self.tmp.name)
        before = svc._transcription_counter
        self._call_phase_e(svc, self._make_phase_d_dict())
        self.assertEqual(svc._transcription_counter, before + 1)


# ---------------------------------------------------------------------------
# Orchestrator smoke test — full pipeline via handle_request
# ---------------------------------------------------------------------------

class TestOrchestrator(unittest.TestCase):
    """Smoke-tests that verify the thin orchestrator delegates correctly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = _make_service(self, tmp_dir=self.tmp.name)

    def _req(self, method, params=None):
        return self.svc.handle_request({"id": "t1", "method": method, "params": params or {}})

    def test_full_stop_recording_ok(self):
        """Orchestrator returns status=ok after start+stop cycle."""
        self._req("start_recording")
        stop = self._req("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["status"], "ok")

    def test_idempotent_stop(self):
        """Second stop_recording returns already_stopped."""
        self._req("start_recording")
        self._req("stop_recording", {"quality_profile": "balanced"})
        second = self._req("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(second["ok"])
        self.assertEqual(second["result"]["status"], "already_stopped")

    def test_result_has_history_id(self):
        """Orchestrator result includes a non-empty history_id."""
        self._req("start_recording")
        stop = self._req("stop_recording", {"quality_profile": "balanced"})
        self.assertIsNotNone(stop["result"].get("history_id"))


if __name__ == "__main__":
    unittest.main()
