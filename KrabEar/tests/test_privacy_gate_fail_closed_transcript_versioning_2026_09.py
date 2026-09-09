"""Privacy-гейты TranscriptVersionManager обязаны быть fail-CLOSED (2026-09).

ЧТО НАЙДЕНО
-----------
``TranscriptVersionManager._is_privacy_mode()`` ловит любой сбой
``settings_fn`` и возвращает ``False`` — fail-OPEN. IPC-хендлеры
``get_transcript_versions`` / ``revert_transcript_version`` и метод
``diff_versions`` уже гейтятся, но при OSError (ENOSPC/EMFILE/EACCES в
фазе flock ``load_settings``) гейт открывается и отдаёт cleartext версий.

``settings_fn=None`` (data-dir-only конструктор) остаётся no-op: это
осознанный backward-compat, не сбой чтения. Нет проводки в service.py.

Эталон: ``recording_core_service._privacy_mode_enabled`` +
``test_privacy_gate_fail_closed_2026_09_01.py``.
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
    # Именно OSError, а не StateStoreLockTimeout: cached_settings() ловит
    # только второй, а первый документирован как реалистичный в _lock().
    raise OSError(24, "Too many open files")


def _make_manager(settings_fn, *, seed: bool = True):
    from backend.transcript_versioning import TranscriptVersionManager

    tmp = tempfile.TemporaryDirectory()
    mgr = TranscriptVersionManager(Path(tmp.name), settings_fn=settings_fn)
    if seed:
        # Сидим версии при выключенном гейте: settings_fn может уже бросать.
        seed_mgr = TranscriptVersionManager(Path(tmp.name), settings_fn=None)
        seed_mgr.save_version("item-1", _SECRET, "stt_raw")
        seed_mgr.save_version("item-1", _SECRET + " v2", "manual")
    return mgr, tmp


class TranscriptVersioningPrivacyGateFailsClosedTests(unittest.TestCase):
    """Неизвестное состояние приватности обязано читаться как «privacy ON»."""

    def tearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def _mgr(self, settings_fn, *, seed: bool = True):
        mgr, tmp = _make_manager(settings_fn, seed=seed)
        self._tmp = tmp
        return mgr

    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        mgr = self._mgr(_raises_oserror, seed=False)
        self.assertTrue(
            mgr._is_privacy_mode(),
            "сбой settings_fn() обязан читаться как privacy ON (fail-closed)",
        )

    def test_privacy_helper_returns_true_when_settings_missing(self) -> None:
        """settings_fn вернул None — состояние неизвестно, не «privacy OFF»."""
        mgr = self._mgr(lambda: None, seed=False)
        self.assertTrue(
            mgr._is_privacy_mode(),
            "отсутствующие settings обязаны читаться как privacy ON (fail-closed)",
        )

    def test_privacy_helper_returns_real_flag_on_normal_path(self) -> None:
        mgr = self._mgr(lambda: {"privacy_mode_enabled": False}, seed=False)
        self.assertFalse(mgr._is_privacy_mode())

        mgr_on = self._mgr(lambda: {"privacy_mode_enabled": True}, seed=False)
        self.assertTrue(mgr_on._is_privacy_mode())

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        """Отсутствие ключа ≠ сбой: настройки прочитаны, режим просто выключен."""
        mgr = self._mgr(lambda: {}, seed=False)
        self.assertFalse(mgr._is_privacy_mode())

    def test_settings_fn_none_is_noop(self) -> None:
        """Optional settings_fn=None — гейт no-op (data-dir-only конструктор)."""
        mgr = self._mgr(None, seed=False)
        self.assertFalse(mgr._is_privacy_mode())

    def test_partial_new_without_init_does_not_raise(self) -> None:
        """__new__ без __init__: AttributeError не должен ронять и не маскирует
        сбой settings как privacy ON через getattr-обёртку."""
        from backend.transcript_versioning import TranscriptVersionManager

        mgr = TranscriptVersionManager.__new__(TranscriptVersionManager)
        self.assertFalse(mgr._is_privacy_mode())

    def test_get_versions_hides_text_when_settings_raise(self) -> None:
        mgr = self._mgr(_raises_oserror)
        res = mgr.handle_get_transcript_versions({"item_id": "item-1"})
        self.assertEqual(res.get("versions"), [])
        self.assertEqual(res.get("total"), 0)
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        self.assertNotIn(_SECRET, str(res))

    def test_revert_hides_text_when_settings_raise(self) -> None:
        mgr = self._mgr(_raises_oserror)
        res = mgr.handle_revert_transcript_version(
            {"item_id": "item-1", "version_num": 1},
        )
        self.assertEqual(res.get("ok"), False)
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        self.assertNotIn(_SECRET, str(res))

    def test_diff_hides_text_when_settings_raise(self) -> None:
        mgr = self._mgr(_raises_oserror)
        res = mgr.diff_versions("item-1", 1, 2)
        self.assertEqual(res.get("text_v1"), "")
        self.assertEqual(res.get("text_v2"), "")
        self.assertEqual(res.get("unified_diff"), [])
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        self.assertNotIn(_SECRET, str(res))


if __name__ == "__main__":
    unittest.main()
