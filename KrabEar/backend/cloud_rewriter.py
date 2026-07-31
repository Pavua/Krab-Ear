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
import threading
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Protocol
from urllib.parse import urlparse

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
# SSRF guard for the CUSTOM provider (base_url is user-controlled via
# set_settings). Mirrors backend/lm_studio_lifecycle.py: scheme allowlist +
# a custom opener WITHOUT FileHandler/FTPHandler + a redirect handler that
# re-validates the scheme on every 30x (blocks `302 → file://`).
# localhost/LAN hosts are intentionally allowed — that's the whole point of a
# self-hosted endpoint; we only block dangerous schemes (file/ftp/data).
# -------------------------------------------------------------------------
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _scheme_allowed(url: str) -> bool:
    """True если схема url входит в allowlist (http/https)."""
    return urlparse(url).scheme.lower() in _ALLOWED_SCHEMES


class _SchemeCheckingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Отклоняет 30x-редиректы на запрещённую схему (напр. 302 → file://)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme.lower() not in _ALLOWED_SCHEMES:
            raise urllib.error.HTTPError(
                newurl, code,
                f"redirect to disallowed scheme blocked: {newurl!r}",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_safe_opener() -> urllib.request.OpenerDirector:
    """Opener только с HTTP(S) — намеренно БЕЗ FileHandler/FTPHandler/DataHandler."""
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.HTTPHandler())
    opener.add_handler(urllib.request.HTTPSHandler())
    opener.add_handler(_SchemeCheckingRedirectHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    return opener


# Один разделяемый opener — потокобезопасен для конкурентных open().
_SAFE_OPENER = _build_safe_opener()


def _normalize_endpoint(base_url: str) -> str:
    """Нормализует base_url к полному OpenAI-совместимому chat endpoint.

    Толерантно к тому, как юзер вводит URL:
      http://x:11434                    → http://x:11434/v1/chat/completions
      http://x:11434/v1                 → http://x:11434/v1/chat/completions
      http://x:11434/v1/chat/completions → без изменений
    """
    b = base_url.strip().rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    if b.endswith("/v1"):
        return b + "/chat/completions"
    return b + "/v1/chat/completions"


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
        api_key = _load_settings().get("openai_api_key", "").strip()
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
        api_key = _load_settings().get("anthropic_api_key", "").strip()
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
# Custom / self-hosted OpenAI-compatible provider
# -------------------------------------------------------------------------

class CustomRewriterProvider:
    """Свой OpenAI-совместимый endpoint (self-hosted Ollama/vLLM или no-log провайдер).

    Privacy-CORRECT вариант: транскрипт идёт ТОЛЬКО на указанный юзером сервер.
    API-ключ ОПЦИОНАЛЕН (self-hosted часто без auth) — при пустом ключе
    заголовок Authorization не отправляется. base_url защищён SSRF-гардом.
    """

    def rewrite(self, text: str, language: str) -> Dict[str, Any]:
        s = _load_settings()
        base_url = s.get("cloud_rewriter_base_url", "").strip()
        if not base_url:
            return {
                "error": "no_endpoint",
                "provider": "custom",
                "message": "cloud_rewriter_base_url not set in settings",
            }
        model = s.get("cloud_rewriter_custom_model", "").strip()
        if not model:
            return {
                "error": "no_model",
                "provider": "custom",
                "message": "cloud_rewriter_custom_model not set in settings",
            }
        # SSRF guard: только http/https до любого сетевого вызова.
        if not _scheme_allowed(base_url):
            logger.warning("Custom rewriter: refusing non-http(s) base_url: %r", base_url)
            return {
                "error": "bad_endpoint",
                "provider": "custom",
                "message": "base_url scheme not allowed (http/https only)",
            }

        endpoint = _normalize_endpoint(base_url)

        lang_key = (language or "ru").lower()[:2]
        system_prompt = _CLEANUP_SYSTEM_PROMPTS.get(lang_key, _DEFAULT_SYSTEM_PROMPT)

        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.0,
            "max_tokens": min(max(256, len(text.split()) * 4 + 50), 4096),
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        api_key = s.get("cloud_rewriter_api_key", "").strip()
        if api_key:  # опционально: self-hosted часто без ключа
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")

        try:
            # _SAFE_OPENER (без FileHandler) + повторная проверка схемы на редиректах.
            with _SAFE_OPENER.open(req, timeout=30) as resp:
                result = json.loads(_read_capped(resp).decode("utf-8", "replace"))
                content = result["choices"][0]["message"]["content"]
                return {"text": (content or "").strip()}
        except urllib.error.HTTPError as e:
            msg = _err_body(e)
            logger.error("Custom rewriter HTTP error: %s", msg, extra={"provider": "custom"})
            return {"error": "api_error", "provider": "custom", "message": msg}
        except Exception as e:
            logger.error("Custom rewriter network error: %s", e, extra={"provider": "custom"})
            return {"error": "network_error", "provider": "custom", "message": str(e)}


# -------------------------------------------------------------------------
# Factory
# -------------------------------------------------------------------------

_PROVIDERS: Dict[str, type] = {
    "openai": OpenAIRewriterProvider,
    "anthropic": AnthropicRewriterProvider,
    "custom": CustomRewriterProvider,
}


def get_cloud_rewriter(provider_name: Optional[str] = None) -> CloudRewriterProvider:
    """Возвращает провайдера по имени (из настроек или аргумента).

    Если имя неизвестно — возвращает OpenAI как умолчание.
    """
    name = (provider_name or _load_settings().get("cloud_rewriter_provider", "openai")).lower()
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
        provider_name = _load_settings().get("cloud_rewriter_provider", "openai")
        provider = get_cloud_rewriter(provider_name)
        result = provider.rewrite(text, language)

        if "error" in result:
            if result["error"] not in ("no_api_key", "no_endpoint", "no_model"):
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
