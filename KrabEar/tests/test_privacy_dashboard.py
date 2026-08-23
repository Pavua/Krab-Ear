"""Тесты IPC-метода get_privacy_dashboard (агрегированный дашборд privacy/security).

Покрывает:
- Корректная форма ответа (все ключи присутствуют).
- Отражение флага privacy_mode.
- Отражение флага encryption_enabled.
- Счётчики и breakdown по audit-событиям.
- Поля storage (item_count, history_bytes и т.д.).
- Поля retention из настроек.
- Graceful degradation: отказ одного источника не ломает весь дашборд.
- Отсутствие транскрипционного текста / словарей / имён спикеров в ответе.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.privacy_audit import PrivacyAuditLogger  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.service import BackendService  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal fakes — same pattern as test_backend_service.py
# ---------------------------------------------------------------------------

class _FakeRecorder:
    def __init__(self):
        self.is_recording = False
        self.sample_rate = 16000

    def start(self):
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        self.is_recording = False
        return None


class _FakeTranscriber:
    def __init__(self):
        self.vocabulary = []
        self.profile = "balanced"

    def transcribe(self, audio, sample_rate=16000, language=None, task="transcribe"):
        return {"text": "", "segments": [], "language": "ru", "confidence": 0.0}

    def transcribe_file(self, path, language=None):
        return {"text": "", "segments": [], "language": "ru", "confidence": 0.0}


class _FakeTranslator:
    def __init__(self):
        self.glossary = {}
        self._settings_getter = None

    def translate(self, text, source_lang=None, target_lang=None):
        from backend.translator import TranslationResult
        return TranslationResult(text=text, source_lang="ru", target_lang="en",
                                 method="none", cached=False)


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class _PrivacyDashboardBase(unittest.TestCase):
    """Поднимает BackendService с изолированным temp store + PrivacyAuditLogger."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name) / "data"
        store = StateStore(self.data_dir)
        # Изолированный audit log в temp dir
        self.audit_log_path = Path(self.tmpdir.name) / "privacy_audit.log"
        PrivacyAuditLogger.reset_instance()
        self._audit = PrivacyAuditLogger(log_path=self.audit_log_path)

        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    def tearDown(self) -> None:
        # Обязательно: иначе daemon-треды BackendService → exit(1) в CI chunk
        self.service.close()
        PrivacyAuditLogger.reset_instance()

    def _call(self, params=None):
        """Удобная обёртка вызова IPC get_privacy_dashboard."""
        return self.service.handle_request(
            {"id": "pd-test", "method": "get_privacy_dashboard", "params": params or {}}
        )

    def _patch_audit(self):
        """Патчит get_privacy_audit_logger в service.py на наш изолированный экземпляр."""
        return patch(
            "backend.service.get_privacy_audit_logger",
            return_value=self._audit,
        )


# ---------------------------------------------------------------------------
# 1. Форма ответа
# ---------------------------------------------------------------------------

class TestPrivacyDashboardSchema(_PrivacyDashboardBase):
    """Все обязательные ключи присутствуют в ответе."""

    def test_top_level_keys_present(self):
        with self._patch_audit():
            resp = self._call()
        self.assertTrue(resp.get("ok", True), f"Ответ содержит ошибку: {resp}")
        # Если IPC оборачивает в ok/result — разворачиваем
        data = resp.get("result", resp)
        required = {"privacy_mode", "encryption_enabled", "storage", "retention",
                    "audit", "purge_available"}
        self.assertEqual(required, required & set(data.keys()),
                         f"Отсутствующие ключи: {required - set(data.keys())}")

    def test_storage_subkeys(self):
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        storage = data["storage"]
        for key in ("item_count", "history_bytes", "history_file_size_mb",
                    "transcripts_count", "transcripts_size_mb",
                    "total_bytes", "total_data_mb"):
            self.assertIn(key, storage, f"storage.{key} отсутствует")

    def test_retention_subkeys(self):
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        retention = data["retention"]
        for key in ("auto_cleanup_enabled", "auto_cleanup_after_days",
                    "auto_purge_enabled", "auto_purge_retention_days"):
            self.assertIn(key, retention, f"retention.{key} отсутствует")

    def test_audit_subkeys(self):
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        audit = data["audit"]
        for key in ("total_events", "last_event_ts", "by_type"):
            self.assertIn(key, audit, f"audit.{key} отсутствует")

    def test_purge_available_is_true(self):
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        self.assertIs(data["purge_available"], True)


