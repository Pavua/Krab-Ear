"""Tests for W1146 F1+F2 fixes in backend/llm_rewriter.py.

F1 MED: chatbot_response guard now calls _circuit.record_failure() so repeated
        chatbot-mode responses eventually open the circuit breaker.

F2 MED: 503 / Stream(gpu) backoff sleeps replaced with shutdown_event.wait()
        so the IPC thread unblocks immediately when shutdown is requested.
"""

import threading
import unittest
from unittest.mock import MagicMock, patch

import requests


def _make_rewriter(**kwargs):
    from backend.llm_rewriter import LLMRewriter
    defaults = dict(
        base_url="http://localhost:1234/v1",
        api_key="sk-test",
        model="test-model",
    )
    defaults.update(kwargs)
    return LLMRewriter(**defaults)


def _ok_resp(content: str):
    """Return a mock HTTP 200 response with the given content."""
    mock = MagicMock()
    mock.status_code = 200
    mock.text = content
    mock.json.return_value = {"choices": [{"message": {"content": content}}]}
    return mock


def _status_resp(status_code: int, text: str = ""):
    mock = MagicMock()
    mock.status_code = status_code
    mock.text = text
    return mock


# ---------------------------------------------------------------------------
# F1 — chatbot_response records circuit failure
# ---------------------------------------------------------------------------

class ChatbotRecordsFailureTestCase(unittest.TestCase):
    """W1146 F1: chatbot_response guard must call record_failure()."""

    def setUp(self):
        self.rewriter = _make_rewriter()

    def test_chatbot_response_records_failure(self):
        """Single chatbot reply increments consecutive_failures."""
        before = self.rewriter._circuit._consecutive_failures
        self.rewriter._session.post = MagicMock(
            return_value=_ok_resp("Sure, here is the corrected text: тест.")
        )
        result = self.rewriter.rewrite("тест текст для rewrite")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")
        self.assertEqual(
            self.rewriter._circuit._consecutive_failures,
            before + 1,
            "chatbot_response must call record_failure() to increment consecutive_failures",
        )

    def test_circuit_state_unchanged_after_single_chatbot(self):
        """With threshold=3 (default), one chatbot reply keeps circuit CLOSED."""
        self.rewriter._session.post = MagicMock(
            return_value=_ok_resp("Here is the corrected version: что-то тут.")
        )
        self.rewriter.rewrite("что-то тут интересное происходит")
        self.assertEqual(self.rewriter._circuit.state, "closed")

    def test_circuit_opens_after_chatbot_streak(self):
        """Repeated chatbot responses (>= fail_threshold) open the circuit breaker.

        This was impossible before W1146 F1 because record_failure() was not called
        in the chatbot guard path.
        """
        from backend.llm_rewriter import LLMRewriter
        # Use threshold=3 (default), so 3 consecutive chatbot replies should open circuit.
        rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            circuit_fail_threshold=3,
        )
        chatbot_reply = "К сожалению, я не могу это сделать прямо сейчас без дополнительного контекста."
        rewriter._session.post = MagicMock(return_value=_ok_resp(chatbot_reply))

        for i in range(3):
            result = rewriter.rewrite("произвольный длинный входной текст для rewrite теста " + str(i))
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "chatbot_response",
                             f"call {i}: expected chatbot_response")

        self.assertEqual(
            rewriter._circuit.state, "open",
            "Circuit breaker must be OPEN after fail_threshold chatbot responses",
        )

    def test_circuit_blocks_after_chatbot_streak(self):
        """After the circuit opens due to chatbot streak, subsequent calls are circuit_open."""
        from backend.llm_rewriter import LLMRewriter
        rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            circuit_fail_threshold=2,
        )
        chatbot_reply = "Конечно, я помогу вам с этим запросом!"
        rewriter._session.post = MagicMock(return_value=_ok_resp(chatbot_reply))

        # Exhaust the threshold
        for _ in range(2):
            rewriter.rewrite("достаточно длинный текст для проверки circuit breaker threshold")

        self.assertEqual(rewriter._circuit.state, "open")

        # Next call should be blocked by circuit
        result = rewriter.rewrite("ещё один текст для обработки rewriter")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "circuit_open")
        # session.post should NOT have been called for the blocked call
        # (call count stays at 2 — one per chatbot call above)
        self.assertEqual(rewriter._session.post.call_count, 2)

    def test_successful_rewrite_resets_chatbot_failure_count(self):
        """A successful rewrite between chatbot calls resets the failure counter."""
        from backend.llm_rewriter import LLMRewriter
        rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            circuit_fail_threshold=3,
        )
        chatbot_reply = "Sure, I will help you with that request."
        # Two chatbot replies (below threshold of 3)
        rewriter._session.post = MagicMock(return_value=_ok_resp(chatbot_reply))
        for _ in range(2):
            rewriter.rewrite("текст для проверки сброса счётчика failures")
        self.assertEqual(rewriter._circuit.state, "closed")

        # Now a normal successful reply — should reset failure counter
        rewriter._session.post = MagicMock(
            return_value=_ok_resp("Это нормально исправленный текст с пунктуацией.")
        )
        result = rewriter.rewrite("это нормально исправленный текст с пунктуацией")
        self.assertTrue(result.ok)
        self.assertEqual(rewriter._circuit._consecutive_failures, 0)

        # Now two more chatbot replies — should still be CLOSED (counter was reset)
        rewriter._session.post = MagicMock(return_value=_ok_resp(chatbot_reply))
        for _ in range(2):
            rewriter.rewrite("ещё один тест для сброса счётчика failures")
        self.assertEqual(rewriter._circuit.state, "closed")


