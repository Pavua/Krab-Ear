"""Двухрежимный TTS-сервис: Silero (RU primary) + Kokoro (EN fallback) + macOS say.

Архитектура fallback chain:
1. RU-текст -> Silero (если загружен и TTS_ENABLED), иначе macOS say
2. EN-текст -> Kokoro (если загружен и TTS_ENABLED), иначе Silero (умеет EN),
              иначе macOS say
3. Auto-detect -> определяем язык по доле кириллицы, затем chain выше

Все ML-импорты -- ленивые (lazy): тесты проходят без torch/kokoro в окружении.
Lazy load без прямых method calls на nn.Module (нет train/eval mode switching).
"""

from __future__ import annotations

import base64
import io
import logging
import re
import subprocess
import tempfile
import threading
import wave
from typing import Any

from core.config import settings

logger = logging.getLogger("KrabEar.Backend.TTS")

# Порог кириллицы для определения RU
_CYRILLIC_THRESHOLD = 0.30  # >30% символов кириллица -> Russian


def _detect_language(text: str) -> str:
    """Эвристика определения языка: доля кириллических символов.

    Returns:
        "ru" если доля кириллических алфавитных символов > 30%, иначе "en".
    """
    alpha_chars = [c for c in text if c.isalpha()]
    if not alpha_chars:
        return "en"
    cyrillic = sum(1 for c in alpha_chars if "\u0400" <= c <= "\u04FF")
    return "ru" if (cyrillic / len(alpha_chars)) > _CYRILLIC_THRESHOLD else "en"


# Lazy loader: Silero

def _load_silero(model_id: str) -> Any | None:
    """Ленивая загрузка Silero TTS через torch.hub.

    Returns tuple (model, symbols, sample_rate, example_text, apply_tts, device) или None.
    """
    try:
        import torch  # type: ignore[import-untyped]
        device = torch.device("cpu")
        model, symbols, sample_rate, example_text, apply_tts = torch.hub.load(
            repo_or_dir="snakers4/silero-models",
            model="silero_tts",
            language="ru",
            speaker=model_id,
        )
        model = model.to(device)
        logger.info("Silero TTS загружен: model=%s sample_rate=%s", model_id, sample_rate)
        return model, symbols, sample_rate, example_text, apply_tts, device
    except ImportError:
        logger.debug("torch не установлен -- Silero TTS недоступен")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Silero TTS загрузка провалилась: %s", exc)
        return None


# Lazy loader: Kokoro

def _load_kokoro(model_id: str) -> Any | None:
    """Ленивая загрузка Kokoro-82M pipeline.

    Returns KPipeline объект или None.
    """
    try:
        from kokoro import KPipeline  # type: ignore[import-untyped]
        pipeline = KPipeline(lang_code="en-us", repo_id=model_id)
        logger.info("Kokoro TTS загружен: model=%s", model_id)
        return pipeline
    except ImportError:
        logger.debug("kokoro не установлен -- Kokoro TTS недоступен")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kokoro TTS загрузка провалилась: %s", exc)
        return None


# macOS say fallback

def _say_to_wav(text: str, voice: str | None = None, rate: int = 185) -> bytes:
    """Синтезирует речь через macOS say и возвращает WAV-байты.

    Записывает AIFF через say -o, затем конвертирует afconvert -> WAV PCM.
    """
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as aiff_f:
        aiff_path = aiff_f.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_f:
        wav_path = wav_f.name

    try:
        cmd_say = ["say", "-r", str(rate), "-o", aiff_path]
        if voice:
            safe_voice = voice if re.match(r"^[a-zA-Z0-9 _\-]+$", voice) else "Milena"
            cmd_say.extend(["-v", safe_voice])
        cmd_say.append(text)
        subprocess.run(cmd_say, check=False, capture_output=True)

        # AIFF -> WAV через встроенный macOS afconvert
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEF32@22050", aiff_path, wav_path],
            check=False,
            capture_output=True,
        )

        import os as _os
        if _os.path.exists(wav_path) and _os.path.getsize(wav_path) > 0:
            with open(wav_path, "rb") as f:
                return f.read()
        return b""
    finally:
        import os as _os
        for p in (aiff_path, wav_path):
            try:
                _os.unlink(p)
            except OSError:
                pass


# TTSService

