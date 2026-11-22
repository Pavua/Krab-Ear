"""KE05B-22: ответы LM Studio читаются с ограничением размера.

``resp.read()`` без аргумента в REST load/unload и в каталожных пробах читал тело целиком —
неисправный или подменённый сервер мог отдать гигабайты. Лимит тот же, что уже стоял в
``models_share_loaded_instance`` (1 МиБ).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import lm_studio_lifecycle as lc  # noqa: E402

_LIMIT = 1024 * 1024


class _Resp:
    status = 200

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.read_sizes: list[int] = []

    def read(self, n: int = -1) -> bytes:
        self.read_sizes.append(n)
        return self._data if n is None or n < 0 else self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patched(resp: _Resp):
    return patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp)


class ReadsAreBounded(unittest.TestCase):
    def _assert_bounded(self, resp: _Resp):
        self.assertTrue(resp.read_sizes, "ответ не читался")
        for n in resp.read_sizes:
            self.assertTrue(n is not None and 0 < n <= _LIMIT + 1, f"read({n}) без ограничения")

    def test_rest_unload_read_is_bounded(self):
        resp = _Resp(b"{}")
        with _patched(resp):
            self.assertTrue(lc._try_rest_unload("http://x:1234/v1", "m"))
        self._assert_bounded(resp)

    def test_rest_load_read_is_bounded(self):
        resp = _Resp(b"{}")
        with _patched(resp):
            self.assertTrue(lc._try_rest_load("http://x:1234/v1", "m"))
        self._assert_bounded(resp)

    def test_catalog_probes_reads_are_bounded(self):
        for call in (
            lambda: lc.model_loaded("http://x:1234/v1", "m"),
            lambda: lc.probe_loaded_chat_models("http://x:1234/v1"),
        ):
            resp = _Resp(b'{"data": []}')
            with _patched(resp):
                call()
            self._assert_bounded(resp)

    def test_oversized_unload_body_is_a_failure(self):
        with _patched(_Resp(b" " * (_LIMIT + 10))):
            self.assertFalse(lc._try_rest_unload("http://x:1234/v1", "m"))
        with _patched(_Resp(b" " * (_LIMIT + 10))):
            self.assertFalse(lc._try_rest_load("http://x:1234/v1", "m"))

    def test_oversized_catalog_is_unknown_not_a_crash(self):
        big = b'{"data": [' + b'{"id": "m", "state": "loaded"},' * 50000 + b"{}]}"
        self.assertGreater(len(big), _LIMIT)
        with _patched(_Resp(big)):
            self.assertIsNone(lc.model_loaded("http://x:1234/v1", "m"))
        with _patched(_Resp(big)):
            self.assertIsNone(lc.probe_loaded_chat_models("http://x:1234/v1"))


if __name__ == "__main__":
    unittest.main()
