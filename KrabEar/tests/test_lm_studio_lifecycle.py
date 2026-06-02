"""Wave 143 — unit tests for lm_studio_lifecycle (load_model_async / unload_model_async).

All HTTP and subprocess calls are mocked; no real LM Studio required.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.lm_studio_lifecycle import (
    load_model_async,
    unload_model_async,
    _try_rest_load,
    _try_rest_unload,
    _try_cli,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:1234/v1"
MODEL_ID = "qwen3.6-35b-a3b"


def _fake_response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _wait_for_thread(name_prefix: str, timeout: float = 3.0) -> None:
    """Wait until daemon thread with name_prefix finishes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = [t for t in threading.enumerate() if t.name.startswith(name_prefix)]
        if not active:
            return
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Wave 143.1: test_load_via_rest_api
# ---------------------------------------------------------------------------

class TestLoadViaRestApi(unittest.TestCase):
    """_try_rest_load() returns True on 2xx response."""

    def test_load_via_rest_api(self):
        """_try_rest_load() must POST to /api/v0/models/load and return True on 200."""
        resp = _fake_response(200)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as mock_open:
            result = _try_rest_load(BASE_URL, MODEL_ID)
        self.assertTrue(result)
        mock_open.assert_called_once()
        req = mock_open.call_args[0][0]
        self.assertIn("/api/v0/models/load", req.full_url)
        self.assertEqual(req.method, "POST")

    def test_unload_via_rest_api_separate(self):
        """_try_rest_unload() must POST to /api/v0/models/<id>/unload and return True on 200."""
        resp = _fake_response(200)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as mock_open:
            result = _try_rest_unload(BASE_URL, MODEL_ID)
        self.assertTrue(result)
        req = mock_open.call_args[0][0]
        self.assertIn(f"/api/v0/models/{MODEL_ID}/unload", req.full_url)

    def test_load_rest_base_url_v1_stripped(self):
        """Base URL ending in /v1 must be stripped when building API root."""
        resp = _fake_response(200)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as mock_open:
            _try_rest_load("http://localhost:1234/v1", MODEL_ID)
        req = mock_open.call_args[0][0]
        self.assertNotIn("/v1/api", req.full_url)
        self.assertIn("localhost:1234", req.full_url)

    def test_load_rest_returns_false_on_4xx(self):
        """HTTP 404 from load endpoint must return False silently."""
        err = urllib.error.HTTPError(url="", code=404, msg="Not Found", hdrs=MagicMock(), fp=None)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=err):
            result = _try_rest_load(BASE_URL, MODEL_ID)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Wave 143.2: test_load_falls_back_to_cli
# ---------------------------------------------------------------------------

class TestLoadFallbackToCli(unittest.TestCase):
    """When REST returns 404/405, CLI lms is tried."""

    def test_load_falls_back_to_cli(self):
        """REST 404 must trigger _try_cli('load', model_id) fallback."""
        err404 = urllib.error.HTTPError(url="", code=404, msg="Not Found", hdrs=MagicMock(), fp=None)
        cli_result = MagicMock()
        cli_result.returncode = 0

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=err404):
            with patch("shutil.which", return_value="/usr/local/bin/lms"):
                with patch("subprocess.run", return_value=cli_result) as mock_run:
                    result = _try_cli("load", MODEL_ID)

        self.assertTrue(result)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("load", cmd)
        self.assertIn(MODEL_ID, cmd)

    def test_cli_not_found_returns_false(self):
        """If lms binary is not in PATH, _try_cli() returns False."""
        with patch("shutil.which", return_value=None):
            result = _try_cli("load", MODEL_ID)
        self.assertFalse(result)

    def test_cli_nonzero_exit_returns_false(self):
        """If lms exits non-zero, _try_cli() returns False."""
        proc = MagicMock()
        proc.returncode = 1
        with patch("shutil.which", return_value="/usr/local/bin/lms"):
            with patch("subprocess.run", return_value=proc):
                result = _try_cli("load", MODEL_ID)
        self.assertFalse(result)

    def test_load_async_falls_back_to_cli_when_rest_fails(self):
        """load_model_async() uses CLI when REST returns 404."""
        rest_err = urllib.error.HTTPError(url="", code=404, msg="NF", hdrs=MagicMock(), fp=None)
        cli_proc = MagicMock()
        cli_proc.returncode = 0

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=rest_err):
            with patch("shutil.which", return_value="/usr/local/bin/lms"):
                with patch("subprocess.run", return_value=cli_proc) as mock_run:
                    load_model_async(BASE_URL, MODEL_ID)
                    _wait_for_thread(f"LMStudio-load-{MODEL_ID[:20]}")

        mock_run.assert_called()


