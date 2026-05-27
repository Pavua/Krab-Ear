"""W1381 — тесты IPC-хэндлера get_audit_log.

Проверяет:
  - get_audit_log_dispatched          — хэндлер зарегистрирован и возвращает ok=True
  - get_audit_log_privacy_mode        — privacy_mode_enabled=True → пустой список + reason
  - get_audit_log_default_days_back   — без параметров days_back=7 (нет ошибок)
  - get_audit_log_invalid_days_clamped — значения вне [1,90] обрезаются
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from backend.state_store import StateStore
from backend.service import BackendService
from backend.translator import TranslationResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Minimal stubs (same pattern as wave654 invariant tests)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    is_recording = False
    sample_rate = 16000
    last_stop_trim_ms = 0
    last_stop_timeout_sec = 3.0

    def start(self):
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        import numpy as np
        return np.zeros(16000, dtype=np.float32), 1.0

    def snapshot_audio(self, max_duration_sec=12.0):
        import numpy as np
        return np.zeros(32000, dtype=np.float32), 1.0


class _FakeEngine:
    _last_llm_diff = None
    _llm_rewriter = None
    _settings_get = None
    quality_profile = "balanced"
    current_model = "fake-model"

    def _resolve_diarization_device(self):
        return "cpu"


class _FakeTranscriber:
    def __init__(self):
        self.counter = 0
        self.engine = _FakeEngine()

    def transcribe(self, audio_data, quality_profile="balanced",
                   cleanup_profile="soft", domain="casual",
                   extra_vocabulary=None, lang_hint=None):
        self.counter += 1
        return f"fake transcription #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return "preview"


class _FakeTranslator:
    last_mode = "off"

    def translate(self, text, mode, network_mode,
                  translation_style="neutral", glossary=None):
        return TranslationResult(
            text="" if mode == "off" else f"TRANSLATED:{text}",
            status="not_requested" if mode == "off" else "ok",
            source_lang="",
            target_lang="",
            mode=mode,
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class _AuditLogBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.svc = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    def req(self, method, params=None, req_id="t"):
        return self.svc.handle_request({
            "id": req_id,
            "method": method,
            "params": params or {},
        })

    def assert_dispatch(self, method, params=None, *, ok_required=None):
        resp = self.req(method, params)
        self.assertIsInstance(resp, dict, f"{method}: response is not dict")
        self.assertIn("id", resp, f"{method}: missing 'id' key")
        self.assertIn("ok", resp, f"{method}: missing 'ok' key")
        if ok_required is True:
            self.assertTrue(resp["ok"], f"{method}: ok=False, error={resp.get('error')}")
        elif ok_required is False:
            self.assertFalse(resp["ok"], f"{method}: expected ok=False, got ok=True")
        return resp


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestGetAuditLogDispatched(_AuditLogBase):
    """get_audit_log зарегистрирован в таблице диспетчеризации и возвращает ok=True."""

    def test_get_audit_log_dispatched(self):
        """Хэндлер существует, ответ содержит ok=True и поле entries."""
        resp = self.assert_dispatch("get_audit_log", ok_required=True)
        result = resp.get("result", {})
        self.assertIn("entries", result, "result должен содержать 'entries'")
        self.assertIsInstance(result["entries"], list, "'entries' должен быть списком")

    def test_get_audit_log_not_unknown_method(self):
        """Метод не возвращает unknown_method ошибку."""
        resp = self.req("get_audit_log", {})
        self.assertNotEqual(resp.get("error"), "unknown_method",
                            "get_audit_log не должен быть unknown_method")


class TestGetAuditLogPrivacyMode(_AuditLogBase):
    """privacy_mode_enabled=True → пустой список с reason=privacy_mode."""

    def test_get_audit_log_privacy_mode_returns_empty(self):
        """При privacy_mode_enabled entries=[] и reason='privacy_mode'."""
        # Включаем privacy mode
        self.req("set_settings", {"privacy_mode_enabled": True})

        resp = self.assert_dispatch("get_audit_log", ok_required=True)
        result = resp.get("result", {})
        self.assertEqual(result.get("entries"), [],
                         "privacy_mode должен вернуть пустой список")
        self.assertEqual(result.get("reason"), "privacy_mode",
                         "privacy_mode должен вернуть reason='privacy_mode'")

    def test_get_audit_log_privacy_mode_off_returns_list(self):
        """При privacy_mode_enabled=False entries является списком (без reason)."""
        self.req("set_settings", {"privacy_mode_enabled": False})
        resp = self.assert_dispatch("get_audit_log", ok_required=True)
        result = resp.get("result", {})
        self.assertIsInstance(result.get("entries"), list,
                              "без privacy_mode entries должен быть списком")
        self.assertNotIn("reason", result,
                         "без privacy_mode reason не должен присутствовать")


class TestGetAuditLogDefaultDaysBack(_AuditLogBase):
    """По умолчанию days_back=7 — запрос без параметра не вызывает ошибок."""

    def test_get_audit_log_default_days_back_7(self):
        """Запрос без days_back возвращает ok=True."""
        resp = self.assert_dispatch("get_audit_log", {}, ok_required=True)
        result = resp.get("result", {})
        self.assertIn("entries", result)

    def test_get_audit_log_explicit_days_back_7(self):
        """Явный days_back=7 возвращает ok=True."""
        resp = self.assert_dispatch("get_audit_log", {"days_back": 7}, ok_required=True)
        result = resp.get("result", {})
        self.assertIn("entries", result)


class TestGetAuditLogInvalidDaysClamped(_AuditLogBase):
    """Значения days_back вне диапазона [1,90] обрезаются — нет ошибок."""

    def test_get_audit_log_days_back_zero_clamped(self):
        """days_back=0 обрезается до 1 — ответ ok=True."""
        resp = self.assert_dispatch("get_audit_log", {"days_back": 0}, ok_required=True)
        self.assertIn("entries", resp.get("result", {}))

    def test_get_audit_log_days_back_negative_clamped(self):
        """days_back=-5 обрезается до 1 — ответ ok=True."""
        resp = self.assert_dispatch("get_audit_log", {"days_back": -5}, ok_required=True)
        self.assertIn("entries", resp.get("result", {}))

    def test_get_audit_log_days_back_overflow_clamped(self):
        """days_back=9999 обрезается до 90 — ответ ok=True."""
        resp = self.assert_dispatch("get_audit_log", {"days_back": 9999}, ok_required=True)
        self.assertIn("entries", resp.get("result", {}))

    def test_get_audit_log_days_back_max_boundary(self):
        """days_back=90 (граница) — ответ ok=True."""
        resp = self.assert_dispatch("get_audit_log", {"days_back": 90}, ok_required=True)
        self.assertIn("entries", resp.get("result", {}))

    def test_get_audit_log_days_back_string_falls_back(self):
        """days_back='invalid' (не int) обрабатывается без исключения — ответ ok=True."""
        resp = self.assert_dispatch("get_audit_log", {"days_back": "invalid"}, ok_required=True)
        self.assertIn("entries", resp.get("result", {}))


if __name__ == "__main__":
    unittest.main()