# ---------------------------------------------------------------------------
# F2 — interruptible backoff via shutdown_event.wait()
# ---------------------------------------------------------------------------

class ShutdownInterruptibleBackoffTestCase(unittest.TestCase):
    """W1146 F2: time.sleep() in 503 / Stream(gpu) paths replaced with shutdown_event.wait()."""

    def setUp(self):
        self.rewriter = _make_rewriter()

    def _resp503(self):
        return _status_resp(503)

    def _resp200(self, content="Исправленный текст готов."):
        return _ok_resp(content)

    def test_sleep_interrupted_by_shutdown_event(self):
        """Setting shutdown_event before rewrite() causes 503 retry path to return 'shutdown'.

        The shutdown_event is already set, so wait(timeout=10) returns True immediately
        and the method returns LLMRewriteResult(fallback_reason='shutdown') without
        making a second HTTP call.
        """
        # Pre-set shutdown event so wait() returns True immediately
        self.rewriter._shutdown_event.set()

        self.rewriter._session.post = MagicMock(
            side_effect=[self._resp503(), self._resp200()]
        )
        result = self.rewriter.rewrite("текст для проверки прерывания backoff")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "shutdown",
                         "Pre-set shutdown_event must cause 503 backoff to return 'shutdown'")
        # Second HTTP call should NOT have been made
        self.assertEqual(self.rewriter._session.post.call_count, 1,
                         "With shutdown already set, no retry POST should be issued")

    def test_503_retry_succeeds_without_shutdown(self):
        """Without shutdown, 503 → wait → retry path still works correctly.

        We mock the event.wait() to always return False (not shutting down)
        so the retry proceeds as before.
        """
        rewritten = "Исправленный текст, готов к использованию."
        self.rewriter._session.post = MagicMock(
            side_effect=[self._resp503(), self._resp200(rewritten)]
        )
        # Patch wait to return False (no shutdown) so retry fires
        with patch.object(self.rewriter._shutdown_event, "wait", return_value=False):
            result = self.rewriter.rewrite("исходный текст для проверки retry")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, rewritten)
        self.assertEqual(self.rewriter._session.post.call_count, 2)

    def test_shutdown_event_unblocks_immediately(self):
        """shutdown_event.wait(timeout=10) returns within milliseconds when event is set.

        This test verifies that the call does not actually sleep for 10 seconds.
        We set the shutdown_event and measure that the round-trip is <1 second.
        """
        import time
        self.rewriter._shutdown_event.set()
        self.rewriter._session.post = MagicMock(return_value=self._resp503())

        start = time.monotonic()
        result = self.rewriter.rewrite("текст для проверки быстрого возврата при shutdown")
        elapsed = time.monotonic() - start

        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "shutdown")
        self.assertLess(elapsed, 1.0,
                        f"Expected fast return (<1s) on shutdown, got {elapsed:.3f}s")

    def test_stream_gpu_retry_interrupted_by_shutdown(self):
        """shutdown_event also interrupts the 2s Stream(gpu) backoff."""
        self.rewriter._shutdown_event.set()

        gpu_resp = _status_resp(500, "Stream(gpu, 0) context lost error text here")
        self.rewriter._session.post = MagicMock(
            side_effect=[gpu_resp, self._resp200()]
        )
        result = self.rewriter.rewrite("текст для проверки gpu stream retry")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "shutdown",
                         "Pre-set shutdown_event must interrupt Stream(gpu) 2s backoff")
        # Second HTTP call should NOT have been made
        self.assertEqual(self.rewriter._session.post.call_count, 1)

    def test_stream_gpu_retry_proceeds_without_shutdown(self):
        """Without shutdown, Stream(gpu) backoff retries the POST normally."""
        rewritten = "Исправленный текст после gpu retry."
        gpu_resp = _status_resp(500, "Stream(gpu, 0) context lost error text here")
        self.rewriter._session.post = MagicMock(
            side_effect=[gpu_resp, self._resp200(rewritten)]
        )
        # Patch event.wait to return False (no shutdown)
        with patch.object(self.rewriter._shutdown_event, "wait", return_value=False):
            result = self.rewriter.rewrite("текст для gpu retry без shutdown")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, rewritten)
        self.assertEqual(self.rewriter._session.post.call_count, 2)

    def test_close_sets_shutdown_event(self):
        """close() signals shutdown_event so keepalive/warmup loops terminate."""
        self.assertFalse(self.rewriter._shutdown_event.is_set())
        self.rewriter.close()
        self.assertTrue(self.rewriter._shutdown_event.is_set())


if __name__ == "__main__":
    unittest.main()
