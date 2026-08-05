"""2026-08-05: conftest._block_real_lm_studio_calls fixture contract.

LM Studio слушает на loopback (127.0.0.1) — существующий W957 network guard
(_block_real_network) намеренно пропускает loopback, поэтому
LLMRewriter.ping()/warmup_probe() били в реальный LM Studio, а не в затычку.
Когда LM Studio недоступен/медленный, тесты висели минутами (нет таймаута
на уровне socket.recv, который ловит W957-гард) — задело несколько разных
файлов в один и тот же день, никак не связанных друг с другом по коду.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_rewriter import LLMRewriter


class TestLMStudioNetworkGuardFixtureActive(unittest.TestCase):
    """Этот файл НЕ в exclusion-списке фикстуры — ping/warmup_probe должны
    быть подменены на быстрые фейки без реального сетевого вызова."""

    def test_ping_returns_false_without_network_call(self):
        rewriter = LLMRewriter.__new__(LLMRewriter)
        self.assertFalse(rewriter.ping())

    def test_warmup_probe_returns_fake_dict_without_network_call(self):
        rewriter = LLMRewriter.__new__(LLMRewriter)
        result = rewriter.warmup_probe()
        self.assertFalse(result["ok"])
        self.assertEqual(result["latency_ms"], 0)
        self.assertIsNotNone(result["error"])

    def test_warmup_delegates_to_patched_warmup_probe(self):
        """warmup() calls self.warmup_probe(...) — тоже должен быть быстрым."""
        rewriter = LLMRewriter.__new__(LLMRewriter)
        self.assertFalse(rewriter.warmup())


if __name__ == "__main__":
    unittest.main()
