"""KE05B-01 / KE05B-02: вызовы LM Studio не следуют за редиректами.

Дефект: ``llm_rewriter`` слал ``requests.post`` без ``allow_redirects=False``
(``requests`` повторяет POST с телом на 307/308), а ``lm_studio_lifecycle``
переносил ``Authorization: Bearer`` на другой хост через urllib. Остальные
исходящие пути проекта (``telegram_bridge``, ``webhook_manager``,
``llm_ops_service``) редиректы уже запрещают — это сиблинг-асимметрия.

Тесты поднимают два настоящих локальных HTTP-сервера: A отвечает редиректом
на B; B записывает всё, что до него дошло. Проверяется РЕАЛЬНЫЙ провод, а не
мок сессии.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import lm_studio_lifecycle as lifecycle  # noqa: E402
from backend.llm_rewriter import LLMRewriter  # noqa: E402

# conftest глушит ping/warmup_probe/summarize — здесь нужен настоящий сетевой путь.
pytestmark = pytest.mark.llm_network_live

_SECRET_TEXT = "секретная расшифровка разговора"
_SECRET_KEY = "sk-redirect-secret-key"


class _Recorder(BaseHTTPRequestHandler):
    """Сервер B: записывает любой дошедший до него запрос."""

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.hits.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "body": body.decode("utf-8", errors="replace"),
                "authorization": self.headers.get("Authorization"),
            }
        )
        payload = json.dumps({"choices": [{"message": {"content": "stolen-ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = _record  # noqa: N815

    def log_message(self, *args, **kwargs) -> None:  # noqa: D401
        return


def _redirector(code: int, target_port_getter):
    class _Redirect(BaseHTTPRequestHandler):
        """Сервер A: на любой запрос отвечает редиректом на B."""

        def _go(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.server.hits.append({"method": self.command, "path": self.path})  # type: ignore[attr-defined]
            self.send_response(code)
            self.send_header("Location", f"http://127.0.0.1:{target_port_getter()}/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = do_PUT = _go  # noqa: N815

        def log_message(self, *args, **kwargs) -> None:  # noqa: D401
            return

    return _Redirect


class _TwoServers:
    def __init__(self, code: int) -> None:
        self.sink = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        self.sink.hits = []  # type: ignore[attr-defined]
        self.front = ThreadingHTTPServer(("127.0.0.1", 0), _redirector(code, lambda: self.sink.server_address[1]))
        self.front.hits = []  # type: ignore[attr-defined]
        self._threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.sink, self.front)]

    def __enter__(self) -> "_TwoServers":
        for t in self._threads:
            t.start()
        return self

    def __exit__(self, *exc) -> None:
        for s in (self.sink, self.front):
            s.shutdown()
            s.server_close()
        for t in self._threads:
            t.join(timeout=5)

    @property
    def front_url(self) -> str:
        return f"http://127.0.0.1:{self.front.server_address[1]}"

    @property
    def sink_hits(self) -> list:
        return self.sink.hits  # type: ignore[attr-defined]

    @property
    def front_hits(self) -> list:
        return self.front.hits  # type: ignore[attr-defined]


def _rewriter(base: str) -> LLMRewriter:
    return LLMRewriter(base_url=f"{base}/v1", api_key=_SECRET_KEY, model="m", timeout_sec=5.0)


class RewriterDoesNotFollowRedirects(unittest.TestCase):
    """requests-путь: транскрипт не уходит на адрес из Location."""

    def _assert_sink_empty(self, srv: _TwoServers) -> None:
        self.assertGreaterEqual(len(srv.front_hits), 1, "запрос до сервера A не дошёл — тест слеп")
        self.assertEqual(srv.sink_hits, [], f"редирект был выполнен, до B дошло: {srv.sink_hits}")

    def test_rewrite_post_307_not_followed(self) -> None:
        with _TwoServers(307) as srv:
            _rewriter(srv.front_url).rewrite(_SECRET_TEXT)
            self._assert_sink_empty(srv)

    def test_rewrite_post_308_not_followed(self) -> None:
        with _TwoServers(308) as srv:
            _rewriter(srv.front_url).rewrite(_SECRET_TEXT)
            self._assert_sink_empty(srv)

    def test_fix_punctuation_only_not_followed(self) -> None:
        with _TwoServers(307) as srv:
            _rewriter(srv.front_url).fix_punctuation_only(_SECRET_TEXT)
            self._assert_sink_empty(srv)

    def test_summarize_not_followed(self) -> None:
        with patch("backend.lm_studio_lifecycle.probe_loaded_chat_models", return_value=None):
            with _TwoServers(307) as srv:
                _rewriter(srv.front_url).summarize(_SECRET_TEXT)
                self._assert_sink_empty(srv)

    def test_warmup_probe_not_followed(self) -> None:
        with _TwoServers(307) as srv:
            _rewriter(srv.front_url).warmup_probe(timeout_sec=5.0)
            self._assert_sink_empty(srv)

    def test_ping_does_not_report_redirect_target_as_reachable(self) -> None:
        # GET 302 → B отвечает 200: ping() не должен принять чужой 200 за доступность LM Studio.
        with _TwoServers(302) as srv:
            reachable = _rewriter(srv.front_url).ping()
            self.assertFalse(reachable)
            self._assert_sink_empty(srv)

    def test_session_never_follows_redirects_by_construction(self) -> None:
        # Новый вызов session.post/get не должен тихо вернуть редиректы (структурная защита).
        with _TwoServers(307) as srv:
            rw = _rewriter(srv.front_url)
            rw._session.post(f"{srv.front_url}/x", json={"t": _SECRET_TEXT}, timeout=5)
            rw._session.get(f"{srv.front_url}/y", timeout=5)
            self._assert_sink_empty(srv)


class LifecycleDoesNotLeakBearerAcrossHosts(unittest.TestCase):
    """urllib-путь: Authorization не переносится на другой хост."""

    def _assert_no_leak(self, srv: _TwoServers) -> None:
        self.assertGreaterEqual(len(srv.front_hits), 1, "запрос до сервера A не дошёл — тест слеп")
        leaked = [h for h in srv.sink_hits if h.get("authorization")]
        self.assertEqual(leaked, [], f"Bearer ушёл на другой хост: {leaked}")
        self.assertEqual(srv.sink_hits, [], f"редирект выполнен: {srv.sink_hits}")

    def test_rest_unload_redirect_not_followed(self) -> None:
        with _TwoServers(302) as srv:
            lifecycle._try_rest_unload(srv.front_url, "m", api_key=_SECRET_KEY)
            self._assert_no_leak(srv)

    def test_rest_load_redirect_not_followed(self) -> None:
        with _TwoServers(302) as srv:
            lifecycle._try_rest_load(srv.front_url, "m", api_key=_SECRET_KEY)
            self._assert_no_leak(srv)

    def test_catalog_probe_redirect_not_followed(self) -> None:
        with _TwoServers(302) as srv:
            lifecycle.probe_loaded_chat_models(srv.front_url, api_key=_SECRET_KEY)
            self._assert_no_leak(srv)

    def test_model_loaded_redirect_not_followed(self) -> None:
        with _TwoServers(302) as srv:
            lifecycle.model_loaded(srv.front_url, "m", api_key=_SECRET_KEY)
            self._assert_no_leak(srv)


if __name__ == "__main__":
    unittest.main()
