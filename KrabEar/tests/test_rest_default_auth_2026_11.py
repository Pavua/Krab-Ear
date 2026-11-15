"""REST по умолчанию требует Bearer-ключ (решение владельца 2026-11-15).

Раньше при пустых ``REST_API_KEY`` и ``REST_API_AUTH_ENABLED=False`` защищённые
маршруты отвечали всем (режим 3 «pass through»). Теперь при старте реального
сервера ``ensure_rest_api_key`` выдаёт случайный ключ в ``<data_dir>/rest_api_key``
(0600) и кладёт его в ``settings.REST_API_KEY``; дальше работает прежний
legacy-режим. Явно заданные ключ/токен-стор и опт-аут
``REST_API_ALLOW_ANONYMOUS`` уважаются. ``/health`` остаётся открытым.

Тестовые приложения (``create_app`` без старта сервера) по-прежнему открыты:
выдача ключа привязана к реальным точкам запуска (``service.py`` в-процессе и
``rest_server`` standalone), чтобы не менять поведение сотен тестов.
"""
from __future__ import annotations

import ast
import os
import stat
import threading
from pathlib import Path

import pytest

from test_rest_vg_contract_M1 import _deps_with
import backend.rest_server as rs
from backend.rest_api_key import KEY_FILENAME, ensure_rest_api_key

BACKEND = Path(__file__).resolve().parents[1] / "backend"


@pytest.fixture
def fresh_settings(monkeypatch):
    monkeypatch.setattr(rs.settings, "REST_API_KEY", "")
    monkeypatch.setattr(rs.settings, "REST_API_AUTH_ENABLED", False)
    monkeypatch.setattr(rs.settings, "REST_API_ALLOW_ANONYMOUS", False, raising=False)
    return rs.settings


def test_generates_key_file_with_private_mode(tmp_path, fresh_settings):
    key = ensure_rest_api_key(tmp_path)

    path = tmp_path / KEY_FILENAME
    assert key and len(key) >= 32
    assert path.read_text(encoding="utf-8").strip() == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert fresh_settings.REST_API_KEY == key


def test_second_call_reuses_existing_key(tmp_path, fresh_settings):
    first = ensure_rest_api_key(tmp_path)
    fresh_settings.REST_API_KEY = ""

    second = ensure_rest_api_key(tmp_path)

    assert second == first


def test_configured_key_is_respected_and_no_file_created(tmp_path, fresh_settings):
    fresh_settings.REST_API_KEY = "operator-chosen-key-0123456789abcdef"

    assert ensure_rest_api_key(tmp_path) is None
    assert fresh_settings.REST_API_KEY == "operator-chosen-key-0123456789abcdef"
    assert not (tmp_path / KEY_FILENAME).exists()


def test_token_store_mode_is_respected(tmp_path, fresh_settings):
    fresh_settings.REST_API_AUTH_ENABLED = True

    assert ensure_rest_api_key(tmp_path) is None
    assert fresh_settings.REST_API_KEY == ""
    assert not (tmp_path / KEY_FILENAME).exists()


def test_explicit_anonymous_optout_is_respected(tmp_path, fresh_settings):
    fresh_settings.REST_API_ALLOW_ANONYMOUS = True

    assert ensure_rest_api_key(tmp_path) is None
    assert fresh_settings.REST_API_KEY == ""
    assert not (tmp_path / KEY_FILENAME).exists()


def test_too_short_key_file_is_regenerated(tmp_path, fresh_settings):
    (tmp_path / KEY_FILENAME).write_text("short", encoding="utf-8")

    key = ensure_rest_api_key(tmp_path)

    assert key and len(key) >= 32
    assert (tmp_path / KEY_FILENAME).read_text(encoding="utf-8").strip() == key


def test_unreadable_key_file_fails_closed_without_touching_it(tmp_path, fresh_settings, monkeypatch):
    original = ensure_rest_api_key(tmp_path)
    fresh_settings.REST_API_KEY = ""
    real_read_text = Path.read_text

    def flaky(self_path, *a, **k):
        if self_path.name == KEY_FILENAME:
            raise PermissionError("временный отказ")
        return real_read_text(self_path, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)

    key = ensure_rest_api_key(tmp_path)

    monkeypatch.setattr(Path, "read_text", real_read_text)
    assert key and key != original, "при сбое чтения auth обязан остаться ВКЛЮЧЁН (временный ключ)"
    assert fresh_settings.REST_API_KEY == key
    assert (tmp_path / KEY_FILENAME).read_text(encoding="utf-8").strip() == original


def test_concurrent_starts_agree_on_one_key(tmp_path, monkeypatch):
    """backend (in-process) и standalone REST стартуют одновременно: один ключ."""
    import backend.rest_api_key as mod

    class _S:  # у каждого «процесса» (потока) пустой ключ: все идут в файл
        REST_API_KEY = ""
        REST_API_AUTH_ENABLED = False
        REST_API_ALLOW_ANONYMOUS = False

    monkeypatch.setattr(mod, "settings", _S())
    results: list[str | None] = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait(5.0)
        results.append(ensure_rest_api_key(tmp_path))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)

    assert len(results) == 6 and all(results)
    assert len(set(results)) == 1, "гонка запусков выдала разные ключи"


@pytest.fixture
def client_with_key(monkeypatch, tmp_path):
    monkeypatch.setattr(rs, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(rs.settings, "REST_API_AUTH_ENABLED", False)
    monkeypatch.setattr(rs.settings, "REST_API_KEY", "")
    monkeypatch.setattr(rs.settings, "REST_API_ALLOW_ANONYMOUS", False, raising=False)
    key = ensure_rest_api_key(tmp_path)
    app = rs.create_app(_deps_with())
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    return app.test_client(), key


def test_protected_route_requires_bearer_after_provisioning(client_with_key):
    client, key = client_with_key

    assert client.get("/v1/vocabulary").status_code == 401
    assert client.get("/v1/vocabulary", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/v1/vocabulary", headers={"Authorization": f"Bearer {key}"})
    assert ok.status_code != 401


def test_health_stays_open(client_with_key):
    client, _ = client_with_key

    assert client.get("/health").status_code == 200


def _calls(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)
    ]


def test_in_process_start_provisions_key():
    assert _calls(BACKEND / "service.py", "ensure_rest_api_key"), (
        "service.py обязан выдавать ключ перед стартом in-process REST"
    )


def test_standalone_main_provisions_key():
    assert _calls(BACKEND / "rest_server.py", "ensure_rest_api_key"), (
        "rest_server.py __main__ обязан выдавать ключ перед app.run"
    )


def test_key_file_name_matches_swift_and_contract():
    assert KEY_FILENAME == "rest_api_key"
    assert os.sep not in KEY_FILENAME
