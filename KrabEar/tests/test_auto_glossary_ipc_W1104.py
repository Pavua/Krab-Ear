"""Tests for AutoGlossary IPC handlers — W1104.

Verifies that `get_auto_glossary` and `refresh_auto_glossary` are wired in the
BackendService dispatch table and behave correctly including the privacy guard.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------

class _FakeEngine:
    quality_profile: str = "balanced"
    current_model: str = "fake"

    def _resolve_diarization_device(self) -> str:
        return "cpu"


class _FakeRecorder:
    is_recording: bool = False
    sample_rate: int = 16000

    def start(self) -> bool:
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        self.is_recording = False
        return None

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        import numpy as np
        return np.zeros(1600, dtype=np.float32), 0.1


class _FakeTranscriber:
    engine = _FakeEngine()

    def transcribe(self, *a, **kw) -> str:
        return "fake"

    def transcribe_preview(self, *a, **kw) -> str:
        return "preview"


class _FakeTranslator:
    def translate(self, text, mode, network_mode, **kw) -> TranslationResult:
        return TranslationResult(
            text="", status="not_requested", source_lang="", target_lang="",
            mode="off", engine="fake",
        )


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.store = StateStore(self.data_dir)
        self.service = BackendService(
            store=self.store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )
        self.addCleanup(self.service.close)

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict:
        return self.service.handle_request(
            {"id": "t1", "method": method, "params": params or {}}
        )

    def _ok(self, resp: dict) -> dict:
        self.assertTrue(resp.get("ok"), f"Expected ok=True, got: {resp}")
        return resp["result"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetAutoGlossaryDispatched(_Base):
    """get_auto_glossary is routed and returns expected shape."""

    def test_get_auto_glossary_dispatched(self) -> None:
        """Method is registered in dispatch table and returns ok=True."""
        resp = self._call("get_auto_glossary")
        result = self._ok(resp)
        self.assertIn("terms", result)
        self.assertIn("count", result)
        self.assertIsInstance(result["terms"], list)

    def test_get_auto_glossary_returns_cached(self) -> None:
        """Returns from_cache field indicating cache source."""
        result = self._ok(self._call("get_auto_glossary"))
        self.assertIn("from_cache", result)


class TestRefreshAutoGlossaryDispatched(_Base):
    """refresh_auto_glossary is routed and returns expected shape."""

    def test_refresh_auto_glossary_dispatched(self) -> None:
        """Method is registered and returns ok=True with refreshed flag."""
        resp = self._call("refresh_auto_glossary")
        result = self._ok(resp)
        self.assertIn("terms", result)
        self.assertIn("count", result)
        self.assertTrue(result.get("refreshed"), "Expected refreshed=True")

    def test_refresh_auto_glossary_custom_params(self) -> None:
        """Accepts window_days and top_n params without error."""
        resp = self._call("refresh_auto_glossary", {"window_days": 3, "top_n": 10})
        result = self._ok(resp)
        self.assertIsInstance(result["terms"], list)
        # top_n respected — list should not exceed top_n
        self.assertLessEqual(len(result["terms"]), 10)


class TestGetAutoGlossaryEmptyInPrivacyMode(_Base):
    """In privacy_mode_enabled=True both handlers return empty terms list."""

    def _enable_privacy(self) -> None:
        # Write privacy mode directly to settings store
        self.service.handle_request({
            "id": "s1",
            "method": "set_settings",
            "params": {"privacy_mode_enabled": True},
        })

    def test_get_auto_glossary_empty_in_privacy_mode(self) -> None:
        self._enable_privacy()
        result = self._ok(self._call("get_auto_glossary"))
        self.assertEqual(result["terms"], [])
        self.assertEqual(result["count"], 0)
        self.assertFalse(result.get("from_cache"), "Should not return from cache in privacy mode")

    def test_refresh_auto_glossary_empty_in_privacy_mode(self) -> None:
        self._enable_privacy()
        result = self._ok(self._call("refresh_auto_glossary"))
        self.assertEqual(result["terms"], [])
        self.assertEqual(result["count"], 0)
        self.assertFalse(result.get("refreshed"), "Should not refresh in privacy mode")


if __name__ == "__main__":
    unittest.main()
