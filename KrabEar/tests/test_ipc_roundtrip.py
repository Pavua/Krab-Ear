"""Интеграционные тесты IPC roundtrip: handle_request принимает JSON-RPC-подобные дикты и возвращает структурированные ответы."""

from __future__ import annotations
from KrabEar.__version__ import __version__ as APP_VERSION
from backend.translator import TranslationResult
from backend.state_store import StateStore
from backend.service import BackendService

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Фейковые коллабораторы
# ---------------------------------------------------------------------------

class FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000
        self._snapshot_counter = 0
        self.last_stop_trim_ms = 0
        self.last_stop_timeout_sec = 3.0

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        self.last_stop_timeout_sec = timeout_sec
        self.last_stop_trim_ms = trim_tail_ms
        return np.zeros(16000, dtype=np.float32), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        self._snapshot_counter += 1
        return np.ones(32000, dtype=np.float32), float(self._snapshot_counter)


class FakeTranscriber:
    def __init__(self) -> None:
        self.counter = 0

    def transcribe(self, audio_data, quality_profile: str = "balanced",
                   cleanup_profile: str = "soft", domain: str = "casual",
                   extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None) -> str:
        self.counter += 1
        return f"тест #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        return "preview"


class FakeTranslator:
    def translate(self, text: str, mode: str, network_mode: str,
                  translation_style: str = "neutral",
                  glossary: dict | None = None) -> TranslationResult:
        return TranslationResult(
            text="",
            status="not_requested",
            source_lang="",
            target_lang="",
            mode="off",
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Тестовый класс
# ---------------------------------------------------------------------------

class IPCRoundtripTestCase(unittest.TestCase):
    """Проверяет полный цикл JSON-RPC запрос→ответ через handle_request."""

    def setUp(self) -> None:
        # ignore_cleanup_errors=True: BackendService starts background threads
        # (DiskSpaceMonitor и т.п.; R1 startup-recovery — только когда есть
        # реальная работа) that may write to data dir after the test ends →
        # OSError on cleanup in CI (established pattern, see BackendServiceTestCase
        # in test_backend_service.py).
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def _call(self, method: str, params: dict | None = None, req_id: str = "t1") -> dict:
        return self.service.handle_request(
            {"id": req_id, "method": method, "params": params or {}}
        )

    # ------------------------------------------------------------------

    def test_ping_roundtrip(self) -> None:
        """ping → ok=True, result содержит status=ok и стандартные поля."""
        resp = self._call("ping")

        self.assertTrue(resp.get("ok"), msg=f"Ожидали ok=True, получили: {resp}")
        self.assertEqual(resp.get("id"), "t1")
        result = resp["result"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["service"], "krabear-backend")
        self.assertEqual(result["version"], APP_VERSION)
        self.assertGreaterEqual(result["uptime_sec"], 0)
        self.assertIn("is_recording", result)
        self.assertIn("history_count", result)

    def test_get_settings_roundtrip(self) -> None:
        """get_settings → ok=True, result содержит ожидаемые ключи настроек."""
        resp = self._call("get_settings")

        self.assertTrue(resp.get("ok"), msg=f"Ожидали ok=True, получили: {resp}")
        result = resp["result"]
        expected_keys = [
            "translation_mode",
            "history_policy",
            "hotkey_profile",
            "quality_profile",
            "translate_and_paste",
            "translation_style",
            "clipboard_mode",
        ]
        for key in expected_keys:
            self.assertIn(key, result, msg=f"Ключ '{key}' отсутствует в get_settings")

    def test_unknown_method(self) -> None:
        """Неизвестный метод → ok=False, error содержит код и сообщение."""
        resp = self._call("nonexistent")

        self.assertFalse(resp.get("ok"), msg=f"Ожидали ok=False, получили: {resp}")
        self.assertIn("error", resp)
        error = resp["error"]
        self.assertIn("code", error)
        self.assertIn("message", error)
        self.assertEqual(error["code"], "unknown_method")

    def test_get_history_page_empty(self) -> None:
        """get_history_page на пустой БД → ok=True, items=[]."""
        resp = self._call("get_history_page")

        self.assertTrue(resp.get("ok"), msg=f"Ожидали ok=True, получили: {resp}")
        result = resp["result"]
        self.assertIn("items", result)
        self.assertIsInstance(result["items"], list)
        self.assertEqual(len(result["items"]), 0)
        self.assertIn("next_cursor", result)

    def test_get_storage_info(self) -> None:
        """get_storage_info → ok=True, result содержит поля размера данных."""
        resp = self._call("get_storage_info")

        self.assertTrue(resp.get("ok"), msg=f"Ожидали ok=True, получили: {resp}")
        result = resp["result"]
        expected_keys = [
            "history_file_size_mb",
            "transcripts_count",
            "transcripts_size_mb",
            "total_data_mb",
        ]
        for key in expected_keys:
            self.assertIn(key, result, msg=f"Ключ '{key}' отсутствует в get_storage_info")
        self.assertGreaterEqual(result["history_file_size_mb"], 0.0)
        self.assertGreaterEqual(result["transcripts_count"], 0)
        self.assertGreaterEqual(result["total_data_mb"], 0.0)


if __name__ == "__main__":
    unittest.main()
