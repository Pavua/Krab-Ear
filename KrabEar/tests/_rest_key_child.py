"""Дочерний процесс для test_rest_default_auth: независимые settings и общий каталог ключа."""
from __future__ import annotations


def provision(tmp_dir: str, barrier, queue) -> None:
    try:
        import backend.rest_api_key as mod

        class _S:  # собственные settings процесса: у каждого «воркера» ключ пуст
            REST_API_KEY = ""
            REST_API_AUTH_ENABLED = False
            REST_API_ALLOW_ANONYMOUS = False

        mod.settings = _S()
        barrier.wait(30.0)
        queue.put(("ok", mod.ensure_rest_api_key(tmp_dir)))
    except BaseException as exc:  # noqa: BLE001
        queue.put(("error", type(exc).__name__))
