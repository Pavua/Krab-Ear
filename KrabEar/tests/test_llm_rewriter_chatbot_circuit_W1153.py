"""W1153: chatbot_response rejection must call record_failure() on the CircuitBreaker.

W1146 F1 MED — model persistently in assistant mode never tripped the circuit
because the chatbot_response branch returned ok=False without recording a failure.
W826 fixed output_too_short/long; chatbot_response was missed.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Resolve backend package
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


class ChatbotRejectionRecordsFailureTestCase(unittest.TestCase):
    """chatbot_response rejection calls record_failure() (W1153 F1)."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            circuit_fail_threshold=5,
            circuit_initial_reset_sec=60,
        )

    def _mock_resp(self, content: str):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": content}}]}
        return mock_resp

    def test_chatbot_rejection_calls_record_failure(self):
        """Single chatbot response must increment consecutive_failures on the circuit."""
        self.rewriter._session.post = MagicMock(
            return_value=self._mock_resp("Sure, here is the corrected text: привет.")
        )

        before = self.rewriter._circuit._consecutive_failures
        result = self.rewriter.rewrite("привет мир тест строка")

        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")
        after = self.rewriter._circuit._consecutive_failures
        self.assertEqual(
            after,
            before + 1,
            "consecutive_failures must increase by 1 after chatbot_response rejection",
        )

    def test_chatbot_rejection_russian_marker_records_failure(self):
        """Russian chatbot marker 'к сожалению' also records a failure."""
        self.rewriter._session.post = MagicMock(
            return_value=self._mock_resp("К сожалению, я не могу это исправить.")
        )

        before = self.rewriter._circuit._consecutive_failures
        result = self.rewriter.rewrite("какой-то текст для обработки guard")

        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "chatbot_response")
        self.assertEqual(self.rewriter._circuit._consecutive_failures, before + 1)


class ChatbotCircuitOpensAfterThresholdTestCase(unittest.TestCase):
    """5 consecutive chatbot responses trip the CircuitBreaker (W1153 F2)."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        # Low threshold to keep the test fast
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            circuit_fail_threshold=5,
            circuit_initial_reset_sec=60,
        )

    def _mock_resp_chatbot(self) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Sure, here is your text: test."}}]
        }
        return mock_resp

    def test_five_consecutive_chatbot_opens_circuit(self):
        """After fail_threshold chatbot responses the circuit must open."""
        from backend.llm_rewriter import CircuitState

        self.rewriter._session.post = MagicMock(
            return_value=self._mock_resp_chatbot()
        )

        # All 5 must return chatbot_response (circuit still CLOSED)
        for i in range(5):
            result = self.rewriter.rewrite(f"тестовый текст номер {i} достаточно длинный")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "chatbot_response")

        # Circuit should now be OPEN
        self.assertEqual(
            self.rewriter._circuit.state,
            "open",
            "CircuitBreaker must be OPEN after 5 consecutive chatbot_response failures",
        )

    def test_circuit_open_blocks_subsequent_requests(self):
        """After circuit opens due to chatbot loop, next call returns circuit_open."""
        from backend.llm_rewriter import CircuitState

        self.rewriter._session.post = MagicMock(
            return_value=self._mock_resp_chatbot()
        )

        for _ in range(5):
            self.rewriter.rewrite("тест достаточно длинный текст здесь")

        self.assertEqual(self.rewriter._circuit.state, "open")

        # 6th call should be blocked by circuit, not hit session.post
        call_count_before = self.rewriter._session.post.call_count
        result = self.rewriter.rewrite("ещё один текст для проверки circuit")
        self.assertEqual(result.fallback_reason, "circuit_open")
        # session.post must NOT have been called on the 6th attempt
        self.assertEqual(
            self.rewriter._session.post.call_count,
            call_count_before,
            "session.post must not be called when circuit is OPEN",
        )


if __name__ == "__main__":
    unittest.main()
