"""Раздельные бюджеты STT (спека 2026-08-26-stt-timeout-budgets-design.md).

Инцидент-источник: 2026-08-26 04:21–06:21 — 4.71 с аудио держали
TRANSCRIBE_TIMEOUT_SEC=3600 дважды (7184 с суммарно), абандоненный поток
2 часа удерживал MLX-локи, тост «Критическая ошибка» пришёл через 2 часа.
"""
from __future__ import annotations

import ast
import concurrent.futures
import logging
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

    def test_settings_getter_exception_falls_back_to_defaults(self):
        # Находка 1: чтение настроек делит файловый лок с историей и умеет
        # бросать StateStoreLockTimeout (и что угодно ещё) — сбой геттера не
        # смеет ронять STT, обязан fail-open в дефолты профиля.
        class _BoomError(Exception):
            pass

        def _boom_get(key, default):
            raise _BoomError("settings unavailable under lock contention")

        with stt_budget.stt_budget_scope(
            stt_budget.INTERACTIVE, settings_get=_boom_get
        ):
            got = stt_budget.resolve_attempt_timeout_sec(4.71)
        self.assertAlmostEqual(got, 104.13, delta=0.5)

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


    def test_quiet_scope_logs_at_debug_not_info(self):
        # Находка 2: живые субтитры открывают scope раз в ~3с — INFO на
        # каждый scope залил бы лог (~1200 строк/час). quiet=True обязан
        # писать ту же строку на DEBUG, не на INFO.
        with self.assertLogs(
            "KrabEar.Core.STTBudget", level="DEBUG"
        ) as cm:
            with stt_budget.stt_budget_scope(
                stt_budget.INTERACTIVE, quiet=True
            ):
                pass
        self.assertFalse(
            any(record.levelno >= logging.INFO for record in cm.records),
            f"quiet=True не должен писать на INFO+: {cm.output}",
        )

    def test_default_scope_logs_at_info(self):
        # Сиблинг предыдущего теста: диктовка/импорт (quiet=False, дефолт)
        # обязаны сохранить существующую INFO-наблюдаемость.
        with self.assertLogs(
            "KrabEar.Core.STTBudget", level="INFO"
        ) as cm:
            with stt_budget.stt_budget_scope(stt_budget.INTERACTIVE):
                pass
        self.assertTrue(
            any(record.levelno >= logging.INFO for record in cm.records)
        )

    def test_call_in_scope_propagates_quiet(self):
        # quiet обязан пробрасываться через call_in_scope тем же кваргом.
        def _noop(**kw):
            return None

        with self.assertLogs(
            "KrabEar.Core.STTBudget", level="DEBUG"
        ) as cm:
            stt_budget.call_in_scope(
                _noop, profile=stt_budget.INTERACTIVE, quiet=True
            )
        self.assertFalse(
            any(record.levelno >= logging.INFO for record in cm.records)
        )


class BudgetSettingsWiringTests(unittest.TestCase):
    """§9: DEFAULT_SETTINGS + _RANGE_FIELDS (правило wave-34) синхронны
    с KNOB_BOUNDS — единственным источником границ в core."""

    def test_default_settings_carry_all_knobs(self):
        from core.config import DEFAULT_SETTINGS
        for key, (_lo, _hi, default) in stt_budget.KNOB_BOUNDS.items():
            self.assertIn(key, DEFAULT_SETTINGS, key)
            self.assertEqual(DEFAULT_SETTINGS[key], default, key)

    def test_range_fields_clamp_all_knobs_with_same_bounds(self):
        # _RANGE_FIELDS достраивается из KNOB_BOUNDS импортом (validator уже
        # импортирует core — см. SUPPORTED_GIGAAM_ASR_MODES, :19). Тест —
        # guard от удаления этой достройки, не от рассинхрона литералов.
        from backend.settings_validator import _RANGE_FIELDS
        for key, (lo, hi, default) in stt_budget.KNOB_BOUNDS.items():
            self.assertIn(key, _RANGE_FIELDS, key)
            v_lo, v_hi, v_default, v_coerce = _RANGE_FIELDS[key]
            self.assertEqual((v_lo, v_hi, v_default), (lo, hi, default), key)
            self.assertIs(v_coerce, float, key)


def _engine_source() -> str:
    return (PROJECT_ROOT / "core" / "engine.py").read_text(encoding="utf-8")


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"функция {name} не найдена в engine.py")


def _attr_names(node: ast.AST) -> list[str]:
    return [n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)]


