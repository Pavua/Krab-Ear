"""Privacy-гейты AutoGlossaryBuilder обязаны быть fail-CLOSED (2026-09).

ЧТО НАЙДЕНО
-----------
``AutoGlossaryBuilder._is_privacy_mode_active()`` ловит любой сбой чтения
настроек и возвращает ``False`` — fail-OPEN. Glossary строится из
``source_text``/``text`` истории; при ``OSError`` (ENOSPC/EMFILE/EACCES в
фазе flock) термины из транскриптов уходят в кэш и в STT ``initial_prompt``.

``get_cached()`` не гейтит privacy вовсе: кэш, собранный до включения
режима, отдаёт transcript-derived terms.

``settings_provider=None`` после ``__init__`` остаётся privacy OFF
(существующие unit-тесты с FakeStore ждут экстракцию). Атрибут, который
так и не появился (``__new__`` без ``__init__``), — это уже неизвестное
состояние, не «режим выключен».

Эталон: ``RecordingCoreService._privacy_mode_enabled`` +
``test_privacy_gate_fail_closed_2026_09_01.py``.
Wiring ``service.py`` не трогаем; прод уже передаёт
``settings_provider=self._settings_svc.cached_settings``.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_SECRET_TERM = "СекретныйГлоссарий"
_SECRET_TEXT = f"{_SECRET_TERM} встречается в транскрипте владельца"


def _raises_oserror(*_a, **_k):
    # Именно OSError, а не StateStoreLockTimeout: cached_settings() ловит
    # только второй, а первый документирован как реалистичный в _lock().
    raise OSError(24, "Too many open files")


def _now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _secret_items() -> list[dict]:
    return [
        {
            "text": _SECRET_TEXT,
            "source_text": _SECRET_TEXT,
            "ts": _now_ts(),
        }
        for _ in range(3)
    ]


class _FakeStore:
    """Minimal stub StateStore with optional broken load_settings()."""

    def __init__(self, items=None, *, load_settings=None):
        self._items = items or []
        self.history_calls = 0
        if load_settings is not None:
            self.load_settings = load_settings

    def get_history_page(self, cursor=None, limit=500):
        self.history_calls += 1
        return self._items, None


def _make_builder(settings_side_effect, *, items=None, data_dir=None):
    from core.auto_glossary import AutoGlossaryBuilder

    store = _FakeStore(items if items is not None else _secret_items())
    return AutoGlossaryBuilder(
        store=store,
        data_dir=data_dir,
        settings_provider=settings_side_effect,
    ), store


class AutoGlossaryPrivacyGateFailsClosedTests(unittest.TestCase):
    """Неизвестное состояние приватности обязано читаться как «privacy ON»."""

    def test_privacy_on_returns_empty_and_skips_history(self) -> None:
        builder, store = _make_builder(lambda: {"privacy_mode_enabled": True})
        result = builder.build(force=True)
        self.assertEqual(result, [])
        self.assertEqual(store.history_calls, 0)
        self.assertNotIn(_SECRET_TERM, result)
        self.assertEqual(builder.get_cached(), [])

    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        builder, _store = _make_builder(_raises_oserror)
        self.assertTrue(
            builder._is_privacy_mode_active(),
            "сбой settings_provider обязан читаться как privacy ON (fail-closed)",
        )

    def test_privacy_helper_returns_real_flag_on_normal_path(self) -> None:
        builder_off, _ = _make_builder(lambda: {"privacy_mode_enabled": False})
        self.assertFalse(builder_off._is_privacy_mode_active())

        builder_on, _ = _make_builder(lambda: {"privacy_mode_enabled": True})
        self.assertTrue(builder_on._is_privacy_mode_active())

        builder_alias, _ = _make_builder(lambda: {"privacy_mode": True})
        self.assertTrue(builder_alias._is_privacy_mode_active())

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        """Отсутствие ключа ≠ сбой: настройки прочитаны, режим просто выключен."""
        builder, _ = _make_builder(lambda: {})
        self.assertFalse(builder._is_privacy_mode_active())

    def test_missing_settings_svc_attribute_fail_closed(self) -> None:
        """``__new__`` без ``__init__``: нет settings_provider → privacy ON.

        Не путать с явным ``settings_provider=None`` после конструктора —
        тот путь оставляем OFF, иначе существующие FakeStore-тесты
        перестанут извлекать термины.
        """
        from core.auto_glossary import AutoGlossaryBuilder

        builder = AutoGlossaryBuilder.__new__(AutoGlossaryBuilder)
        self.assertTrue(
            builder._is_privacy_mode_active(),
            "отсутствующий settings_provider (не сконструирован) = privacy ON",
        )
        self.assertEqual(builder.get_cached(), [])

    def test_broken_load_settings_fail_closed(self) -> None:
        """settings_provider = store.load_settings; IO-сбой → не читаем историю."""
        from core.auto_glossary import AutoGlossaryBuilder

        store = _FakeStore(_secret_items(), load_settings=_raises_oserror)
        builder = AutoGlossaryBuilder(
            store=store,
            settings_provider=store.load_settings,
        )
        self.assertTrue(builder._is_privacy_mode_active())
        result = builder.build(force=True)
        self.assertEqual(result, [])
        self.assertEqual(store.history_calls, 0)
        self.assertNotIn(_SECRET_TERM, result)
        self.assertEqual(builder.get_cached(), [])

    def test_get_cached_hides_stale_terms_when_privacy_on(self) -> None:
        builder, _store = _make_builder(lambda: {"privacy_mode_enabled": True})
        builder._cache = [_SECRET_TERM]
        builder._cache_built_at = time.time()
        self.assertEqual(builder.get_cached(), [])
        self.assertNotIn(_SECRET_TERM, builder.get_cached())

    def test_privacy_off_still_extracts(self) -> None:
        """Существующие тесты ждут экстракцию при явно выключенном privacy."""
        items = [
            {
                "text": "TensorFlow и PyTorch популярны",
                "source_text": "TensorFlow и PyTorch популярны",
                "ts": _now_ts(),
            },
            {
                "text": "TensorFlow используется в ML",
                "source_text": "TensorFlow используется в ML",
                "ts": _now_ts(),
            },
            {
                "text": "TensorFlow — фреймворк от Google",
                "source_text": "TensorFlow — фреймворк от Google",
                "ts": _now_ts(),
            },
        ]
        builder, store = _make_builder(
            lambda: {"privacy_mode_enabled": False},
            items=items,
        )
        result = builder.build(force=True)
        self.assertIn("TensorFlow", result)
        self.assertGreater(store.history_calls, 0)

    def test_init_none_provider_still_extracts(self) -> None:
        """Явный settings_provider=None после __init__ — путь FakeStore-тестов."""
        from core.auto_glossary import AutoGlossaryBuilder

        items = [
            {
                "text": "TensorFlow используется в ML",
                "source_text": "TensorFlow используется в ML",
                "ts": _now_ts(),
            },
            {
                "text": "TensorFlow популярен в Python",
                "source_text": "TensorFlow популярен в Python",
                "ts": _now_ts(),
            },
            {
                "text": "TensorFlow — фреймворк Google",
                "source_text": "TensorFlow — фреймворк Google",
                "ts": _now_ts(),
            },
        ]
        store = _FakeStore(items)
        builder = AutoGlossaryBuilder(store=store)
        self.assertIsNone(builder._settings_provider)
        self.assertFalse(builder._is_privacy_mode_active())
        result = builder.build(force=True)
        self.assertIn("TensorFlow", result)

    def test_broken_provider_does_not_persist_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            builder, store = _make_builder(
                _raises_oserror,
                data_dir=tmp_path,
            )
            result = builder.build(force=True)
            self.assertEqual(result, [])
            self.assertEqual(store.history_calls, 0)
            self.assertFalse((tmp_path / "auto_glossary.json").exists())


if __name__ == "__main__":
    unittest.main()
