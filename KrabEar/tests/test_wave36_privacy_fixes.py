"""Tests for wave-36 privacy fixes.

FIX C1 (MED): bulk_reprocess_start dead privacy gate on the IPC path.
  Before: service.py _handle_bulk_reprocess_start called
          self._bulk_reprocessor.reprocess() WITHOUT passing settings=,
          so the inner gate in BulkReprocessor.reprocess() was dead on
          the live IPC path (settings=None -> gate skipped unconditionally).
  After:  service.py gates at the IPC boundary itself via
          self._get_runtime_setting('privacy_mode_enabled', False) before
          delegating to the reprocessor, matching the standard pattern.

FIX C2 (MED): recording_core_service auto_save_transcripts writes .md in privacy mode.
  Before: _stop_recording_phase_e wrote a .md file via TranscriptWriter
          when auto_save_transcripts=True regardless of privacy_mode_enabled.
  After:  the write block is additionally guarded by `not _privacy_mode`
          (_privacy_mode already resolved at ~line 1371 in the same function).
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_KRAB_EAR = os.path.join(_PROJECT_ROOT, "KrabEar")
if _KRAB_EAR not in sys.path:
    sys.path.insert(0, _KRAB_EAR)


# ---------------------------------------------------------------------------
# C1: bulk_reprocess_start IPC privacy gate (service.py handler)
# ---------------------------------------------------------------------------

class TestBulkReprocessStartPrivacyGateServicePy(unittest.TestCase):
    """wave-36 C1: privacy gate is enforced IN service.py before delegating to reprocessor."""

    def _make_fake_service(self, privacy: bool):
        """Return a minimal fake BackendService exposing _handle_bulk_reprocess_start.

        We bind the real handler from service.py so that any change to the handler
        body is immediately tested here.  The fake implements _cached_settings and
        _get_runtime_setting the same way the real BackendService does.
        """
        from backend.bulk_reprocess import BulkReprocessor
        import backend.service as _svc_mod

        bulk_rp = MagicMock(spec=BulkReprocessor)
        bulk_rp.reprocess = MagicMock(return_value={
            "total": 5, "reprocessed": 3, "skipped": 2, "errors": [], "cancelled": False,
        })

        settings_data = {"privacy_mode_enabled": privacy}

        class _FakeService:
            def __init__(self):
                self._bulk_reprocessor = bulk_rp
                self._settings_data = settings_data

            def _cached_settings(self):
                return self._settings_data

            def _get_runtime_setting(self, key, default):
                try:
                    return self._cached_settings().get(key, default)
                except Exception:
                    return default

            # Bind the real handler from service.py
            _handle_bulk_reprocess_start = (
                _svc_mod.BackendService._handle_bulk_reprocess_start
            )

        return _FakeService(), bulk_rp

    def test_privacy_on_returns_error_and_skips_reprocessor(self):
        """Privacy mode active: returns ok=False, reason=privacy_mode_active; reprocessor not called."""
        svc, bulk_rp = self._make_fake_service(privacy=True)

        result = svc._handle_bulk_reprocess_start({})

        self.assertFalse(result.get("ok"), "Expected ok=False when privacy_mode is True")
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        bulk_rp.reprocess.assert_not_called()

    def test_privacy_on_schema_parity(self):
        """Response schema is parity-consistent (contains total/reprocessed/skipped/errors/cancelled)."""
        svc, _ = self._make_fake_service(privacy=True)

        result = svc._handle_bulk_reprocess_start({})

        for field in ("total", "reprocessed", "skipped", "errors", "cancelled"):
            self.assertIn(field, result, f"Missing field {field!r} in privacy-mode response")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["reprocessed"], 0)
        self.assertEqual(result["errors"], [])
        self.assertFalse(result["cancelled"])

    def test_privacy_off_delegates_to_reprocessor(self):
        """Privacy mode inactive: reprocessor.reprocess() is called normally."""
        svc, bulk_rp = self._make_fake_service(privacy=False)

        result = svc._handle_bulk_reprocess_start({"only_low_confidence": True, "threshold": 0.7})

        bulk_rp.reprocess.assert_called_once()
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["reprocessed"], 3)

    def test_privacy_toggle_respected_dynamically(self):
        """Toggling privacy_mode on at runtime blocks the next call."""
        svc, bulk_rp = self._make_fake_service(privacy=False)

        # First call: privacy off -> reprocessor called
        svc._handle_bulk_reprocess_start({})
        self.assertEqual(bulk_rp.reprocess.call_count, 1)

        # Toggle privacy on
        svc._settings_data["privacy_mode_enabled"] = True

        # Second call: privacy on -> blocked
        result2 = svc._handle_bulk_reprocess_start({})
        self.assertFalse(result2.get("ok"))
        self.assertEqual(result2.get("reason"), "privacy_mode_active")
        # reprocessor still only called once
        self.assertEqual(bulk_rp.reprocess.call_count, 1)


# ---------------------------------------------------------------------------
# C1 regression: validate service.py source-level guard is present
# ---------------------------------------------------------------------------

class TestBulkReprocessServicePySourceGuard(unittest.TestCase):
    """White-box: service.py source must contain the IPC-level privacy gate."""

    def _service_source(self) -> str:
        service_path = os.path.join(_KRAB_EAR, "backend", "service.py")
        with open(service_path) as f:
            return f.read()

    def test_ipc_privacy_gate_present_in_handler(self):
        """_handle_bulk_reprocess_start must gate on _get_runtime_setting privacy_mode_enabled."""
        src = self._service_source()
        handler_idx = src.find("def _handle_bulk_reprocess_start(")
        self.assertGreater(handler_idx, 0, "_handle_bulk_reprocess_start not found in service.py")

        # Find the next handler definition after this one
        next_handler_idx = src.find("\n    def _handle_bulk_reprocess_cancel(", handler_idx)
        handler_body = src[handler_idx:next_handler_idx]

        self.assertIn("privacy_mode_enabled", handler_body,
                      "Privacy gate for privacy_mode_enabled missing from "
                      "_handle_bulk_reprocess_start in service.py")
        self.assertIn("_get_runtime_setting", handler_body,
                      "_get_runtime_setting call missing from privacy gate in service.py handler")

    def test_bulk_reprocessor_reprocess_called_without_settings_kwarg(self):
        """The delegation call must NOT pass settings= to the reprocessor (gate is in service.py now)."""
        src = self._service_source()
        handler_idx = src.find("def _handle_bulk_reprocess_start(")
        next_handler_idx = src.find("\n    def _handle_bulk_reprocess_cancel(", handler_idx)
        handler_body = src[handler_idx:next_handler_idx]

        # Find the reprocess() call
        reprocess_idx = handler_body.find("self._bulk_reprocessor.reprocess(")
        self.assertGreater(reprocess_idx, 0, ".reprocess() call not found in handler body")

        # Extract the call arguments (until the closing paren)
        call_start = handler_body.find("(", reprocess_idx + len("self._bulk_reprocessor.reprocess"))
        call_end = handler_body.find(")", call_start)
        call_args = handler_body[call_start:call_end]

        # The wave-35 inner gate requires settings= to be passed; if it's not passed,
        # the inner gate is dead. The wave-36 fix moves the gate to service.py and
        # does NOT pass settings= to the reprocessor (the gate is now before the call).
        self.assertNotIn("settings=", call_args,
                         "service.py should NOT pass settings= to reprocessor.reprocess() — "
                         "the privacy gate now lives in the IPC handler itself (wave-36 fix)")


# ---------------------------------------------------------------------------
# C2: auto_save_transcripts skipped in privacy mode (recording_core_service.py)
# ---------------------------------------------------------------------------

class TestAutoSaveTranscriptsPrivacyGuard(unittest.TestCase):
    """wave-36 C2: TranscriptWriter.write_transcript must NOT be called in privacy mode.

    Because _stop_recording_phase_e is very complex (full STT/LLM pipeline),
    we use two complementary approaches:
      1. Source inspection: verify the guard `not _privacy_mode` is present near
         the auto_save_transcripts conditional.
      2. Guard-replication test: simulate the auto_save branch logic in isolation
         to confirm it respects _privacy_mode, mirroring the wave-31 B2 approach.
    """

    def _rcs_source(self) -> str:
        path = os.path.join(_KRAB_EAR, "backend", "recording_core_service.py")
        with open(path) as f:
            return f.read()

    # --- Source-inspection tests ---

    def test_privacy_guard_present_near_auto_save(self):
        """The .md auto_save write must be privacy-guarded.

        crypto-audit (2026-06-20) refactor: the guard now lives in the static helper
        `_should_write_plaintext_md` (privacy_mode + history_encryption_enabled +
        auto_save).  Verify the helper exists, checks privacy_mode, AND that the live
        TranscriptWriter.write_transcript call is gated by it.
        """
        src = self._rcs_source()
        helper_idx = src.find("def _should_write_plaintext_md")
        self.assertGreater(helper_idx, 0,
                           "_should_write_plaintext_md helper missing in recording_core_service.py")
        helper_body = src[helper_idx: helper_idx + 800]
        self.assertIn("privacy_mode", helper_body,
                      "privacy_mode guard missing inside _should_write_plaintext_md")

        # The live .md write path must call the gate with require_auto_save=True
        # (direct-substring check — robust against intervening block length).
        self.assertIn(
            "_should_write_plaintext_md(settings, _privacy_mode, require_auto_save=True)",
            src,
            "live TranscriptWriter path not gated by _should_write_plaintext_md(require_auto_save=True)")
        # And the import path must gate with require_auto_save=False.
        self.assertIn(
            "_should_write_plaintext_md(settings, _privacy_mode, require_auto_save=False)",
            src,
            "import path not gated by _should_write_plaintext_md(require_auto_save=False)")

    def test_md_gate_combines_privacy_encryption_autosave(self):
        """The .md write gate must combine privacy_mode AND encryption AND auto_save.

        After the 2026-06-20 refactor all three live in _should_write_plaintext_md.
        """
        src = self._rcs_source()
        helper_idx = src.find("def _should_write_plaintext_md")
        self.assertGreater(helper_idx, 0, "_should_write_plaintext_md helper missing")
        helper_body = src[helper_idx: helper_idx + 800]
        for token in ("privacy_mode", "history_encryption_enabled", "auto_save_transcripts"):
            self.assertIn(token, helper_body,
                          f"{token} guard missing inside _should_write_plaintext_md")

    # --- Поведенческие тесты: НАСТОЯЩИЙ helper и настоящий _stop_recording_phase_e ---

    @staticmethod
    def _real_gate(privacy: bool, auto_save: bool, encryption: bool = False,
                   require_auto_save: bool = True) -> bool:
        """Настоящий RecordingCoreService._should_write_plaintext_md (раньше тесты исполняли копию
        без учёта history_encryption_enabled — неверное рабочее условие оставалось незамеченным)."""
        from backend.recording_core_service import RecordingCoreService

        return RecordingCoreService._should_write_plaintext_md(
            {"auto_save_transcripts": auto_save, "history_encryption_enabled": encryption},
            privacy, require_auto_save=require_auto_save,
        )

    def test_write_called_privacy_off_autosave_on(self):
        """Normal path: запись разрешена при privacy=False, auto_save=True, без шифрования."""
        self.assertTrue(self._real_gate(privacy=False, auto_save=True))

    def test_write_skipped_privacy_on_autosave_on(self):
        """Privacy gate: запись запрещена при privacy=True даже если auto_save=True."""
        self.assertFalse(self._real_gate(privacy=True, auto_save=True))

    def test_write_skipped_privacy_off_autosave_off(self):
        """Baseline: live-путь не пишет при auto_save=False."""
        self.assertFalse(self._real_gate(privacy=False, auto_save=False))

    def test_write_skipped_both_privacy_on_and_autosave_on(self):
        """privacy_mode=True имеет приоритет над auto_save_transcripts=True."""
        self.assertFalse(self._real_gate(privacy=True, auto_save=True, require_auto_save=False))

    def test_gate_matrix_privacy_encryption_autosave(self):
        """Полная матрица privacy × encryption × auto_save × require_auto_save."""
        import itertools

        for privacy, enc, auto, req in itertools.product((False, True), repeat=4):
            expected = (not privacy) and (not enc) and (auto or not req)
            self.assertEqual(
                self._real_gate(privacy, auto, enc, req), expected,
                f"privacy={privacy} encryption={enc} auto_save={auto} require_auto_save={req}",
            )

    def _run_real_phase_e(self, settings: dict):
        """Настоящий _stop_recording_phase_e со spy на TranscriptWriter.write_transcript."""
        import tempfile
        from pathlib import Path as _Path
        from unittest.mock import MagicMock, patch
        import backend.recording_core_service as rcs
        from backend.state_store import StateStore
        from backend.translator import TranslationResult

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        session_tracker = MagicMock()
        session_tracker._active_session = None
        vocab = MagicMock()
        vocab.get_words.return_value = []
        searcher = MagicMock()
        searcher.is_enabled = False
        svc = rcs.RecordingCoreService(
            recorder=MagicMock(), transcriber=MagicMock(), translator=MagicMock(),
            store=StateStore(data_dir=_Path(tmp.name)), vocabulary=vocab,
            settings_svc=MagicMock(cached_settings=lambda: dict(settings)),
            llm_rewriter=None, auto_glossary=None, semantic_searcher=searcher,
            context_memory=None, clipboard_history=[], auto_backup=MagicMock(),
            session_tracker=session_tracker, action_items_extractor=None,
            transcription_counter_ref=[0], last_stt_engine_ref=[None],
        )
        text = "Синтетическая запись для автосохранения."
        tr = TranslationResult(text=text, status="skipped", source_lang="auto", target_lang="ru",
                               mode="auto", engine="fake")
        phase_d = {"text": text, "display_text": text, "translated_text": "", "final_text": text,
                   "translation": tr, "translation_status": "skipped", "confidence": 0.9,
                   "diarization_data": None, "tp": {"language": "ru"}}
        sr = {"quality_profile": "balanced", "cleanup_profile": "soft",
              "translation_style": "neutral", "translate_and_paste": False}
        with patch.object(rcs, "TranscriptWriter") as writer:
            writer.write_transcript.return_value = _Path(tmp.name) / "t.md"
            svc._stop_recording_phase_e(
                phase_d=phase_d, sr=sr, duration_sec=5.0, stop_tail_trim_ms=0,
                silence_detected=False, silence_guard_enabled=False,
                background_guard_rejected=False, rt_session_id=None, settings=settings,
            )
        return writer.write_transcript

    def test_real_phase_e_writes_md_when_allowed(self):
        spy = self._run_real_phase_e({"auto_save_transcripts": True})
        spy.assert_called_once()

    def test_real_phase_e_does_not_write_md_in_privacy_mode(self):
        spy = self._run_real_phase_e({"auto_save_transcripts": True, "privacy_mode_enabled": True})
        spy.assert_not_called()

    def test_real_phase_e_does_not_write_md_with_encryption(self):
        spy = self._run_real_phase_e({"auto_save_transcripts": True, "history_encryption_enabled": True})
        spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
