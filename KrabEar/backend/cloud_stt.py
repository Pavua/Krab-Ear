"""Cloud STT providers (OpenAI, Deepgram, AssemblyAI).

Обеспечивает fallback-транскрибацию через облачные API.
Если API-ключ не задан в settings, возвращается понятная ошибка
(stub-режим), чтобы избежать падения сервиса.
"""

import io
import json
import logging
import math
import re
import threading
import time
import urllib.request
import urllib.parse
import wave
from typing import Callable, Dict, Any, Optional, Protocol

from backend.state_store import StateStore
from core.config import settings

logger = logging.getLogger("KrabEar.CloudSTT")

# S3/I-C: собственный module-level StateStore здесь был лок-миной. После
# выравнивания DATA_DIR он смотрел бы на ТЕ ЖЕ файлы, что основной store
# процесса, а per-thread depth-counter реентерабельности (#1872) живёт в поле
# ЭКЗЕМПЛЯРА — между двумя экземплярами он не защищает: вход в лок второго
# из-под лока первого берёт flock на новом fd и заклинивает навсегда. Читаем
# настройки через аксессор владельца процесса; фоллбэк на собственный ленивый
# store оставлен для standalone-режима и тестов, где владельца нет.
_settings_fn: Callable[[], dict] | None = None
_fallback_store_instance: Optional[StateStore] = None
_fallback_store_lock = threading.Lock()


def adopt_settings_reader(settings_fn: Callable[[], dict]) -> None:
    """Подменяет источник настроек ссылкой на аксессор владельца процесса."""
    global _settings_fn
    _settings_fn = settings_fn


def _fallback_store() -> StateStore:
    """Ленивый синглтон StateStore для standalone-режима/тестов без владельца.

    Double-checked locking: наивный check-then-set создал бы два экземпляра на
    одних файлах под конкурентным доступом — ту же лок-мину, от которой уходим.
    """
    global _fallback_store_instance
    if _fallback_store_instance is None:
        with _fallback_store_lock:
            if _fallback_store_instance is None:
                _fallback_store_instance = StateStore(settings.DATA_DIR)
    return _fallback_store_instance


def _load_settings() -> dict:
    if _settings_fn is not None:
        return _settings_fn()
    return _fallback_store().load_settings()


# --- Hardening limits / validators (2026-06-16 audit of the new VG-bridge surface) ---
_MAX_RESP_BYTES = 4 * 1024 * 1024          # cap any provider HTTP body (success path) — bound memory
_MAX_ERR_BYTES = 2048                       # truncate provider error bodies before logging/forwarding
MAX_CLOUD_AUDIO_BYTES = 50 * 1024 * 1024    # ~26 min of 16 kHz mono PCM16 — WS accumulator cap (rest_server)
_POLL_BUDGET_SEC = 90.0                     # AssemblyAI: overall wall-clock cap on the poll loop
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z]{2,4})?$", re.IGNORECASE)
_TRANSCRIPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _sanitize_lang(source_lang: str, default: str = "ru") -> str:
    """Validate a WS-supplied language code before it reaches an HTTP request.

    `source_lang` arrives from the `/v1/stream` WS `config` unvalidated, and the
    OpenAI provider inserts it verbatim into a raw multipart body with a FIXED
    boundary — so a value containing CRLF + that boundary could inject extra form
    fields (`prompt`/`model`). Accept only short ISO-639-style codes; fall back to
    `default` for "auto"/garbage. Deepgram (urlencode) and AssemblyAI (json) are
    not injectable, but sanitizing centrally defends every provider uniformly.
    """
    if not source_lang or source_lang == "auto":
        return default
    candidate = source_lang.strip()
    if _LANG_RE.match(candidate):
        return candidate.lower()
    return default


def _read_capped(resp, limit: int = _MAX_RESP_BYTES) -> bytes:
    """Read at most `limit` bytes from an HTTP response to bound memory use.

    A misbehaving/compromised provider could otherwise stream an unbounded body
    into the WS handler thread. `http.client.HTTPResponse.read(amt)` fills up to
    `amt` bytes (for both Content-Length and chunked transfers) and leaves the
    remainder unread — so a single capped read bounds memory; `[:limit]` drops
    the sentinel +1 byte used to detect an over-limit body.
    """
    data = resp.read(limit + 1)
    return data[:limit] if data else b""


