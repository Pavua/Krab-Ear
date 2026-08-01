"""Прогрев LLM не грузит модель, которой никто не пользуется.

Инцидент 01.08.2026: владелец выключил постобработку LLM в UI, но при каждом
старте backend прогрев всё равно поднимал gemma-4-e4b (6.86 ГБ) в LM Studio и
держал её 31-45 с — при свопе 13.9/15.4 ГБ это душило и диктовку, и систему.
Причина: прогрев гейтился только собственным флагом rewriter_warmup_on_startup
и не спрашивал, включён ли хоть один ПОТРЕБИТЕЛЬ (rewrite / punctuation-pass).
"""

from __future__ import annotations

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.service import llm_warmup_needed


def _getter(**overrides):
    def _get(key, default=None):
        return overrides.get(key, default)
    return _get


class TestWarmupSkippedWithoutConsumers(unittest.TestCase):

    def test_no_consumers_no_warmup(self):
        """Обе LLM-функции выключены — модель поднимать незачем."""
        self.assertFalse(llm_warmup_needed(_getter(
            rewriter_warmup_on_startup=True,
            llm_rewrite_enabled=False,
            stt_punctuation_llm_pass_enabled=False,
        )))

    def test_rewrite_consumer_triggers_warmup(self):
        self.assertTrue(llm_warmup_needed(_getter(
            rewriter_warmup_on_startup=True,
            llm_rewrite_enabled=True,
            stt_punctuation_llm_pass_enabled=False,
        )))

    def test_punctuation_consumer_triggers_warmup(self):
        """Punctuation-pass — самостоятельный потребитель, его нельзя забыть."""
        self.assertTrue(llm_warmup_needed(_getter(
            rewriter_warmup_on_startup=True,
            llm_rewrite_enabled=False,
            stt_punctuation_llm_pass_enabled=True,
        )))

    def test_explicit_warmup_off_wins_over_consumers(self):
        """Прежний контракт: rewriter_warmup_on_startup=False отключает всё."""
        self.assertFalse(llm_warmup_needed(_getter(
            rewriter_warmup_on_startup=False,
            llm_rewrite_enabled=True,
            stt_punctuation_llm_pass_enabled=True,
        )))

    def test_privacy_mode_blocks_warmup(self):
        """W1229/W1755: в приватном режиме потребители всё равно заблокированы."""
        self.assertFalse(llm_warmup_needed(_getter(
            rewriter_warmup_on_startup=True,
            llm_rewrite_enabled=True,
            privacy_mode_enabled=True,
        )))

    def test_defaults_are_conservative(self):
        """Пустые настройки (всё по умолчанию) прогрев не запускают."""
        self.assertFalse(llm_warmup_needed(_getter()))


if __name__ == "__main__":
    unittest.main()
