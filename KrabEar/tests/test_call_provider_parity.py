"""Parity tests — TelnyxAdapter vs TwilioAdapter против CallProvider Protocol.

Wave 263: Верификация что оба адаптера полностью удовлетворяют контракту
CallProvider и совпадают по форме ответов в зеркальных сценариях.

Задокументированные расхождения (behavioral gaps) — см. конец файла.
"""

from __future__ import annotations

import inspect
import sys
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_provider import CallProvider  # noqa: E402
from backend.telnyx_adapter import TelnyxAdapter  # noqa: E402
from backend.twilio_adapter import TwilioAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _telnyx(key: str = "tk_live_test_key") -> TelnyxAdapter:
    return TelnyxAdapter(
        api_key=key,
        connection_id="conn_test_123",
        from_number="+15550001111",
    )


def _twilio(sid: str = "ACtest123456", token: str = "authtoken789") -> TwilioAdapter:
    return TwilioAdapter(
        account_sid=sid,
        auth_token=token,
        from_number="+15550001111",
    )


def _mock_resp(
    status: int,
    json_data: object = None,
    headers: dict | None = None,
    content: bytes = b"{}",
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = ""
    resp.content = content
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no body")
    return resp


def _inject_post(adapter: Any, mock_resp: MagicMock) -> None:
    sess = MagicMock()
    sess.post.return_value = mock_resp
    sess.headers = {}
    adapter._session = sess


def _inject_get(adapter: Any, mock_resp: MagicMock) -> None:
    sess = MagicMock()
    sess.get.return_value = mock_resp
    sess.headers = {}
    adapter._session = sess


def _inject_post_raises(adapter: Any, exc: Exception) -> None:
    sess = MagicMock()
    sess.post.side_effect = exc
    sess.headers = {}
    adapter._session = sess


# ---------------------------------------------------------------------------
# 1. Protocol isinstance checks
# ---------------------------------------------------------------------------

class ProtocolImplementationTestCase(unittest.TestCase):
    """Оба адаптера должны проходить isinstance(x, CallProvider) check."""

    # test_telnyx_implements_call_provider_protocol
    def test_telnyx_implements_call_provider_protocol(self) -> None:
        """TelnyxAdapter должен быть экземпляром runtime_checkable CallProvider."""
        adapter = TelnyxAdapter()
        self.assertIsInstance(adapter, CallProvider)

    # test_twilio_implements_call_provider_protocol
    def test_twilio_implements_call_provider_protocol(self) -> None:
        """TwilioAdapter должен быть экземпляром runtime_checkable CallProvider."""
        adapter = TwilioAdapter()
        self.assertIsInstance(adapter, CallProvider)

    def test_telnyx_configured_implements_call_provider_protocol(self) -> None:
        """Сконфигурированный TelnyxAdapter тоже удовлетворяет протоколу."""
        adapter = _telnyx()
        self.assertIsInstance(adapter, CallProvider)

    def test_twilio_configured_implements_call_provider_protocol(self) -> None:
        """Сконфигурированный TwilioAdapter тоже удовлетворяет протоколу."""
        adapter = _twilio()
        self.assertIsInstance(adapter, CallProvider)


# ---------------------------------------------------------------------------
# 2. Method signature parity
# ---------------------------------------------------------------------------

_REQUIRED_METHODS: List[str] = [
    "dial",
    "hangup",
    "get_call_status",
    "list_active_calls",
    "is_configured",
]

_DIAL_PARAMS: List[str] = ["to_number", "call_control_id", "webhook_url"]
_HANGUP_PARAMS: List[str] = ["call_control_id"]
_GET_STATUS_PARAMS: List[str] = ["call_control_id"]


class MethodSignatureParityTestCase(unittest.TestCase):
    """TelnyxAdapter и TwilioAdapter имеют идентичные подписи Protocol-методов."""

    # test_both_methods_return_same_shape (signature half)
    def test_both_adapters_have_identical_method_set(self) -> None:
        for method in _REQUIRED_METHODS:
            for cls in (TelnyxAdapter, TwilioAdapter):
                self.assertTrue(
                    callable(getattr(cls, method, None)),
                    f"{cls.__name__} missing callable '{method}'",
                )

    def test_dial_params_parity(self) -> None:
        for cls in (TelnyxAdapter, TwilioAdapter):
            sig = inspect.signature(cls.dial)
            params = list(sig.parameters.keys())
            for p in _DIAL_PARAMS:
                self.assertIn(p, params, f"{cls.__name__}.dial missing param '{p}'")

    def test_hangup_params_parity(self) -> None:
        for cls in (TelnyxAdapter, TwilioAdapter):
            sig = inspect.signature(cls.hangup)
            self.assertIn("call_control_id", sig.parameters)

    def test_get_call_status_params_parity(self) -> None:
        for cls in (TelnyxAdapter, TwilioAdapter):
            sig = inspect.signature(cls.get_call_status)
            self.assertIn("call_control_id", sig.parameters)

    def test_list_active_calls_no_required_params(self) -> None:
        """list_active_calls() не требует обязательных параметров."""
        for cls in (TelnyxAdapter, TwilioAdapter):
            sig = inspect.signature(cls.list_active_calls)
            required = [
                n for n, p in sig.parameters.items()
                if n != "self" and p.default is inspect.Parameter.empty
            ]
            self.assertEqual(
                required, [],
                f"{cls.__name__}.list_active_calls has unexpected required params: {required}",
            )


# ---------------------------------------------------------------------------
# 3. Return value shape parity (stub mode)
# ---------------------------------------------------------------------------

class StubModeShapeParityTestCase(unittest.TestCase):
    """test_stub_mode_when_no_credentials — оба адаптера в stub-режиме."""

    def setUp(self) -> None:
        self.telnyx_stub = TelnyxAdapter()
        self.twilio_stub = TwilioAdapter()
        self.adapters: List[Tuple[str, Any]] = [
            ("TelnyxAdapter", self.telnyx_stub),
            ("TwilioAdapter", self.twilio_stub),
        ]

    def _assert_not_ok(self, result: Dict[str, Any], ctx: str) -> None:
        self.assertIn("ok", result, f"{ctx}: missing 'ok' key")
        self.assertFalse(result["ok"], f"{ctx}: expected ok=False, got {result}")
        self.assertIn("error", result, f"{ctx}: missing 'error' key")
        self.assertIsInstance(result["error"], str, f"{ctx}: 'error' must be str")

    # test_stub_mode_when_no_credentials — dial
    def test_stub_dial_shape_both(self) -> None:
        for name, adapter in self.adapters:
            result = adapter.dial("+15550001234")
            self._assert_not_ok(result, f"{name}.dial(stub)")

    # test_stub_mode_when_no_credentials — hangup
    def test_stub_hangup_shape_both(self) -> None:
        for name, adapter in self.adapters:
            result = adapter.hangup("some_ctrl_id")
            self._assert_not_ok(result, f"{name}.hangup(stub)")

    # test_stub_mode_when_no_credentials — get_call_status
    def test_stub_get_call_status_shape_both(self) -> None:
        for name, adapter in self.adapters:
            result = adapter.get_call_status("some_ctrl_id")
            self._assert_not_ok(result, f"{name}.get_call_status(stub)")

    # test_stub_mode_when_no_credentials — list_active_calls
    def test_stub_list_active_calls_shape_both(self) -> None:
        for name, adapter in self.adapters:
            result = adapter.list_active_calls()
            self._assert_not_ok(result, f"{name}.list_active_calls(stub)")

    def test_stub_is_configured_both_false(self) -> None:
        for name, adapter in self.adapters:
            self.assertFalse(adapter.is_configured(), f"{name}.is_configured() should be False in stub")

    def test_stub_error_contains_provider_name(self) -> None:
        """Error string должна содержать имя провайдера (telnyx/twilio)."""
        r_tel = self.telnyx_stub.dial("+15550001234")
        r_twi = self.twilio_stub.dial("+15550001234")
        self.assertIn("telnyx", r_tel["error"])
        self.assertIn("twilio", r_twi["error"])


# ---------------------------------------------------------------------------
# 4. test_both_methods_return_same_shape — success path
# ---------------------------------------------------------------------------

class SuccessShapeParityTestCase(unittest.TestCase):
    """Успешные ответы обоих адаптеров имеют единый набор ключей."""

    _DIAL_OK_KEYS = {"ok", "call_id", "call_control_id", "to_number", "data"}
    _HANGUP_OK_KEYS = {"ok"}
    _GET_STATUS_OK_KEYS = {"ok", "status", "data"}
    _LIST_CALLS_OK_KEYS = {"ok", "calls"}

    def setUp(self) -> None:
        self.telnyx = _telnyx()
        self.twilio = _twilio()

    def test_dial_success_keys_parity(self) -> None:
        """dial() при успехе: оба возвращают одинаковый набор ключей."""
        # Telnyx
        _inject_post(
            self.telnyx,
            _mock_resp(
                201,
                {"data": {"call_leg_id": "leg1", "call_control_id": "ctrl1", "status": "ringing"}},
            ),
        )
        r_tel = self.telnyx.dial("+15550009999")
        self.assertTrue(r_tel["ok"])
        for key in self._DIAL_OK_KEYS:
            self.assertIn(key, r_tel, f"TelnyxAdapter.dial missing key '{key}'")

        # Twilio
        self.twilio = _twilio()
        _inject_post(
            self.twilio,
            _mock_resp(201, {"sid": "CA001", "status": "queued", "to": "+15550009999"}),
        )
        r_twi = self.twilio.dial("+15550009999")
        self.assertTrue(r_twi["ok"])
        for key in self._DIAL_OK_KEYS:
            self.assertIn(key, r_twi, f"TwilioAdapter.dial missing key '{key}'")

    def test_hangup_success_keys_parity(self) -> None:
        """hangup() при успехе: оба возвращают {"ok": True}."""
        _inject_post(self.telnyx, _mock_resp(200, {"data": {}}))
        r_tel = self.telnyx.hangup("ctrl1")
        self.assertTrue(r_tel["ok"])

        self.twilio = _twilio()
        _inject_post(self.twilio, _mock_resp(200, {"sid": "CA1", "status": "completed"}))
        r_twi = self.twilio.hangup("CA1")
        self.assertTrue(r_twi["ok"])

    def test_get_call_status_keys_parity(self) -> None:
        """get_call_status() при успехе: оба возвращают ok/status/data."""
        _inject_get(self.telnyx, _mock_resp(200, {"data": {"status": "bridged"}}))
        r_tel = self.telnyx.get_call_status("ctrl1")
        self.assertTrue(r_tel["ok"])
        for key in self._GET_STATUS_OK_KEYS:
            self.assertIn(key, r_tel)

        self.twilio = _twilio()
        _inject_get(self.twilio, _mock_resp(200, {"status": "in-progress", "sid": "CA1"}))
        r_twi = self.twilio.get_call_status("CA1")
        self.assertTrue(r_twi["ok"])
        for key in self._GET_STATUS_OK_KEYS:
            self.assertIn(key, r_twi)

    def test_list_active_calls_keys_parity(self) -> None:
        """list_active_calls() при успехе: оба возвращают ok/calls."""
        _inject_get(self.telnyx, _mock_resp(200, {"data": []}))
        r_tel = self.telnyx.list_active_calls()
        self.assertTrue(r_tel["ok"])
        for key in self._LIST_CALLS_OK_KEYS:
            self.assertIn(key, r_tel)

        self.twilio = _twilio()
        _inject_get(self.twilio, _mock_resp(200, {"calls": []}))
        r_twi = self.twilio.list_active_calls()
        self.assertTrue(r_twi["ok"])
        for key in self._LIST_CALLS_OK_KEYS:
            self.assertIn(key, r_twi)


# ---------------------------------------------------------------------------
# 5. test_dial_with_bearer_auth (Telnyx)
# ---------------------------------------------------------------------------

class TelnyxBearerAuthTestCase(unittest.TestCase):
    """test_dial_with_bearer_auth — Telnyx использует Bearer-схему."""

    def test_bearer_auth_header_on_session(self) -> None:
        """Заголовок Authorization: Bearer <key> присутствует в сессии."""
        adapter = _telnyx(key="KEY_super_secret_99")
        session = adapter._get_session()
        auth_header = session.headers.get("Authorization", "")
        self.assertTrue(
            auth_header.startswith("Bearer "),
            f"Expected Bearer auth, got: {auth_header!r}",
        )
        self.assertIn("KEY_super_secret_99", auth_header)

    def test_bearer_content_type_json(self) -> None:
        """Telnyx использует Content-Type: application/json (не form-encoded)."""
        adapter = _telnyx()
        session = adapter._get_session()
        self.assertEqual(session.headers.get("Content-Type"), "application/json")

    def test_bearer_dial_sends_json_payload(self) -> None:
        """POST при dial Telnyx передаёт json= (не data=)."""
        adapter = _telnyx()
        mock_resp = _mock_resp(
            201,
            {"data": {"call_leg_id": "leg1", "call_control_id": "ctrl1"}},
        )
        sess = MagicMock()
        sess.post.return_value = mock_resp
        sess.headers = {}
        adapter._session = sess

        adapter.dial("+15550001234")

        call_args = sess.post.call_args
        # json= kwarg, не data=
        self.assertIn("json", call_args.kwargs, "Telnyx should POST with json= kwarg")
        self.assertNotIn("data", call_args.kwargs)


# ---------------------------------------------------------------------------
# 6. test_dial_with_basic_auth (Twilio)
# ---------------------------------------------------------------------------

class TwilioBasicAuthTestCase(unittest.TestCase):
    """test_dial_with_basic_auth — Twilio использует HTTP Basic Auth."""

    def test_basic_auth_credentials(self) -> None:
        """_auth() возвращает HTTPBasicAuth с правильными credentials."""
        from requests.auth import HTTPBasicAuth
        adapter = _twilio(sid="ACsid_xyz", token="tok_abc")
        auth = adapter._auth()
        self.assertIsInstance(auth, HTTPBasicAuth)
        self.assertEqual(auth.username, "ACsid_xyz")
        self.assertEqual(auth.password, "tok_abc")

    def test_basic_auth_content_type_form_encoded(self) -> None:
        """Twilio использует Content-Type: application/x-www-form-urlencoded."""
        adapter = _twilio()
        session = adapter._get_session()
        self.assertEqual(
            session.headers.get("Content-Type"),
            "application/x-www-form-urlencoded",
        )

    def test_basic_auth_dial_sends_form_payload(self) -> None:
        """POST при dial Twilio передаёт data= (не json=)."""
        adapter = _twilio()
        mock_resp = _mock_resp(201, {"sid": "CA001", "status": "queued"})
        sess = MagicMock()
        sess.post.return_value = mock_resp
        sess.headers = {}
        adapter._session = sess

        adapter.dial("+15550001234")

        call_args = sess.post.call_args
        self.assertIn("data", call_args.kwargs, "Twilio should POST with data= kwarg")
        self.assertNotIn("json", call_args.kwargs)


# ---------------------------------------------------------------------------
# 7. test_hangup_idempotent_both
# ---------------------------------------------------------------------------

class HangupIdempotentTestCase(unittest.TestCase):
    """test_hangup_idempotent_both — повторный hangup не должен поднимать исключение."""

    def test_telnyx_hangup_twice_no_exception(self) -> None:
        adapter = _telnyx()
        _inject_post(adapter, _mock_resp(200, {"data": {}}))
        r1 = adapter.hangup("ctrl_abc")
        # Второй вызов — инжектируем 200 снова (idempotent API)
        _inject_post(adapter, _mock_resp(200, {"data": {}}))
        r2 = adapter.hangup("ctrl_abc")
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])

    def test_twilio_hangup_twice_no_exception(self) -> None:
        adapter = _twilio()
        _inject_post(adapter, _mock_resp(200, {"sid": "CA1", "status": "completed"}))
        r1 = adapter.hangup("CA1")
        _inject_post(adapter, _mock_resp(200, {"sid": "CA1", "status": "completed"}))
        r2 = adapter.hangup("CA1")
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])

    def test_both_hangup_missing_id_same_error_code(self) -> None:
        """Оба адаптера при пустом call_control_id возвращают одинаковый error-код."""
        r_tel = _telnyx().hangup("")
        r_twi = _twilio().hangup("")
        self.assertFalse(r_tel["ok"])
        self.assertFalse(r_twi["ok"])
        self.assertEqual(r_tel["error"], r_twi["error"])
        self.assertEqual(r_tel["error"], "missing_call_control_id")


