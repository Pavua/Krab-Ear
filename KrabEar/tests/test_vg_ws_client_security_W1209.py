"""Tests for W1204 security fixes in vg_ws_client.py.

Covers:
  F1 HIGH — explicit ssl.SSLContext with CERT_REQUIRED passed to websockets.connect
  F2 HIGH — session_id path traversal rejected via regex
  F4 MED  — max_size=2 MiB passed to websockets.connect
"""
from __future__ import annotations

import ast
import asyncio
import os
import ssl
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Stub out heavy dependencies so we can import vg_ws_client cleanly
# ---------------------------------------------------------------------------

def _install_stubs() -> None:
    for mod_name in (
        "backend.event_bus",
        "contracts.registry",
        "backend.error_bus",
        "backend.error_codes",
        "backend.observability",
    ):
        if mod_name not in sys.modules:
            stub = types.ModuleType(mod_name)
            sys.modules[mod_name] = stub

    sys.modules["backend.event_bus"].bus = MagicMock()  # type: ignore[attr-defined]
    sys.modules["contracts.registry"].EVENT_SCHEMA_MAP = {}  # type: ignore[attr-defined]

    # websockets stub (no real package needed)
    if "websockets" not in sys.modules:
        ws_stub = types.ModuleType("websockets")
        ws_stub.connect = MagicMock()  # type: ignore[attr-defined]
        sys.modules["websockets"] = ws_stub


_install_stubs()

from backend.vg_ws_client import (  # noqa: E402
    _SESSION_ID_RE,
    _VG_WS_DEFAULT_TIMEOUT_SEC,
    _VG_WS_MAX_SIZE,
)

# We import the class inside each async helper so asyncio.Event() is
# created within a running event loop (Python 3.9 compatibility).


# ---------------------------------------------------------------------------
# Async helper: run one connect, capture call_args, stop cleanly
# ---------------------------------------------------------------------------

async def _run_client_once(gateway_url: str, session_id: str, api_key: str = ""):
    """Create a VGWebSocketClient inside a running loop, mock websockets.connect,
    run the client for one iteration, and return the connect mock call_args."""
    # Late import ensures asyncio.Event() gets the running loop (Python 3.9)
    from backend.vg_ws_client import VGWebSocketClient  # noqa: PLC0415

    client = VGWebSocketClient(gateway_url=gateway_url, session_id=session_id, api_key=api_key)

    # async generator that yields nothing (empty ws stream)
    async def _aiter_empty(self):
        return
        yield  # makes it an async generator

    ws_mock = MagicMock()
    ws_mock.__aiter__ = _aiter_empty

    async def _fake_enter(*_a, **_kw):
        # signal stop so the while-loop exits after this single connect
        client._stop.set()
        return ws_mock

    cm = MagicMock()
    cm.__aenter__ = _fake_enter
    cm.__aexit__ = AsyncMock(return_value=False)

    connect_mock = MagicMock(return_value=cm)
    # Patch the module-level websockets stub
    sys.modules["websockets"].connect = connect_mock  # type: ignore[attr-defined]

    await client.run()

    return connect_mock


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

