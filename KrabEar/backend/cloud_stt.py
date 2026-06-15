"""Cloud STT providers (OpenAI, Deepgram, AssemblyAI).

Обеспечивает fallback-транскрибацию через облачные API.
Если API-ключ не задан в settings, возвращается понятная ошибка
(stub-режим), чтобы избежать падения сервиса.
"""

import io
import json
import logging
import time
import urllib.request
import urllib.parse
import wave
from typing import Dict, Any, Optional, Protocol

from backend.state_store import StateStore
from core.config import settings

logger = logging.getLogger("KrabEar.CloudSTT")

# Используем тот же store, что и в rest_server.py (или создаём новый с тем же DATA_DIR)
store = StateStore(settings.DATA_DIR)


def pcm16_to_wav(pcm_bytes: bytes, sample_rate: int) -> bytes:
    """Конвертирует raw PCM (16-bit, mono) в WAV."""
    with io.BytesIO() as wav_io:
        with wave.open(wav_io, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
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
        api_key = store.load_settings().get("openai_api_key", "").strip()
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
        lang = source_lang if source_lang and source_lang != "auto" else "ru"
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
                result = json.loads(response.read().decode("utf-8"))
                return {
                    "text": result.get("text", ""),
                    "lang": result.get("language", lang),
                    "confidence": 1.0,  # OpenAI API не возвращает confidence в verbose_json по умолчанию?
                }
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
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
        api_key = store.load_settings().get("deepgram_api_key", "").strip()
        if not api_key:
            return {
                "error": "no_api_key",
                "provider": "deepgram",
                "message": "Deepgram API key is missing in settings"
            }

        lang = source_lang if source_lang and source_lang != "auto" else "ru"

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
                result = json.loads(response.read().decode("utf-8"))
                channels = result.get("results", {}).get("channels", [])
                if not channels:
                    return {"text": "", "lang": lang, "confidence": 0.0}
                alts = channels[0].get("alternatives", [])
                if not alts:
                    return {"text": "", "lang": lang, "confidence": 0.0}
                return {
                    "text": alts[0].get("transcript", ""),
                    "lang": lang,
                    "confidence": float(alts[0].get("confidence", 0.0)),
                }
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
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
        api_key = store.load_settings().get("assemblyai_api_key", "").strip()
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
                upload_res = json.loads(upload_resp.read().decode("utf-8"))
                upload_url = upload_res.get("upload_url")
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            logger.error("AssemblyAI Upload error: %s", err_msg)
            return {"error": "api_error", "provider": "assemblyai", "message": err_msg}
        except Exception as e:
            logger.error("AssemblyAI Upload network error: %s", e)
            return {"error": "network_error", "provider": "assemblyai", "message": str(e)}

        if not upload_url:
            return {"error": "api_error", "provider": "assemblyai", "message": "No upload URL returned"}

        lang = source_lang if source_lang and source_lang != "auto" else "ru"

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
                transcript_res = json.loads(transcript_resp.read().decode("utf-8"))
                transcript_id = transcript_res.get("id")
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            logger.error("AssemblyAI Transcribe error: %s", err_msg)
            return {"error": "api_error", "provider": "assemblyai", "message": err_msg}
        except Exception as e:
            logger.error("AssemblyAI Transcribe network error: %s", e)
            return {"error": "network_error", "provider": "assemblyai", "message": str(e)}

        if not transcript_id:
            return {"error": "api_error", "provider": "assemblyai", "message": "No transcript ID returned"}

        # 3. Poll
        poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
        poll_req = urllib.request.Request(
            poll_url,
            headers={"Authorization": api_key},
            method="GET"
        )

        max_attempts = 15
        for _ in range(max_attempts):
            time.sleep(2)
            try:
                with urllib.request.urlopen(poll_req, timeout=30) as poll_resp:
                    poll_res = json.loads(poll_resp.read().decode("utf-8"))
                    status = poll_res.get("status")
                    if status == "completed":
                        return {
                            "text": poll_res.get("text", ""),
                            "lang": poll_res.get("language_code", lang),
                            "confidence": float(poll_res.get("confidence", 1.0)),
                        }
                    if status == "error":
                        return {"error": "api_error", "provider": "assemblyai", "message": poll_res.get("error", "Unknown")}
            except Exception as e:
                logger.error("AssemblyAI Poll network error: %s", e)
                return {"error": "network_error", "provider": "assemblyai", "message": str(e)}

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
