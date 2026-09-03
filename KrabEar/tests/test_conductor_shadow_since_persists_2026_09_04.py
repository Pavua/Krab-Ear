"""Счётчик дней теневого режима обязан пережить перезапуск (2026-09-04).

Дирижёр памяти живёт в shadow-режиме с 20.08.2026 — четырнадцать суток на
момент разбора. Панель памяти показывает «SHADOW — решения только логируются
(N дн)», и по спеке именно эта строка должна была напомнить владельцу, что
пора решать про enforce.

🔴 Напомнить она не могла НИКОГДА: ``_shadow_since`` ставился в ``time.time()``
при входе в режим, то есть при каждом старте процесса. Живой замер 03.09:
``shadow_since`` отставал от текущего времени на 69 минут — ровно аптайм
бэкенда, а не четырнадцать суток. На четырнадцатый день строка показывала
«0 дн», и механизм, ждавший решения владельца, не мог о себе напомнить.

Класс: «аварийный механизм ждёт решения, но не в состоянии его попросить» —
сиблинг «ноль срабатываний = непроверен» и «всегда красный монитор = слепота».
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

from backend.memory_conductor import MemoryConductor  # noqa: E402


class _Settings:
    """Минимальный settings_service: shadow-режим, всё остальное по умолчанию."""

    def __init__(self, enforce: bool = False):
        self._enforce = enforce

    def cached_settings(self):
        return {
            "memory_conductor_enabled": True,
            "memory_conductor_enforce": self._enforce,
            "memory_conductor_enforce_gigaam": self._enforce,
            "memory_conductor_enforce_rewriter": self._enforce,
            "memory_conductor_enforce_brain": self._enforce,
        }


def _make_conductor(marker_path: Path, enforce: bool = False) -> MemoryConductor:
    return MemoryConductor(
        settings_service=_Settings(enforce),
        ledger=None,
        is_recording=lambda: False,
        is_meeting_active=lambda: False,
        pressure_fn=lambda: 0,
        gigaam_close_if_idle=lambda _t: False,
        gigaam_idle_sec_fn=lambda: 0.0,
        last_stt_activity_ts_fn=lambda: time.monotonic(),
        unload_model_fn=lambda *_a, **_k: True,
        load_model_fn=lambda *_a, **_k: True,
        model_loaded_fn=lambda *_a, **_k: False,
        lease_holder_fn=lambda: None,
        shadow_marker_path=marker_path,
    )


class ShadowSinceSurvivesRestartTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.marker = Path(self._tmp.name) / "conductor_shadow.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_second_process_reports_first_process_date(self):
        """Перезапуск не обнуляет счётчик: вторая жизнь видит дату первой."""
        first = _make_conductor(self.marker)
        first.get_diagnostics()
        started = first.get_diagnostics()["shadow_since"]
        self.assertIsNotNone(started, "первая жизнь обязана зафиксировать начало тени")

        # Вторая жизнь процесса: тот же маркер, объект новый.
        second = _make_conductor(self.marker)
        self.assertEqual(
            second.get_diagnostics()["shadow_since"], started,
            "после перезапуска счётчик дней в тени начался заново — панель снова "
            "покажет «0 дн», и напоминание владельцу не сработает никогда",
        )

    def test_marker_survives_as_file(self):
        """Дата лежит на диске, а не только в памяти процесса."""
        conductor = _make_conductor(self.marker)
        conductor.get_diagnostics()
        self.assertTrue(
            self.marker.exists(),
            "маркер теневого режима не записан на диск — пережить перезапуск нечему",
        )

    def test_enforce_clears_the_marker(self):
        """Переход в enforce снимает маркер: вернувшись в тень, считаем заново.

        Иначе счётчик показывал бы дни, накопленные до включения — число,
        которое ничего не значит.
        """
        shadow = _make_conductor(self.marker)
        shadow.get_diagnostics()
        self.assertTrue(self.marker.exists())

        enforcing = _make_conductor(self.marker, enforce=True)
        self.assertIsNone(
            enforcing.get_diagnostics()["shadow_since"],
            "в enforce счётчик тени обязан быть пустым",
        )
        self.assertFalse(
            self.marker.exists(),
            "маркер не снят при переходе в enforce — вернувшись в тень, панель "
            "покажет чужие накопленные дни",
        )

    def test_unreadable_marker_does_not_break_conductor(self):
        """Сбой диска не смеет ломать дирижёра — маркер лишь витрина.

        Направление отказа: лучше потерять счётчик дней, чем остановить
        механизм, который следит за памятью.
        """
        self.marker.write_text("{ это не json", encoding="utf-8")
        conductor = _make_conductor(self.marker)
        diag = conductor.get_diagnostics()  # не должно бросить
        self.assertIn("shadow_since", diag)


if __name__ == "__main__":
    unittest.main()
