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


def test_concurrent_starts_agree_on_one_key(tmp_path):
    """backend (in-process) и standalone REST стартуют одновременно: один ключ.

    Настоящие процессы (spawn): у каждого СВОИ settings (раньше шесть потоков делили один объект:
    после установки ключа первым остальные видели его заполненным и честно возвращали None —
    ложный FAIL и неверная модель независимой памяти процессов). Barrier — перед provisioning."""
    import multiprocessing

    from _rest_key_child import provision

    ctx = multiprocessing.get_context("spawn")
    n = 6
    barrier = ctx.Barrier(n)
    queue = ctx.Queue()
    procs = [ctx.Process(target=provision, args=(str(tmp_path), barrier, queue)) for _ in range(n)]
    started = []
    try:
        for proc in procs:
            proc.start()
            started.append(proc)
        results = [queue.get(timeout=120) for _ in range(n)]
        for proc in started:
            proc.join(timeout=60)
            assert not proc.is_alive(), "дочерний процесс завис"
            assert proc.exitcode == 0, f"дочерний процесс упал: {proc.exitcode}"
    finally:
        for proc in started:
            if proc.is_alive():
                proc.terminate()
            proc.join(timeout=10)
            proc.close()

    assert all(kind == "ok" for kind, _ in results), results
    keys = [value for _, value in results]
    assert all(keys), keys
    assert len(set(keys)) == 1, "гонка запусков выдала разные ключи"
    assert (tmp_path / KEY_FILENAME).read_text(encoding="utf-8").strip() == keys[0]


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


def _is_call_to(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and (
        getattr(node.func, "id", None) == name or getattr(node.func, "attr", None) == name
    )


def _stmt_has_call(stmt: ast.stmt, names: tuple[str, ...]) -> bool:
    return any(_is_call_to(n, name) for n in ast.walk(stmt) for name in names)


def _provisioned_before_start(body: list[ast.stmt], start_names: tuple[str, ...]) -> bool:
    """В ОДНОМ списке операторов ensure_rest_api_key (отдельным оператором, достижимо) стоит строго
    раньше оператора, запускающего сервер. Вызов в неиспользуемой функции или после запуска не
    засчитывается (раньше принимался любой вызов где угодно в файле)."""
    ensure_idx = next((i for i, st in enumerate(body)
                       if isinstance(st, ast.Expr) and _is_call_to(st.value, "ensure_rest_api_key")), None)
    start_idx = next((i for i, st in enumerate(body) if _stmt_has_call(st, start_names)), None)
    return ensure_idx is not None and start_idx is not None and ensure_idx < start_idx


def _bodies(tree: ast.AST):
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield node, block


def _has_provision_before_start(source: str, container, start_names) -> bool:
    tree = ast.parse(source)
    for node, block in _bodies(tree):
        if container(node) and _provisioned_before_start(block, start_names):
            return True
    return False


def _is_main_guard(node) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and getattr(node.test.left, "id", None) == "__name__")


def _is_try_with_inprocess(node) -> bool:
    return isinstance(node, ast.Try) and any(
        isinstance(n, ast.Name) and n.id == "InProcessRestServer" for st in node.body for n in ast.walk(st))


class TestStartupOrderCheckers:
    """Сами проверки обязаны отвергать вызов в неиспользуемой функции и вызов после запуска."""

    GOOD = "if __name__ == '__main__':\n    ensure_rest_api_key(d)\n    app.run()\n"

    def test_accepts_provision_before_run(self):
        assert _has_provision_before_start(self.GOOD, _is_main_guard, ("run",))

    def test_rejects_provision_after_run(self):
        src = "if __name__ == '__main__':\n    app.run()\n    ensure_rest_api_key(d)\n"
        assert not _has_provision_before_start(src, _is_main_guard, ("run",))

    def test_rejects_call_only_in_unused_function(self):
        src = "def unused():\n    ensure_rest_api_key(d)\nif __name__ == '__main__':\n    app.run()\n"
        assert not _has_provision_before_start(src, _is_main_guard, ("run",))


def test_in_process_start_provisions_key():
    source = (BACKEND / "service.py").read_text(encoding="utf-8")
    assert _has_provision_before_start(source, _is_try_with_inprocess, ("InProcessRestServer",)), (
        "service.py обязан выдавать ключ ДО создания/старта in-process REST (в том же try-блоке)"
    )


def test_standalone_main_provisions_key():
    source = (BACKEND / "rest_server.py").read_text(encoding="utf-8")
    assert _has_provision_before_start(source, _is_main_guard, ("run",)), (
        "rest_server.py __main__ обязан выдавать ключ ДО app.run"
    )


def test_key_file_name_matches_swift_and_contract():
    assert KEY_FILENAME == "rest_api_key"
    assert os.sep not in KEY_FILENAME
