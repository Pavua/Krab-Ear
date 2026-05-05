"""SenseVoice STT adapter — Phase D.2.2.

FunAudioLLM SenseVoiceSmall via funasr package.
Multilingual: Mandarin (zh), Cantonese (yue), Japanese (ja), Korean (ko), English (en).
PyTorch + MPS native (NOT MLX) — no mlx_lock needed.

Model: FunAudioLLM/SenseVoiceSmall (HuggingFace)
Library: funasr (pip install funasr)
HuggingFace: https://huggingface.co/FunAudioLLM/SenseVoiceSmall

API shape (verified from FunASR docs / modelscope):
    from funasr import AutoModel
    model = AutoModel(model="FunAudioLLM/SenseVoiceSmall", device="mps")
    result = model.generate(input=audio_path_or_array, language="auto", use_itn=True)
    # result: list of dicts with keys "text", "key" (and optionally "timestamp", "emotion")

Language codes supported by SenseVoiceSmall:
    zh  — Mandarin Chinese
    yue — Cantonese
    ja  — Japanese
    ko  — Korean
    en  — English (acceptable quality)
    auto — auto-detect (recommended default)

SenseVoice also outputs emotion tags (<|HAPPY|>, <|SAD|>, etc.) embedded in text
when emotion detection is active. These are stripped from the returned STTResult text
but preserved in metadata for optional downstream use.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .stt_adapter import STTAdapterBase, STTResult

logger = logging.getLogger("KrabEar.STT.SenseVoice")

# Default model — SenseVoiceSmall is the lightweight variant (~250 MB).
_DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"

# Languages officially supported by SenseVoiceSmall.
_SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"zh", "yue", "ja", "ko", "en"})

# Regex to strip SenseVoice emotion/event/language tags like:
# <|HAPPY|>, <|BGM|>, <|Speech|>, <|zh|>, <|en|>, <|NEUTRAL|>
# The tag content may be all-uppercase, all-lowercase, or mixed (Title case).
_EMOTION_TAG_RE = re.compile(r"<\|[A-Za-z_]+\|>")

# One-time warning sentinel.
_warned_unavailable: bool = False


def _try_import_funasr() -> Optional[Any]:
    """Attempt to import funasr.AutoModel. Returns the class or None."""
    try:
        from funasr import AutoModel  # type: ignore[import]
        return AutoModel
    except ImportError:
        return None


def _strip_emotion_tags(text: str) -> str:
    """Remove SenseVoice emotion/event tags from transcription text.

    SenseVoice prepends tags like <|zh|><|HAPPY|><|Speech|> to the text.
    We strip all angle-bracket tags so downstream gets clean text.
    """
    return _EMOTION_TAG_RE.sub("", text).strip()


class SenseVoiceSTTAdapter(STTAdapterBase):
    """FunAudioLLM SenseVoiceSmall — multilingual East-Asian STT adapter.

    Supported languages: zh, yue, ja, ko, en (auto-detect default).
    Uses funasr library with PyTorch + MPS/CPU backend.
    No mlx_lock required — PyTorch runtime (not MLX).

    Args:
        model_id_or_path: HuggingFace repo ID or local path to model.
                          Default: "FunAudioLLM/SenseVoiceSmall".
        device: Inference device — "mps" (Apple Silicon GPU), "cpu", or "auto".
                "auto" selects MPS when torch.backends.mps.is_available().
    """

    def __init__(
        self,
        model_id_or_path: str | None = None,
        device: str = "auto",
    ) -> None:
        self._model_id_or_path = model_id_or_path or _DEFAULT_MODEL
        self._device_setting = device
        self._model: Any = None   # lazy-loaded on first transcribe()
        self._load_failed: bool = False

    # ------------------------------------------------------------------
    # STTAdapterBase contract
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return f"sensevoice/{self._model_id_or_path.split('/')[-1]}"

    @property
    def display_name(self) -> str:
        return f"SenseVoice ({self._model_id_or_path.split('/')[-1]})"

    def supports_language(self, language: str) -> bool:
        """Returns True for zh, yue, ja, ko, en (and 'auto')."""
        return language in _SUPPORTED_LANGUAGES or language == "auto"

    def is_available(self) -> bool:
        """Returns True if funasr is importable in current environment."""
        return _try_import_funasr() is not None

    def transcribe(
        self,
        audio: Any,
        *,
        language: str | None = None,
        max_duration_sec: float | None = None,
    ) -> STTResult:
        """Transcribe audio using SenseVoiceSmall via funasr.

        Args:
            audio: numpy.ndarray PCM float32 16 kHz mono, or str path to audio file.
            language: ISO 639-1 hint ("zh", "yue", "ja", "ko", "en") or None for auto.
                      SenseVoice default is "auto" — best choice for multi-lingual use.
            max_duration_sec: Not enforced internally; caller should truncate beforehand.

        Returns:
            STTResult with text (emotion tags stripped), detected language, metadata.

        Raises:
            ImportError: if funasr not installed.
            RuntimeError: if model loading or inference fails.
        """
        global _warned_unavailable

        AutoModel = _try_import_funasr()
        if AutoModel is None:
            if not _warned_unavailable:
                logger.warning(
                    "SenseVoiceSTTAdapter: funasr not installed. "
                    "Install with: pip install funasr  "
                    "(adapter will be skipped until installed)"
                )
                _warned_unavailable = True
            raise ImportError(
                "funasr not installed. Run: pip install funasr"
            )

        # Lazy model load — cache as instance attribute.
        if self._model is None and not self._load_failed:
            self._load_model(AutoModel)

        if self._load_failed or self._model is None:
            raise RuntimeError(
                "SenseVoiceSTTAdapter: model unavailable (load previously failed)"
            )

        # Language arg for generate(): use "auto" when not specified.
        lang_arg = language if language in _SUPPORTED_LANGUAGES else "auto"

        try:
            raw_results = self._model.generate(
                input=audio,
                language=lang_arg,
                use_itn=True,   # Inverse Text Normalization (numbers, dates)
            )
        except Exception as exc:
            logger.error(
                "SenseVoiceSTTAdapter: inference failed: %s", exc, exc_info=True
            )
            raise RuntimeError(
                f"SenseVoiceSTTAdapter: inference error: {exc}"
            ) from exc

        # raw_results is a list of dicts; use first element (single-audio input).
        if not raw_results:
            return STTResult(
                text="",
                engine=self.model_id,
                language=lang_arg if lang_arg != "auto" else None,
                confidence=None,
                duration_sec=None,
                word_count=0,
                metadata={"raw": []},
            )

        first = raw_results[0]
        raw_text: str = first.get("text", "") or ""

        # Extract emotion tags BEFORE stripping (preserve for metadata).
        emotion_tags = _EMOTION_TAG_RE.findall(raw_text)
        clean_text = _strip_emotion_tags(raw_text)

        # Attempt to detect language from SenseVoice text prefix tags.
        detected_lang: Optional[str] = self._extract_language_tag(raw_text) or (
            language if language in _SUPPORTED_LANGUAGES else None
        )

        # SenseVoice doesn't expose a per-token confidence; no native confidence output.
        confidence: Optional[float] = None

        # Optionally extract timestamps if present in result.
        timestamps = first.get("timestamp", None)

        return STTResult(
            text=clean_text,
            engine=self.model_id,
            language=detected_lang,
            confidence=confidence,
            duration_sec=None,
            word_count=len(clean_text.split()) if clean_text else 0,
            metadata={
                "raw_text": raw_text,
                "emotion_tags": emotion_tags,
                "timestamps": timestamps,
                "lang_arg": lang_arg,
                "model_path": self._model_id_or_path,
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_device(self) -> str:
        """Resolve effective device string (mps / cpu)."""
        if self._device_setting == "auto":
            try:
                import torch
                if torch.backends.mps.is_available():
                    return "mps"
            except ImportError:
                pass
            return "cpu"
        return self._device_setting

    def _load_model(self, AutoModel: Any) -> None:
        """Perform lazy model load; sets _load_failed on error."""
        device = self._resolve_device()
        try:
            logger.info(
                "SenseVoiceSTTAdapter: loading model %s on device=%s (first call)",
                self._model_id_or_path,
                device,
            )
            self._model = AutoModel(
                model=self._model_id_or_path,
                device=device,
                # disable_update=True prevents funasr from phoning home for updates.
                disable_update=True,
            )
            logger.info(
                "SenseVoiceSTTAdapter: model loaded successfully (device=%s)", device
            )
        except Exception as exc:
            self._load_failed = True
            logger.error(
                "SenseVoiceSTTAdapter: failed to load model %s on %s: %s",
                self._model_id_or_path,
                device,
                exc,
            )
            raise RuntimeError(
                f"SenseVoiceSTTAdapter: model load failed: {exc}"
            ) from exc

    @staticmethod
    def _extract_language_tag(raw_text: str) -> Optional[str]:
        """Extract language code from SenseVoice language prefix tag.

        SenseVoice embeds a language tag like <|zh|> or <|en|> at the start
        of output text when language=auto. Returns the ISO code or None.
        """
        m = re.match(r"<\|([a-z]{2,3})\|>", raw_text)
        if m:
            code = m.group(1)
            if code in _SUPPORTED_LANGUAGES:
                return code
        return None

    def warmup(self) -> bool:
        """Pre-load model to reduce first-call latency.

        Returns True on success, False if funasr not installed or load fails.
        """
        if not self.is_available():
            return False
        AutoModel = _try_import_funasr()
        if AutoModel is None:
            return False
        if self._model is None and not self._load_failed:
            try:
                self._load_model(AutoModel)
            except Exception:
                return False
        return self._model is not None

    def unload(self) -> None:
        """Release model from memory."""
        self._model = None
        self._load_failed = False