# ---------------------------------------------------------------------------
# 8. test_handles_api_error_both
# ---------------------------------------------------------------------------

class ApiErrorParityTestCase(unittest.TestCase):
    """test_handles_api_error_both — общие HTTP error-коды обрабатываются одинаково."""

    def test_401_unauthorized_both(self) -> None:
        for name, adapter, injector in [
            ("Telnyx", _telnyx(), _inject_post),
            ("Twilio", _twilio(), _inject_post),
        ]:
            injector(adapter, _mock_resp(401, {"message": "Unauthorized"}))
            result = adapter.dial("+15550001234")
            self.assertFalse(result["ok"], f"{name}: expected ok=False on 401")
            self.assertEqual(result["error"], "unauthorized", f"{name}: wrong error code on 401")

    def test_402_insufficient_balance_both(self) -> None:
        for name, adapter, injector in [
            ("Telnyx", _telnyx(), _inject_post),
            ("Twilio", _twilio(), _inject_post),
        ]:
            injector(adapter, _mock_resp(402, None, content=b""))
            result = adapter.dial("+15550001234")
            self.assertFalse(result["ok"], f"{name}: expected ok=False on 402")
            self.assertEqual(result["error"], "insufficient_balance", f"{name}: wrong error on 402")

    def test_429_rate_limit_both(self) -> None:
        for name, adapter, injector in [
            ("Telnyx", _telnyx(), _inject_post),
            ("Twilio", _twilio(), _inject_post),
        ]:
            injector(adapter, _mock_resp(429, {}, headers={"Retry-After": "0"}))
            result = adapter.dial("+15550001234")
            self.assertFalse(result["ok"], f"{name}: expected ok=False on 429")
            self.assertEqual(result["error"], "rate_limit", f"{name}: wrong error on 429")

    def test_network_error_both(self) -> None:
        for name, adapter in [("Telnyx", _telnyx()), ("Twilio", _twilio())]:
            _inject_post_raises(adapter, requests.exceptions.ConnectionError("refused"))
            result = adapter.dial("+15550001234")
            self.assertFalse(result["ok"], f"{name}: expected ok=False on network error")
            self.assertEqual(result["error"], "network_error", f"{name}: wrong error on network err")

    def test_500_error_both(self) -> None:
        for name, adapter, injector in [
            ("Telnyx", _telnyx(), _inject_post),
            ("Twilio", _twilio(), _inject_post),
        ]:
            injector(adapter, _mock_resp(500, None, content=b""))
            result = adapter.dial("+15550001234")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "http_500")