def _err_body(exc) -> str:
    """Capped, decoded HTTPError body (truncated for both logging and WS forwarding)."""
    try:
        return _read_capped(exc, _MAX_ERR_BYTES).decode("utf-8", "replace")
    except Exception:
        return ""


def _safe_confidence(value: Any, default: float) -> float:
    """float() от confidence провайдера, терпимое к null/нечисловому/NaN/Inf.

    Cloud-audit (2026-06-20): Deepgram/AssemblyAI иногда возвращают
    ``"confidence": null`` (или опускают на error-статусах). ``dict.get(k, d)``
    отдаёт ``None`` при ПРИСУТСТВУЮЩЕМ null-ключе → ``float(None)`` → TypeError,
    который ронял всю transcribe() и молча рвал WS-соединение.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def pcm16_to_wav(pcm_bytes: bytes, sample_rate: int) -> bytes:
    """Конвертирует raw PCM (16-bit, mono) в WAV."""
    # Cloud-audit (2026-06-20): sample_rate приходит из WS-config клиента без
    # валидации; wave.setframerate(<=0) бросает wave.Error → молчаливый обрыв WS.
    # Клампим в разумный диапазон (мусорное значение → дефолт 16 кГц).
    try:
        sr = int(sample_rate)
    except (TypeError, ValueError):
        sr = 16000
    if not (8000 <= sr <= 48000):
        sr = 16000
    with io.BytesIO() as wav_io:
        with wave.open(wav_io, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sr)
            wav_file.writeframes(pcm_bytes)
        return wav_io.getvalue()


class CloudSTTProvider(Protocol):
    """Интерфейс облачного STT-провайдера."""
    def transcribe(self, pcm_bytes: bytes, sample_rate: int, source_lang: str) -> Dict[str, Any]:
        """Транскрибирует аудио.

        Возвращает:
            {"text": str, "lang": str, "confidence": float}
            Или {"error": str, "provider": str, "message": str} в случае ошибки.
        """
        ...


class OpenAISTTProvider:
    """Провайдер OpenAI Whisper."""
    def __init__(self) -> None:
        pass

    def transcribe(self, pcm_bytes: bytes, sample_rate: int, source_lang: str) -> Dict[str, Any]:
        api_key = _load_settings().get("openai_api_key", "").strip()
        if not api_key:
            return {
                "error": "no_api_key",
                "provider": "openai",
                "message": "OpenAI API key is missing in settings"
            }

        wav_bytes = pcm16_to_wav(pcm_bytes, sample_rate)
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"

        # Строим multipart/form-data
        body = bytearray()

        # File field
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n')
        body.extend(b'Content-Type: audio/wav\r\n\r\n')
        body.extend(wav_bytes)
        body.extend(b'\r\n')

        # Model field
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(b'Content-Disposition: form-data; name="model"\r\n\r\n')
        body.extend(b'whisper-1\r\n')

        # Language field
        lang = _sanitize_lang(source_lang)
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(b'Content-Disposition: form-data; name="language"\r\n\r\n')
        body.extend(lang.encode("utf-8"))
        body.extend(b'\r\n')

        # Response format
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(b'Content-Disposition: form-data; name="response_format"\r\n\r\n')
        body.extend(b'verbose_json\r\n')

        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=bytes(body),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(_read_capped(response).decode("utf-8", "replace"))
                # verbose_json returns "language" as a full English word ("russian",
                # "english", "spanish") — NOT an ISO-639-1 code.  Normalise to the
                # 2-letter code that every other provider and downstream consumer expects.
                _LANG_WORD_TO_ISO: Dict[str, str] = {
                    "russian": "ru", "english": "en", "spanish": "es",
                    "french": "fr", "german": "de", "italian": "it",
                    "portuguese": "pt", "chinese": "zh", "japanese": "ja",
                    "korean": "ko", "arabic": "ar", "turkish": "tr",
                    "polish": "pl", "dutch": "nl", "ukrainian": "uk",
                }
                raw_lang = result.get("language", "")
                normalised_lang = _LANG_WORD_TO_ISO.get(
                    raw_lang.lower(), raw_lang[:2].lower() if raw_lang else lang
                )
                return {
                    "text": result.get("text", ""),
                    "lang": normalised_lang or lang,
                    "confidence": 1.0,  # OpenAI verbose_json does not include per-segment confidence
                }
        except urllib.error.HTTPError as e:
            err_msg = _err_body(e)
            logger.error("OpenAI STT error: %s", err_msg)
            return {"error": "api_error", "provider": "openai", "message": err_msg}
        except Exception as e:
            logger.error("OpenAI STT network error: %s", e)
            return {"error": "network_error", "provider": "openai", "message": str(e)}


class DeepgramSTTProvider:
    """Провайдер Deepgram."""
    def __init__(self) -> None:
        pass

    def transcribe(self, pcm_bytes: bytes, sample_rate: int, source_lang: str) -> Dict[str, Any]:
        api_key = _load_settings().get("deepgram_api_key", "").strip()
        if not api_key:
            return {
                "error": "no_api_key",
                "provider": "deepgram",
                "message": "Deepgram API key is missing in settings"
            }

        lang = _sanitize_lang(source_lang)

        # Для Deepgram можно отправлять raw pcm если указать параметры
        params = {
            "model": "nova-2",
            "language": lang,
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "channels": "1",
        }
        qs = urllib.parse.urlencode(params)
        url = f"https://api.deepgram.com/v1/listen?{qs}"

        req = urllib.request.Request(
            url,
            data=pcm_bytes,
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "audio/x-raw",
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(_read_capped(response).decode("utf-8", "replace"))
                channels = result.get("results", {}).get("channels", [])
                if not channels:
                    return {"text": "", "lang": lang, "confidence": 0.0}
                alts = channels[0].get("alternatives", [])
                if not alts:
                    return {"text": "", "lang": lang, "confidence": 0.0}
                return {
                    "text": alts[0].get("transcript", ""),
                    "lang": lang,
                    "confidence": _safe_confidence(alts[0].get("confidence"), 0.0),
                }
        except urllib.error.HTTPError as e:
            err_msg = _err_body(e)
            logger.error("Deepgram STT error: %s", err_msg)
            return {"error": "api_error", "provider": "deepgram", "message": err_msg}
        except Exception as e:
            logger.error("Deepgram STT network error: %s", e)
            return {"error": "network_error", "provider": "deepgram", "message": str(e)}


class AssemblyAISTTProvider:
    """Провайдер AssemblyAI (Batch upload + poll)."""
    def __init__(self) -> None:
        pass

    def transcribe(self, pcm_bytes: bytes, sample_rate: int, source_lang: str) -> Dict[str, Any]:
        api_key = _load_settings().get("assemblyai_api_key", "").strip()
        if not api_key:
            return {
                "error": "no_api_key",
                "provider": "assemblyai",
                "message": "AssemblyAI API key is missing in settings"
            }

        wav_bytes = pcm16_to_wav(pcm_bytes, sample_rate)

        # 1. Upload
        upload_req = urllib.request.Request(
            "https://api.assemblyai.com/v2/upload",
            data=wav_bytes,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/octet-stream",
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(upload_req, timeout=30) as upload_resp:
                upload_res = json.loads(_read_capped(upload_resp).decode("utf-8", "replace"))
                upload_url = upload_res.get("upload_url")
        except urllib.error.HTTPError as e:
            err_msg = _err_body(e)
            logger.error("AssemblyAI Upload error: %s", err_msg)
            return {"error": "api_error", "provider": "assemblyai", "message": err_msg}
        except Exception as e:
            logger.error("AssemblyAI Upload network error: %s", e)
            return {"error": "network_error", "provider": "assemblyai", "message": str(e)}

        if not upload_url:
            return {"error": "api_error", "provider": "assemblyai", "message": "No upload URL returned"}

        lang = _sanitize_lang(source_lang)

        # 2. Transcribe
        transcript_req = urllib.request.Request(
            "https://api.assemblyai.com/v2/transcript",
            data=json.dumps({
                "audio_url": upload_url,
                "language_code": lang
            }).encode("utf-8"),
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(transcript_req, timeout=30) as transcript_resp:
                transcript_res = json.loads(_read_capped(transcript_resp).decode("utf-8", "replace"))
                transcript_id = transcript_res.get("id")
        except urllib.error.HTTPError as e:
            err_msg = _err_body(e)
            logger.error("AssemblyAI Transcribe error: %s", err_msg)
            return {"error": "api_error", "provider": "assemblyai", "message": err_msg}
        except Exception as e:
            logger.error("AssemblyAI Transcribe network error: %s", e)
            return {"error": "network_error", "provider": "assemblyai", "message": str(e)}

        if not transcript_id:
            return {"error": "api_error", "provider": "assemblyai", "message": "No transcript ID returned"}
        # Provider-supplied id is interpolated into the poll URL — keep it within
        # the host path (no '/', '?', CRLF) even though the host stays fixed.
        if not _TRANSCRIPT_ID_RE.match(str(transcript_id)):
            return {"error": "api_error", "provider": "assemblyai", "message": "Invalid transcript id from provider"}

        # 3. Poll
        poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
        poll_req = urllib.request.Request(
            poll_url,
            headers={"Authorization": api_key},
            method="GET"
        )

        max_attempts = 15
        # Bound the total blocking time of this synchronous poll loop so it cannot
        # tie up the WS handler thread for the worst-case 15*(2+30)s ≈ 8 min.
        poll_deadline = time.monotonic() + _POLL_BUDGET_SEC
        # Track consecutive poll failures so a persistent error (cert failure, DNS
        # outage) doesn't spin silently to the deadline.  A single transient blip
        # (TCP reset, brief DNS hiccup) must NOT abort the transcript — AssemblyAI
        # is async and the result is almost certainly ready on the next iteration.
        _consecutive_poll_errors = 0
        _MAX_CONSECUTIVE_POLL_ERRORS = 3
        for _ in range(max_attempts):
            if time.monotonic() >= poll_deadline:
                break
            time.sleep(2)
            try:
                with urllib.request.urlopen(poll_req, timeout=15) as poll_resp:
                    poll_res = json.loads(_read_capped(poll_resp).decode("utf-8", "replace"))
                    _consecutive_poll_errors = 0  # reset on any successful HTTP exchange
                    status = poll_res.get("status")
                    if status == "completed":
                        return {
                            "text": poll_res.get("text", ""),
                            "lang": poll_res.get("language_code", lang),
                            "confidence": _safe_confidence(poll_res.get("confidence"), 1.0),
                        }
                    if status == "error":
                        return {"error": "api_error", "provider": "assemblyai", "message": poll_res.get("error", "Unknown")}
            except Exception as e:
                _consecutive_poll_errors += 1
                logger.warning(
                    "AssemblyAI Poll transient error (%d/%d): %s",
                    _consecutive_poll_errors, _MAX_CONSECUTIVE_POLL_ERRORS, e,
                )
                if _consecutive_poll_errors >= _MAX_CONSECUTIVE_POLL_ERRORS:
                    logger.error("AssemblyAI Poll aborted after %d consecutive errors", _MAX_CONSECUTIVE_POLL_ERRORS)
                    return {"error": "network_error", "provider": "assemblyai", "message": str(e)}
                # Transient failure — continue within the deadline / attempt budget.

        return {"error": "timeout", "provider": "assemblyai", "message": "Polling timeout"}


def get_cloud_stt_provider(name: str) -> Optional[CloudSTTProvider]:
    """Возвращает облачного провайдера по имени."""
    providers = {
        "openai": OpenAISTTProvider,
        "deepgram": DeepgramSTTProvider,
        "assemblyai": AssemblyAISTTProvider,
    }
    cls = providers.get(name.lower())
    if cls:
        return cls()
    return None
