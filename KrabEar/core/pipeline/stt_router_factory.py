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
from .stt_parakeet import ParakeetSTTAdapter
from .stt_sensevoice import SenseVoiceSTTAdapter
from .stt_sherpa import SherpaOnnxSTTAdapter
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
        except (ImportError, ValueError) as exc:
            # ValueError: transport="mlx" поддержан только основным стеком
            # (stt_router.get_gigaam_adapter); Phase D.2 фабрика не подключена
            # к engine — деградируем мягко, не роняя build_router у REST.
            logger.warning("RouterFactory: GigaAMSTTAdapter unavailable: %s", exc)

    # ----------------------------------------------------------------
    # Parakeet MLX (EN-only, optional — requires parakeet-mlx install)
    # ----------------------------------------------------------------
    if cfg.get("stt_parakeet_enabled", False):
        try:
            parakeet = ParakeetSTTAdapter(
                model_path=cfg.get("stt_parakeet_model", None),
            )
            if parakeet.is_available():
                adapters.append(parakeet)
                logger.debug(
                    "RouterFactory: Parakeet adapter added (model=%s)", parakeet._model_path
                )
            else:
                logger.warning(
                    "RouterFactory: Parakeet enabled but parakeet-mlx not installed; "
                    "install with: pip install parakeet-mlx"
                )
        except Exception as exc:
            logger.warning("RouterFactory: ParakeetSTTAdapter init failed: %s", exc)

    # ----------------------------------------------------------------
    # SenseVoice (East Asian multilingual — zh/yue/ja/ko/en, optional)
    # Added BEFORE Whisper so Asian-language audio routes to the
    # specialized adapter first (better CER/WER on zh/yue/ja/ko).
    # Uses PyTorch + MPS (NOT MLX) — no mlx_lock required.
    # ----------------------------------------------------------------
    if cfg.get("stt_sensevoice_enabled", False):
        try:
            sensevoice = SenseVoiceSTTAdapter(
                model_id_or_path=cfg.get("stt_sensevoice_model", None),
                device=cfg.get("stt_sensevoice_device", "auto"),
            )
            if sensevoice.is_available():
                adapters.append(sensevoice)
                logger.debug(
                    "RouterFactory: SenseVoice adapter added (model=%s, device=%s)",
                    sensevoice._model_id_or_path,
                    sensevoice._device_setting,
                )
            else:
                logger.warning(
                    "RouterFactory: SenseVoice enabled but funasr not installed; "
                    "install with: pip install funasr"
                )
        except Exception as exc:
            logger.warning("RouterFactory: SenseVoiceSTTAdapter init failed: %s", exc)

    # ----------------------------------------------------------------
    # Sherpa-ONNX (Paraformer — ultra-low-latency for calls, optional,
    # requires `pip install sherpa-onnx`). NOT MLX — no mlx_lock required.
    # ----------------------------------------------------------------
    if cfg.get("stt_sherpa_enabled", False):
        try:
            sherpa = SherpaOnnxSTTAdapter(
                model_dir=cfg.get("stt_sherpa_model_dir", None),
            )
            if sherpa.is_available():
                adapters.append(sherpa)
                logger.debug(
                    "RouterFactory: Sherpa-ONNX adapter added (model_dir=%s)", sherpa._model_dir
                )
            else:
                logger.warning(
                    "RouterFactory: Sherpa enabled but sherpa-onnx not installed; "
                    "install with: pip install sherpa-onnx"
                )
        except Exception as exc:
            logger.warning("RouterFactory: SherpaOnnxSTTAdapter init failed: %s", exc)

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
