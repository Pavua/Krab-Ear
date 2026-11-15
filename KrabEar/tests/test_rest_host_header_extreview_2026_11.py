"""Внешнее ревью E1 (2026-11-15): REST не проверял заголовок Host.

DNS rebinding: страница ``http://evil.example:5005`` после смены A-записи на
127.0.0.1 становится «своей» для браузера (тот же origin, поэтому ``Origin``-гейт
ничего не видит), и её скрипт читает ``/v1/events`` и ``/v1/vocabulary``.
Сервер слушает только loopback, но защищает от этого лишь проверка Host:
законный клиент шлёт ``127.0.0.1``/``localhost``/``[::1]`` (с портом или без),
ребайндинг-страница шлёт своё имя.
"""
from __future__ import annotations

import pytest

from test_rest_vg_contract_M1 import _deps_with
import backend.rest_server as rs


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(rs, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(rs.settings, "REST_API_AUTH_ENABLED", False)
    monkeypatch.setattr(rs.settings, "REST_API_KEY", "")
    app = rs.create_app(_deps_with())
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    return app.test_client()


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:5005",
        "rebind.attacker.test:5005",
        "127.0.0.1.evil.example",
        "localhost.evil.example:5005",
        "192.168.1.10:5005",
    ],
)
def test_foreign_host_is_rejected(client, host):
    resp = client.get("/health", headers={"Host": host})

    assert resp.status_code == 403, (host, resp.status_code)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.1:5005", "localhost", "localhost:5005", "[::1]:5005", "LOCALHOST:5005"],
)
def test_loopback_host_is_accepted(client, host):
    resp = client.get("/health", headers={"Host": host})

    assert resp.status_code != 403, (host, resp.status_code)


def test_foreign_host_is_rejected_on_transcript_route(client):
    resp = client.get("/v1/vocabulary", headers={"Host": "evil.example:5005"})

    assert resp.status_code == 403


def test_extra_allowed_host_can_be_configured(monkeypatch, client):
    monkeypatch.setattr(rs.settings, "REST_ALLOWED_HOSTS", "krab-ear.local", raising=False)

    ok = client.get("/health", headers={"Host": "krab-ear.local:5005"})
    bad = client.get("/health", headers={"Host": "evil.example"})

    assert ok.status_code != 403
    assert bad.status_code == 403
