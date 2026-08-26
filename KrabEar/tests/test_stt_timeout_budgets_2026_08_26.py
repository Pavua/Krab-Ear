"""Раздельные бюджеты STT (спека 2026-08-26-stt-timeout-budgets-design.md).

Инцидент-источник: 2026-08-26 04:21–06:21 — 4.71 с аудио держали
TRANSCRIBE_TIMEOUT_SEC=3600 дважды (7184 с суммарно), абандоненный поток
2 часа удерживал MLX-локи, тост «Критическая ошибка» пришёл через 2 часа.
"""
from __future__ import annotations

import concurrent.futures
import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import stt_budget  # noqa: E402


class BudgetFormulaTests(unittest.TestCase):
    """§4.2/§4.4: формула overhead + duration×factor с потолком профиля."""

    def test_incident_audio_interactive_budget_is_scaled_not_3600(self):
        # Спека-тест 1: 4.71 с → 90 + 4.71×3 = 104.13, НЕ 3600.
        with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE):
            got = stt_budget.resolve_attempt_timeout_sec(4.71)
        self.assertAlmostEqual(got, 104.13, delta=0.5)
        self.assertLess(got, 3600.0)

    def test_batch_budget_is_larger_than_interactive_for_same_audio(self):
        # Спека-тест 2.
        with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE):
            inter = stt_budget.resolve_attempt_timeout_sec(4.71)
        with stt_budget.stt_budget_scope(stt_budget.BATCH):
            batch = stt_budget.resolve_attempt_timeout_sec(4.71)
        self.assertGreater(batch, inter)

    def test_profile_cap_applies_for_52_minute_dictation(self):
        # Спека-тест 3: 52 мин = 3120 с → 90 + 3120×3 = 9450 → cap 1800.
        with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE):
            got = stt_budget.resolve_attempt_timeout_sec(3120.0)
        self.assertEqual(got, 1800.0)

    def test_unknown_duration_falls_back_to_profile_cap(self):
        # Спека-тест 4: fail-open в потолок ПРОФИЛЯ, не в час на interactive.
        with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE):
            self.assertEqual(stt_budget.resolve_attempt_timeout_sec(None), 1800.0)
        with stt_budget.stt_budget_scope(stt_budget.BATCH):
            self.assertEqual(stt_budget.resolve_attempt_timeout_sec(None), 3600.0)

    def test_no_scope_defaults_to_interactive(self):
        # §5: незалейбленный путь = interactive (fail-fast), не час.
        self.assertEqual(stt_budget.current_profile(), stt_budget.INTERACTIVE)
        self.assertEqual(stt_budget.resolve_attempt_timeout_sec(None), 1800.0)
        self.assertIsNone(stt_budget.remaining_sec())
        self.assertFalse(stt_budget.budget_exhausted())

    def test_explicit_deadline_clips_attempt_budget(self):
        # Спека-тест 5: REST deadline 30 с урезает расчётные 104 с.
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, deadline_sec=30.0
        ):
            got = stt_budget.resolve_attempt_timeout_sec(4.71)
        self.assertLessEqual(got, 30.0)
        self.assertGreaterEqual(got, stt_budget.MIN_USEFUL_ATTEMPT_SEC)

    def test_expired_deadline_floors_at_min_useful_and_reports_exhausted(self):
        # Спека-тесты 6 и 16: future.result никогда не получит отрицательный
        # таймаут — resolve floor'ится, а budget_exhausted говорит «не сабмить».
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, deadline_sec=0.0
        ):
            self.assertTrue(stt_budget.budget_exhausted())
            got = stt_budget.resolve_attempt_timeout_sec(4.71)
        self.assertEqual(got, stt_budget.MIN_USEFUL_ATTEMPT_SEC)

    def test_remaining_sec_decreases_monotonically(self):
        # Спека-тест 6.
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, deadline_sec=60.0
        ):
            first = stt_budget.remaining_sec()
            time.sleep(0.05)
            second = stt_budget.remaining_sec()
        self.assertLess(second, first)

    def test_settings_snapshot_overrides_defaults(self):
        # Спека-тест 18 (ядро): значения берутся из снапшота на входе scope,
        # НЕ из engine._settings_get (в REST-процессе тот — заглушка).
        snap = {"stt_timeout_overhead_sec": 30.0,
                "stt_timeout_interactive_factor": 1.0}
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, settings_get=snap.get
        ):
            got = stt_budget.resolve_attempt_timeout_sec(10.0)
        self.assertAlmostEqual(got, 40.0, delta=0.01)

    def test_knob_garbage_is_clamped_or_defaulted(self):
        # Спека-тест 9 (модульная половина): NaN/мусор/1e9 не проходят.
        cases = {
            "stt_timeout_overhead_sec": float("nan"),
            "stt_timeout_interactive_factor": "мусор",
            "stt_timeout_interactive_max_sec": 10 ** 9,
        }
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, settings_get=cases.get
        ):
            got = stt_budget.resolve_attempt_timeout_sec(None)
        # max_sec заклампился к верхней границе 7200, не к 10**9.
        self.assertLessEqual(got, 7200.0)

    def test_timeout_blacklist_allowed_semantics(self):
        # §4.7: исчерпанный бюджет запроса → блэклист запрещён;
        # живой дедлайн / нет дедлайна → разрешён.
        self.assertTrue(stt_budget.timeout_blacklist_allowed())
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, deadline_sec=600.0
        ):
            self.assertTrue(stt_budget.timeout_blacklist_allowed())
        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, deadline_sec=0.0
        ):
            self.assertFalse(stt_budget.timeout_blacklist_allowed())