class TestVGWSClientSecurityW1209(unittest.TestCase):

    # ------------------------------------------------------------------ #
    # F1: explicit SSL context with CERT_REQUIRED                         #
    # ------------------------------------------------------------------ #

    def test_ssl_context_explicit_verify_mode_required(self):
        """websockets.connect must be called with an ssl.SSLContext whose
        verify_mode is CERT_REQUIRED and check_hostname is True."""
        connect_mock = asyncio.run(
            _run_client_once(
                gateway_url="wss://gateway.example.com",
                session_id="abc123",
                api_key="tok",
            )
        )

        connect_mock.assert_called_once()
        kwargs = connect_mock.call_args.kwargs
        ssl_arg = kwargs.get("ssl")
        self.assertIsNotNone(ssl_arg, "ssl= kwarg must be passed to websockets.connect")
        self.assertIsInstance(ssl_arg, ssl.SSLContext, "ssl= must be an ssl.SSLContext instance")
        self.assertEqual(
            ssl_arg.verify_mode,
            ssl.CERT_REQUIRED,
            "SSLContext.verify_mode must be CERT_REQUIRED",
        )
        self.assertTrue(ssl_arg.check_hostname, "SSLContext.check_hostname must be True")

    def test_plain_ws_url_passes_ssl_none(self):
        """For plain ws:// URLs (non-TLS), ssl= must be None (no forced TLS)."""
        connect_mock = asyncio.run(
            _run_client_once(
                gateway_url="ws://localhost:8080",
                session_id="localtest",
            )
        )

        connect_mock.assert_called_once()
        kwargs = connect_mock.call_args.kwargs
        self.assertIsNone(kwargs.get("ssl"), "ssl= must be None for plain ws:// URLs")

    # ------------------------------------------------------------------ #
    # F2: session_id traversal rejection                                  #
    # ------------------------------------------------------------------ #

    def _make_client_in_loop(self, gateway_url: str, session_id: str):
        """Create a VGWebSocketClient inside asyncio.run() for Python 3.9 compat."""
        async def _build():
            from backend.vg_ws_client import VGWebSocketClient  # noqa: PLC0415
            return VGWebSocketClient(gateway_url=gateway_url, session_id=session_id)
        return asyncio.run(_build())

    def test_session_id_traversal_rejected(self):
        """Raw path traversal like '../admin/stream' must raise ValueError."""
        with self.assertRaises(ValueError):
            self._make_client_in_loop(
                gateway_url="wss://gateway.example.com",
                session_id="../admin/stream",
            )

    def test_session_id_url_encoded_traversal_rejected(self):
        """URL-encoded traversal like '%2F..%2F' must raise ValueError."""
        with self.assertRaises(ValueError):
            self._make_client_in_loop(
                gateway_url="wss://gateway.example.com",
                session_id="%2F..%2F",
            )

    def test_session_id_slash_rejected(self):
        """session_id containing a literal slash must raise ValueError."""
        with self.assertRaises(ValueError):
            self._make_client_in_loop(
                gateway_url="wss://gateway.example.com",
                session_id="valid/inject",
            )

    def test_session_id_too_long_rejected(self):
        """session_id exceeding 128 characters must raise ValueError."""
        with self.assertRaises(ValueError):
            self._make_client_in_loop(
                gateway_url="wss://gateway.example.com",
                session_id="a" * 129,
            )

    def test_session_id_empty_rejected(self):
        """Empty session_id must raise ValueError."""
        with self.assertRaises(ValueError):
            self._make_client_in_loop(
                gateway_url="wss://gateway.example.com",
                session_id="",
            )

    def test_session_id_valid_alphanumeric_accepted(self):
        """Alphanumeric session_id (letters, digits, dash, underscore) must be accepted."""
        client = self._make_client_in_loop(
            gateway_url="wss://gateway.example.com",
            session_id="Session_123-ABC",
        )
        self.assertEqual(client.session_id, "Session_123-ABC")

    def test_session_id_max_length_128_accepted(self):
        """session_id of exactly 128 characters must be accepted."""
        sid = "a" * 128
        client = self._make_client_in_loop(
            gateway_url="wss://gateway.example.com",
            session_id=sid,
        )
        self.assertEqual(client.session_id, sid)

    # ------------------------------------------------------------------ #
    # F4: max_size cap                                                    #
    # ------------------------------------------------------------------ #

    def test_max_size_2mib_passed_to_connect(self):
        """websockets.connect must receive max_size=2*1024*1024 (2 MiB)."""
        connect_mock = asyncio.run(
            _run_client_once(
                gateway_url="wss://gateway.example.com",
                session_id="testSession1",
            )
        )

        connect_mock.assert_called_once()
        kwargs = connect_mock.call_args.kwargs
        self.assertEqual(
            kwargs.get("max_size"),
            2 * 1024 * 1024,
            "max_size must be 2 MiB (2097152 bytes)",
        )

    # ------------------------------------------------------------------ #
    # Module-level constants sanity                                       #
    # ------------------------------------------------------------------ #

    def test_timeout_constant_is_30(self):
        self.assertEqual(_VG_WS_DEFAULT_TIMEOUT_SEC, 30)

    def test_max_size_constant_is_2mib(self):
        self.assertEqual(_VG_WS_MAX_SIZE, 2 * 1024 * 1024)

    def test_session_id_regex_rejects_dot_dot(self):
        self.assertIsNone(_SESSION_ID_RE.match("../etc/passwd"))

    def test_session_id_regex_accepts_uuid_like(self):
        self.assertIsNotNone(_SESSION_ID_RE.match("550e8400-e29b-41d4-a716-446655440000"))

    # ------------------------------------------------------------------ #
    # AST: every websockets.connect() must have ssl= and max_size=       #
    # ------------------------------------------------------------------ #

    def _get_connect_kwarg_sets(self) -> list[set]:
        """Parse vg_ws_client.py source; return list of kwarg-name sets for
        each websockets.connect() call node."""
        src_path = os.path.join(
            os.path.dirname(__file__), "..", "backend", "vg_ws_client.py"
        )
        with open(src_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        result = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "connect"
                and isinstance(func.value, ast.Name)
                and func.value.id == "websockets"
            ):
                continue
            result.append({kw.arg for kw in node.keywords})
        return result

    def test_ast_connect_always_passes_ssl_kwarg(self):
        """AST: every websockets.connect() call in source must include ssl=."""
        all_calls = self._get_connect_kwarg_sets()
        self.assertTrue(len(all_calls) > 0, "No websockets.connect() calls found in source")
        for kw_names in all_calls:
            self.assertIn(
                "ssl",
                kw_names,
                f"websockets.connect() call missing ssl= kwarg (kwargs: {kw_names})",
            )

    def test_ast_connect_always_passes_max_size_kwarg(self):
        """AST: every websockets.connect() call in source must include max_size=."""
        all_calls = self._get_connect_kwarg_sets()
        self.assertTrue(len(all_calls) > 0, "No websockets.connect() calls found in source")
        for kw_names in all_calls:
            self.assertIn(
                "max_size",
                kw_names,
                f"websockets.connect() call missing max_size= kwarg (kwargs: {kw_names})",
            )


if __name__ == "__main__":
    unittest.main()
