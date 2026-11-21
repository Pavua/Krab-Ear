"""REST migration не управляет launchd без общего admission/drain."""

import pytest

from test_migrate_inprocess_rest_contract import _FakeEnv, _diag


@pytest.mark.parametrize("socket_present,rollback", [(False, False), (True, False), (True, True)])
def test_migration_refuses_lifecycle_without_fence(tmp_path, socket_present, rollback):
    env = _FakeEnv(tmp_path)
    sock = env.bind_socket() if socket_present else None
    try:
        env.plist_path.write_text("<plist/>", encoding="utf-8")
        result = env.run(
            ["--rollback"] if rollback else [],
            diagnostics=_diag(running=False),
            unit_loaded=not rollback,
        )
        assert result.returncode == 1
        assert env.launchctl_calls() == []
        assert env.plist_path.exists()
    finally:
        if sock is not None:
            sock.close()
        env.cleanup()
