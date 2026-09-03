"""Memory Conductor T3 (2026-08-19) — unit tests for lm_studio_lifecycle.model_loaded().

C-EFFECT-CHECK (docs/superpowers/specs/2026-08-19-memory-conductor-design.md §3):
model_loaded() is a THREE-state probe — True/False/None. None means "unknown"
(HTTP/parse error, timeout, disallowed scheme) and callers MUST NOT read it as
False. All HTTP calls are mocked via the same `_SAFE_OPENER.open` patch point
used by the sibling test_lm_studio_lifecycle.py — no real LM Studio required.
"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.lm_studio_lifecycle import model_loaded  # noqa: E402


BASE_URL = "http://localhost:1234/v1"
MODEL_ID = "qwen3.6-35b-a3b"


def _fake_response(status: int, body: bytes = b"") -> MagicMock:
    """Simulate a urllib HTTP response context manager (mirrors sibling test file)."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _models_body(ids: list[str], *, loaded: list[str] | None = None) -> bytes:
    """Живая форма /api/v0/models: каталог + признак загрузки у каждой записи.

    🔴 До 03.09.2026 хелпер отдавал записи БЕЗ ``state``, а тесты ждали от них
    True — то есть закрепляли «есть в каталоге» = «загружена». Каталог
    перечисляет всё скачанное, так что на реальном ответе это давало бы True
    для любой незагруженной модели; форму без ``state`` живой endpoint не
    отдаёт вовсе.
    """
    loaded_set = set(ids if loaded is None else loaded)
    return json.dumps({
        "object": "list",
        "data": [
            {"id": i, "state": "loaded" if i in loaded_set else "not-loaded"}
            for i in ids
        ],
    }).encode()


class TestModelLoadedTrue(unittest.TestCase):
    """model_id present in data[].id -> True."""

    def test_model_loaded_true(self):
        resp = _fake_response(200, _models_body(["other-model", MODEL_ID]))
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as mock_open:
            result = model_loaded(BASE_URL, MODEL_ID)
        self.assertIs(result, True)
        req = mock_open.call_args[0][0]
        # Опрашиваются только формы, несущие состояние загрузки
        # (lm_studio_lifecycle.LOADED_STATE_ENDPOINTS); первая из них — v0.
        self.assertTrue(req.full_url.endswith("/api/v0/models"))
        self.assertNotIn("/v1/api/", req.full_url)

    def test_model_loaded_true_base_url_without_v1_suffix(self):
        """base_url without a trailing /v1 must still resolve to .../api/v1/models."""
        resp = _fake_response(200, _models_body([MODEL_ID]))
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            result = model_loaded("http://localhost:1234", MODEL_ID)
        self.assertIs(result, True)


class TestModelLoadedFalse(unittest.TestCase):
    """LM Studio reachable, model_id absent from the list -> False (not None!)."""

    def test_model_loaded_false_when_present_but_not_loaded(self):
        """Модель скачана, но не загружена — False, а не True."""
        resp = _fake_response(200, _models_body([MODEL_ID], loaded=[]))
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            result = model_loaded(BASE_URL, MODEL_ID)
        self.assertIs(result, False)

    def test_model_loaded_false_when_absent(self):
        resp = _fake_response(200, _models_body(["some-other-model"]))
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            result = model_loaded(BASE_URL, MODEL_ID)
        self.assertIs(result, False)

    def test_model_loaded_false_when_list_empty(self):
        resp = _fake_response(200, _models_body([]))
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            result = model_loaded(BASE_URL, MODEL_ID)
        self.assertIs(result, False)


class TestModelLoadedNoneOnError(unittest.TestCase):
    """Three-state lesson: any HTTP/parse failure -> None, never False."""

    def test_connection_error_returns_none(self):
        with patch(
            "backend.lm_studio_lifecycle._SAFE_OPENER.open",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = model_loaded(BASE_URL, MODEL_ID)
        self.assertIsNone(result)

    def test_timeout_returns_none(self):
        with patch(
            "backend.lm_studio_lifecycle._SAFE_OPENER.open",
            side_effect=TimeoutError("timed out"),
        ):
            result = model_loaded(BASE_URL, MODEL_ID)
        self.assertIsNone(result)

    def test_http_error_returns_none(self):
        err = urllib.error.HTTPError(url="", code=500, msg="Server Error", hdrs=MagicMock(), fp=None)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", side_effect=err):
            result = model_loaded(BASE_URL, MODEL_ID)
        self.assertIsNone(result)

    def test_non_200_status_returns_none(self):
        resp = _fake_response(503, b"")
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            result = model_loaded(BASE_URL, MODEL_ID)
        self.assertIsNone(result)

    def test_malformed_json_returns_none(self):
        resp = _fake_response(200, b"not json{{{")
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            result = model_loaded(BASE_URL, MODEL_ID)
        self.assertIsNone(result)

    def test_missing_data_key_returns_none(self):
        """A 200 body that parses but has no 'data' list is a malformed-schema case, not False."""
        resp = _fake_response(200, json.dumps({"unexpected": "shape"}).encode())
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            result = model_loaded(BASE_URL, MODEL_ID)
        self.assertIsNone(result)

    def test_disallowed_scheme_returns_none(self):
        """SSRF guard: file:// (and any non-http(s) scheme) must never be dialed."""
        result = model_loaded("file:///etc/passwd", MODEL_ID)
        self.assertIsNone(result)

    def test_empty_model_id_returns_none(self):
        resp = _fake_response(200, _models_body([MODEL_ID]))
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp) as mock_open:
            result = model_loaded(BASE_URL, "")
        self.assertIsNone(result)
        mock_open.assert_not_called()


class TestModelLoadedNoneIsNotFalse(unittest.TestCase):
    """Explicit assertion that the two failure states are distinguishable by identity."""

    def test_none_is_not_false_identity(self):
        with patch(
            "backend.lm_studio_lifecycle._SAFE_OPENER.open",
            side_effect=urllib.error.URLError("refused"),
        ):
            unknown = model_loaded(BASE_URL, MODEL_ID)
        resp = _fake_response(200, _models_body(["other"]))
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            confirmed_absent = model_loaded(BASE_URL, MODEL_ID)
        self.assertIsNone(unknown)
        self.assertIs(confirmed_absent, False)
        self.assertIsNot(unknown, confirmed_absent)


if __name__ == "__main__":
    unittest.main()