# ---------------------------------------------------------------------------
# 2. Флаги privacy_mode и encryption_enabled
# ---------------------------------------------------------------------------

class TestPrivacyDashboardFlags(_PrivacyDashboardBase):
    """Флаги privacy_mode и encryption_enabled отражают реальные настройки."""

    def _set_setting(self, key, value):
        self.service.handle_request({
            "id": "set", "method": "set_settings", "params": {key: value}
        })

    def test_privacy_mode_default_false(self):
        with self._patch_audit():
            data = self._call().get("result", self._call())
        self.assertIs(data["privacy_mode"], False)

    def test_privacy_mode_reflects_true(self):
        self._set_setting("privacy_mode_enabled", True)
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        self.assertIs(data["privacy_mode"], True)

    def test_encryption_enabled_default_false(self):
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        self.assertIs(data["encryption_enabled"], False)

    def test_encryption_enabled_reflects_setting(self):
        # Устанавливаем напрямую через settings (не через set_history_encryption —
        # та проверяет Keychain, которого нет в CI)
        self._set_setting("history_encryption_enabled", True)
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        self.assertIs(data["encryption_enabled"], True)


# ---------------------------------------------------------------------------
# 3. Audit counts и by_type breakdown
# ---------------------------------------------------------------------------

class TestPrivacyDashboardAudit(_PrivacyDashboardBase):
    """Audit-секция корректно суммирует события PrivacyAuditLogger."""

    def test_audit_empty_on_no_events(self):
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        self.assertEqual(data["audit"]["total_events"], 0)
        self.assertIsNone(data["audit"]["last_event_ts"])
        self.assertEqual(data["audit"]["by_type"], {})

    def test_audit_counts_single_event(self):
        self._audit.log_event("privacy", "mode_enabled")
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        self.assertEqual(data["audit"]["total_events"], 1)
        self.assertIsNotNone(data["audit"]["last_event_ts"])
        self.assertEqual(data["audit"]["by_type"].get("mode_enabled", 0), 1)

    def test_audit_by_type_breakdown(self):
        self._audit.log_event("privacy", "mode_enabled")
        self._audit.log_event("privacy", "mode_disabled")
        self._audit.log_event("privacy", "mode_enabled")
        self._audit.log_event("privacy", "purge_all_data")
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        by_type = data["audit"]["by_type"]
        self.assertEqual(by_type.get("mode_enabled", 0), 2)
        self.assertEqual(by_type.get("mode_disabled", 0), 1)
        self.assertEqual(by_type.get("purge_all_data", 0), 1)
        self.assertEqual(data["audit"]["total_events"], 4)

    def test_audit_last_event_ts_is_latest(self):
        self._audit.log_event("privacy", "first")
        self._audit.log_event("privacy", "second")
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        # last_event_ts должен быть ISO-8601 строкой (больше "first" ts)
        ts = data["audit"]["last_event_ts"]
        self.assertIsNotNone(ts)
        self.assertIn("T", ts)  # ISO-8601 формат


# ---------------------------------------------------------------------------
# 4. Storage fields
# ---------------------------------------------------------------------------

class TestPrivacyDashboardStorage(_PrivacyDashboardBase):
    """Storage-секция содержит корректные числовые значения."""

    def test_storage_defaults_are_numeric(self):
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        storage = data["storage"]
        for key in ("item_count", "history_bytes", "transcripts_count", "total_bytes"):
            self.assertIsInstance(storage[key], int,
                                  f"storage.{key} должен быть int, не {type(storage[key])}")
        for key in ("history_file_size_mb", "transcripts_size_mb", "total_data_mb"):
            self.assertIsInstance(storage[key], (int, float),
                                  f"storage.{key} должен быть float")

    def test_storage_item_count_zero_on_empty_history(self):
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        # Пустая история → item_count = 0
        self.assertEqual(data["storage"]["item_count"], 0)

    def test_storage_item_count_reflects_items(self):
        """После добавления записи item_count увеличивается."""
        self.service.handle_request({
            "id": "add", "method": "add_history_item",
            "params": {"text": "test text", "language": "ru", "duration": 1.0}
        })
        with self._patch_audit():
            resp = self._call()
        data = resp.get("result", resp)
        self.assertGreaterEqual(data["storage"]["item_count"], 1)


