"""Cloud rewriter fallback — облачная полировка транскрипта когда LM Studio недоступен.

PRIVACY-SENSITIVE: текст пользователя покидает устройство.
Защиты:
  1. Opt-in: cloud_rewriter_enabled по умолчанию False.
  2. Privacy gate: privacy_mode_enabled=True ВСЕГДА блокирует (engine.py).
  3. Audit trail: каждый реальный вызов логируется в PrivacyAuditLogger (engine.py).
  4. Не сохраняет транскрипт локально — только полирует и возвращает строку.

Архитектура зеркалирует backend/cloud_stt.py (Protocol + per-provider + stub + caps).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Protocol

from backend.state_store import StateStore
from core.config import settings

logger = logging.getLogger("KrabEar.Backend.CloudRewriter")

# -------------------------------------------------------------------------
# Hardening limits (mirror cloud_stt.py)
# -------------------------------------------------------------------------
_MAX_RESP_BYTES = 512 * 1024      # cap successful response body (~512 KB)
_MAX_ERR_BYTES = 2048             # truncate error bodies before logging


def _read_capped(resp, limit: int = _MAX_RESP_BYTES) -> bytes:
    """Читает не более limit байт из HTTP-ответа провайдера.

    Защищает от unbounded stream: misbehaving provider could send GB of data
    into the handler thread.  read(limit+1) → slice to limit.
    """
    data = resp.read(limit + 1)
    return data[:limit] if data else b""


def _err_body(exc) -> str:
    """Capped, decoded HTTPError body (truncated for logging)."""
    try:
        return _read_capped(exc, _MAX_ERR_BYTES).decode("utf-8", "replace")
    except Exception:
        return ""


# -------------------------------------------------------------------------
# System prompts — cleanup only (mirror _PUNCTUATION_SYSTEM_PROMPTS в llm_rewriter.py)
# -------------------------------------------------------------------------
_CLEANUP_SYSTEM_PROMPTS: Dict[str, str] = {
    "ru": (
        "Ты редактор пунктуации и орфографии для STT-транскрипта. "
        "Исправь пунктуацию, расставь заглавные буквы, поправь очевидные STT-ошибки. "
        "ЗАПРЕЩЕНО менять или удалять слова (кроме явных filler'ов «э-э», «ну» в начале). "
        "ЗАПРЕЩЕНО переводить текст на другой язык. "
        "Верни только исправленный текст. Без пояснений. Без кавычек."
    ),
    "es": (
        "Eres un editor de puntuación y ortografía para transcripciones STT. "
        "Corrige la puntuación, las mayúsculas y los errores obvios de STT. "
        "PROHIBIDO cambiar o eliminar palabras (excepto muletillas como 'eh', 'este' al inicio). "
        "PROHIBIDO traducir el texto a otro idioma. "
        "Devuelve solo el texto corregido. Sin explicaciones. Sin comillas."
    ),
    "en": (
        "You are a punctuation and spelling editor for STT transcripts. "
        "Fix punctuation, capitalization, and obvious STT errors. "
        "FORBIDDEN to change or delete words (except filler words like 'um', 'uh' at the start). "
        "FORBIDDEN to translate the text to another language. "
        "Return only the corrected text. No explanations. No quotes."
    ),
}

_DEFAULT_SYSTEM_PROMPT = _CLEANUP_SYSTEM_PROMPTS["ru"]

# Module-level store — same pattern as cloud_stt.py
store = StateStore(settings.DATA_DIR)


# -------------------------------------------------------------------------
# Protocol
# -------------------------------------------------------------------------

class CloudRewriterProvider(Protocol):
    """Интерфейс облачного провайдера для полировки транскрипта."""

    def rewrite(self, text: str, language: str) -> Dict[str, Any]:
        """Полирует транскрипт.

        Возвращает:
            {"text": <polished>}  — успех.
            {"error": "no_api_key"|"api_error"|"network_error", "provider": str, "message": str}
        """
        ...


# -------------------------------------------------------------------------
# OpenAI provider
# -------------------------------------------------------------------------

class OpenAIRewriterProvider:
    """Провайдер OpenAI (gpt-4o-mini) — дешёвый и быстрый для cleanup-задач."""

    _MODEL = "gpt-4o-mini"

    def rewrite(self, text: str, language: str) -> Dict[str, Any]:
        api_key = store.load_settings().get("openai_api_key", "").strip()
        if not api_key:
            return {
                "error": "no_api_key",
                "provider": "openai",
                "message": "openai_api_key not set in settings",
            }

        lang_key = (language or "ru").lower()[:2]
        system_prompt = _CLEANUP_SYSTEM_PROMPTS.get(lang_key, _DEFAULT_SYSTEM_PROMPT)

        payload = json.dumps({
            "model": self._MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.0,
            "max_tokens": min(max(256, len(text.split()) * 4 + 50), 4096),
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(_read_capped(resp).decode("utf-8", "replace"))
                content = result["choices"][0]["message"]["content"]
                return {"text": (content or "").strip()}
        except urllib.error.HTTPError as e:
            msg = _err_body(e)
            logger.error("OpenAI rewriter HTTP error: %s", msg, extra={"provider": "openai"})
            return {"error": "api_error", "provider": "openai", "message": msg}
        except Exception as e:
            logger.error("OpenAI rewriter network error: %s", e, extra={"provider": "openai"})
            return {"error": "network_error", "provider": "openai", "message": str(e)}


# -------------------------------------------------------------------------
# Anthropic provider
# -------------------------------------------------------------------------

class AnthropicRewriterProvider:
    """Провайдер Anthropic (claude-haiku-4-5-20251001) — дёшево и быстро."""

    _MODEL = "claude-haiku-4-5-20251001"
    _API_VERSION = "2023-06-01"

    def rewrite(self, text: str, language: str) -> Dict[str, Any]:
        api_key = store.load_settings().get("anthropic_api_key", "").strip()
        if not api_key:
            return {
                "error": "no_api_key",
                "provider": "anthropic",
                "message": "anthropic_api_key not set in settings",
            }

        lang_key = (language or "ru").lower()[:2]
        system_prompt = _CLEANUP_SYSTEM_PROMPTS.get(lang_key, _DEFAULT_SYSTEM_PROMPT)

        payload = json.dumps({
            "model": self._MODEL,
            "max_tokens": min(max(256, len(text.split()) * 4 + 50), 4096),
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": text},
            ],
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": self._API_VERSION,
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(_read_capped(resp).decode("utf-8", "replace"))
                content_blocks = result.get("content", [])
                if not content_blocks:
                    return {"error": "api_error", "provider": "anthropic", "message": "Empty content blocks"}
                text_block = next((b for b in content_blocks if b.get("type") == "text"), None)
                if text_block is None:
                    return {"error": "api_error", "provider": "anthropic", "message": "No text block in response"}
                return {"text": (text_block.get("text") or "").strip()}
        except urllib.error.HTTPError as e:
            msg = _err_body(e)
            logger.error("Anthropic rewriter HTTP error: %s", msg, extra={"provider": "anthropic"})
            return {"error": "api_error", "provider": "anthropic", "message": msg}
        except Exception as e:
            logger.error("Anthropic rewriter network error: %s", e, extra={"provider": "anthropic"})
            return {"error": "network_error", "provider": "anthropic", "message": str(e)}


# -------------------------------------------------------------------------
# Factory
# -------------------------------------------------------------------------

_PROVIDERS: Dict[str, type] = {
    "openai": OpenAIRewriterProvider,
    "anthropic": AnthropicRewriterProvider,
}


def get_cloud_rewriter(provider_name: Optional[str] = None) -> CloudRewriterProvider:
    """Возвращает провайдера по имени (из настроек или аргумента).

    Если имя неизвестно — возвращает OpenAI как умолчание.
    """
    name = (provider_name or store.load_settings().get("cloud_rewriter_provider", "openai")).lower()
    cls = _PROVIDERS.get(name, OpenAIRewriterProvider)
    return cls()


# -------------------------------------------------------------------------
# Top-level convenience function (called from engine.py)
# -------------------------------------------------------------------------

_LENGTH_RATIO_MIN = 0.35
_LENGTH_RATIO_MAX = 3.0


def cloud_rewrite(text: str, language: str) -> Optional[str]:
    """Полирует транскрипт через облачного провайдера.

    PRIVACY CONTRACT: эта функция вызывается ТОЛЬКО когда caller убедился,
    что privacy_mode_enabled=False AND cloud_rewriter_enabled=True.
    Функция сама не проверяет privacy gate — это намеренно (engine.py держит gate).

    Защиты внутри:
    - stub-mode при отсутствии ключа → None.
    - length-ratio guard (< 0.35 или > 3.0 от входа) → None.
    - try/except всё → None (caller сохраняет raw text).

    Returns:
        Полированный текст или None при любой ошибке / guard rejection.
    """
    if not text or not text.strip():
        return None

    try:
        provider_name = store.load_settings().get("cloud_rewriter_provider", "openai")
        provider = get_cloud_rewriter(provider_name)
        result = provider.rewrite(text, language)

        if "error" in result:
            if result["error"] != "no_api_key":
                logger.warning(
                    "Cloud rewrite failed: provider=%s error=%s message=%s",
                    result.get("provider"), result.get("error"), result.get("message", ""),
                    extra={"provider": result.get("provider"), "error": result.get("error")},
                )
            return None

        out = result.get("text", "").strip()
        if not out:
            logger.debug("Cloud rewrite returned empty text, keeping raw")
            return None

        # Length-ratio guard: reject hallucinated / mangled output
        input_len = len(text)
        output_len = len(out)
        if input_len > 0:
            ratio = output_len / input_len
            if ratio < _LENGTH_RATIO_MIN:
                logger.warning(
                    "Cloud rewrite rejected (too short): ratio=%.2f input=%d output=%d",
                    ratio, input_len, output_len,
                )
                return None
            if ratio > _LENGTH_RATIO_MAX:
                logger.warning(
                    "Cloud rewrite rejected (too long): ratio=%.2f input=%d output=%d",
                    ratio, input_len, output_len,
                )
                return None

        return out

    except Exception as e:
        logger.error("cloud_rewrite unexpected error: %s", e, extra={"error": str(e)})
        return None
