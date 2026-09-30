"""Early startup settings must come from the process's actual StateStore."""

from pathlib import Path
import time
from unittest.mock import MagicMock, patch

from backend.service import BackendService
from backend.state_store import StateStore
from test_backend_service import FakeRecorder, FakeTranscriber, FakeTranslator


class _StartupSettingsProbe(BackendService):
    def _init_llm_rewriter(self):
        # This callback runs before the former late SettingsService assignment.
        self.early_settings = {
            "privacy": self._get_runtime_setting("privacy_mode_enabled", False),
            "keepalive": self._get_runtime_setting("llm_idle_keepalive_enabled", False),
        }
        return None  # Never connect to LM Studio in this test.


def test_early_llm_setup_reads_persisted_settings(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "data")
    store.save_settings({
        "privacy_mode_enabled": False,
        "llm_idle_keepalive_enabled": True,
    })
    service = _StartupSettingsProbe(
        store=store,
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        translator=FakeTranslator(),
    )
    try:
        assert service.early_settings == {"privacy": False, "keepalive": True}
        assert service._get_runtime_setting("llm_idle_keepalive_enabled", False) is True
    finally:
        service.close()


def test_startup_warmup_uses_saved_timeout_without_network(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "data")
    store.save_settings({
        "privacy_mode_enabled": False,
        "rewriter_warmup_on_startup": True,
        "llm_rewrite_enabled": True,
        "rewriter_warmup_timeout_sec": 17,
    })
    rewriter = MagicMock()
    with patch.object(BackendService, "_init_llm_rewriter", return_value=rewriter):
        service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
    try:
        deadline = time.monotonic() + 1
        while not rewriter.warmup_sync.called and time.monotonic() < deadline:
            time.sleep(0.01)
        rewriter.warmup_sync.assert_called_once()
        kwargs = rewriter.warmup_sync.call_args.kwargs
        assert kwargs["timeout_sec"] == 17.0
        assert kwargs["should_continue"]() is True
    finally:
        service.close()
