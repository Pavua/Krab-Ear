"""KE05B-03: ``LLMRewriter.rewrite()`` сам проверяет privacy_mode_enabled.

Сиблинг-асимметрия: ``fix_punctuation_only()`` и ``summarize()`` блокируют
отправку текста в LLM при включённой приватности на уровне метода, а
``rewrite()`` полагался только на проверку вызывающего (``engine._llm_rewrite_allowed``
или ``LLMRewriteStage.should_run``) — между проверкой вызывающего и HTTP режим
может переключиться, а прямой вызов (``RewriterFallbackChain``) гейта не имеет.

Контракт ошибок тот же, что у соседей: геттер упал — fail-closed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_rewriter import LLMRewriter, RewriterFallbackChain  # noqa: E402

_TEXT = "Это секретный разговор о сделке."


def _rewriter() -> LLMRewriter:
    rw = LLMRewriter(base_url="http://localhost:1234/v1", api_key="sk-test", model="primary-model")
    rw._session.post = MagicMock()
    return rw


def _ok_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


def _privacy(value):
    return lambda key, default=None: value if key == "privacy_mode_enabled" else default


class RewritePrivacyGate(unittest.TestCase):
    def test_privacy_on_blocks_http(self):
        rw = _rewriter()
        rw._settings_getter = _privacy(True)
        result = rw.rewrite(_TEXT)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "privacy_mode")
        self.assertIsNone(result.text)
        rw._session.post.assert_not_called()

    def test_getter_failure_is_fail_closed(self):
        rw = _rewriter()

        def boom(key, default=None):
            raise RuntimeError("settings store unavailable")

        rw._settings_getter = boom
        result = rw.rewrite(_TEXT)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "privacy_guard_error")
        rw._session.post.assert_not_called()

    def test_privacy_off_still_rewrites(self):
        rw = _rewriter()
        rw._settings_getter = _privacy(False)
        rw._session.post.return_value = _ok_response("Это секретный разговор о сделке.")
        result = rw.rewrite(_TEXT)
        self.assertTrue(result.ok)
        rw._session.post.assert_called_once()

    def test_no_getter_keeps_backward_compat(self):
        rw = _rewriter()
        self.assertIsNone(rw._settings_getter)
        rw._session.post.return_value = _ok_response("Это секретный разговор о сделке.")
        self.assertTrue(rw.rewrite(_TEXT).ok)

    def test_privacy_switched_after_caller_check_blocks_send(self):
        # Вызывающий проверил режим (OFF), затем режим включили до отправки.
        rw = _rewriter()
        state = {"privacy": False}
        rw._settings_getter = lambda key, default=None: state["privacy"] if key == "privacy_mode_enabled" else default
        state["privacy"] = True
        self.assertFalse(rw.rewrite(_TEXT).ok)
        rw._session.post.assert_not_called()

    def test_fallback_chain_sends_nothing_under_privacy(self):
        rw = _rewriter()
        rw._settings_getter = _privacy(True)
        chain = RewriterFallbackChain(rw, ["fallback-a", "fallback-b"])
        result = chain.rewrite(_TEXT)
        self.assertFalse(result.ok)
        rw._session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
