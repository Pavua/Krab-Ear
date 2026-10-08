"""Изоляция settings/wiring тестов от реального аудио и ML constructors."""
from types import SimpleNamespace
from unittest.mock import Mock, patch


def safe_backend_for_settings(owner, store):
    """Реальный BackendService и store; ресурсоёмкие adapters — inert fixtures."""
    from backend.service import BackendService

    engine = SimpleNamespace(
        quality_profile="balanced", current_model="fixture-model",
        _llm_rewriter=None, _settings_get=None,
        _resolve_diarization_device=lambda: "cpu", warmup=lambda: None,
    )
    with patch("backend.service.settings.LLM_ENABLED", False):
        service = BackendService(
            store=store,
            recorder=SimpleNamespace(is_recording=False, start=lambda: None, stop=lambda: b""),
            transcriber=SimpleNamespace(engine=engine, _error_bus=None),
            translator=Mock(),
        )
    owner.addCleanup(service.close)
    return service
