"""Tests for W1154: LLMRewriter shutdown_event.wait replaces time.sleep.

Verifies:
1. 503 retry path uses shutdown_event.wait(10) instead of time.sleep(10)
2. Stream(gpu) retry path uses shutdown_event.wait(2) instead of time.sleep(2)
3. set_shutdown() interrupts the wait early
4. Normal retry (no shutdown) waits the full duration
"""
import ast
import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestLLMRewriterSleepReplacedAST(unittest.TestCase):
    """AST-level check: no bare time.sleep() calls remain in _rewrite_impl."""

    def _get_rewrite_impl_source(self):
        module_path = os.path.join(
            PROJECT_ROOT,
            "KrabEar", "backend", "llm_rewriter.py",
        )
        with open(module_path, "r", encoding="utf-8") as f:
            source = f.read()
        return source

    def test_no_bare_time_sleep_in_rewrite_impl(self):
        """_rewrite_impl must not contain any time.sleep() calls."""
        source = self._get_rewrite_impl_source()
        tree = ast.parse(source)

        sleep_calls_in_rewrite = []

        class SleepFinder(ast.NodeVisitor):
            def __init__(self):
                self._in_rewrite_impl = False

            def visit_FunctionDef(self, node):
                if node.name == "_rewrite_impl":
                    prev = self._in_rewrite_impl
                    self._in_rewrite_impl = True
                    self.generic_visit(node)
                    self._in_rewrite_impl = prev
                else:
                    self.generic_visit(node)

            def visit_Call(self, node):
                if self._in_rewrite_impl:
                    # time.sleep(...)
                    if (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "sleep"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "time"
                    ):
                        sleep_calls_in_rewrite.append(node)
                self.generic_visit(node)

        SleepFinder().visit(tree)
        self.assertEqual(
            sleep_calls_in_rewrite,
            [],
            "time.sleep() calls remain in _rewrite_impl — should use shutdown_event.wait()",
        )

    def test_shutdown_event_wait_present_in_rewrite_impl(self):
        """_rewrite_impl must contain _shutdown_event.wait() calls."""
        source = self._get_rewrite_impl_source()
        tree = ast.parse(source)

        wait_calls = []

        class WaitFinder(ast.NodeVisitor):
            def __init__(self):
                self._in_rewrite_impl = False

            def visit_FunctionDef(self, node):
                if node.name == "_rewrite_impl":
                    prev = self._in_rewrite_impl
                    self._in_rewrite_impl = True
                    self.generic_visit(node)
                    self._in_rewrite_impl = prev
                else:
                    self.generic_visit(node)

            def visit_Call(self, node):
                if self._in_rewrite_impl:
                    if (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "wait"
                        and isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "_shutdown_event"
                    ):
                        wait_calls.append(node)
                self.generic_visit(node)

        WaitFinder().visit(tree)
        self.assertGreaterEqual(
            len(wait_calls),
            2,
            f"Expected ≥2 _shutdown_event.wait() calls in _rewrite_impl, found {len(wait_calls)}",
        )

    def test_set_shutdown_method_exists(self):
        """LLMRewriter must have a set_shutdown() method."""
        source = self._get_rewrite_impl_source()
        tree = ast.parse(source)

        method_names = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        ]
        self.assertIn(
            "set_shutdown",
            method_names,
            "set_shutdown() method not found in llm_rewriter.py",
        )


