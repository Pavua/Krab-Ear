"""Privacy-гейты ErrorReporter обязаны быть fail-CLOSED (2026-09).

ЧТО НАЙДЕНО
-----------
``ErrorReporter._is_privacy_mode()`` ловит любой сбой чтения настроек и
возвращает ``False`` — fail-OPEN. ``report_error`` тогда сохраняет
``message`` и ``context`` как есть, включая сниппеты транскрипта /
истории диктовок, которые вызывающий положил в диагностику.

``SettingsService.cached_settings()`` ловит ТОЛЬКО ``StateStoreLockTimeout``;
``OSError`` (ENOSPC/EMFILE/EACCES в фазе flock) пролетает мимо → ошибка
с текстом диктовки уходит в ring-buffer и в IPC ``get_error_report``.

Эталон: ``history_service._is_privacy_mode`` +
``test_privacy_gate_fail_closed_history_2026_09.py``.

Не ломаем легитимную техдиагностику: component / error_type / stats
остаются; при privacy OFF полный traceback кода сохраняется.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_SECRET = "секретный транскрипт владельца"


def _raises_oserror(*_a, **_k):
    raise OSError(24, "Too many open files")


def _make_reporter(settings_side_effect):
    from backend.error_reporter import ErrorReporter

    return ErrorReporter(settings_provider=settings_side_effect)


class ErrorReporterPrivacyGateFailsClosedTests(unittest.TestCase):
    """Неизвестное состояние приватности обязано читаться как «privacy ON»."""

    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        reporter = _make_reporter(_raises_oserror)
        self.assertTrue(
            reporter._is_privacy_mode(),
            "сбой settings_provider() обязан читаться как privacy ON (fail-closed)",
        )

    def test_privacy_helper_returns_real_flag_on_normal_path(self) -> None:
        reporter = _make_reporter(lambda: {"privacy_mode_enabled": False})
        self.assertFalse(reporter._is_privacy_mode())

        reporter_on = _make_reporter(lambda: {"privacy_mode_enabled": True})
        self.assertTrue(reporter_on._is_privacy_mode())

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        """Отсутствие ключа ≠ сбой: настройки прочитаны, режим просто выключен."""
        reporter = _make_reporter(lambda: {})
        self.assertFalse(reporter._is_privacy_mode())

    def test_none_provider_is_privacy_off(self) -> None:
        from backend.error_reporter import ErrorReporter

        reporter = ErrorReporter()
        self.assertFalse(reporter._is_privacy_mode())

    def test_partial_instance_via_new_does_not_raise(self) -> None:
        """``__new__`` без ``__init__``: AttributeError не должен ронять гейт.

        Как у HistoryService: частично сконструированный инстанс — не IO-сбой,
        privacy OFF (юнит-тесты), а не «всегда ON».
        """
        from backend.error_reporter import ErrorReporter

        reporter = ErrorReporter.__new__(ErrorReporter)
        self.assertFalse(reporter._is_privacy_mode())


class ErrorReporterTranscriptRedactionFailsClosedTests(unittest.TestCase):
    """Текст диктовки не должен попадать в отчёт при privacy ON / сбое settings."""

    def test_report_error_redacts_transcript_when_settings_raise(self) -> None:
        reporter = _make_reporter(_raises_oserror)
        rec = reporter.report_error(
            "stt",
            "ValueError",
            f"user said: {_SECRET}",
            context={"transcript": _SECRET, "device": "mic"},
        )
        self.assertEqual(rec.message, "<redacted: privacy_mode>")
        self.assertEqual(rec.context, {})
        self.assertNotIn(_SECRET, rec.message)
        self.assertNotIn(_SECRET, str(rec.context))
        # Технические поля (не текст диктовки) сохраняются.
        self.assertEqual(rec.component, "stt")
        self.assertEqual(rec.error_type, "ValueError")

    def test_get_error_report_hides_transcript_when_settings_raise(self) -> None:
        reporter = _make_reporter(_raises_oserror)
        reporter.report_error(
            "stt",
            "ValueError",
            f"user said: {_SECRET}",
            context={"transcript": _SECRET},
        )
        result = reporter.handle_get_error_report({"limit": 10})
        self.assertNotIn(_SECRET, str(result))
        self.assertEqual(result["errors"][0]["message"], "<redacted: privacy_mode>")
        self.assertEqual(result["errors"][0]["context"], {})
        self.assertEqual(result["errors"][0]["component"], "stt")
        self.assertEqual(result["errors"][0]["error_type"], "ValueError")

    def test_previously_stored_transcript_redacted_on_ipc_when_settings_raise(self) -> None:
        """Ошибки, записанные при privacy OFF, не утекают через IPC после сбоя settings."""
        from backend.error_reporter import ErrorReporter

        settings = {"privacy_mode_enabled": False}
        reporter = ErrorReporter(settings_provider=lambda: settings)
        reporter.report_error(
            "stt",
            "ValueError",
            f"user said: {_SECRET}",
            context={"transcript": _SECRET},
        )
        self.assertIn(_SECRET, reporter.get_recent_errors()[0].message)

        def boom():
            raise OSError(24, "Too many open files")

        reporter._settings_provider = boom
        result = reporter.handle_get_error_report({"limit": 10})
        self.assertNotIn(_SECRET, str(result))
        self.assertEqual(result["errors"][0]["message"], "<redacted: privacy_mode>")
        self.assertEqual(result["errors"][0]["context"], {})


class ErrorReporterTechnicalErrorsRemainWhenPrivacyOffTests(unittest.TestCase):
    """Легитимная техдиагностика (traceback кода, не диктовка) не должна ломаться."""

    def test_technical_traceback_kept_when_privacy_off(self) -> None:
        traceback_msg = (
            "Traceback (most recent call last):\n"
            "  File 'KrabEar/core/engine.py', line 42, in transcribe\n"
            "RuntimeError: mlx timeout"
        )
        reporter = _make_reporter(lambda: {"privacy_mode_enabled": False})
        rec = reporter.report_error(
            "stt",
            "RuntimeError",
            traceback_msg,
            context={"model": "whisper-large", "timeout_sec": 120},
        )
        self.assertEqual(rec.message, traceback_msg)
        self.assertEqual(rec.context["model"], "whisper-large")
        self.assertEqual(rec.error_type, "RuntimeError")
        self.assertEqual(rec.component, "stt")

        result = reporter.handle_get_error_report({"limit": 1})
        self.assertEqual(result["errors"][0]["message"], traceback_msg)
        self.assertEqual(result["errors"][0]["context"]["model"], "whisper-large")

    def test_error_stats_still_count_when_privacy_on(self) -> None:
        reporter = _make_reporter(lambda: {"privacy_mode_enabled": True})
        reporter.report_error("stt", "TimeoutError", f"user said: {_SECRET}")
        reporter.report_error("llm", "RuntimeError", "boom")
        stats = reporter.handle_get_error_stats({})
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["by_component"]["stt"], 1)
        self.assertEqual(stats["by_component"]["llm"], 1)
        self.assertEqual(stats["by_type"]["TimeoutError"], 1)
        self.assertNotIn(_SECRET, str(stats))


class ErrorReporterPrivacyProductionWiringTests(unittest.TestCase):
    """BackendService передаёт cached_settings в ErrorReporter.settings_provider."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._svc = None

    def tearDown(self) -> None:
        if self._svc is not None:
            self._svc.close()

    def test_backend_wires_settings_provider_into_error_reporter(self) -> None:
        from backend.service import BackendService
        from backend.state_store import StateStore

        store = StateStore(data_dir=Path(self._tmpdir))
        self._svc = BackendService(store=store)

        self.assertIsNotNone(
            self._svc._error_reporter._settings_provider,
            "BackendService должен wire settings_provider в _error_reporter",
        )
        self.assertTrue(callable(self._svc._error_reporter._settings_provider))
        self.assertEqual(
            self._svc._error_reporter._settings_provider,
            self._svc._settings_svc.cached_settings,
        )


if __name__ == "__main__":
    unittest.main()