class BudgetScopeTests(unittest.TestCase):
    """§4.1: изоляция тредов, сброс токена, пропагация через call_in_scope."""

    def test_thread_isolation(self):
        # Спека-тест 7: чужой тред не видит scope главного.
        seen: dict[str, object] = {}

        def _probe():
            seen["profile"] = stt_budget.current_profile()
            seen["remaining"] = stt_budget.remaining_sec()

        with stt_budget.stt_budget_scope(
            stt_budget.BATCH, deadline_sec=600.0
        ):
            t = threading.Thread(target=_probe)
            t.start()
            t.join(timeout=5)
        self.assertEqual(seen["profile"], stt_budget.INTERACTIVE)
        self.assertIsNone(seen["remaining"])

    def test_scope_resets_on_exception(self):
        # Спека-тест 8.
        with self.assertRaises(RuntimeError):
            with stt_budget.stt_budget_scope(stt_budget.BATCH):
                raise RuntimeError("boom")
        self.assertEqual(stt_budget.current_profile(), stt_budget.INTERACTIVE)

    def test_call_in_scope_propagates_into_pool_worker_thread(self):
        # Спека-тест 13: ContextVar не наследуется тредом пула — scope обязан
        # открываться ВНУТРИ submitted callable. Это runtime-тест, который
        # поймал бы scope, открытый во Flask-треде вокруг submit.
        seen: dict[str, object] = {}

        def _fake_transcribe(path, **kw):
            seen["profile"] = stt_budget.current_profile()
            seen["remaining"] = stt_budget.remaining_sec()
            return {"text": "ok", "path": path}

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = pool.submit(
                stt_budget.call_in_scope,
                _fake_transcribe,
                "/tmp/x.wav",
                profile=stt_budget.INTERACTIVE,
                deadline_sec=42.0,
                settings_snapshot=None,
                quality_profile="balanced",
            )
            result = fut.result(timeout=10)
        finally:
            pool.shutdown(wait=True)
        self.assertEqual(result["text"], "ok")
        self.assertEqual(seen["profile"], stt_budget.INTERACTIVE)
        self.assertIsNotNone(seen["remaining"])
        self.assertLessEqual(seen["remaining"], 42.0)
        self.assertGreater(seen["remaining"], 30.0)


if __name__ == "__main__":
    unittest.main()