# ---------------------------------------------------------------------------
# 5. Retention fields
# ---------------------------------------------------------------------------

class TestPrivacyDashboardRetention(unittest.TestCase):
    """Retention-секция отражает настройки auto_cleanup и auto_purge."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmpdir.cleanup)
        store = StateStore(Path(self.tmpdir.name) / "data")
        PrivacyAuditLogger.reset_instance()
        self._audit = PrivacyAuditLogger(
            log_path=Path(self.tmpdir.name) / "audit.log"
        )
        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()
        PrivacyAuditLogger.reset_instance()

    def _call(self):
        with patch("backend.service.get_privacy_audit_logger", return_value=self._audit):
            return self.service.handle_request(
                {"id": "rd", "method": "get_privacy_dashboard", "params": {}}
            )

    def test_retention_defaults(self):
        data = self._call().get("result", self._call())
        retention = data["retention"]
        self.assertIs(retention["auto_cleanup_enabled"], False)
        self.assertEqual(retention["auto_cleanup_after_days"], 365)
        self.assertIs(retention["auto_purge_enabled"], False)
        self.assertEqual(retention["auto_purge_retention_days"], 90)

    def test_retention_reflects_custom_settings(self):
        self.service.handle_request({
            "id": "s", "method": "set_settings",
            "params": {"auto_cleanup_enabled": True, "auto_cleanup_after_days": 30}
        })
        data = self._call().get("result", self._call())
        retention = data["retention"]
        self.assertIs(retention["auto_cleanup_enabled"], True)
        self.assertEqual(retention["auto_cleanup_after_days"], 30)


# ---------------------------------------------------------------------------
# 6. No transcript text leakage
# ---------------------------------------------------------------------------

class TestPrivacyDashboardNoLeakage(unittest.TestCase):
    """Ответ не содержит транскрипционного текста, словарей или имён спикеров."""

    LEAK_SENTINEL = "SENTINEL_SECRET_TEXT_DO_NOT_LEAK_xyz987"

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmpdir.cleanup)
        store = StateStore(Path(self.tmpdir.name) / "data")
        PrivacyAuditLogger.reset_instance()
        self._audit = PrivacyAuditLogger(
            log_path=Path(self.tmpdir.name) / "audit.log"
        )
        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )
        # Добавляем запись с секретным текстом
        self.service.handle_request({
            "id": "add", "method": "add_history_item",
            "params": {"text": self.LEAK_SENTINEL, "language": "ru", "duration": 2.0}
        })

    def tearDown(self) -> None:
        self.service.close()
        PrivacyAuditLogger.reset_instance()

    def test_no_transcript_text_in_response(self):
        import json as _json
        with patch("backend.service.get_privacy_audit_logger", return_value=self._audit):
            resp = self.service.handle_request(
                {"id": "pd", "method": "get_privacy_dashboard", "params": {}}
            )
        raw = _json.dumps(resp)
        self.assertNotIn(self.LEAK_SENTINEL, raw,
                         "Транскрипционный текст просочился в ответ get_privacy_dashboard!")

    def test_no_text_key_at_any_level(self):
        """Ключ 'text' с пользовательским контентом не должен появляться в ответе."""
        import json as _json
        with patch("backend.service.get_privacy_audit_logger", return_value=self._audit):
            resp = self.service.handle_request(
                {"id": "pd", "method": "get_privacy_dashboard", "params": {}}
            )
        raw = _json.dumps(resp)
        self.assertNotIn(self.LEAK_SENTINEL, raw)


# ---------------------------------------------------------------------------
# 7. Graceful degradation
# ---------------------------------------------------------------------------

class TestPrivacyDashboardGracefulDegradation(unittest.TestCase):
    """Сбой одного источника данных не валит весь дашборд."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmpdir.cleanup)
        store = StateStore(Path(self.tmpdir.name) / "data")
        PrivacyAuditLogger.reset_instance()
        self._audit = PrivacyAuditLogger(
            log_path=Path(self.tmpdir.name) / "audit.log"
        )
        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()
        PrivacyAuditLogger.reset_instance()

    def _call_with_storage_failure(self):
        """storage source raises — остальные поля должны заполниться."""
        import backend.history_service as _hs_mod

        def boom(self_, params):
            raise RuntimeError("simulated storage failure")

        with patch.object(_hs_mod.HistoryService, "handle_get_storage_info", boom):
            with patch("backend.service.get_privacy_audit_logger",
                       return_value=self._audit):
                return self.service.handle_request(
                    {"id": "pd", "method": "get_privacy_dashboard", "params": {}}
                )

    def test_storage_failure_returns_defaults(self):
        resp = self._call_with_storage_failure()
        # Не должна вернуться ошибка IPC (ok=False)
        self.assertNotIn("error", resp.get("result", resp),
                         "Сбой storage неожиданно вернул IPC error")
        data = resp.get("result", resp)
        # storage должен содержать дефолты, а не поднимать исключение
        self.assertIn("storage", data)
        storage = data["storage"]
        self.assertEqual(storage["item_count"], 0)
        self.assertEqual(storage["history_bytes"], 0)

    def test_storage_failure_does_not_break_other_sections(self):
        """Другие секции (privacy_mode, retention, audit) заполняются корректно."""
        # Добавляем audit-событие, чтобы было что проверять
        self._audit.log_event("test", "degradation_check")
        resp = self._call_with_storage_failure()
        data = resp.get("result", resp)
        # privacy_mode по умолчанию False
        self.assertIn("privacy_mode", data)
        self.assertIn("retention", data)
        self.assertIn("audit", data)
        # Audit должен найти наше событие
        self.assertGreaterEqual(data["audit"]["total_events"], 1)

    def test_audit_failure_returns_defaults(self):
        """Сбой PrivacyAuditLogger не ломает дашборд."""
        broken_audit = PrivacyAuditLogger(
            log_path=Path(self.tmpdir.name) / "broken_audit.log"
        )

        def boom_summarize(*a, **kw):
            raise OSError("simulated audit failure")

        broken_audit.summarize = boom_summarize  # type: ignore[method-assign]
        with patch("backend.service.get_privacy_audit_logger",
                   return_value=broken_audit):
            resp = self.service.handle_request(
                {"id": "pd", "method": "get_privacy_dashboard", "params": {}}
            )
        data = resp.get("result", resp)
        self.assertIn("audit", data)
        self.assertEqual(data["audit"]["total_events"], 0)
        self.assertIsNone(data["audit"]["last_event_ts"])
        self.assertEqual(data["audit"]["by_type"], {})
        # Другие секции не сломались
        self.assertIn("privacy_mode", data)
        self.assertIn("storage", data)