# ---------------------------------------------------------------------------
# Wave 143.3: test_unload_via_rest_api
# ---------------------------------------------------------------------------

class TestUnloadViaRestApi(unittest.TestCase):
    """unload_model_async() dispatches REST unload and logs success."""

    def test_unload_via_rest_api(self):
        """unload_model_async() triggers REST POST and logs success."""
        resp = _fake_response(200)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as mock_open:
            unload_model_async(BASE_URL, MODEL_ID)
            _wait_for_thread(f"LMStudio-unload-{MODEL_ID[:20]}")

        mock_open.assert_called_once()
        req = mock_open.call_args[0][0]
        self.assertIn("unload", req.full_url)

    def test_unload_rest_returns_true_on_200(self):
        """_try_rest_unload returns True on HTTP 200."""
        resp = _fake_response(200)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            self.assertTrue(_try_rest_unload(BASE_URL, MODEL_ID))

    def test_unload_rest_returns_true_on_204(self):
        """_try_rest_unload returns True on HTTP 204 No Content."""
        resp = _fake_response(204)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            self.assertTrue(_try_rest_unload(BASE_URL, MODEL_ID))

    def test_unload_rest_returns_false_on_500(self):
        """HTTP 500 from unload endpoint returns False."""
        err = urllib.error.HTTPError(url="", code=500, msg="Server Error", hdrs=MagicMock(), fp=None)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=err):
            result = _try_rest_unload(BASE_URL, MODEL_ID)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Wave 143.4: test_handles_lm_studio_not_running
# ---------------------------------------------------------------------------

class TestHandlesLmStudioNotRunning(unittest.TestCase):
    """When LM Studio is not running, REST raises ConnectionRefusedError / URLError."""

    def test_handles_lm_studio_not_running(self):
        """ConnectionRefusedError from REST must be silently handled; returns False."""
        import urllib.error
        conn_err = urllib.error.URLError("Connection refused")
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=conn_err):
            result = _try_rest_load(BASE_URL, MODEL_ID)
        self.assertFalse(result)

    def test_load_async_silent_when_studio_offline(self):
        """load_model_async() must not raise when LM Studio is offline + lms absent."""
        conn_err = urllib.error.URLError("Connection refused")
        try:
            with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=conn_err):
                with patch("shutil.which", return_value=None):
                    load_model_async(BASE_URL, MODEL_ID)
                    _wait_for_thread(f"LMStudio-load-{MODEL_ID[:20]}")
        except Exception as exc:
            self.fail(f"load_model_async raised unexpectedly: {exc}")

    def test_unload_async_silent_when_studio_offline(self):
        """unload_model_async() must not raise when LM Studio is offline + lms absent."""
        conn_err = urllib.error.URLError("Connection refused")
        try:
            with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=conn_err):
                with patch("shutil.which", return_value=None):
                    unload_model_async(BASE_URL, MODEL_ID)
                    _wait_for_thread(f"LMStudio-unload-{MODEL_ID[:20]}")
        except Exception as exc:
            self.fail(f"unload_model_async raised unexpectedly: {exc}")

    def test_empty_model_id_is_no_op(self):
        """Empty model_id must be a no-op; no network call made."""
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open") as mock_open:
            load_model_async(BASE_URL, "")
            unload_model_async(BASE_URL, "")
            time.sleep(0.1)  # let any daemon threads run
        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# Wave 143.5: test_concurrent_load_idempotent
# ---------------------------------------------------------------------------