class TTSService:
    """Двухрежимный TTS: Silero (RU) primary, Kokoro (EN) fallback, macOS say last resort.

    Все ML-модели загружаются лениво при первом обращении (thread-safe locks).
    При TTS_ENABLED=False (дефолт) синтез идёт через macOS say.
    """

    def __init__(self) -> None:
        self._silero: Any | None = None  # tuple или None
        self._kokoro: Any | None = None  # KPipeline или None
        self._silero_lock = threading.Lock()
        self._kokoro_lock = threading.Lock()
        self._silero_attempted = False
        self._kokoro_attempted = False

    # Lazy accessors

    def _get_silero(self) -> Any | None:
        """Возвращает Silero tuple, загружая при первом вызове (thread-safe)."""
        if self._silero_attempted:
            return self._silero
        with self._silero_lock:
            if not self._silero_attempted:
                self._silero = _load_silero(settings.TTS_SILERO_MODEL)
                self._silero_attempted = True
        return self._silero

    def _get_kokoro(self) -> Any | None:
        """Возвращает Kokoro pipeline, загружая при первом вызове (thread-safe)."""
        if self._kokoro_attempted:
            return self._kokoro
        with self._kokoro_lock:
            if not self._kokoro_attempted:
                self._kokoro = _load_kokoro(settings.TTS_KOKORO_MODEL)
                self._kokoro_attempted = True
        return self._kokoro

    # Synthesis backends

    def _synthesize_silero(self, text: str, voice: str | None = None) -> bytes | None:
        """Синтез через Silero. Возвращает WAV-байты или None при ошибке."""
        silero = self._get_silero()
        if silero is None:
            return None
        model, symbols, sample_rate, _example_text, apply_tts, device = silero
        try:
            import numpy as np
            speaker = voice or settings.TTS_SILERO_VOICE
            audio_tensor = apply_tts(
                texts=[text],
                model=model,
                sample_rate=sample_rate,
                symbols=symbols,
                device=device,
                speaker=speaker,
            )
            # audio_tensor: shape [1, samples], float32 in [-1, 1]
            samples = audio_tensor.squeeze().cpu().numpy()

            # Encode to WAV bytes (PCM 16-bit mono)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                pcm = (samples * 32767).clip(-32768, 32767).astype(np.int16)
                wf.writeframes(pcm.tobytes())
            return buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Silero синтез провалился: %s", exc)
            return None

    def _synthesize_kokoro(self, text: str, voice: str | None = None) -> bytes | None:
        """Синтез через Kokoro. Возвращает WAV-байты или None при ошибке."""
        pipeline = self._get_kokoro()
        if pipeline is None:
            return None
        try:
            import numpy as np
            all_samples: list[Any] = []
            sample_rate = 24000  # Kokoro default
            for _gs, _ps, audio in pipeline(text, voice=voice or "af_heart"):
                if audio is not None:
                    all_samples.append(audio)
            if not all_samples:
                return None
            samples = np.concatenate(all_samples) if len(all_samples) > 1 else all_samples[0]
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                pcm = (samples * 32767).clip(-32768, 32767).astype(np.int16)
                wf.writeframes(pcm.tobytes())
            return buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kokoro синтез провалился: %s", exc)
            return None

    # Public API

    def synthesize_speech(
        self,
        text: str,
        language: str = "auto",
        voice: str | None = None,
    ) -> bytes:
        """Синтезирует речь и возвращает WAV-байты.

        Fallback chain:
        - RU: Silero -> macOS say
        - EN: Kokoro -> Silero (умеет EN) -> macOS say
        - auto: определяется эвристикой (_detect_language), затем chain выше

        Args:
            text: Текст для синтеза.
            language: "ru" / "en" / "auto" (по умолчанию auto).
            voice: Имя голоса (Silero speaker или Kokoro voice id).
                   None = используется дефолт из настроек.

        Returns:
            WAV-байты. Пустой bytes если TTS_ENABLED=False и TTS_FALLBACK_SAY=False.
        """
        if not text.strip():
            return b""

        # Определяем язык
        if language == "auto":
            lang = _detect_language(text)
        else:
            lang = language.lower().strip()

        logger.debug(
            "TTS synthesize: lang=%s enabled=%s text_len=%d",
            lang, settings.TTS_ENABLED, len(text),
        )

        if settings.TTS_ENABLED:
            if lang == "ru":
                # Silero primary for Russian
                wav = self._synthesize_silero(text, voice=voice)
                if wav:
                    return wav
                logger.debug("Silero недоступен для RU, переход на say")
            else:
                # EN: Kokoro -> Silero -> say
                wav = self._synthesize_kokoro(text, voice=voice)
                if wav:
                    return wav
                logger.debug("Kokoro недоступен для EN, пробуем Silero")
                wav = self._synthesize_silero(text, voice=voice)
                if wav:
                    return wav
                logger.debug("Silero тоже недоступен для EN, переход на say")

        # Последний резерв: macOS say
        if settings.TTS_FALLBACK_SAY:
            say_voice = voice or (settings.SAY_VOICE or None)
            return _say_to_wav(text, voice=say_voice)

        return b""

    def handle_synthesize_speech(self, params: dict) -> dict:
        """IPC handler для метода synthesize_speech.

        Params:
            text (str): Текст для синтеза. Обязателен.
            language (str): "ru" / "en" / "auto". По умолчанию "auto".
            voice (str | None): Голос. Опционально.

        Returns:
            dict с ключами wav_bytes_b64, language, engine, byte_count.
        """
        text = str(params.get("text", "")).strip()
        if not text:
            return {"ok": False, "error": "text is required"}

        language = str(params.get("language", "auto")).strip().lower()
        if language not in ("ru", "en", "auto"):
            language = "auto"
        voice = params.get("voice") or None
        if voice is not None:
            voice = str(voice).strip() or None

        # Определяем итоговый язык для ответа
        resolved_lang = language if language != "auto" else _detect_language(text)

        wav = self.synthesize_speech(text=text, language=language, voice=voice)
        if not wav:
            return {
                "wav_bytes_b64": "",
                "language": resolved_lang,
                "engine": "none",
                "byte_count": 0,
            }

        # Определяем движок: приоритет по загруженным моделям
        engine = "say"
        if settings.TTS_ENABLED:
            if resolved_lang == "ru" and self._silero is not None:
                engine = "silero"
            elif resolved_lang != "ru" and self._kokoro is not None:
                engine = "kokoro"
            elif self._silero is not None:
                engine = "silero"

        return {
            "wav_bytes_b64": base64.b64encode(wav).decode("ascii"),
            "language": resolved_lang,
            "engine": engine,
            "byte_count": len(wav),
        }