# ---------------------------------------------------------------------------
# 8. Dispatch table sanity — метод зарегистрирован
# ---------------------------------------------------------------------------

class TestPrivacyDashboardDispatch(unittest.TestCase):
    """get_privacy_dashboard присутствует в dispatch table."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmpdir.cleanup)
        store = StateStore(Path(self.tmpdir.name) / "data")
        PrivacyAuditLogger.reset_instance()
        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()
        PrivacyAuditLogger.reset_instance()

    def test_method_in_dispatch_table(self):
        self.assertIn(
            "get_privacy_dashboard",
            self.service._dispatch_table,
            "get_privacy_dashboard не зарегистрирован в _dispatch_table",
        )

    def test_unknown_method_returns_error(self):
        resp = self.service.handle_request(
            {"id": "x", "method": "get_privacy_dashboard_nonexistent", "params": {}}
        )
        self.assertIn("error", resp)

    def test_call_returns_ok(self):
        PrivacyAuditLogger.reset_instance()
        audit = PrivacyAuditLogger(
            log_path=Path(self.tmpdir.name) / "audit.log"
        )
        with patch("backend.service.get_privacy_audit_logger", return_value=audit):
            resp = self.service.handle_request(
                {"id": "disp", "method": "get_privacy_dashboard", "params": {}}
            )
        # Не должно быть IPC-ошибки
        self.assertNotIn("error", resp)


if __name__ == "__main__":
    unittest.main()