class TestConcurrentLoadIdempotent(unittest.TestCase):
    """Calling load_model_async() from multiple threads must all succeed without error."""

    def test_concurrent_load_idempotent(self):
        """N concurrent load_model_async calls must all complete without raising."""
        n = 8
        resp = _fake_response(200)
        errors: list[Exception] = []
        lock = threading.Lock()

        def _caller():
            try:
                load_model_async(BASE_URL, MODEL_ID)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            threads = [threading.Thread(target=_caller, daemon=True) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

            # Wait for all worker threads
            _wait_for_thread("LMStudio-load-", timeout=5.0)

        self.assertEqual(errors, [], f"Errors in concurrent load: {errors}")

    def test_concurrent_unload_idempotent(self):
        """N concurrent unload_model_async calls must all complete without raising."""
        n = 8
        resp = _fake_response(200)
        errors: list[Exception] = []
        lock = threading.Lock()

        def _caller():
            try:
                unload_model_async(BASE_URL, MODEL_ID)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            threads = [threading.Thread(target=_caller, daemon=True) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

            _wait_for_thread("LMStudio-unload-", timeout=5.0)

        self.assertEqual(errors, [], f"Errors in concurrent unload: {errors}")


# ---------------------------------------------------------------------------
# Wave 143.6: test_timeout_respected
# ---------------------------------------------------------------------------

class TestTimeoutRespected(unittest.TestCase):
    """urlopen is called with the correct timeout constant."""

    def test_load_rest_uses_configured_timeout(self):
        """urlopen must be called with timeout=_REST_TIMEOUT_SEC for load."""
        from backend.lm_studio_lifecycle import _REST_TIMEOUT_SEC
        resp = _fake_response(200)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as mock_open:
            _try_rest_load(BASE_URL, MODEL_ID)
        _, kwargs = mock_open.call_args if mock_open.call_args.kwargs else (None, {})
        # urlopen is called as urlopen(req, timeout=X) — check positional args
        call_args = mock_open.call_args
        timeout_used = None
        if call_args.args and len(call_args.args) >= 2:
            timeout_used = call_args.args[1]
        elif "timeout" in call_args.kwargs:
            timeout_used = call_args.kwargs["timeout"]
        self.assertIsNotNone(timeout_used, "timeout must be passed to urlopen")
        self.assertAlmostEqual(timeout_used, _REST_TIMEOUT_SEC, places=2)

    def test_unload_rest_uses_configured_timeout(self):
        """urlopen must be called with timeout=_REST_TIMEOUT_SEC for unload."""
        from backend.lm_studio_lifecycle import _REST_TIMEOUT_SEC
        resp = _fake_response(200)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as mock_open:
            _try_rest_unload(BASE_URL, MODEL_ID)
        call_args = mock_open.call_args
        timeout_used = None
        if call_args.args and len(call_args.args) >= 2:
            timeout_used = call_args.args[1]
        elif "timeout" in call_args.kwargs:
            timeout_used = call_args.kwargs["timeout"]
        self.assertIsNotNone(timeout_used, "timeout must be passed to urlopen")
        self.assertAlmostEqual(timeout_used, _REST_TIMEOUT_SEC, places=2)

    def test_cli_uses_configured_timeout(self):
        """subprocess.run must be called with timeout=_CLI_TIMEOUT_SEC."""
        from backend.lm_studio_lifecycle import _CLI_TIMEOUT_SEC
        proc = MagicMock()
        proc.returncode = 0
        with patch("shutil.which", return_value="/usr/local/bin/lms"):
            with patch("subprocess.run", return_value=proc) as mock_run:
                _try_cli("load", MODEL_ID)
        call_kwargs = mock_run.call_args.kwargs
        self.assertIn("timeout", call_kwargs)
        self.assertAlmostEqual(call_kwargs["timeout"], _CLI_TIMEOUT_SEC, places=2)

    def test_async_thread_is_daemon(self):
        """Worker threads spawned by load/unload_model_async must be daemon threads."""
        spawned: list[threading.Thread] = []

        class ThreadSpy(threading.Thread):

            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                spawned.append(self)

        resp = _fake_response(200)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            with patch("backend.lm_studio_lifecycle.threading.Thread", ThreadSpy):
                load_model_async(BASE_URL, MODEL_ID)

        self.assertTrue(len(spawned) > 0)
        for t in spawned:
            self.assertTrue(t.daemon, "worker thread must be a daemon thread")


# ---------------------------------------------------------------------------
# Wave 1188: JSON injection + URL injection guards
# ---------------------------------------------------------------------------

class TestJsonAndUrlInjectionGuards(unittest.TestCase):
    """W1179 F1 MED — json.dumps body + urllib.parse.quote URL + length cap."""

    def test_load_model_quotes_special_chars_in_json(self):
        """model_id with double-quotes must produce valid JSON body, not a broken f-string."""
        evil_id = 'model"name"with"quotes'
        resp = _fake_response(200)
        captured_body: list[bytes] = []

        def fake_urlopen(req, timeout=None):
            captured_body.append(req.data)
            return resp

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=fake_urlopen):
            _try_rest_load(BASE_URL, evil_id)

        self.assertEqual(len(captured_body), 1, "urlopen should be called exactly once")
        body = captured_body[0]
        self.assertIsNotNone(body, "request body must not be None")
        # Must parse as valid JSON
        import json as _json
        try:
            parsed = _json.loads(body.decode())
        except _json.JSONDecodeError as exc:
            self.fail(f"Request body is not valid JSON: {exc!r}  body={body!r}")
        self.assertEqual(parsed.get("model"), evil_id,
                         "Parsed 'model' field must equal the original model_id exactly")

    def test_unload_model_quotes_special_chars_in_url(self):
        """model_id with special URL chars must be percent-encoded in the unload URL path."""
        evil_id = "model/with/slashes and spaces"
        resp = _fake_response(200)
        captured_url: list[str] = []

        def fake_urlopen(req, timeout=None):
            captured_url.append(req.full_url)
            return resp

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=fake_urlopen):
            _try_rest_unload(BASE_URL, evil_id)

        self.assertEqual(len(captured_url), 1)
        url = captured_url[0]
        # Raw special chars must NOT appear unencoded in the URL path segment
        self.assertNotIn(" ", url, "spaces must be percent-encoded in the URL")
        self.assertIn("%2F", url, "forward slashes in model_id must be percent-encoded")
        self.assertIn("unload", url, "URL must still end with /unload")

    def test_load_model_rejects_overlong_id(self):
        """model_id longer than 256 chars must be rejected; no network call made."""
        overlong_id = "x" * 257
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open") as mock_open:
            load_model_async(BASE_URL, overlong_id)
            _wait_for_thread(f"LMStudio-load-{overlong_id[:20]}", timeout=1.0)
        mock_open.assert_not_called()

    def test_unload_model_rejects_overlong_id(self):
        """model_id longer than 256 chars must be rejected for unload too; no network call."""
        overlong_id = "y" * 300
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open") as mock_open:
            unload_model_async(BASE_URL, overlong_id)
            _wait_for_thread(f"LMStudio-unload-{overlong_id[:20]}", timeout=1.0)
        mock_open.assert_not_called()

    def test_load_model_accepts_exactly_256_chars(self):
        """model_id of exactly 256 chars must be accepted (boundary value)."""
        boundary_id = "a" * 256
        resp = _fake_response(200)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as mock_open:
            _try_rest_load(BASE_URL, boundary_id)
        mock_open.assert_called_once()


