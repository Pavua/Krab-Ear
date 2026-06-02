"""Dispatch invariant tests — Wave 768 (critical IPC methods).

Asserts that 20 critical IPC method keys are permanently present in the
``BackendService.handle_request`` lookup table.  Tests are pure source-grep —
no runtime import of service.py is required.

Swift relies on every method here; removing any one would silently break the
agent without a compile error.

Methods covered (organised by concern):
  Core recording lifecycle
    ping, start_recording, stop_recording, get_recording_state

  History
    get_history_page, search_history, delete_history_item, export_history_srt

  Settings
    get_settings, set_settings, apply_profile_preset

  Translation
    translate_text, translate_selection

  STT hotwords
    add_stt_hotword, remove_stt_hotword, list_stt_hotwords

  System / diagnostics
    handshake, get_diagnostics, get_metrics_dashboard, list_audio_inputs
"""

import os
import re
import sys
import unittest
from typing import Dict, Optional, Set

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

SERVICE_PY = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
# W1769: dispatch table consolidated inline in service.py
# (BackendService._build_dispatch_table — single source of truth); ipc_dispatch.py removed.
# RHS values below keep the historical ``svc.`` prefix; _dispatch_rhs() normalizes
# the live ``self.`` prefix → ``svc.`` so these assertions remain stable.

# The exact dispatch-table RHS values expected for each critical method.
# Format: either "svc._handle_X" (local) or "svc._svc.handle_X" (delegated).
_EXPECTED: Dict[str, str] = {
    # Core recording lifecycle — Swift BackendSupervisor + main.swift + HistoryPanel
    "ping": "svc._handle_ping",
    "start_recording": "svc._handle_start_recording",
    "stop_recording": "svc._handle_stop_recording",
    "get_recording_state": "svc._handle_get_recording_state",
    # History — Swift HistoryPanelController+History.swift
    "get_history_page": "svc._history.handle_get_history_page",
    "search_history": "svc._history.handle_search_history",
    "delete_history_item": "svc._history.handle_delete_history_item",
    "export_history_srt": "svc._history.handle_export_history_srt",
    # Settings — Swift main.swift + HistoryPanelController+Settings.swift
    "get_settings": "svc._settings_svc.handle_get_settings",
    "set_settings": "svc._settings_svc.handle_set_settings",
    "apply_profile_preset": "svc._settings_svc.handle_apply_profile_preset",
    # Translation — Phase 2A SelectionTranslator.swift + HistoryPanel+LiveTranslation.swift
    "translate_text": "svc._translation.handle_translate_text",
    "translate_selection": "svc._translation.handle_translate_selection",
    # STT hotwords — Swift HistoryPanelController+Settings.swift
    "add_stt_hotword": "svc._stt_mgmt_svc.handle_add_stt_hotword",
    "remove_stt_hotword": "svc._stt_mgmt_svc.handle_remove_stt_hotword",
    "list_stt_hotwords": "svc._stt_mgmt_svc.handle_list_stt_hotwords",
    # System / diagnostics — various Swift callers
    "handshake": "svc._handle_handshake",
    "get_diagnostics": "svc._handle_get_diagnostics",
    "get_metrics_dashboard": "svc._handle_get_metrics_dashboard",
    "list_audio_inputs": "svc._handle_list_audio_inputs",
}


def _read_source() -> str:
    """Read service.py source (for implementation method checks)."""
    with open(SERVICE_PY, encoding="utf-8") as f:
        return f.read()


def _read_dispatch_source() -> str:
    """Read service.py source (W1769: dispatch table is the inline dict in
    BackendService._build_dispatch_table)."""
    with open(SERVICE_PY, encoding="utf-8") as f:
        return f.read()


def _dispatch_block(src: str) -> str:
    """Return the dispatch table source.

    W1769: dispatch table is inline in service.py; src IS the dispatch table.
    """
    return src


def _all_dispatch_keys(block: str) -> set:
    return set(re.findall(r'"([a-z][a-z0-9_]*)"\s*:', block))