# ---------------------------------------------------------------------------
# 9. test_concurrent_dial_per_adapter
# ---------------------------------------------------------------------------

class ConcurrentDialParityTestCase(unittest.TestCase):
    """test_concurrent_dial_per_adapter — параллельные вызовы из 8 потоков."""

    @staticmethod
    def _run_concurrent_dials(
        make_adapter_fn,  # () -> adapter
        make_success_resp_fn,  # (number: str) -> mock_resp
        inject_fn,  # (adapter, resp) -> None
        n: int = 8,
    ) -> Tuple[List[Dict], List[Exception]]:
        results: List[Dict] = []
        errors: List[Exception] = []
        lock = threading.Lock()

        def worker(number: str) -> None:
            try:
                adapter = make_adapter_fn()
                resp = make_success_resp_fn(number)
                inject_fn(adapter, resp)
                r = adapter.dial(number)
                with lock:
                    results.append(r)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        numbers = [f"+155500{i:05d}" for i in range(n)]
        threads = [threading.Thread(target=worker, args=(num,)) for num in numbers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        return results, errors

    def test_telnyx_concurrent_dial(self) -> None:
        def make_resp(number: str) -> MagicMock:
            return _mock_resp(
                201,
                {"data": {"call_leg_id": f"leg_{number[-4:]}", "call_control_id": f"ctrl_{number[-4:]}"}},
            )

        results, errors = self._run_concurrent_dials(
            make_adapter_fn=_telnyx,
            make_success_resp_fn=make_resp,
            inject_fn=_inject_post,
        )
        self.assertEqual(errors, [], f"Telnyx concurrent errors: {errors}")
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r["ok"] for r in results))

    def test_twilio_concurrent_dial(self) -> None:
        def make_resp(number: str) -> MagicMock:
            return _mock_resp(
                201,
                {"sid": f"CA_{number[-4:]}", "status": "queued"},
            )

        results, errors = self._run_concurrent_dials(
            make_adapter_fn=_twilio,
            make_success_resp_fn=make_resp,
            inject_fn=_inject_post,
        )
        self.assertEqual(errors, [], f"Twilio concurrent errors: {errors}")
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r["ok"] for r in results))