# ---------------------------------------------------------------------------
# Wave 1768: SSRF guard — base_url scheme allowlist + no file:// via redirect
# ---------------------------------------------------------------------------

class TestSsrfSchemeGuard(unittest.TestCase):
    """base_url с не-http(s) схемой (file://, ftp://, data:) НЕ должен ходить в сеть.

    Fail-before: до фикса _try_rest_* передавали base_url прямо в urlopen,
    дефолтный opener содержит FileHandler → file:///etc/passwd читался бы.
    Pass-after: scheme allowlist отклоняет до сети (return False, opener не зовётся).
    """

    def test_rest_load_rejects_file_scheme(self):
        """file:// base_url → _try_rest_load returns False, urlopen НЕ вызван.

        Implementation-agnostic fail-before: патчим только stdlib-поверхность
        urllib.request.urlopen. До фикса file:// уходил прямо в urlopen
        (с активным FileHandler) → mock был бы вызван → тест падал бы честно.
        После фикса scheme-guard отклоняет до сети → urlopen не вызван.
        """
        with patch("urllib.request.urlopen") as mock_urlopen:
            result = _try_rest_load("file:///etc/passwd", MODEL_ID)
        self.assertFalse(result)
        mock_urlopen.assert_not_called()

    def test_rest_unload_rejects_file_scheme(self):
        """file:// base_url → _try_rest_unload returns False, urlopen НЕ вызван."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            result = _try_rest_unload("file:///etc/passwd", MODEL_ID)
        self.assertFalse(result)
        mock_urlopen.assert_not_called()

    def test_rest_load_rejects_other_schemes(self):
        """ftp://, gopher://, data: и пустая схема одинаково отклоняются."""
        for bad in ("ftp://localhost/x", "gopher://localhost", "data:text/plain,hi",
                    "//localhost:1234/v1", "/etc/passwd"):
            with patch("urllib.request.urlopen") as mock_urlopen:
                self.assertFalse(_try_rest_load(bad, MODEL_ID), f"scheme not blocked: {bad!r}")
                mock_urlopen.assert_not_called()

    def test_rest_load_accepts_http_scheme(self):
        """http:// по-прежнему проходит (положительный контроль)."""
        resp = _fake_response(200)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as mock_open:
            result = _try_rest_load("http://localhost:1234/v1", MODEL_ID)
        self.assertTrue(result)
        mock_open.assert_called_once()

    def test_rest_unload_accepts_https_scheme(self):
        """https:// тоже разрешён."""
        resp = _fake_response(200)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as mock_open:
            result = _try_rest_unload("https://lan-box.local:1234/v1", MODEL_ID)
        self.assertTrue(result)
        mock_open.assert_called_once()

    def test_load_model_async_file_scheme_no_network(self):
        """load_model_async() с file:// не делает сетевого вызова (end-to-end)."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            with patch("shutil.which", return_value=None):  # без CLI fallback
                load_model_async("file:///etc/passwd", MODEL_ID)
                _wait_for_thread(f"LMStudio-load-{MODEL_ID[:20]}")
        mock_urlopen.assert_not_called()

    def test_unload_model_async_file_scheme_no_network(self):
        """unload_model_async() с file:// не делает сетевого вызова (end-to-end)."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            with patch("shutil.which", return_value=None):
                unload_model_async("file:///etc/passwd", MODEL_ID)
                _wait_for_thread(f"LMStudio-unload-{MODEL_ID[:20]}")
        mock_urlopen.assert_not_called()

    def test_safe_opener_has_no_file_or_ftp_handler(self):
        """Defence-in-depth: opener не содержит File/FTP/Data handler.

        Так даже 302→file:// не сможет быть открыт самим opener'ом.
        """
        from backend.lm_studio_lifecycle import _SAFE_OPENER
        handler_names = {type(h).__name__ for h in _SAFE_OPENER.handlers}
        for forbidden in ("FileHandler", "FTPHandler", "DataHandler"):
            self.assertNotIn(forbidden, handler_names,
                             f"_SAFE_OPENER must not include {forbidden}")

    def test_redirect_to_file_scheme_is_blocked(self):
        """302 → file:// отклоняется redirect_request'ом (raises HTTPError)."""
        from backend.lm_studio_lifecycle import _SchemeCheckingRedirectHandler
        handler = _SchemeCheckingRedirectHandler()
        req = urllib.request.Request("http://localhost:1234/v1/api/v0/models/load")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(
                req, fp=None, code=302, msg="Found",
                headers={}, newurl="file:///etc/passwd",
            )

    def test_redirect_to_http_scheme_is_allowed(self):
        """302 → http:// проходит через redirect_request (положительный контроль)."""
        from backend.lm_studio_lifecycle import _SchemeCheckingRedirectHandler
        handler = _SchemeCheckingRedirectHandler()
        req = urllib.request.Request("http://localhost:1234/v1/api/v0/models/load")
        new_req = handler.redirect_request(
            req, fp=None, code=302, msg="Found",
            headers={}, newurl="http://localhost:1234/v1/other",
        )
        self.assertIsNotNone(new_req)


if __name__ == "__main__":
    unittest.main()
