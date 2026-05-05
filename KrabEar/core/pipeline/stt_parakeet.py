"""Parakeet STT adapter scaffold (Phase D.2).

NOTE: scaffold-only — actual model loading deferred to D.2.1 implementation.
This file provides the class shape + is_available() check + raises NotImplementedError
on transcribe() for now.
"""
from __future__ import annotations

import logging
from typing import Any

from .stt_adapter import STTAdapterBase, STTResult

logger = logging.getLogger("KrabEar.STT.Parakeet")


class ParakeetAdapter(STTAdapterBase):
    """NVIDIA Parakeet TDT 1.1B — English-first, low-latency real-time STT.

    Status: SCAFFOLD only. Real implementation requires:
    - Install nemo-toolkit + onnxruntime
    - Download Parakeet-TDT-1.1B weights
    - Wire CUDA/MPS device handling

    Track in: docs/superpowers/specs/2026-05-05-phase-d-roadmap-design.md D.2
    """

    @property
    def model_id(self) -> str:
        return "parakeet-tdt-1.1b"

    @property
    def display_name(self) -> str:
        return "Parakeet TDT 1.1B"

    def supports_language(self, language: str) -> bool:
        return language == "en"  # English only initially

    def is_available(self) -> bool:
        # TODO D.2.1: check nemo-toolkit + model file presence
        return False

    def transcribe(self, audio: Any, *, language: str | None = None,
                   max_duration_sec: float | None = None) -> STTResult:
        raise NotImplementedError("Parakeet adapter is scaffold-only — D.2.1 will implement.")
