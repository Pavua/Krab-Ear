"""probe_llm_http — «однократный ping LM Studio» — не должен грузить модель.

Живой инцидент 03.09.2026: при каждом старте агента секция «Рекомендованная
настройка» зовёт ``apply_recommended_setup {dry_run: true}``; гейт ключа
``llm_rewrite_enabled`` дёргает ``probe_llm_http``, а тот делал
``LLMRewriter.warmup()`` = POST /v1/chat/completions. LM Studio на такой запрос
грузит модель JIT — 11 ГБ GigaChat поднимались при ВЫКЛЮЧЕННОМ рерайтере.
Тот же класс, что чинили в ``llm_probe.py`` (PR #364): сиблинг остался тяжёлым.

Контракт: пинг = ``passive_health_check()`` (GET /api/v1/models), ``warmup()``
не вызывается; форма ответа ``{reachable, latency_ms, model}`` сохраняется.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.health_check_service import HealthCheckService  # noqa: E402


class _Rewriter:
    _model = "gigachat3.1-10b-a1.8b-mlx-oq8"
    _last_latency_ms = 12000

    def __init__(self, passive=(True, True)):
        self.warmup_calls = 0
        self.passive_calls = 0
        self._passive = passive

    def warmup(self, timeout_sec=None):
        self.warmup_calls += 1
        return True

    def passive_health_check(self):
        self.passive_calls += 1
        return self._passive


def _svc(rw):
    """Все обязательные коллабораторы — None: пинг трогает только llm_rewriter."""
    import inspect
    kwargs = {}
    for name, prm in inspect.signature(HealthCheckService.__init__).parameters.items():
        if name == "self" or prm.default is not inspect.Parameter.empty:
            continue
        kwargs[name] = None
    kwargs["llm_rewriter"] = rw
    return HealthCheckService(**kwargs)


class ProbeLlmHttpIsPassiveTest(unittest.TestCase):
    def test_probe_does_not_call_warmup(self):
        rw = _Rewriter()
        result = _svc(rw).handle_probe_llm_http({})
        self.assertEqual(rw.warmup_calls, 0, "пинг сделал POST chat/completions → JIT-загрузка модели")
        self.assertEqual(rw.passive_calls, 1)
        self.assertTrue(result["reachable"])
        self.assertEqual(result["model"], rw._model)

    def test_unreachable_when_passive_says_so(self):
        rw = _Rewriter(passive=(False, False))
        result = _svc(rw).handle_probe_llm_http({})
        self.assertFalse(result["reachable"])
        self.assertEqual(rw.warmup_calls, 0)

    def test_latency_is_measured_not_taken_from_last_rewrite(self):
        rw = _Rewriter()
        result = _svc(rw).handle_probe_llm_http({})
        self.assertIsInstance(result["latency_ms"], int)
        self.assertLess(result["latency_ms"], 1000)

    def test_has_model_flag_exposed(self):
        rw = _Rewriter(passive=(True, False))
        result = _svc(rw).handle_probe_llm_http({})
        self.assertTrue(result["reachable"])
        self.assertFalse(result["has_model"])
