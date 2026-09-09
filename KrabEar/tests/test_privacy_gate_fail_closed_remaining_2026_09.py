"""Privacy-гейты remaining siblings обязаны быть fail-CLOSED (2026-09).

Не покрыто открытыми PR #2005–#2014:
  * ``AutoBackupManager`` — ``except Exception: pass`` копирует history.ndjson
  * ``RecapScheduler`` — ``_current_settings()`` глотает IO в ``{}`` → email дайджеста
  * ``MetadataEnricher`` — собственный ``_get_runtime_setting`` except→default
  * ``TranslationService`` — glossary/vocabulary suggestions из истории

Эталон: ``RecordingCoreService._privacy_mode_enabled`` +
``test_privacy_gate_fail_closed_2026_09_01.py``.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _raises_oserror(*_a, **_k):
    # Именно OSError: cached_settings() ловит только StateStoreLockTimeout.
    raise OSError(24, "Too many open files")


def _make_backup_store(data_dir: Path) -> MagicMock:
    store = MagicMock()
    store.data_dir = str(data_dir)
    store.history_path = data_dir / "history.ndjson"
    store.tombstones_path = data_dir / "tombstones.ndjson"
    store.status_path = data_dir / "status.json"
    store.settings_path = data_dir / "settings.json"
    store.count_active_items.return_value = 3
    store.history_path.write_text("секретный транскрипт владельца\n", encoding="utf-8")
    store.settings_path.write_text("{}", encoding="utf-8")
    return store


def _make_backup(settings_fn):
    from backend.auto_backup import AutoBackupManager

    tmp = tempfile.mkdtemp()
    return AutoBackupManager(
        store=_make_backup_store(Path(tmp)),
        interval_hours=0,
        settings_fn=settings_fn,
    )


def _make_recap(settings_provider):
    from backend.recap_scheduler import RecapScheduler

    tmp = tempfile.mkdtemp()
    sender = MagicMock()
    digest_gen = MagicMock()
    digest = MagicMock()
    digest.total_recordings = 1
    digest.formatted_markdown = "секретный транскрипт владельца"
    digest.top_topics = ["секрет"]
    digest_gen.generate_digest.return_value = digest
    sched = RecapScheduler(
        email_sender=sender,
        digest_generator=digest_gen,
        store=MagicMock(),
        data_dir=tmp,
        recap_email_to="owner@example.com",
        recap_time_hour=20,
        enabled=True,
        check_interval_sec=1,
        settings_provider=settings_provider,
    )
    return sched, sender, digest_gen


def _make_enricher(settings_provider):
    from backend.metadata_enricher import MetadataEnricher

    return MetadataEnricher(settings_provider=settings_provider)


def _secret_item() -> dict:
    return {
        "text": "секретный транскрипт владельца технологии программирование данные",
        "duration_sec": 10.0,
        "confidence": 0.9,
        "has_diarization": False,
        "has_llm_enhancement": False,
        "timestamp": "",
    }


def _make_translation(cached_settings):
    from backend.translation_service import TranslationService

    store = MagicMock()
    store.get_history_page.return_value = (
        [{"text": "секретный транскрипт владельца Krab Krab Krab"}],
        None,
    )
    return TranslationService(
        translator=MagicMock(),
        store=store,
        cached_settings=cached_settings,
        invalidate_settings_cache=lambda: None,
    )


class AutoBackupPrivacyGateFailsClosedTests(unittest.TestCase):
    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        mgr = _make_backup(_raises_oserror)
        self.assertTrue(
            mgr._is_privacy_mode(),
            "сбой settings_fn обязан читаться как privacy ON (fail-closed)",
        )

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        mgr = _make_backup(lambda: {})
        self.assertFalse(mgr._is_privacy_mode())

    def test_backup_skipped_when_settings_raise(self) -> None:
        mgr = _make_backup(_raises_oserror)
        result = mgr.check_and_backup()
        self.assertFalse(result.get("backed_up"))
        self.assertEqual(result.get("skipped_reason"), "privacy_mode")
        self.assertFalse(
            (Path(mgr.store.data_dir) / "backups").exists(),
            "backups/ не должен создаваться, пока состояние приватности неизвестно",
        )


class RecapSchedulerPrivacyGateFailsClosedTests(unittest.TestCase):
    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        sched, _, _ = _make_recap(_raises_oserror)
        self.assertTrue(
            sched._is_privacy_mode(),
            "сбой settings_provider обязан читаться как privacy ON (fail-closed)",
        )

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        sched, _, _ = _make_recap(lambda: {})
        self.assertFalse(sched._is_privacy_mode())

    def test_send_recap_blocked_when_settings_raise(self) -> None:
        sched, sender, digest_gen = _make_recap(_raises_oserror)
        result = sched.send_recap("2026-09-09")
        sender.send.assert_not_called()
        digest_gen.generate_digest.assert_not_called()
        self.assertFalse(result.get("sent"))
        self.assertEqual(result.get("reason"), "privacy_mode_active")


class MetadataEnricherPrivacyGateFailsClosedTests(unittest.TestCase):
    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        enricher = _make_enricher(_raises_oserror)
        self.assertTrue(
            enricher._privacy_mode_enabled(),
            "сбой settings_provider обязан читаться как privacy ON (fail-closed)",
        )

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        enricher = _make_enricher(lambda: {})
        self.assertFalse(enricher._privacy_mode_enabled())

    def test_topics_redacted_when_settings_raise(self) -> None:
        enricher = _make_enricher(_raises_oserror)
        result = enricher.enrich(_secret_item())
        self.assertEqual(
            result["metadata"].get("topics"),
            [],
            "topics обязаны быть пусты, пока состояние приватности неизвестно",
        )


class TranslationServicePrivacyGateFailsClosedTests(unittest.TestCase):
    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        svc = _make_translation(_raises_oserror)
        self.assertTrue(
            svc._is_privacy_mode(),
            "сбой cached_settings обязан читаться как privacy ON (fail-closed)",
        )

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        svc = _make_translation(lambda: {})
        self.assertFalse(svc._is_privacy_mode())

    def test_vocabulary_suggestions_blocked_when_settings_raise(self) -> None:
        svc = _make_translation(_raises_oserror)
        result = svc.handle_get_vocabulary_suggestions({})
        self.assertEqual(result.get("suggestions"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        svc.store.get_history_page.assert_not_called()

    def test_glossary_suggestions_blocked_when_settings_raise(self) -> None:
        svc = _make_translation(_raises_oserror)
        result = svc.handle_get_glossary_suggestions({})
        self.assertEqual(result.get("suggestions"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        svc.store.get_history_page.assert_not_called()


class NewWithoutInitEnabledVsPrivacyTests(unittest.TestCase):
    """``__new__`` без ``__init__``: ENABLED → False, privacy helper — не OFF.

    ``getattr(..., False)`` / ``except AttributeError: return False`` годится
    для флага ``enabled``. Тот же паттерн на privacy-хелпере — fail-OPEN.
    """

    def test_new_without_init_enabled_false_privacy_helper_not_open(self) -> None:
        from backend.recap_scheduler import RecapScheduler

        sched = RecapScheduler.__new__(RecapScheduler)
        try:
            enabled = sched.enabled
        except AttributeError:
            enabled = False
        self.assertFalse(
            bool(getattr(sched, "enabled", False) or enabled),
            "ENABLED через getattr/AttributeError обязан быть False",
        )
        self.assertTrue(
            sched._is_privacy_mode(),
            "privacy helper не должен трактовать AttributeError как privacy OFF",
        )


if __name__ == "__main__":
    unittest.main()