class EngineBudgetContractTests(unittest.TestCase):
    """Спека-тесты 10/14/17 (AST, привязка к именам функций — не строкам)."""

    FUNCS = ("_maybe_multipass_retry", "_transcribe_with_fallback_impl")

    def test_no_direct_transcribe_timeout_sec_in_stt_loops(self):
        # Спека-тест 10 (приём PR #1953): сиблинги не разойдутся снова.
        tree = ast.parse(_engine_source())
        for fname in self.FUNCS:
            node = _function_node(tree, fname)
            self.assertNotIn(
                "TRANSCRIBE_TIMEOUT_SEC", _attr_names(node),
                f"{fname} читает settings.TRANSCRIBE_TIMEOUT_SEC напрямую — "
                "обязана идти через stt_budget.resolve_attempt_timeout_sec",
            )

    def test_budget_helpers_are_wired_into_both_loops(self):
        tree = ast.parse(_engine_source())
        for fname in self.FUNCS:
            attrs = _attr_names(_function_node(tree, fname))
            self.assertIn("resolve_attempt_timeout_sec", attrs, fname)
            self.assertIn("budget_exhausted", attrs, fname)
            # Гейт блэклиста — напрямую или через хелпер engine.
            self.assertTrue(
                "timeout_blacklist_allowed" in attrs
                or "_blacklist_allowed_for" in attrs,
                f"{fname} не гейтит запись в _unavailable_models (§4.7)",
            )

    def test_adapter_branch_applies_min_budget_floor(self):
        # Спека-тест 17 (§4.8): floor против осиротевшего GigaAM-subprocess.
        node = _function_node(
            ast.parse(_engine_source()), "_transcribe_with_fallback_impl"
        )
        self.assertIn("ADAPTER_MIN_BUDGET_SEC", _attr_names(node))

    def test_budget_exhausted_error_code_registered(self):
        from backend.error_codes import ERROR_REGISTRY
        self.assertIn("stt.budget_exhausted", ERROR_REGISTRY)
        self.assertEqual(
            ERROR_REGISTRY["stt.budget_exhausted"]["severity"], "error"
        )


class MultipassBudgetBehaviorTests(unittest.TestCase):
    """Спека-тесты 12/14 (поведение): фейковый каскад ретраев multipass."""

    def _make_engine(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from core.engine import AudioEngine

        eng = object.__new__(AudioEngine)
        eng.current_model = "balanced-model"
        eng._unavailable_models = {}
        # Детерминируем читателей result: без внутренностей segments-логики.
        eng._raw_confidence_from_result = (
            lambda r: float(r.get("confidence") or 0.0)
        )
        calls: list[str] = []

        def _fake_transcribe_model(audio, model, prompt, language=None):
            calls.append(model)
            return {"text": "retry-text", "confidence": 0.99}

        eng._transcribe_model = _fake_transcribe_model
        fake_settings = SimpleNamespace(
            STT_MIN_CONFIDENCE_THRESHOLD=0.9,
            STT_MAX_RETRIES=2,
            model_max_list=["big-a", "big-b"],
            NETWORK_MODE="offline_strict",
        )
        return eng, calls, fake_settings, patch

    def test_multipass_retries_when_budget_alive(self):
        eng, calls, fake_settings, patch = self._make_engine()
        first = {"text": "низко", "confidence": 0.1, "model_used": "gigaam"}
        with patch("core.engine.settings", fake_settings), patch(
            "core.engine.should_skip_second_mlx_checkpoint",
            return_value=False,
        ):
            with stt_budget.stt_budget_scope(
                stt_budget.INTERACTIVE, deadline_sec=600.0
            ):
                result = eng._maybe_multipass_retry(None, "", "ru", first)
        self.assertEqual(calls, ["big-a"])  # 0.99 >= 0.9 → break после первой
        self.assertEqual(result["text"], "retry-text")

    def test_multipass_skips_all_retries_when_deadline_exhausted(self):
        # Спека-тест 12: счётчик попыток = 0, следующая модель не пробуется.
        eng, calls, fake_settings, patch = self._make_engine()
        first = {"text": "низко", "confidence": 0.1, "model_used": "gigaam"}
        with patch("core.engine.settings", fake_settings), patch(
            "core.engine.should_skip_second_mlx_checkpoint",
            return_value=False,
        ):
            with stt_budget.stt_budget_scope(
                stt_budget.INTERACTIVE, deadline_sec=0.0
            ):
                result = eng._maybe_multipass_retry(None, "", "ru", first)
        self.assertEqual(calls, [])
        self.assertEqual(result["text"], "низко")
        # Спека-тест 14: прерывание по бюджету НЕ отравляет блэклист.
        self.assertEqual(eng._unavailable_models, {})


if __name__ == "__main__":
    unittest.main()