# ---------------------------------------------------------------------------
# 10. test_unicode_phone_number_handled
# ---------------------------------------------------------------------------

class UnicodePhoneNumberTestCase(unittest.TestCase):
    """test_unicode_phone_number_handled — Unicode/non-ASCII в номере."""

    _UNICODE_NUMBERS = [
        "+１５５５０００１２３４",  # fullwidth digits
        "+7•16•987",          # bullet-separated
        "+1 (555) 000-1234",            # with spaces and parens
        "+7​916​123​45​67",  # zero-width spaces
        "++15550001234",                # double plus
        "",                             # empty
        "+",                            # only plus
    ]

    def test_telnyx_unicode_phone_returns_ok_false(self) -> None:
        """TelnyxAdapter должен отклонить невалидные/Unicode номера."""
        adapter = _telnyx()
        for number in self._UNICODE_NUMBERS:
            result = adapter.dial(number)
            self.assertFalse(
                result["ok"],
                f"TelnyxAdapter.dial({number!r}) should return ok=False",
            )
            self.assertIn(
                result.get("error", ""),
                ("invalid_phone_number", "telnyx_not_configured"),
                f"Unexpected error for {number!r}: {result}",
            )

    def test_twilio_unicode_phone_returns_ok_false(self) -> None:
        """TwilioAdapter должен отклонить невалидные/Unicode номера."""
        adapter = _twilio()
        for number in self._UNICODE_NUMBERS:
            result = adapter.dial(number)
            self.assertFalse(
                result["ok"],
                f"TwilioAdapter.dial({number!r}) should return ok=False",
            )
            self.assertIn(
                result.get("error", ""),
                ("invalid_phone_number", "twilio_not_configured"),
                f"Unexpected error for {number!r}: {result}",
            )

    def test_valid_e164_accepted_both(self) -> None:
        """Валидные E.164 номера проходят валидацию у обоих адаптеров."""
        valid_numbers = ["+15550001234", "+79161234567", "+34612345678"]
        for number in valid_numbers:
            for name, adapter, make_resp_fn, inject_fn in [
                ("Telnyx", _telnyx(),
                 lambda n: _mock_resp(201, {"data": {"call_leg_id": "l", "call_control_id": "c"}}),
                 _inject_post),
                ("Twilio", _twilio(),
                 lambda n: _mock_resp(201, {"sid": "CA1", "status": "queued"}),
                 _inject_post),
            ]:
                inject_fn(adapter, make_resp_fn(number))
                result = adapter.dial(number)
                self.assertTrue(
                    result["ok"],
                    f"{name}.dial({number!r}) should succeed, got {result}",
                )


