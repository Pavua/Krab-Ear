"""Dispatch invariant tests — Wave 693.

Five structural invariants for recently-added IPC handlers (all distinct from
Wave 654 set: score_transcription, replace_word_in_last_transcript,
get_stt_routing_decision, compare_periods, export_glossary_csv).

Handlers verified here:
  1. probe_llm_http
  2. extract_action_items
  3. get_last_llm_diff
  4. get_activity_calendar
  5. check_integrity
"""

import os
import re
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

SERVICE_PY = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")
# W828: dispatch table moved to ipc_dispatch.py
IPC_DISPATCH_PY = os.path.join(KRAB_EAR_ROOT, "backend", "ipc_dispatch.py")

_WAVE693_HANDLERS = frozenset(
    {
        "probe_llm_http",
        "extract_action_items",
        "get_last_llm_diff",
        "get_activity_calendar",
        "check_integrity",
    }
)


def _read_dispatch_keys() -> set[str]:
    """Return all IPC method keys from ipc_dispatch.py dispatch dict.

    W828: dispatch table moved from service.py to backend/ipc_dispatch.py.
    """
    with open(IPC_DISPATCH_PY, encoding="utf-8") as f:
        src = f.read()
    return set(re.findall(r'"([a-z][a-z0-9_]*)"\s*:', src))


def _read_dispatch_impl_map() -> dict[str, str]:
    """Return {ipc_key: _handle_method_name} from dispatch block.

    W828: dispatch table moved from service.py to backend/ipc_dispatch.py.
    """
    with open(IPC_DISPATCH_PY, encoding="utf-8") as f:
        src = f.read()
    return dict(re.findall(r'"([a-z][a-z0-9_]*)"\s*:\s*svc\.(\_handle_\w+)', src))


class TestWave693DispatchInvariants(unittest.TestCase):
    """Wave 693 — structural dispatch invariants for 5 recently-added handlers."""

    @classmethod
    def setUpClass(cls):
        cls.dispatch_keys = _read_dispatch_keys()
        cls.impl_map = _read_dispatch_impl_map()

    # ------------------------------------------------------------------
    # Test 1 — all 5 Wave 693 handlers are present in the dispatch table
    # ------------------------------------------------------------------
    def test_all_wave693_handlers_registered(self):
        """Every Wave 693 handler must appear as a key in the dispatch table."""
        missing = _WAVE693_HANDLERS - self.dispatch_keys
        self.assertSetEqual(
            missing,
            set(),
            f"Wave 693 handler(s) missing from dispatch table: {missing}",
        )

    # ------------------------------------------------------------------
    # Test 2 — probe_llm_http maps to the correct implementation method
    # ------------------------------------------------------------------
    def test_probe_llm_http_maps_to_correct_impl(self):
        """'probe_llm_http' must resolve to _handle_probe_llm_http, not an alias."""
        impl = self.impl_map.get("probe_llm_http")
        self.assertEqual(
            impl,
            "_handle_probe_llm_http",
            f"'probe_llm_http' maps to {impl!r}; expected '_handle_probe_llm_http'",
        )

    # ------------------------------------------------------------------
    # Test 3 — extract_action_items maps to _handle_extract_action_items
    # ------------------------------------------------------------------
    def test_extract_action_items_maps_to_correct_impl(self):
        """'extract_action_items' must resolve to _handle_extract_action_items."""
        impl = self.impl_map.get("extract_action_items")
        self.assertEqual(
            impl,
            "_handle_extract_action_items",
            f"'extract_action_items' maps to {impl!r}; expected '_handle_extract_action_items'",
        )

    # ------------------------------------------------------------------
    # Test 4 — get_last_llm_diff is delegated to LLMOpsService (W783)
    # ------------------------------------------------------------------
    def test_get_last_llm_diff_maps_to_correct_impl(self):
        """'get_last_llm_diff' must resolve to LLMOpsService.handle_get_last_llm_diff (W783).

        W783 extracted this handler out of BackendService into LLMOpsService.
        The dispatch entry now points to self._llm_ops_svc.handle_get_last_llm_diff.
        """
        # W828: dispatch table is now in ipc_dispatch.py
        with open(IPC_DISPATCH_PY, encoding="utf-8") as f:
            block = f.read()
        # Verify the method appears in the dispatch block (key present)
        self.assertIn(
            '"get_last_llm_diff"',
            block,
            "'get_last_llm_diff' key missing from dispatch table",
        )
        # Verify it delegates to LLMOpsService (not a local _handle_ stub)
        self.assertIn(
            "_llm_ops_svc.handle_get_last_llm_diff",
            block,
            "'get_last_llm_diff' should delegate to _llm_ops_svc.handle_get_last_llm_diff (W783)",
        )

    # ------------------------------------------------------------------
    # Test 5 — get_activity_calendar and check_integrity implementation
    #          methods exist as defs in service.py
    # ------------------------------------------------------------------
    def test_get_activity_calendar_and_check_integrity_impl_defs_exist(self):
        """Both _handle_get_activity_calendar and _handle_check_integrity must be
        defined as methods in service.py (not just referenced but missing body).
        """
        with open(SERVICE_PY, encoding="utf-8") as f:
            src = f.read()

        for method in ("_handle_get_activity_calendar", "_handle_check_integrity"):
            self.assertIn(
                f"def {method}(",
                src,
                f"Implementation method '{method}' is missing from service.py",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
