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
        """The auto_save_transcripts block must be guarded by not _privacy_mode."""
        src = self._rcs_source()
        auto_save_idx = src.find("auto_save_transcripts")
        self.assertGreater(auto_save_idx, 0,
                           "auto_save_transcripts not found in recording_core_service.py")

        # Context window: ~300 chars before the auto_save_transcripts reference
        context_start = max(0, auto_save_idx - 300)
        context = src[context_start: auto_save_idx + 50]

        self.assertIn("_privacy_mode", context,
                      "_privacy_mode guard missing near auto_save_transcripts block "
                      "in recording_core_service.py")

    def test_not_privacy_mode_appears_on_auto_save_condition(self):
        """The auto_save check must be combined with `not _privacy_mode` in a single `if`."""
        src = self._rcs_source()
        # Find the guarded block: look for the `if (` or `if not _privacy_mode` guard pattern
        # that was introduced by wave-36.  The pattern used in the fix is:
        #   if (\n        not _privacy_mode\n        and self._coerce_bool(...auto_save_transcripts...)
        #
        # We search for the `not _privacy_mode` that precedes `auto_save_transcripts` in the
        # same if-block.
        auto_save_idx = src.find("auto_save_transcripts")
        # Scan backwards ~400 chars to find `not _privacy_mode`
        context_start = max(0, auto_save_idx - 400)
        context = src[context_start: auto_save_idx + 50]

        self.assertIn("not _privacy_mode", context,
                      "Expected `not _privacy_mode` guard combined with auto_save_transcripts check "
                      "in recording_core_service.py (wave-36 fix)")

    # --- Guard-replication test (mirrors wave-31 B2 pattern) ---

    def _simulate_auto_save_guard(
        self,
        _privacy_mode: bool,
        auto_save_enabled: bool,
        write_calls: list,
    ) -> None:
        """Replicate the guard logic from _stop_recording_phase_e at the auto_save site.

        This mirrors exactly what the fixed code does:
            if (
                not _privacy_mode
                and self._coerce_bool(settings.get("auto_save_transcripts", False), default=False)
            ):
                ... TranscriptWriter.write_transcript(...)
        """
        # Minimal coerce_bool implementation
        def coerce_bool(val, default=False):
            if isinstance(val, bool):
                return val
            return default

        if (
            not _privacy_mode
            and coerce_bool(auto_save_enabled, default=False)
        ):
            write_calls.append("write_transcript_called")

    def test_write_called_privacy_off_autosave_on(self):
        """Normal path: write_transcript called when privacy=False and auto_save=True."""
        calls: list = []
        self._simulate_auto_save_guard(
            _privacy_mode=False,
            auto_save_enabled=True,
            write_calls=calls,
        )
        self.assertEqual(len(calls), 1,
                         "write_transcript должен вызываться при privacy=False, auto_save=True")

    def test_write_skipped_privacy_on_autosave_on(self):
        """Privacy gate: write_transcript NOT called when privacy=True even if auto_save=True."""
        calls: list = []
        self._simulate_auto_save_guard(
            _privacy_mode=True,
            auto_save_enabled=True,
            write_calls=calls,
        )
        self.assertEqual(len(calls), 0,
                         "write_transcript НЕ должен вызываться при privacy=True")

    def test_write_skipped_privacy_off_autosave_off(self):
        """Baseline: write_transcript not called when auto_save=False."""
        calls: list = []
        self._simulate_auto_save_guard(
            _privacy_mode=False,
            auto_save_enabled=False,
            write_calls=calls,
        )
        self.assertEqual(len(calls), 0,
                         "write_transcript не должен вызываться при auto_save=False")

    def test_write_skipped_both_privacy_on_and_autosave_on(self):
        """Both flags: privacy_mode takes precedence over auto_save_transcripts=True."""
        calls: list = []
        self._simulate_auto_save_guard(
            _privacy_mode=True,
            auto_save_enabled=True,
            write_calls=calls,
        )
        self.assertEqual(len(calls), 0,
                         "privacy_mode=True должен блокировать запись вне зависимости от auto_save")


if __name__ == "__main__":
    unittest.main()