# ---------------------------------------------------------------------------
# 11. Behavioral gap documentation tests
#     (тесты явно фиксируют РАСХОЖДЕНИЯ между адаптерами)
# ---------------------------------------------------------------------------

class BehavioralGapTestCase(unittest.TestCase):
    """
    Документирует известные расхождения между TelnyxAdapter и TwilioAdapter.

    Gap 1 — Auth scheme:
        Telnyx: Bearer token (Authorization: Bearer <key>)
        Twilio: HTTP Basic Auth (account_sid:auth_token)

    Gap 2 — Content-Type:
        Telnyx: application/json (POST body = JSON)
        Twilio: application/x-www-form-urlencoded (POST body = form data)

    Gap 3 — call_control_id semantics:
        Telnyx: pre-assigned call_control_id support (passed in payload)
        Twilio: call_control_id parameter is IGNORED (Twilio assigns SID internally)

    Gap 4 — call_id vs call_control_id:
        Telnyx: call_id = call_leg_id, call_control_id = separate field
        Twilio: call_id == call_control_id == sid (same value)

    Gap 5 — HTTP 400 handling:
        Telnyx: returns {"ok": False, "error": "http_400"} (generic)
        Twilio: returns {"ok": False, "error": "validation_error", "twilio_code": ...}

    Gap 6 — HTTP 422 handling:
        Telnyx: returns {"ok": False, "error": "unreachable_number"}
        Twilio: returns {"ok": False, "error": "http_422"} (generic)

    Gap 7 — HTTP 404 handling:
        Telnyx: returns {"ok": False, "error": "http_404"} (generic)
        Twilio: returns {"ok": False, "error": "not_found"} (specific)

    Gap 8 — is_configured condition:
        Telnyx: requires only api_key
        Twilio: requires BOTH account_sid AND auth_token
    """

    # Gap 1: auth scheme
    def test_gap1_telnyx_uses_bearer_twilio_uses_basic(self) -> None:
        telnyx_session = _telnyx(key="MY_KEY").get_session() if hasattr(TelnyxAdapter, "get_session") else _telnyx(key="MY_KEY")._get_session()
        twilio_adapter = _twilio()
        from requests.auth import HTTPBasicAuth
        auth = twilio_adapter._auth()
        self.assertIsInstance(auth, HTTPBasicAuth)
        self.assertIn("Bearer", telnyx_session.headers.get("Authorization", ""))

    # Gap 3: call_control_id parameter behavior
    def test_gap3_twilio_ignores_call_control_id_param(self) -> None:
        """Twilio.dial() принимает call_control_id но игнорирует его."""
        adapter = _twilio()
        mock_resp = _mock_resp(201, {"sid": "CA_NEW", "status": "queued"})
        _inject_post(adapter, mock_resp)
        result = adapter.dial("+15550001234", call_control_id="IGNORED_BY_TWILIO")
        self.assertTrue(result["ok"])
        # Twilio SID всегда назначается API, не берётся из параметра
        self.assertEqual(result["call_control_id"], "CA_NEW")

    # Gap 4: call_id == call_control_id in Twilio but not Telnyx
    def test_gap4_twilio_call_id_equals_call_control_id(self) -> None:
        adapter = _twilio()
        _inject_post(adapter, _mock_resp(201, {"sid": "CA_SID_123", "status": "queued"}))
        result = adapter.dial("+15550001234")
        self.assertTrue(result["ok"])
        self.assertEqual(result["call_id"], result["call_control_id"])

    def test_gap4_telnyx_call_id_may_differ_from_call_control_id(self) -> None:
        adapter = _telnyx()
        _inject_post(
            adapter,
            _mock_resp(
                201,
                {"data": {"call_leg_id": "LEG_001", "call_control_id": "CTRL_002"}},
            ),
        )
        result = adapter.dial("+15550001234")
        self.assertTrue(result["ok"])
        # call_id и call_control_id могут различаться у Telnyx
        self.assertEqual(result["call_id"], "LEG_001")
        self.assertEqual(result["call_control_id"], "CTRL_002")
        self.assertNotEqual(result["call_id"], result["call_control_id"])

    # Gap 5: HTTP 400
    def test_gap5_twilio_400_returns_validation_error(self) -> None:
        adapter = _twilio()
        _inject_post(
            adapter,
            _mock_resp(400, {"message": "Invalid To number", "code": 21211}),
        )
        result = adapter.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "validation_error")
        self.assertIn("twilio_code", result)

    def test_gap5_telnyx_400_returns_generic_http_400(self) -> None:
        adapter = _telnyx()
        _inject_post(adapter, _mock_resp(400, {"errors": [{"detail": "Bad request"}]}))
        result = adapter.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "http_400")

    # Gap 6: HTTP 422
    def test_gap6_telnyx_422_returns_unreachable_number(self) -> None:
        adapter = _telnyx()
        _inject_post(adapter, _mock_resp(422, {}))
        result = adapter.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unreachable_number")

    # Gap 7: HTTP 404
    def test_gap7_twilio_404_returns_not_found(self) -> None:
        adapter = _twilio()
        _inject_get(adapter, _mock_resp(404, {"message": "Not found"}))
        result = adapter.get_call_status("CA_missing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_found")

    # Gap 8: is_configured condition
    def test_gap8_telnyx_configured_with_key_only(self) -> None:
        """Telnyx сконфигурирован если задан только api_key."""
        adapter = TelnyxAdapter(api_key="some_key", connection_id="", from_number="")
        self.assertTrue(adapter.is_configured())

    def test_gap8_twilio_requires_both_sid_and_token(self) -> None:
        """Twilio НЕ сконфигурирован если задан только один из двух параметров."""
        only_sid = TwilioAdapter(account_sid="ACxxx", auth_token="", from_number="")
        only_token = TwilioAdapter(account_sid="", auth_token="tok", from_number="")
        self.assertFalse(only_sid.is_configured())
        self.assertFalse(only_token.is_configured())


if __name__ == "__main__":
    unittest.main()
