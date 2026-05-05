"""STT router factory — assembles STTRouter with all enabled adapters.

Phase D.2: convenience builder that reads settings and instantiates
adapter wrappers. engine.py is NOT modified here — that migration
is a separate followup (Phase D.2 router migration).

Usage:
    from core.pipeline.stt_router_factory import build_router

    router = build_router(settings_dict={"stt_gigaam_enabled": True})
    result = router.transcribe(audio_array, language="ru")
"""
from __future__ import annotations

import logging
from typing import Any

from .stt_router import STTRouter
from .stt_gigaam_adapter import GigaAMSTTAdapter
from .stt_whisper_mlx_adapter import WhisperMLXAdapter

logger = logging.getLogger("KrabEar.STT.RouterFactory")


def build_router(settings_dict: dict[str, Any] | None = None) -> STTRouter:
    """Build STTRouter with all currently-installed and enabled adapters.

    Adapter inclusion rules:
    - GigaAM: included only when ``settings.stt_gigaam_enabled=True``.
    - Whisper MLX: always attempted; skipped silently if mlx_whisper not installed.

    Args:
        settings_dict: flat dict of settings (mirrors backend settings keys).
                       None → treat as empty dict (all defaults).

    Returns:
        STTRouter instance with all resolved adapters in priority order.
        The returned router's settings_provider is bound to ``settings_dict``
        so forced-engine / other runtime overrides apply at select_adapter time.
    """
    cfg = settings_dict or {}
    adapters = []

    # ----------------------------------------------------------------
    # GigaAM (RU-only, optional — requires separate venv or in-process install)
    # ----------------------------------------------------------------
    if cfg.get("stt_gigaam_enabled", False):
        try:
            gigaam = GigaAMSTTAdapter(
                mode=cfg.get("stt_gigaam_mode", "rnnt"),
                device=cfg.get("stt_gigaam_device", "cpu"),
                transport=cfg.get("stt_gigaam_transport", "auto"),
            )
            adapters.append(gigaam)
            logger.debug("RouterFactory: GigaAM adapter added (mode=%s)", gigaam._mode)
        except ImportError as exc:
            logger.warning("RouterFactory: GigaAMSTTAdapter unavailable: %s", exc)

    # ----------------------------------------------------------------
    # Whisper MLX (multilingual, always attempted)
    # ----------------------------------------------------------------
    try:
        whisper_model = cfg.get(
            "stt_ru_primary_model", "mlx-community/whisper-large-v3-mlx"
        )
        whisper = WhisperMLXAdapter(model_path=whisper_model)
        adapters.append(whisper)
        logger.debug("RouterFactory: WhisperMLX adapter added (model=%s)", whisper_model)
    except ImportError as exc:
        logger.warning("RouterFactory: WhisperMLXAdapter unavailable: %s", exc)

    if not adapters:
        logger.warning("RouterFactory: no STT adapters could be loaded")

    return STTRouter(adapters, settings_provider=lambda: cfg)
