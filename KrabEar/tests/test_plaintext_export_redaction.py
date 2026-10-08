"""A5.3 15a: секреты не покидают реальные dispatcher/socket error paths."""
import json
import logging
from pathlib import Path
import socket
import unittest
from unittest import mock


_SECRETS = ("sentinel-capability-73", "sentinel-receipt-84", "sentinel-session-95")


def _params():
    return {"outer": [{"plaintext_export": dict(zip(
        ("capability", "receipt", "app_session_id"), _SECRETS,
    ))}]}


class PlaintextExportRedactionTest(unittest.TestCase):
    def setUp(self):
        from test_plaintext_export_ipc import PlaintextExportIPCTest
        PlaintextExportIPCTest.setUp(self)

    def assert_secret_free(self, value):
        serialized = str(value)
        for secret in _SECRETS:
            self.assertNotIn(secret, serialized)

    def test_dispatcher_exceptions_do_not_leak_in_records_tracebacks_or_response(self):
        from backend.ipc_errors import IpcOperationalError
        for error_type in (RuntimeError, TypeError, IpcOperationalError):
            with self.subTest(error_type=error_type):
                def fail(_params):
                    raise error_type("safe detail " + " / ".join(_SECRETS))
                self.service._dispatch_table["redaction_test"] = fail
                with self.assertLogs("KrabEar.Backend.Service", level="WARNING") as logs:
                    response = self.service.handle_request({
                        "id": 1, "method": "redaction_test", "params": _params(),
                    })
                self.assertFalse(response["ok"])
                expected = "invalid_request" if error_type is RuntimeError else "internal_error"
                self.assertEqual(response["error"]["code"], expected)
                self.assertIn("safe detail", response["error"]["message"])
                self.assert_secret_free(response)
                for record in logs.records:
                    self.assertIsNone(record.exc_info)
                    self.assert_secret_free(vars(record))
                    self.assert_secret_free(logging.Formatter().format(record))
                    if record.exc_info:
                        self.assert_secret_free(logging.Formatter().formatException(record.exc_info))
                # Диагностический экспорт читает тот же audit NDJSON, не raw params.
                self.assert_secret_free(self.service._audit_logger.get_audit_log())

    def test_logging_integration_keeps_independent_method_and_type_quota(self):
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.transport import Transport
        from backend import observability
        delivered = []

        class LocalTransport(Transport):
            def capture_envelope(self, envelope):
                delivered.append(envelope.get_event())

        client = sentry_sdk.Client(
            dsn="https://public@example.invalid/1", transport=LocalTransport,
            default_integrations=False, auto_enabling_integrations=False,
            integrations=[LoggingIntegration(level=None, event_level=logging.ERROR)],
            before_send=observability._sentry_before_send, include_local_variables=False,
        )
        self.addCleanup(client.close)
        observability._reset_sentry_rate_limiter()
        self.addCleanup(observability._reset_sentry_rate_limiter)
        self.service._ipc_throttle = None
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            for method, kind in (("redaction_alpha", TypeError), ("redaction_beta", TypeError),
                                 ("redaction_beta", KeyError)):
                def fail(_params, error_kind=kind):
                    raise error_kind("same safe detail " + _SECRETS[0])

                self.service._dispatch_table[method] = fail
                before = len(delivered)
                for _ in range(observability.SENTRY_HOURLY_CAP_PER_SIGNATURE + 1):
                    response = self.service.handle_request({"method": method, "params": _params()})
                    self.assertFalse(response["ok"])
                self.assertEqual(len(delivered) - before, observability.SENTRY_HOURLY_CAP_PER_SIGNATURE)
        self.assert_secret_free(delivered)
        self.assert_secret_free(observability._sentry_sent_per_signature)

    def test_audit_parameter_names_cannot_repeat_auth_values(self):
        params = _params()
        params[_SECRETS[0]] = "ordinary value"
        response = self.service.handle_request({"method": "ping", "params": params})
        self.assertTrue(response["ok"])
        self.assert_secret_free(self.service._audit_logger.get_audit_log())

    def test_dispatcher_sanitizer_failure_is_constant_and_fail_closed(self):
        def fail(_params):
            raise TypeError(" / ".join(_SECRETS))
        self.service._dispatch_table["redaction_test"] = fail
        with mock.patch("backend.plaintext_export_redaction.redact_auth", side_effect=ValueError("bad sanitizer")):
            with self.assertLogs("KrabEar.Backend.Service", level="ERROR") as logs:
                result = self.service.handle_request({"method": "redaction_test", "params": _params()})
        self.assertFalse(result["ok"])
        self.assert_secret_free((result, [vars(r) for r in logs.records]))

    def test_invalid_requests_unknown_method_and_signing_do_not_echo_secrets(self):
        for payload in (
            {"method": "ping", "params": [_params()]},
            {"method": _SECRETS[0], "params": _params()},
            {"id": _params(), "method": "unknown", "params": _params()},
        ):
            self.assert_secret_free(self.service.handle_request(payload))
        self.service._request_signer = mock.Mock()
        self.service._request_signer.verify_request.return_value = False
        with self.assertLogs("KrabEar.Backend.Service", level="WARNING") as logs:
            result = self.service.handle_request({
                "id": 1, "method": "ping", "params": _params(),
                "signature": _SECRETS[0], "timestamp": _SECRETS[1], "nonce": _SECRETS[2],
            })
        self.assertEqual(result["error"]["code"], "unauthorized")
        self.assert_secret_free((result, [vars(r) for r in logs.records]))

    def socket_call(self, raw, *, fail=False):
        from backend.ipc_server import IPCServer
        server = IPCServer(Path(self.tmp.name) / "unused.sock", self.service)
        peer, conn = socket.socketpair()
        self.addCleanup(peer.close)
        peer.settimeout(3)
        peer.sendall(raw + b"\n")
        server._conn_semaphore.acquire()
        if fail:
            with mock.patch.object(self.service, "handle_request", side_effect=TypeError(
                "socket safe detail " + " / ".join(_SECRETS)
            )):
                server._handle_connection(conn)
        else:
            server._handle_connection(conn)
        return json.loads(peer.recv(65536))

    def test_socket_uncaught_exception_is_redacted_before_logging(self):
        raw = json.dumps({"id": 1, "method": "ping", "params": _params()}).encode()
        with self.assertLogs("KrabEar.Backend.Service", level="ERROR") as logs:
            result = self.socket_call(raw, fail=True)
        self.assert_secret_free(result)
        for record in logs.records:
            self.assert_secret_free(vars(record))
            self.assert_secret_free(logging.Formatter().format(record))

    def test_socket_send_failure_cannot_leak_newly_generated_grant(self):
        from backend.ipc_server import IPCServer
        service = self.service
        service.store.save_settings({"history_encryption_enabled": True, "privacy_mode_enabled": False})
        policy = service.handle_request({"method": "get_plaintext_export_policy"})["result"]
        request = {
            "method": "grant_plaintext_export_session",
            "params": {"app_session_id": self.session, "expected_epoch": policy["epoch"],
                       "expected_policy_generation": policy["policy_generation"]},
        }
        generated = []

        class FailedSend:
            def settimeout(self, _timeout):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def recv(self, _size):
                return json.dumps(request).encode() + b"\n"

            def sendall(self, data):
                response = json.loads(data)
                generated.append(response["result"]["capability"])
                raise RuntimeError("send failed: " + generated[-1])
        server = IPCServer(Path(self.tmp.name) / "unused.sock", service)
        server._conn_semaphore.acquire()
        with self.assertLogs("KrabEar.Backend.Service", level="ERROR") as logs:
            server._handle_connection(FailedSend())
        self.assertEqual(len(generated), 1)
        for record in logs.records:
            self.assertNotIn(generated[0], str(vars(record)))
            self.assertNotIn(self.session, str(vars(record)))
        self.assertNotIn(generated[0], json.dumps(service._audit_logger.get_audit_log()))

    def test_malformed_json_send_error_does_not_log_unknown_input(self):
        from backend.ipc_server import IPCServer

        class FailedSend:
            def settimeout(self, _timeout):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def recv(self, _size):
                return b"{broken\n"

            def sendall(self, _data):
                raise RuntimeError("bad decode " + " / ".join(_SECRETS))
        server = IPCServer(Path(self.tmp.name) / "unused.sock", self.service)
        server._conn_semaphore.acquire()
        with self.assertLogs("KrabEar.Backend.Service", level="ERROR") as logs:
            server._handle_connection(FailedSend())
        self.assert_secret_free([vars(record) for record in logs.records])

    def test_socket_malformed_json_never_echoes_decoder_text(self):
        # UTF-8 decoder errors can quote raw input; malformed JSON has no trusted context.
        result = self.socket_call(b'{"capability":"' + _SECRETS[0].encode() + b'\xff"}')
        self.assertEqual(result["error"]["code"], "invalid_json")
        self.assert_secret_free(result)