def _dispatch_rhs(block: str, key: str) -> Optional[str]:
    """Return the RHS for *key*, normalized to the historical ``svc.`` prefix.

    W1769: the live dispatch dict (service.py) uses ``self.`` prefix; we read it
    and rewrite ``self.`` → ``svc.`` so the ``_EXPECTED`` literals (kept as
    ``svc.…`` for continuity) keep matching.
    """
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*(self\.[^\s,#\n]+)', block)
    if m is None:
        return None
    return m.group(1).replace("self.", "svc.", 1)


class TestWave768CriticalDispatchInvariants(unittest.TestCase):
    """Wave 768 — dispatch invariants for 20 critical Swift-facing IPC methods."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_source()
        cls.block = _dispatch_block(_read_dispatch_source())
        cls.keys = _all_dispatch_keys(cls.block)

    # ------------------------------------------------------------------
    # Bulk presence test — fastest guard
    # ------------------------------------------------------------------
    def test_all_20_critical_methods_registered(self):
        """All 20 critical IPC methods must be present as keys in the dispatch table."""
        missing = set(_EXPECTED) - self.keys
        self.assertSetEqual(
            missing,
            set(),
            f"Critical IPC method(s) missing from dispatch table: {sorted(missing)}",
        )

    # ------------------------------------------------------------------
    # Per-method presence + correct RHS — Core recording lifecycle
    # ------------------------------------------------------------------
    def test_ping_present_and_correct(self):
        """ping → self._handle_ping (HealthMonitor 3 s heartbeat)."""
        self.assertIn("ping", self.keys)
        self.assertEqual(_dispatch_rhs(self.block, "ping"), "svc._handle_ping")

    def test_start_recording_present_and_correct(self):
        """start_recording → self._handle_start_recording."""
        self.assertIn("start_recording", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "start_recording"),
            "svc._handle_start_recording",
        )

    def test_stop_recording_present_and_correct(self):
        """stop_recording → self._handle_stop_recording."""
        self.assertIn("stop_recording", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "stop_recording"),
            "svc._handle_stop_recording",
        )

    def test_get_recording_state_present_and_correct(self):
        """get_recording_state → self._handle_get_recording_state."""
        self.assertIn("get_recording_state", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "get_recording_state"),
            "svc._handle_get_recording_state",
        )

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    def test_get_history_page_present_and_correct(self):
        """get_history_page → self._history.handle_get_history_page (HistoryService)."""
        self.assertIn("get_history_page", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "get_history_page"),
            "svc._history.handle_get_history_page",
        )

    def test_search_history_present_and_correct(self):
        """search_history → self._history.handle_search_history."""
        self.assertIn("search_history", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "search_history"),
            "svc._history.handle_search_history",
        )

    def test_delete_history_item_present_and_correct(self):
        """delete_history_item → self._history.handle_delete_history_item."""
        self.assertIn("delete_history_item", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "delete_history_item"),
            "svc._history.handle_delete_history_item",
        )

    def test_export_history_srt_present_and_correct(self):
        """export_history_srt → self._history.handle_export_history_srt."""
        self.assertIn("export_history_srt", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "export_history_srt"),
            "svc._history.handle_export_history_srt",
        )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def test_get_settings_present_and_correct(self):
        """get_settings → self._settings_svc.handle_get_settings (SettingsService)."""
        self.assertIn("get_settings", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "get_settings"),
            "svc._settings_svc.handle_get_settings",
        )

    def test_set_settings_present_and_correct(self):
        """set_settings → self._settings_svc.handle_set_settings."""
        self.assertIn("set_settings", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "set_settings"),
            "svc._settings_svc.handle_set_settings",
        )

    def test_apply_profile_preset_present_and_correct(self):
        """apply_profile_preset → self._settings_svc.handle_apply_profile_preset."""
        self.assertIn("apply_profile_preset", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "apply_profile_preset"),
            "svc._settings_svc.handle_apply_profile_preset",
        )

    # ------------------------------------------------------------------
    # Translation
    # ------------------------------------------------------------------
    def test_translate_text_present_and_correct(self):
        """translate_text → self._translation.handle_translate_text (TranslationService)."""
        self.assertIn("translate_text", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "translate_text"),
            "svc._translation.handle_translate_text",
        )

    def test_translate_selection_present_and_correct(self):
        """translate_selection → self._translation.handle_translate_selection (Phase 2A)."""
        self.assertIn("translate_selection", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "translate_selection"),
            "svc._translation.handle_translate_selection",
        )

    # ------------------------------------------------------------------
    # STT hotwords
    # ------------------------------------------------------------------
    def test_add_stt_hotword_present_and_correct(self):
        """add_stt_hotword → self._stt_mgmt_svc.handle_add_stt_hotword."""
        self.assertIn("add_stt_hotword", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "add_stt_hotword"),
            "svc._stt_mgmt_svc.handle_add_stt_hotword",
        )

    def test_remove_stt_hotword_present_and_correct(self):
        """remove_stt_hotword → self._stt_mgmt_svc.handle_remove_stt_hotword."""
        self.assertIn("remove_stt_hotword", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "remove_stt_hotword"),
            "svc._stt_mgmt_svc.handle_remove_stt_hotword",
        )

    def test_list_stt_hotwords_present_and_correct(self):
        """list_stt_hotwords → self._stt_mgmt_svc.handle_list_stt_hotwords."""
        self.assertIn("list_stt_hotwords", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "list_stt_hotwords"),
            "svc._stt_mgmt_svc.handle_list_stt_hotwords",
        )

    # ------------------------------------------------------------------
    # System / diagnostics
    # ------------------------------------------------------------------
    def test_handshake_present_and_correct(self):
        """handshake → self._handle_handshake (Swift connect negotiation)."""
        self.assertIn("handshake", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "handshake"), "svc._handle_handshake"
        )

    def test_get_diagnostics_present_and_correct(self):
        """get_diagnostics → self._handle_get_diagnostics."""
        self.assertIn("get_diagnostics", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "get_diagnostics"),
            "svc._handle_get_diagnostics",
        )

    def test_get_metrics_dashboard_present_and_correct(self):
        """get_metrics_dashboard → self._handle_get_metrics_dashboard."""
        self.assertIn("get_metrics_dashboard", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "get_metrics_dashboard"),
            "svc._handle_get_metrics_dashboard",
        )

    def test_list_audio_inputs_present_and_correct(self):
        """list_audio_inputs → self._handle_list_audio_inputs (audio device picker)."""
        self.assertIn("list_audio_inputs", self.keys)
        self.assertEqual(
            _dispatch_rhs(self.block, "list_audio_inputs"),
            "svc._handle_list_audio_inputs",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
