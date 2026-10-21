"""LM Studio REST load/unload обязаны слать Authorization при заданном ключе.

LM Studio :1234 с включённой аутентификацией отвечает 401 на запрос без Bearer.
Раньше `_try_rest_load/_try_rest_unload` заголовок не слали вовсе (в отличие от
`model_loaded`/`probe_loaded_chat_models`), и выгрузка/загрузка молча не работала.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.lm_studio_lifecycle import (  # noqa: E402
    _try_rest_load,
    _try_rest_unload,
    load_model_async,
    unload_model_async,
)

BASE_URL = "http://localhost:1234/v1"
MODEL_ID = "qwen3-27b"
KEY = "test-key-not-real"


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = b"{}"
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class RestAuthHeaderTests(unittest.TestCase):
    def _request_for(self, fn, **kwargs):
        with patch(
            "backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=_ok_response()
        ) as mock_open:
            fn(BASE_URL, MODEL_ID, **kwargs)
        return mock_open.call_args[0][0]

    def test_load_sends_bearer_when_key_given(self):
        req = self._request_for(_try_rest_load, api_key=KEY)
        self.assertEqual(req.get_header("Authorization"), f"Bearer {KEY}")

    def test_unload_sends_bearer_when_key_given(self):
        req = self._request_for(_try_rest_unload, api_key=KEY)
        self.assertEqual(req.get_header("Authorization"), f"Bearer {KEY}")

    def test_no_header_without_key(self):
        for fn in (_try_rest_load, _try_rest_unload):
            req = self._request_for(fn)
            self.assertIsNone(req.get_header("Authorization"))
            req = self._request_for(fn, api_key="   ")
            self.assertIsNone(req.get_header("Authorization"))

    def test_load_keeps_json_content_type_with_auth(self):
        req = self._request_for(_try_rest_load, api_key=KEY)
        self.assertEqual(req.get_header("Content-type"), "application/json")


class AsyncWrappersPropagateKeyTests(unittest.TestCase):
    def _run_worker(self, fn, rest_name):
        done = threading.Event()

        def fake_rest(base_url, model_id, api_key=None):
            seen["api_key"] = api_key
            done.set()
            return True

        seen: dict = {}
        with patch(f"backend.lm_studio_lifecycle.{rest_name}", side_effect=fake_rest):
            fn(BASE_URL, MODEL_ID, api_key=KEY)
            self.assertTrue(done.wait(2))
        return seen["api_key"]

    def test_load_async_passes_key(self):
        self.assertEqual(self._run_worker(load_model_async, "_try_rest_load"), KEY)

    def test_unload_async_passes_key(self):
        self.assertEqual(self._run_worker(unload_model_async, "_try_rest_unload"), KEY)


if __name__ == "__main__":
    unittest.main()
