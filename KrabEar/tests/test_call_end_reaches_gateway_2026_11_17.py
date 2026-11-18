"""Завершение звонка из UI обязано дойти до Voice Gateway (ревью 2026-11-16/17, F3/C1–C4).

Было: `call_start` заводил журнальную сессию в `idle`, её никто не продвигал, `mark_completed`
разрешал только `ending → completed`, а `call_session_end` не вызывал `provider.hangup` и журнал
не хранил `gateway_session_id`. Завершить звонок из UI было нельзя, настоящий звонок шёл дальше.

Контракт шлюза (ответ сессии Voice Gateway 17.11, по коду, без живого замера):
 * hangup: 200 `{"ok": true}`; неизвестная сессия — 404, гонка с завершением — 409, сбой
   провайдера — 502. Ошибки приходят HTTP-кодом;
 * набор синхронный и дольше общих 4 с; `client_request_id` делает повтор идемпотентным,
   `GET /v1/sessions/by-request-id/{id}` находит сессию оборвавшегося запроса.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_session_service import CallSessionService  # noqa: E402
from backend.call_session_store import CallSessionStore  # noqa: E402
from backend.gateway_call_provider import DIAL_TIMEOUT_SEC, GatewayCallProvider  # noqa: E402

_GW = "vs_test_gateway_session"


class _ClientDouble:
    """Двойник VoiceGatewayClient: отвечает КОНВЕРТАМИ ровно его формы.

    Успех — `{"ok": True, "payload": <тело>}`; HTTP-ошибка — `{"ok": False,
    "error": "http_<code>:<тело>"}`; транспортный сбой — `{"ok": False, "error": "<текст>"}`.
    """

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict | None, float | None]] = []

    def _answer(self, method, path):
        for (m, prefix), value in self.routes.items():
            if m == method and path.startswith(prefix):
                return value(path) if callable(value) else value
        return {"ok": False, "error": "http_404:{}"}

    def post(self, base, key, path, payload=None, timeout=None):
        self.calls.append(("POST", path, payload, timeout))
        return self._answer("POST", path)

    def get(self, base, key, path, timeout=None):
        self.calls.append(("GET", path, None, timeout))
        return self._answer("GET", path)


def _ok(body):
    return {"ok": True, "payload": body}


_DIAL_OK = _ok({"ok": True, "session_id": _GW, "call_sid": "CA1", "status": "dialing"})


def _service(tmp_path, routes):
    client = _ClientDouble(routes)
    provider = GatewayCallProvider("http://127.0.0.1:8090", "k", http=client)
    service = CallSessionService(
        CallSessionStore(tmp_path), settings_get=lambda key, default: default,
        get_provider_fn=lambda _settings: provider,
    )
    return service, client


def _start(service):
    result = service.handle_call_start({"phone": "+10000000000", "goal_text": "тест", "prompt": "тест"})
    assert result["ok"] is True, result
    return result["session_id"]


def _hangups(client):
    return [c[1] for c in client.calls if c[0] == "POST" and c[1].endswith("/hangup")]


# --- завершение -----------------------------------------------------------------------------

def test_call_session_end_hangs_up_and_closes_the_journal(tmp_path):
    service, client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("POST", f"/v1/telephony/calls/{_GW}/hangup"): _ok({"ok": True, "status": "completed"}),
    })
    sid = _start(service)
    assert service.handle_call_session_get({"id": sid})["status"] == "dialing"

    result = service.handle_call_session_end({"id": sid, "reason": "completed"})

    assert _hangups(client) == [f"/v1/telephony/calls/{_GW}/hangup"]
    assert result.get("ok") is not False, result
    assert result["status"] == "completed"
    assert service.handle_call_session_get({"id": sid})["status"] == "completed"


@pytest.mark.parametrize("code", ["http_404:{\"detail\":\"session_not_found\"}",
                                  "http_409:{\"detail\":\"session_terminal\"}"])
def test_call_already_gone_at_gateway_still_closes_the_journal(tmp_path, code):
    service, _client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("POST", f"/v1/telephony/calls/{_GW}/hangup"): {"ok": False, "error": code},
    })
    sid = _start(service)
    result = service.handle_call_session_end({"id": sid, "reason": "completed"})
    assert result.get("ok") is not False, result
    assert service.handle_call_session_get({"id": sid})["status"] == "completed"


@pytest.mark.parametrize("failure", ['http_404:{"detail":"Not Found"}', 'http_404:{}', 'http_409:{"detail":"other"}'])
def test_bare_404_or_unknown_409_is_a_refusal_not_a_gone_line(tmp_path, failure):
    """Голый 404 = неверный адрес/старый шлюз: звонок мог остаться живым, журнал не закрываем."""
    service, _client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("POST", f"/v1/telephony/calls/{_GW}/hangup"): {"ok": False, "error": failure},
    })
    sid = _start(service)
    result = service.handle_call_session_end({"id": sid, "reason": "completed"})
    assert result["ok"] is False
    assert result["reason"] == "hangup_failed"
    assert service.handle_call_session_get({"id": sid})["status"] == "dialing"


@pytest.mark.parametrize("failure", ["http_502:{\"detail\":\"provider down, secret body\"}", "timed out"])
def test_failed_hangup_keeps_the_session_live_and_hides_gateway_body(tmp_path, failure):
    service, _client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("POST", f"/v1/telephony/calls/{_GW}/hangup"): {"ok": False, "error": failure},
    })
    sid = _start(service)
    result = service.handle_call_session_end({"id": sid, "reason": "completed"})
    assert result["ok"] is False
    assert result["reason"] == "hangup_failed"
    assert "secret body" not in str(result)
    # Звонок мог остаться живым: журнал НЕ закрыт, повторное завершение возможно.
    assert service.handle_call_session_get({"id": sid})["status"] == "dialing"


def test_second_end_does_not_hang_up_again(tmp_path):
    service, client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("POST", f"/v1/telephony/calls/{_GW}/hangup"): _ok({"ok": True}),
    })
    sid = _start(service)
    service.handle_call_session_end({"id": sid, "reason": "completed"})
    again = service.handle_call_session_end({"id": sid, "reason": "completed"})
    assert len(_hangups(client)) == 1
    assert again == {"ok": False, "reason": "invalid_state_transition", "current_state": "completed"}


def test_journal_only_session_keeps_the_old_contract(tmp_path):
    """Сессия без линии (call_session_create) в шлюз не ходит и из idle не завершается."""
    service, client = _service(tmp_path, {})
    created = service.handle_call_session_create({"phone": "+10000000000", "goal_text": "тест"})
    result = service.handle_call_session_end({"id": created["session_id"], "reason": "completed"})
    assert result == {"ok": False, "reason": "invalid_state_transition", "current_state": "idle"}
    assert client.calls == []


def test_gateway_session_id_survives_reload(tmp_path):
    service, _client = _service(tmp_path, {("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK})
    sid = _start(service)
    assert CallSessionStore(tmp_path).get(sid).gateway_session_id == _GW


# --- набор ----------------------------------------------------------------------------------

def test_dial_uses_long_timeout_and_sends_idempotency_key(tmp_path):
    service, client = _service(tmp_path, {("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK})
    _start(service)
    _method, _path, payload, timeout = client.calls[0]
    assert timeout == DIAL_TIMEOUT_SEC and DIAL_TIMEOUT_SEC >= 15
    assert len(payload["client_request_id"]) >= 16


def test_dial_timeout_adopts_the_session_created_by_the_lost_request(tmp_path):
    service, client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): {"ok": False, "error": "timed out"},
        ("GET", "/v1/sessions/by-request-id/"): _ok({"session_id": _GW, "status": "dialing"}),
    })
    sid = _start(service)
    key = client.calls[0][2]["client_request_id"]
    assert client.calls[1][1] == f"/v1/sessions/by-request-id/{key}"
    assert CallSessionStore(tmp_path).get(sid).gateway_session_id == _GW


def test_dial_timeout_without_a_session_is_reported_as_unknown_not_as_no_call(tmp_path):
    service, _client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): {"ok": False, "error": "timed out"},
    })
    result = service.handle_call_start({"phone": "+10000000000", "goal_text": "т", "prompt": "т"})
    assert result["ok"] is False and result["error"] == "gateway_dial_unknown"


def test_gateway_refusal_is_not_mistaken_for_a_lost_request(tmp_path):
    service, client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): {"ok": False, "error": "http_403:{\"detail\":\"forbidden\"}"},
    })
    result = service.handle_call_start({"phone": "+10000000000", "goal_text": "т", "prompt": "т"})
    assert result["ok"] is False and result["error"] != "gateway_dial_unknown"
    assert [c[0] for c in client.calls] == ["POST"]


def test_journal_failure_after_dial_hangs_the_call_up(tmp_path, monkeypatch):
    service, client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("POST", f"/v1/telephony/calls/{_GW}/hangup"): _ok({"ok": True}),
    })
    monkeypatch.setattr(service._store, "create", lambda **kw: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        service.handle_call_start({"phone": "+10000000000", "goal_text": "т", "prompt": "т"})
    assert _hangups(client) == [f"/v1/telephony/calls/{_GW}/hangup"]


@pytest.mark.parametrize("bad", ["../admin", "a/b", "x?y=1", "", "a" * 200])
def test_hangup_rejects_ids_that_would_change_the_request_path(bad):
    client = _ClientDouble({})
    provider = GatewayCallProvider("http://127.0.0.1:8090", "k", http=client)
    assert provider.hangup(bad)["ok"] is False
    assert provider.get_call_status(bad)["ok"] is False
    assert client.calls == []


# --- сверка журнала со шлюзом при опросе -------------------------------------------------------
#
# Журнал никто не продвигал: если собеседник клал трубку сам, сессия навсегда оставалась
# `dialing`, а UI продолжал показывать набор и опрашивать. Шлюз заканчивает сессию статусами
# `stopped` / `failed` (app/main.py, `status_values`); одна сессия приходит голым SessionState.

def _state(status):
    return _ok({"id": _GW, "status": status})


@pytest.mark.parametrize("gateway_status,journal_status", [("stopped", "completed"), ("failed", "failed")])
def test_poll_closes_the_journal_when_the_gateway_session_is_over(tmp_path, gateway_status, journal_status):
    service, client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("GET", f"/v1/sessions/{_GW}"): _state(gateway_status),
    })
    sid = _start(service)

    polled = service.handle_call_session_get({"id": sid})

    assert polled["status"] == journal_status
    assert polled["end_reason"] == f"gateway_{gateway_status}"
    assert _hangups(client) == [], "сверка только читает шлюз, линию не трогает"


def test_poll_keeps_a_live_call_live(tmp_path):
    service, _client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("GET", f"/v1/sessions/{_GW}"): _state("running"),
    })
    sid = _start(service)
    assert service.handle_call_session_get({"id": sid})["status"] == "dialing"


@pytest.mark.parametrize("answer", [
    {"ok": False, "error": "http_404:{\"detail\":\"session_not_found\"}"},
    {"ok": False, "error": "http_502:bad gateway"},
    {"ok": False, "error": "timed out"},
    _ok({"id": _GW}),
])
def test_poll_never_closes_the_journal_on_an_unclear_gateway_answer(tmp_path, answer):
    """Закрыть журнал при живой линии хуже, чем опоздать: «не знаю» не значит «звонка нет»."""
    service, _client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("GET", f"/v1/sessions/{_GW}"): answer,
    })
    sid = _start(service)
    assert service.handle_call_session_get({"id": sid})["status"] == "dialing"


def test_poll_survives_a_gateway_exception(tmp_path):
    def boom(_path):
        raise RuntimeError("шлюз упал")

    service, _client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("GET", f"/v1/sessions/{_GW}"): boom,
    })
    sid = _start(service)
    assert service.handle_call_session_get({"id": sid})["status"] == "dialing"


def test_poll_asks_the_gateway_at_most_once_per_interval(tmp_path):
    service, client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("GET", f"/v1/sessions/{_GW}"): _state("running"),
    })
    sid = _start(service)
    for _ in range(5):
        service.handle_call_session_get({"id": sid})
    assert len([c for c in client.calls if c[0] == "GET"]) == 1


def test_poll_of_a_finished_or_lineless_session_never_touches_the_gateway(tmp_path):
    service, client = _service(tmp_path, {
        ("POST", "/v1/telephony/calls/prompt-call"): _DIAL_OK,
        ("POST", f"/v1/telephony/calls/{_GW}/hangup"): _ok({"ok": True}),
        ("GET", f"/v1/sessions/{_GW}"): _state("stopped"),
    })
    sid = _start(service)
    service.handle_call_session_end({"id": sid, "reason": "completed"})
    journal_only = service.handle_call_session_create(
        {"phone": "+10000000001", "goal_text": "тест"})["session_id"]

    assert service.handle_call_session_get({"id": sid})["end_reason"] == "completed"
    assert service.handle_call_session_get({"id": journal_only})["status"] == "idle"
    assert [c for c in client.calls if c[0] == "GET"] == []
