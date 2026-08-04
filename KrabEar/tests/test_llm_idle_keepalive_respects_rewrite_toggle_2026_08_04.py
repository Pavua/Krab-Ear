"""LLMRewriter idle keepalive обязан уважать llm_rewrite_enabled (2026-08-04).

Живая находка: владелец выключил «постобработку» (llm_rewrite_enabled=False) в
Krab Ear, но LM Studio всё равно загрузила модель (6.86 ГБ) через ~25 минут.
Причина — `_idle_keepalive_loop` гейтился ДРУГИМ тумблером
(`llm_idle_keepalive_enabled`, DEFAULT_SETTINGS=True, core/config.py:1134),
никак не связанным с тем, что реально выключил владелец. Каждый тик
безусловно вызывал `warmup_probe()` — реальный HTTP POST с `max_tokens: 1`,
который триггерит JIT-загрузку модели у LM Studio, независимо от того, нужна
ли постобработка вообще.

Фикс: `_idle_keepalive_tick()` (выделен из `_idle_keepalive_loop` для
тестируемости без реальных тредов/таймеров) читает `llm_rewrite_enabled`
через `_settings_getter` (тот же late-injected callable, что уже использует
`llm_autoload_timeout_sec` — см. rewrite() self-heal path) и пропускает
`warmup_probe()`, если постобработка выключена.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_llm_idle_keepalive_respects_rewrite_toggle_2026_08_04.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.llm_rewriter import LLMRewriter


def _make_rewriter() -> LLMRewriter:
    """Минимальный LLMRewriter для тестирования _idle_keepalive_tick в изоляции.

    _idle_keepalive_tick трогает только _settings_getter, warmup_probe и
    _model — реальный HTTP session/circuit breaker не нужны.
    """
    rewriter = LLMRewriter.__new__(LLMRewriter)
    rewriter._model = "test-model"
    rewriter._settings_getter = None
    rewriter.warmup_probe = MagicMock(return_value={"ok": True, "latency_ms": 5})
    return rewriter


class LLMIdleKeepaliveRewriteToggleTests(unittest.TestCase):

    def test_tick_skips_probe_when_rewrite_disabled(self):
        rewriter = _make_rewriter()
        rewriter._settings_getter = lambda key, default: (
            False if key == "llm_rewrite_enabled" else default
        )

        rewriter._idle_keepalive_tick()

        rewriter.warmup_probe.assert_not_called()

    def test_tick_fires_probe_when_rewrite_enabled(self):
        rewriter = _make_rewriter()
        rewriter._settings_getter = lambda key, default: (
            True if key == "llm_rewrite_enabled" else default
        )

        rewriter._idle_keepalive_tick()

        rewriter.warmup_probe.assert_called_once_with(timeout_sec=60.0)

    def test_tick_fires_probe_when_no_settings_getter(self):
        """Backward-compat: без settings_getter (напр. standalone-конструирование
        в тестах без BackendService) — старое поведение, пингуем безусловно."""
        rewriter = _make_rewriter()
        rewriter._settings_getter = None

        rewriter._idle_keepalive_tick()

        rewriter.warmup_probe.assert_called_once_with(timeout_sec=60.0)

    def test_tick_skips_probe_fail_closed_on_getter_exception(self):
        """Fail-CLOSED, не fail-open: цена ошибки асимметрична (2026-08-04) —
        спорадический пропуск пинга стоит одного холодного старта при
        следующем реальном rewrite; лишняя загрузка 6.86 ГБ при явно
        выключенной постобработке — реальный баг, который эта правка чинит."""
        rewriter = _make_rewriter()

        def _raising_getter(key, default):
            raise RuntimeError("cache read failed")

        rewriter._settings_getter = _raising_getter

        rewriter._idle_keepalive_tick()  # не должен бросить

        rewriter.warmup_probe.assert_not_called()

    def test_tick_never_raises_when_warmup_probe_itself_raises(self):
        rewriter = _make_rewriter()
        rewriter._settings_getter = lambda key, default: (
            True if key == "llm_rewrite_enabled" else default
        )
        rewriter.warmup_probe.side_effect = RuntimeError("network down")

        rewriter._idle_keepalive_tick()  # не должен бросить — daemon thread


if __name__ == "__main__":
    unittest.main()