class SentryExportRedactionTest(unittest.TestCase):
    def setUp(self):
        from backend import observability
        observability._reset_sentry_rate_limiter()

    def test_whole_event_and_nested_auth_values_are_sanitized_before_fake_transport(self):
        from backend.observability import _sentry_before_send
        event = {
            "extra": _params(),
            "exception": {"values": [{"type": "RuntimeError", "value": " / ".join(_SECRETS)}]},
            "logentry": {"formatted": " / ".join(_SECRETS)},
            "breadcrumbs": {"values": [{"message": repr(_params())}]},
        }
        delivered = []
        sanitized = _sentry_before_send(event, {})
        if sanitized is not None:
            delivered.append(sanitized)
        self.assertEqual(len(delivered), 1)
        for secret in _SECRETS:
            self.assertNotIn(secret, json.dumps(delivered))

    def test_auth_in_json_string_scrubs_naked_repeat_and_quota_cache(self):
        from backend import observability
        for encoded in (
            json.dumps({"outer": [{"capability": _SECRETS[0], "receipt": _SECRETS[1],
                                   "app_session_id": _SECRETS[2]}]}),
            "capability='" + _SECRETS[0] + "' receipt=" + _SECRETS[1] + " app_session_id=" + _SECRETS[2],
        ):
            with self.subTest(encoded=encoded):
                event = {
                    "request": {"data": encoded},
                    "exception": {"values": [{"type": "RuntimeError", "value": "failed " + " / ".join(_SECRETS)}]},
                }
                sanitized = observability._sentry_before_send(event, {})
                self.assertIsNotNone(sanitized)
                for secret in _SECRETS:
                    self.assertNotIn(secret, json.dumps(sanitized))
                    self.assertNotIn(secret, str(observability._sentry_sent_per_signature))

    def test_encoded_auth_string_is_removed_as_a_whole_even_for_known_secret(self):
        from backend import observability
        for encoded in (
            r'{"\u0063apability":"\u0054OKEN"}',
            r"{'\u0063apability':'\u0054OKEN'}",
        ):
            with self.subTest(encoded=encoded):
                event = {
                    # Secret already known before visiting request.data; detection
                    # must track auth-bearing strings, not only new secret values.
                    "extra": {"capability": "TOKEN"},
                    "request": {"data": encoded},
                    "exception": {"values": [{"type": "RuntimeError", "value": "failed TOKEN; body=" + encoded}]},
                }
                sanitized = observability._sentry_before_send(event, {})
                self.assertIsNotNone(sanitized)
                # A marker-only scan could miss reversibly encoded secrets.
                # The entire auth-bearing serialized representation must be gone.
                self.assertEqual(sanitized["request"]["data"], "[REDACTED]")
                self.assertNotIn("TOKEN", json.dumps(sanitized))
                self.assertNotIn(encoded, sanitized["exception"]["values"][0]["value"])

    def test_real_sdk_serializes_timestamp_before_redaction_and_fake_transport(self):
        from datetime import datetime, timezone
        import sentry_sdk
        from sentry_sdk.transport import Transport
        from backend.observability import _sentry_before_send
        delivered = []

        class LocalTransport(Transport):
            def capture_envelope(self, envelope):
                delivered.append(envelope.get_event())
        client = sentry_sdk.Client(
            dsn="https://public@example.invalid/1", transport=LocalTransport,
            default_integrations=False, auto_enabling_integrations=False,
            before_send=_sentry_before_send, include_local_variables=False,
        )
        self.addCleanup(client.close)
        client.capture_event({
            "timestamp": datetime(2026, 10, 3, tzinfo=timezone.utc),
            "message": "safe " + " / ".join(_SECRETS),
            "extra": {**_params(), "count": 2, "ratio": 0.5, "ready": True},
        })
        self.assertEqual(len(delivered), 1)
        event = delivered[0]
        self.assertEqual(event["timestamp"], "2026-10-03T00:00:00.000000Z")
        self.assertEqual(event["extra"]["count"], 2)
        self.assertEqual(event["extra"]["ratio"], 0.5)
        self.assertIs(event["extra"]["ready"], True)
        for secret in _SECRETS:
            self.assertNotIn(secret, json.dumps(event))

    def test_redaction_failure_drops_event_instead_of_sending_original(self):
        from backend import observability
        event = {"message": _SECRETS[0], "extra": _params()}
        with mock.patch.object(observability, "_redact_string", side_effect=RuntimeError("bad sanitizer")):
            self.assertIsNone(observability._sentry_before_send(event, {}))
