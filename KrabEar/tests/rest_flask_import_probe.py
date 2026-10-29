"""Disposable import/auth/privacy prerequisite; не STT error/E2E acceptance."""
import sys
from pathlib import Path

SCRATCH = Path(sys.argv[1]).resolve()
sys.pycache_prefix = str(SCRATCH / "pycache")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend_lifecycle_probe import Boundary  # noqa: E402


class RestBoundary(Boundary):
    def find_spec(self, fullname, path=None, target=None):
        # Полный actual REST import — единственное расширение module allowlist.
        if fullname == "backend.rest_server":
            return None
        return super().find_spec(fullname, path, target)

    def start(self, thread):
        self.deny("thread forbidden in import/auth prerequisite")


boundary = RestBoundary(SCRATCH)
boundary.install()
sys.path.insert(0, str(ROOT))
import atexit  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402


def run():
    assert sys.version_info[:2] == (3, 12)
    assert Path.cwd().resolve() == SCRATCH
    assert Path.home().resolve().is_relative_to(SCRATCH)
    assert os.environ.get("KRAB_EAR_MLX_WHISPER_WORKER") == "0"
    from core import config
    for flag in ("RATE_LIMIT_ENABLED", "STT_GIGAAM_ENABLED", "LLM_ENABLED", "REST_IN_PROCESS_ENABLED",
                 "EVENT_BRIDGE_ENABLED", "WAKE_WORD_ENABLED"):
        assert getattr(config.settings, flag) is False, flag
    from backend.state_store import StateStore
    data = Path.home() / "Library/Application Support/KrabEar"
    assert config.settings.DATA_DIR == data
    assert config._SETTINGS_JSON_FILE == data / "settings.json"
    store = StateStore(data)
    store.save_settings({"history_encryption_enabled": False,
                         "privacy_mode_enabled": False,
                         "stt_warmup_on_startup": False,
                         "rewriter_warmup_on_startup": False})
    from backend import rest_server as rs
    try:
        assert type(rs.store) is StateStore
        assert rs.store.data_dir == data
        # Реальный фабричный pipeline и декораторы, без StaticDeps/mocked engine.
        app = rs.create_app(config_mapping={"TESTING": True, "RATELIMIT_ENABLED": False})
        client = app.test_client()
        rs.settings.REST_API_AUTH_ENABLED = True
        rs.settings.REST_API_KEY = ""
        rs.store.save_settings({"privacy_mode_enabled": False})
        response = client.post("/v1/stt/transcribe", data={})
        assert response.status_code == 401, response.status_code
        assert response.json == {"error": "Missing or invalid Authorization header"}
        response.close()
        response = client.post("/v1/stt/transcribe", data={},
                               headers={"Authorization": "Bearer disposable-invalid"})
        assert response.status_code == 401, response.status_code
        response.close()
        rs.store.save_settings({"privacy_mode_enabled": True})
        response = client.post("/v1/stt/transcribe", data={})
        assert response.status_code == 403, response.status_code
        assert response.json.get("skipped") == "privacy_mode"
        response.close()
        assert not boundary.violations, boundary.violations
        assert len(threading.enumerate()) == 1
        assert not list(rs.TEMP_DIR.iterdir())
        return {"full_rest_import": True, "http_cases": 3,
                "worker_starts": 0, "violations": boundary.violations,
                "scope": "import/auth/privacy prerequisite only; no STT/error503/E2E/drain proof"}
    finally:
        # Реальный cleanup собственного standalone engine; не чужого runtime.
        rs._rest_engine_cleanup()
        atexit.unregister(rs._rest_engine_cleanup)
        if not rs.transcriber.close():
            raise AssertionError("scratch transcriber close failed")


if __name__ == "__main__":
    result = run()
    assert not boundary.violations
    print("REST_IMPORT_RECEIPT=" + json.dumps(result), flush=True)
