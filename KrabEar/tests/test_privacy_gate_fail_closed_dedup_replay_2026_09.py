"""Privacy-гейты dedup / event replay / recording chain обязаны быть fail-CLOSED.

Кандидаты аудита (волна 2026-09, без #2008):
``auto_deduplication.py``, ``event_replay.py``, ``recording_chain.py``.

Все три ловят сбой чтения настроек и возвращают ``False`` — fail-OPEN.
``OSError`` (ENOSPC/EMFILE/EACCES в фазе flock) не является
``StateStoreLockTimeout`` и пролетает до generic except.

Эталон: ``RecordingCoreService._privacy_mode_enabled`` + rest w1212.
OSError → privacy ON; отсутствие ключа после успешного чтения → OFF.
Wiring ``service.py`` не трогаем (конфликт с #2008); тесты инжектят settings.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_SECRET = "секретный транскрипт владельца"


def _raises_oserror(*_a, **_k):
    # Именно OSError: StateStore._lock() документирует ENOSPC/EMFILE/EACCES
    # в фазе захвата как реалистичные; generic except глотает их в default=False.
    raise OSError(24, "Too many open files")


class DedupPrivacyGateFailsClosedTests(unittest.TestCase):
    """AutoDeduplicator: неизвестное состояние приватности = privacy ON."""

    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        from backend.auto_deduplication import AutoDeduplicator

        dedup = AutoDeduplicator(settings_provider=_raises_oserror)
        self.assertTrue(
            dedup._privacy_mode_enabled(),
            "сбой settings_provider обязан читаться как privacy ON (fail-closed)",
        )

    def test_privacy_helper_returns_real_flag_on_normal_path(self) -> None:
        from backend.auto_deduplication import AutoDeduplicator

        dedup_off = AutoDeduplicator(
            settings_provider=lambda key, default=False: False,
        )
        self.assertFalse(dedup_off._privacy_mode_enabled())

        dedup_on = AutoDeduplicator(
            settings_provider=lambda key, default=False: True,
        )
        self.assertTrue(dedup_on._privacy_mode_enabled())

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        from backend.auto_deduplication import AutoDeduplicator

        def provider(key: str, default: object = False) -> object:
            return {}.get(key, default)

        dedup = AutoDeduplicator(settings_provider=provider)
        self.assertFalse(dedup._privacy_mode_enabled())

    def test_check_duplicate_skipped_when_settings_raise(self) -> None:
        from backend.auto_deduplication import AutoDeduplicator, _PRIVACY_SKIPPED

        store = MagicMock()
        store.get_history_page.return_value = (
            [{"id": "h1", "text": _SECRET, "ts": "2026-09-09T00:00:00+00:00"}],
            None,
        )
        dedup = AutoDeduplicator(settings_provider=_raises_oserror)
        result = dedup.check_duplicate(
            text=_SECRET,
            timestamp="2026-09-09T00:00:01+00:00",
            store=store,
        )
        self.assertIs(result, _PRIVACY_SKIPPED)
        self.assertEqual(result.action_taken, "privacy_skipped")
        store.get_history_page.assert_not_called()

    def test_run_deduplication_skipped_when_settings_raise(self) -> None:
        from backend.auto_deduplication import AutoDeduplicator

        store = MagicMock()
        store.get_history_page.return_value = (
            [{"id": "h1", "text": _SECRET, "ts": "2026-09-09T00:00:00+00:00"}],
            None,
        )
        dedup = AutoDeduplicator(settings_provider=_raises_oserror)
        result = dedup.run_deduplication(store=store)
        self.assertEqual(result.get("skipped_reason"), "privacy_mode")
        self.assertEqual(result.get("duplicates"), [])
        self.assertNotIn(_SECRET, str(result))
        store.get_history_page.assert_not_called()


class EventReplayPrivacyGateFailsClosedTests(unittest.TestCase):
    """EventReplayManager: сбой чтения settings не должен отдавать cleartext."""

    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        from backend.event_replay import EventReplayManager

        mgr = EventReplayManager(settings_provider=_raises_oserror)
        self.assertTrue(
            mgr._is_privacy_mode(),
            "сбой settings_provider обязан читаться как privacy ON (fail-closed)",
        )
        mgr.close()

    def test_privacy_helper_returns_real_flag_on_normal_path(self) -> None:
        from backend.event_replay import EventReplayManager

        mgr_off = EventReplayManager(
            settings_provider=lambda: {"privacy_mode_enabled": False},
        )
        self.assertFalse(mgr_off._is_privacy_mode())
        mgr_off.close()

        mgr_on = EventReplayManager(
            settings_provider=lambda: {"privacy_mode_enabled": True},
        )
        self.assertTrue(mgr_on._is_privacy_mode())
        mgr_on.close()

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        from backend.event_replay import EventReplayManager

        mgr = EventReplayManager(settings_provider=lambda: {})
        self.assertFalse(mgr._is_privacy_mode())
        mgr.close()

    def test_record_event_redacts_when_settings_raise(self) -> None:
        from backend.event_replay import EventReplayManager

        mgr = EventReplayManager(
            max_buffer=100,
            settings_provider=_raises_oserror,
        )
        mgr.record_event("stt.final", {"text": _SECRET, "confidence": 0.99})
        events = mgr.get_events()
        mgr.close()

        self.assertEqual(len(events), 1)
        data = events[0]["data"]
        self.assertTrue(data.get("redacted"))
        self.assertEqual(data.get("reason"), "privacy_mode")
        self.assertNotIn("text", data)
        self.assertNotIn(_SECRET, str(events))


class _FakeHistoryItem:
    def __init__(self, item_id: str, text: str) -> None:
        self.id = item_id
        self.text = text
        self.duration_sec = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "duration_sec": self.duration_sec}


class _FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, _FakeHistoryItem] = {}

    def add(self, item_id: str, text: str) -> None:
        self._items[item_id] = _FakeHistoryItem(item_id, text)

    def get_history_item_by_id(self, item_id: str):
        return self._items.get(item_id)


class RecordingChainPrivacyGateFailsClosedTests(unittest.TestCase):
    """RecordingChainManager: сбой чтения settings не должен отдавать транскрипты."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = _FakeStore(self._tmpdir)

    def _make_mgr(self, settings_fn):
        from backend.recording_chain import RecordingChainManager

        return RecordingChainManager(store=self._store, settings_fn=settings_fn)

    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        mgr = self._make_mgr(_raises_oserror)
        self.assertTrue(
            mgr._is_privacy_mode(),
            "сбой settings_fn обязан читаться как privacy ON (fail-closed)",
        )

    def test_privacy_helper_returns_real_flag_on_normal_path(self) -> None:
        mgr_off = self._make_mgr(lambda: {"privacy_mode_enabled": False})
        self.assertFalse(mgr_off._is_privacy_mode())

        mgr_on = self._make_mgr(lambda: {"privacy_mode_enabled": True})
        self.assertTrue(mgr_on._is_privacy_mode())

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        mgr = self._make_mgr(lambda: {})
        self.assertFalse(mgr._is_privacy_mode())

    def test_get_chain_hides_transcripts_when_settings_raise(self) -> None:
        self._store.add("item-1", _SECRET)
        mgr = self._make_mgr(lambda: {"privacy_mode_enabled": False})
        chain_id = mgr.start_chain("Совещание")
        mgr.add_to_chain(chain_id, "item-1")

        mgr._settings_fn = _raises_oserror
        data = mgr.get_chain(chain_id)
        self.assertEqual(data.get("items"), [])
        self.assertEqual(data.get("total_word_count"), 0)
        self.assertTrue(data.get("privacy_mode"))
        self.assertNotIn(_SECRET, str(data))

    def test_merge_chain_text_empty_when_settings_raise(self) -> None:
        self._store.add("item-1", _SECRET)
        mgr = self._make_mgr(lambda: {"privacy_mode_enabled": False})
        chain_id = mgr.start_chain("Слияние")
        mgr.add_to_chain(chain_id, "item-1")

        mgr._settings_fn = _raises_oserror
        text = mgr.merge_chain_text(chain_id)
        self.assertEqual(text, "")
        self.assertNotIn(_SECRET, text)


if __name__ == "__main__":
    unittest.main()
