"""`call_session_get` сверяет живую сессию со шлюзом (решение владельца 2026-11-18, вариант «а»).

Было: журнал звонка никто не синхронизировал с Voice Gateway. Собеседник клал трубку сам,
шлюз закрывал сессию, а журнал Ear навсегда оставался `dialing`, и UI продолжал опрашивать.

Контракт: для сессии с `gateway_session_id` в живом статусе `call_session_get` не чаще раза
в `_GATEWAY_RECONCILE_MIN_INTERVAL_SEC` спрашивает `GET /v1/sessions/{id}`; терминальный статус шлюза
(`stopped` → completed, `failed` → failed) закрывает журнал. Любой сбой сверки — журнал как есть.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.call_session_service as _svc  # noqa: E402
from backend.call_session_service import CallSessionService  # noqa: E402
from backend.call_session_store import CallSessionStore  # noqa: E402
from backend.gateway_call_provider import GatewayCallProvider  # noqa: E402

_GW = "vs_reconcile_session"


class _Client:
    def __init__(self, status_answer) -> None:
        self.status_answer = status_answer
        self.status_calls = 0
        self.other_calls: list[str] = []

    def post(self, base, key, path, payload=None, timeout=None):
        self.other_calls.append(path)
        return {"ok": True, "payload": {"ok": True, "session_id": _GW, "call_sid": "CA1", "status": "dialing"}}

    def get(self, base, key, path, timeout=None):
        assert path == f"/v1/sessions/{_GW}", path
        self.status_calls += 1
        answer = self.status_answer
        return answer() if callable(answer) else answer


def _gw(status: str):
    return {"ok": True, "payload": {"session_id": _GW, "status": status}}


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(_svc, "_GATEWAY_RECONCILE_MIN_INTERVAL_SEC", 0.0)


def _service(tmp_path, status_answer, *, privacy=None, with_provider=True):
    client = _Client(status_answer)
    provider = GatewayCallProvider("http://127.0.0.1:8090", "k", http=client)
    service = CallSessionService(
        CallSessionStore(tmp_path),
        settings_get=lambda key, default: bool(privacy and privacy[0]) if key == "privacy_mode_enabled" else default,
        get_provider_fn=(lambda _s: provider) if with_provider else None,
    )
    return service, client


def _start(service):
    result = service.handle_call_start({"phone": "+10000000000", "goal_text": "тест", "prompt": "тест"})
    assert result["ok"] is True, result
    return result["session_id"]


@pytest.mark.parametrize("gateway_status,expected", [("stopped", "completed"), ("failed", "failed")])
def test_terminal_gateway_status_closes_the_journal(tmp_path, gateway_status, expected):
    service, _client = _service(tmp_path, _gw(gateway_status))
    sid = _start(service)
    got = service.handle_call_session_get({"id": sid})
    assert got["status"] == expected
    assert got["end_reason"] == f"gateway_{gateway_status}"
    assert got["ended_at"]
    # Закрытие сохранено, а не только показано в ответе.
    assert service._store.get(sid).status == expected


@pytest.mark.parametrize("gateway_status", ["running", "dialing", "ringing", "talking"])
def test_live_gateway_status_leaves_the_journal_alone(tmp_path, gateway_status):
    service, _client = _service(tmp_path, _gw(gateway_status))
    sid = _start(service)
    assert service.handle_call_session_get({"id": sid})["status"] == "dialing"


@pytest.mark.parametrize("answer", [
    {"ok": False, "error": "timed out"},
    {"ok": False, "error": 'http_404:{"detail":"session_not_found"}'},
    {"ok": False, "error": 'http_502:{"detail":"boom"}'},
    {"ok": True, "payload": {"session_id": _GW}},
])
def test_unreadable_gateway_status_never_closes_or_raises(tmp_path, answer):
    service, _client = _service(tmp_path, answer)
    sid = _start(service)
    assert service.handle_call_session_get({"id": sid})["status"] == "dialing"


def test_provider_exception_is_swallowed(tmp_path):
    def boom():
        raise RuntimeError("network down")

    service, _client = _service(tmp_path, boom)
    sid = _start(service)
    assert service.handle_call_session_get({"id": sid})["status"] == "dialing"


def test_checks_are_throttled_per_session(tmp_path, monkeypatch):
    monkeypatch.setattr(_svc, "_GATEWAY_RECONCILE_MIN_INTERVAL_SEC", 5.0)
    service, client = _service(tmp_path, _gw("running"))
    sid = _start(service)
    for _ in range(5):
        service.handle_call_session_get({"id": sid})
    assert client.status_calls == 1


def test_throttle_is_per_session_not_global(tmp_path, monkeypatch):
    monkeypatch.setattr(_svc, "_GATEWAY_RECONCILE_MIN_INTERVAL_SEC", 5.0)
    service, client = _service(tmp_path, _gw("running"))
    first, second = _start(service), _start(service)
    service.handle_call_session_get({"id": first})
    service.handle_call_session_get({"id": second})
    assert client.status_calls == 2


def test_finished_session_is_not_checked_again(tmp_path):
    service, client = _service(tmp_path, _gw("stopped"))
    sid = _start(service)
    service.handle_call_session_get({"id": sid})
    calls_after_close = client.status_calls
    service.handle_call_session_get({"id": sid})
    assert client.status_calls == calls_after_close


def test_journal_only_session_without_gateway_id_is_not_checked(tmp_path):
    service, client = _service(tmp_path, _gw("stopped"))
    created = service.handle_call_session_create({"phone": "+10000000000", "goal_text": "журнал"})
    sid = created.get("session_id") or created.get("id")
    assert service.handle_call_session_get({"id": sid})["status"] == "idle"
    assert client.status_calls == 0


def test_no_provider_means_no_reconcile(tmp_path):
    service, _client = _service(tmp_path, _gw("stopped"), with_provider=False)
    # call_start без провайдера отказал бы, поэтому заводим сессию с линией напрямую.
    session = service._store.create(phone_number="+10000000000", goal_text="x", gateway_session_id=_GW)
    service._store.update_status(session_id=session.id, new_status="dialing")
    assert service.handle_call_session_get({"id": session.id})["status"] == "dialing"


def test_privacy_mode_still_reconciles_but_scrubs_the_answer(tmp_path):
    privacy = [False]
    service, _client = _service(tmp_path, _gw("stopped"), privacy=privacy)
    sid = _start(service)  # call_start в приватном режиме запрещён: включаем после набора
    privacy[0] = True
    got = service.handle_call_session_get({"id": sid})
    assert got["status"] == "completed"
    assert got["phone_number"] != "+10000000000"
    assert got["transcript_history"] == []
