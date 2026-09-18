"""Cloud rewriter fallback — облако ТОЛЬКО когда LM Studio недоступен.

Пустой каталог Studio ≠ недоступен (C1: extractive / сырой текст, без lms load).
Cloud — connection/timeout, если cloud_rewriter_enabled и не privacy.

PRIVACY-SENSITIVE: текст пользователя покидает устройство.
Защиты:
  1. Opt-in: cloud_rewriter_enabled по умолчанию False.
  2. Privacy gate: privacy_mode_enabled=True ВСЕГДА блокирует (engine.py / rewriter).
  3. Audit trail: каждый реальный вызов логируется в PrivacyAuditLogger.
  4. Не сохраняет транскрипт локально — только полирует/резюмирует и возвращает строку.

Архитектура зеркалирует backend/cloud_stt.py (Protocol + per-provider + stub + caps).
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol
from urllib.parse import urlparse

from backend.state_store import StateStore
from core.atomic_io import atomic_write_text
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
# F2: месячный spend-cap облачного фоллбэка summary (D3-узко)
# -------------------------------------------------------------------------
# Счётчик трат: <data_dir>/cloud_spend.json, формат {"YYYY-MM": usd}.
# Только цифры — ни текстов транскриптов, ни API-ключей здесь нет.
_CLOUD_SPEND_FILENAME = "cloud_spend.json"
# F2b: сериализация read-modify-write резерва (одного процесса достаточно).
_SPEND_LOCK = threading.Lock()
_MONTH_KEY_RE = re.compile(r"^\d{4}-\d{2}$")

# Тарифы USD за 1M токенов: {(provider, model): (in_usd, out_usd)}.
# Токены провайдер в ответе не возвращает — считаем оценку len/4.
# gpt-4o-mini — известный тариф; приближённо, сверить по первому счёту владельца.
_KNOWN_SUMMARIZE_RATES: Dict[tuple, tuple] = {
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
}
# Неизвестная модель/провайдер — консервативный blended $1.00/1M: cap
# срабатывает раньше (fail-closed), а не позже.
_UNKNOWN_SUMMARIZE_RATE = (1.0, 1.0)
# self-hosted / custom endpoint: трат у провайдера нет.
_CUSTOM_SUMMARIZE_RATE = (0.0, 0.0)


def current_month_key() -> str:
    """Месяц счётчика в локальном времени: "YYYY-MM"."""
    return datetime.now().strftime("%Y-%m")


def _spend_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / _CLOUD_SPEND_FILENAME


def _read_spend_map(data_dir: Path | str) -> dict:
    """Прочитать весь файл трат; нет файла / битый — пустой dict (не исключение)."""
    path = _spend_path(data_dir)
    try:
        with path.open("r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        pass
    return {}


def _read_spend_map_strict(data_dir: Path | str) -> dict | None:
    """Строгое чтение файла трат: нет файла → {}; битый/невалидный → None.

    None = «spent unknown» → резерв запрещён (fail-closed), а файл НЕ
    перезаписывается. Логи без содержимого (только факт и причина).
    """
    path = _spend_path(data_dir)
    try:
        with path.open("r", encoding="utf-8") as fh:
            loaded = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("cloud spend file unreadable — treating as unknown")
        return None
    if not isinstance(loaded, dict):
        logger.warning("cloud spend file is not a JSON object — treating as unknown")
        return None
    for key, value in loaded.items():
        if not isinstance(key, str) or not _MONTH_KEY_RE.fullmatch(key):
            logger.warning("cloud spend file has invalid month key — treating as unknown")
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            logger.warning("cloud spend file has non-numeric value — treating as unknown")
            return None
        if not math.isfinite(number) or number < 0:
            logger.warning("cloud spend file has invalid value — treating as unknown")
            return None
    return loaded


def read_spend_usd(data_dir: Path | str, month: str) -> float:
    """Сумма трат за месяц; нет файла / битый JSON / мусор — 0.0."""
    try:
        return float(_read_spend_map(data_dir).get(month, 0.0))
    except Exception:
        return 0.0


def reserve_spend_usd(data_dir: Path | str, month: str, est: float, cap: float) -> bool:
    """Зарезервировать est под месячный cap атомарной записью (F2b).

    Под модульным локом: strict-чтение файла → `spent + est <= cap` → запись
    `spent + est` (округление 9 знаков). True только если резерв записан.
    Битый/невалидный файл → deny (fail-closed), файл не перезаписывается.
    non-finite/<=0 cap, non-finite/отрицательный est, невалидный month
    или сбой записи → False.
    """
    try:
        cap_value = float(cap)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(cap_value) or cap_value <= 0:
        return False
    try:
        est_value = float(est)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(est_value) or est_value < 0:
        # F2b-полировка: отрицательный est — не free-pass, а отказ (LOW ревью).
        return False
    if not _MONTH_KEY_RE.fullmatch(str(month)):
        return False
    with _SPEND_LOCK:
        raw = _read_spend_map_strict(data_dir)
        if raw is None:
            return False
        try:
            spent = float(raw.get(month, 0.0))
        except (TypeError, ValueError):
            return False
        if not spend_allowed(cap_value, spent, est_value):
            return False
        raw[month] = round(spent + est_value, 9)
        try:
            atomic_write_text(_spend_path(data_dir), json.dumps(raw))
        except Exception:
            logger.warning("cloud spend reserve write failed — failing closed", exc_info=True)
            return False
    return True


def add_spend_usd(data_dir: Path | str, month: str, usd: float) -> None:
    """Добавить трату за месяц (дельта может быть отрицательной) атомарно.

    В файле — только числа (месяц-ключ и USD-значение), округление 9 знаков.
    Итог клампится в 0.0 (release/reconcile не уводят счётчик в минус).
    Битый/невалидный файл → лог + no-op, история НЕ перезаписывается.
    """
    try:
        amount = float(usd)
    except (TypeError, ValueError):
        logger.warning("cloud spend add: non-numeric delta ignored")
        return
    if not math.isfinite(amount):
        logger.warning("cloud spend add: non-finite delta ignored")
        return
    if not _MONTH_KEY_RE.fullmatch(str(month)):
        logger.warning("cloud spend add: invalid month key ignored")
        return
    with _SPEND_LOCK:
        raw = _read_spend_map_strict(data_dir)
        if raw is None:
            logger.warning("cloud spend file invalid — add skipped (not overwritten)")
            return
        try:
            prev = float(raw.get(month, 0.0))
        except (TypeError, ValueError):
            logger.warning("cloud spend entry invalid — add skipped")
            return
        raw[month] = max(0.0, round(prev + amount, 9))
        try:
            atomic_write_text(_spend_path(data_dir), json.dumps(raw))
        except Exception:
            logger.warning("cloud spend add write failed", exc_info=True)


def estimate_summarize_usd(
    in_text: str,
    out_text: str,
    provider: str,
    model: str,
) -> float:
    """Оценка стоимости вызова summary по тарифной таблице.

    Точный usage провайдер не возвращает → токены ≈ len/4. custom/self-hosted
    тариф 0.0; неизвестная пара (provider, model) — консервативный blended.
    """
    provider_key = str(provider or "").strip().lower()
    model_key = str(model or "").strip()
    if provider_key == "custom":
        in_rate, out_rate = _CUSTOM_SUMMARIZE_RATE
    else:
        in_rate, out_rate = _KNOWN_SUMMARIZE_RATES.get(
            (provider_key, model_key), _UNKNOWN_SUMMARIZE_RATE
        )
    in_tokens = max(0, len(in_text or "")) / 4.0
    out_tokens = max(0, len(out_text or "")) / 4.0
    return round((in_tokens * in_rate + out_tokens * out_rate) / 1_000_000, 6)


def spend_allowed(cap: float, spent: float, est: float) -> bool:
    """Месячный cap: `cap <= 0` — запрещено всё; иначе spent + est <= cap.

    Fail-closed на любой нечисловой вход.
    """
    try:
        cap_value = float(cap)
    except (TypeError, ValueError):
        return False
    if cap_value <= 0:
        return False
    try:
        return float(spent) + float(est) <= cap_value
    except (TypeError, ValueError):
        return False


# -------------------------------------------------------------------------
# Protocol
# -------------------------------------------------------------------------

class CloudRewriterProvider(Protocol):
    """Интерфейс облачного провайдера для полировки транскрипта."""

    def rewrite(self, text: str, language: str, system_prompt: Optional[str] = None) -> Dict[str, Any]:
        """Полирует транскрипт (или выполняет task из system_prompt).

        Возвращает:
            {"text": <polished>}  — успех.
            {"error": "no_api_key"|"api_error"|"network_error", "provider": str, "message": str}
        """
        ...


# -------------------------------------------------------------------------
# OpenAI provider
# -------------------------------------------------------------------------

class OpenAIRewriterProvider:
    """Провайдер OpenAI — модель берётся из настроек (дефолт gpt-4o-mini)."""

    _MODEL = "gpt-4o-mini"          # дефолт и фоллбэк, если настройка пуста
    _MODEL_SETTING = "cloud_rewriter_openai_model"

    def _model_name(self) -> str:
        """Имя модели из настроек; пустая строка → дефолт.

        Пустое значение нельзя отправлять в запрос: провайдер ответит 400, а
        пользователь увидит невнятную ошибку вместо работающего рерайта.
        """
        raw = str((_load_settings() or {}).get(self._MODEL_SETTING, "") or "").strip()
        return raw or self._MODEL

    def rewrite(self, text: str, language: str, system_prompt: Optional[str] = None) -> Dict[str, Any]:
        api_key = _load_settings().get("openai_api_key", "").strip()
        if not api_key:
            return {
                "error": "no_api_key",
                "provider": "openai",
                "message": "openai_api_key not set in settings",
            }

        lang_key = (language or "ru").lower()[:2]
        system_prompt = system_prompt or _CLEANUP_SYSTEM_PROMPTS.get(lang_key, _DEFAULT_SYSTEM_PROMPT)

        payload = json.dumps({
            "model": self._model_name(),
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
    """Провайдер Anthropic — модель берётся из настроек (дефолт claude-haiku)."""

    _MODEL = "claude-haiku-4-5-20251001"   # дефолт и фоллбэк
    _MODEL_SETTING = "cloud_rewriter_anthropic_model"

    def _model_name(self) -> str:
        """См. OpenAIRewriterProvider._model_name."""
        raw = str((_load_settings() or {}).get(self._MODEL_SETTING, "") or "").strip()
        return raw or self._MODEL
    _API_VERSION = "2023-06-01"

    def rewrite(self, text: str, language: str, system_prompt: Optional[str] = None) -> Dict[str, Any]:
        api_key = _load_settings().get("anthropic_api_key", "").strip()
        if not api_key:
            return {
                "error": "no_api_key",
                "provider": "anthropic",
                "message": "anthropic_api_key not set in settings",
            }

        lang_key = (language or "ru").lower()[:2]
        system_prompt = system_prompt or _CLEANUP_SYSTEM_PROMPTS.get(lang_key, _DEFAULT_SYSTEM_PROMPT)

        payload = json.dumps({
            "model": self._model_name(),
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

    def rewrite(self, text: str, language: str, system_prompt: Optional[str] = None) -> Dict[str, Any]:
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
        system_prompt = system_prompt or _CLEANUP_SYSTEM_PROMPTS.get(lang_key, _DEFAULT_SYSTEM_PROMPT)

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


def cloud_summarize(text: str, max_sentences: int = 3) -> Optional[str]:
    """Краткое резюме через того же провайдера, что cloud_rewrite.

    PRIVACY CONTRACT: caller обязан проверить privacy_mode_enabled=False
    AND cloud_rewriter_enabled=True. Сама функция gate не держит.

    Min-ratio рерайта (0.35) здесь НЕ применяется: summary короче входа.
    Выход длиннее входа отвергается (это уже не резюме).
    """
    if not text or not text.strip():
        return None
    try:
        n = max(1, min(int(max_sentences or 3), 8))
    except (TypeError, ValueError):
        n = 3
    prompt = (
        f"Сделай краткое summary ({n} предложения) этого разговора/диктовки. "
        "Верни ТОЛЬКО summary. Без пояснений. Без кавычек. Без префиксов."
    )
    try:
        provider_name = _load_settings().get("cloud_rewriter_provider", "openai")
        provider = get_cloud_rewriter(provider_name)
        result = provider.rewrite(text, "ru", system_prompt=prompt)
        if "error" in result:
            if result["error"] not in ("no_api_key", "no_endpoint", "no_model"):
                logger.warning(
                    "Cloud summarize failed: provider=%s error=%s message=%s",
                    result.get("provider"), result.get("error"), result.get("message", ""),
                    extra={"provider": result.get("provider"), "error": result.get("error")},
                )
            return None
        out = (result.get("text") or "").strip()
        if not out:
            return None
        if len(text) > 0 and len(out) > len(text):
            logger.warning(
                "Cloud summarize rejected (longer than input): in=%d out=%d",
                len(text), len(out),
            )
            return None
        return out
    except Exception as e:
        logger.error("cloud_summarize unexpected error: %s", e, extra={"error": str(e)})
        return None