class TestLLMRewriterShutdownEventBehavior(unittest.TestCase):
    """Behavioral tests: shutdown_event interaction in 503 and Stream(gpu) paths."""

    def _make_rewriter(self):
        """Build a minimal LLMRewriter with mocked requests.Session."""
        # Stub heavy imports before importing the module
        for mod in ["requests", "backend.performance_profiler", "backend.observability"]:
            if mod not in sys.modules:
                stub = types.ModuleType(mod)
                sys.modules[mod] = stub

        # Ensure requests stubs have needed attrs
        requests_mod = sys.modules.get("requests", types.ModuleType("requests"))
        if not hasattr(requests_mod, "Session"):
            requests_mod.Session = MagicMock
        if not hasattr(requests_mod, "Timeout"):
            requests_mod.Timeout = type("Timeout", (Exception,), {})
        if not hasattr(requests_mod, "ConnectionError"):
            requests_mod.ConnectionError = type("ConnectionError", (Exception,), {})
        if not hasattr(requests_mod, "RequestException"):
            requests_mod.RequestException = type("RequestException", (Exception,), {})
        sys.modules["requests"] = requests_mod

        # Stub observability
        obs = sys.modules.get("backend.observability", types.ModuleType("backend.observability"))
        obs.add_breadcrumb = lambda **kw: None
        obs.capture_exception = lambda *a, **kw: None
        sys.modules["backend.observability"] = obs

        # Stub performance_profiler
        class _Span:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class _Prof:
            def start_span(self, name): return _Span()
        pp = sys.modules.get("backend.performance_profiler", types.ModuleType("backend.performance_profiler"))
        pp.profiler = _Prof()
        sys.modules["backend.performance_profiler"] = pp

        # Now import
        import importlib
        llm_mod_name = "backend.llm_rewriter"
        if llm_mod_name in sys.modules:
            llm_module = sys.modules[llm_mod_name]
        else:
            llm_module = importlib.import_module(llm_mod_name)

        LLMRewriter = llm_module.LLMRewriter

        rewriter = LLMRewriter.__new__(LLMRewriter)
        # Minimal init
        rewriter._base_url = "http://localhost:1234/v1"
        rewriter._api_key = ""
        rewriter._model = "test-model"
        rewriter._fallback_timeout = 5.0
        rewriter._runtime_timeout_provider = None
        rewriter._last_latency_ms = None
        rewriter._last_error = None
        rewriter._post_lock = threading.Lock()
        rewriter._shutdown_event = threading.Event()
        rewriter._idle_keepalive_enabled = False
        rewriter._idle_keepalive_thread = None
        # Circuit breaker
        from backend.llm_rewriter import CircuitBreaker
        rewriter._circuit = CircuitBreaker(fail_threshold=3, initial_reset_sec=60)
        # Session mock
        rewriter._session = MagicMock()

        return rewriter, llm_module

    def test_shutdown_interrupts_503_retry_wait(self):
        """When shutdown_event is set during 503 wait, rewrite returns 'shutdown'."""
        rewriter, llm_module = self._make_rewriter()
        LLMRewriteResult = llm_module.LLMRewriteResult

        # First call → 503; retry → never happens because shutdown is set immediately
        mock_503 = MagicMock()
        mock_503.status_code = 503
        mock_503.text = "Service Unavailable"

        rewriter._session.post.return_value = mock_503

        # Signal shutdown immediately (before rewrite runs — event is already set)
        rewriter._shutdown_event.set()

        result = rewriter.rewrite("hello world")

        self.assertIsInstance(result, LLMRewriteResult)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "shutdown")
        # Session.post should have been called only once (no retry after shutdown)
        self.assertEqual(rewriter._session.post.call_count, 1)

    def test_shutdown_interrupts_stream_gpu_retry_wait(self):
        """When shutdown_event is set during Stream(gpu) 2s wait, rewrite returns 'shutdown'."""
        rewriter, llm_module = self._make_rewriter()
        LLMRewriteResult = llm_module.LLMRewriteResult

        mock_500 = MagicMock()
        mock_500.status_code = 500
        mock_500.text = "Stream(gpu, 0) error in Metal"

        rewriter._session.post.return_value = mock_500

        # Signal shutdown before rewrite — the 2s wait sees the event and returns True
        rewriter._shutdown_event.set()

        result = rewriter.rewrite("hello world")

        self.assertIsInstance(result, LLMRewriteResult)
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "shutdown")
        # Only one post call (the retry is skipped)
        self.assertEqual(rewriter._session.post.call_count, 1)

    def test_set_shutdown_signals_event(self):
        """set_shutdown() sets the _shutdown_event."""
        rewriter, _ = self._make_rewriter()

        self.assertFalse(rewriter._shutdown_event.is_set())
        rewriter.set_shutdown()
        self.assertTrue(rewriter._shutdown_event.is_set())

    def test_set_shutdown_idempotent(self):
        """Calling set_shutdown() twice does not raise."""
        rewriter, _ = self._make_rewriter()
        rewriter.set_shutdown()
        rewriter.set_shutdown()  # should not raise
        self.assertTrue(rewriter._shutdown_event.is_set())

    def test_normal_retry_waits_without_shutdown(self):
        """Without shutdown, the 503 wait completes and the retry POST is attempted."""
        rewriter, llm_module = self._make_rewriter()

        mock_503 = MagicMock()
        mock_503.status_code = 503
        mock_503.text = "Service Unavailable"

        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.text = ""
        mock_200.json.return_value = {
            "choices": [{"message": {"content": "fixed text", "tool_calls": None}}]
        }

        rewriter._session.post.side_effect = [mock_503, mock_200]

        # Patch _shutdown_event.wait to return False immediately (not shutting down)
        # but track it was called with the right timeout
        original_wait = rewriter._shutdown_event.wait
        wait_calls = []

        def fake_wait(timeout=None):
            wait_calls.append(timeout)
            return False  # not shutting down

        rewriter._shutdown_event.wait = fake_wait

        result = rewriter.rewrite("hello world")

        # Should have used wait (not sleep) with 10s timeout
        self.assertIn(10.0, wait_calls)
        # Should have made 2 post calls (initial + retry)
        self.assertEqual(rewriter._session.post.call_count, 2)
        # Successful result from retry
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "fixed text")


if __name__ == "__main__":
    unittest.main()
