"""REST API сервер для Krab Ear на базе Flask + flask-smorest.

Обеспечивает транскрибацию через HTTP-запросы, мониторинг здоровья и метрик.
OpenAPI 3.0 документация доступна по адресу /api/docs.
"""

import atexit
import concurrent.futures
import hmac
import json
import math
import os
import queue
import threading
import time
import uuid
import logging
import functools
from dataclasses import dataclass as _dataclass
from datetime import datetime, timezone
from typing import Any as _Any, Protocol as _Protocol
from flask import Flask, Response, request, jsonify, stream_with_context, g, current_app, has_app_context
from flask_smorest import Api, Blueprint
from flask_sock import Sock
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS
from marshmallow import Schema, fields as ma_fields
from werkzeug.utils import secure_filename

from core.config import settings
from core.engine import AudioEngine
from core import stt_budget
# M1: event_bus/sse_stream/metrics (below) are read exclusively through
# _deps() (deps.event_bus / deps.sse_stream / deps.metrics) — the bare names
# stay live module attributes for _ModuleGlobalsDeps.__getattr__ to resolve.
from backend.event_bus import bus as event_bus, sse_stream  # noqa: F401
from backend.rest_auth import RestAuth
from backend.service import BackendService
from backend.state_store import StateStore
from backend.transcriber import Transcriber
from backend.metrics_collector import metrics  # noqa: F401
from backend.api_versioning import api_version_header, get_api_info
from backend.translator import Translator
from backend.live_subs_service import LiveSubsService
from backend.cloud_stt import get_cloud_stt_provider, MAX_CLOUD_AUDIO_BYTES
from backend.tts_service import TTSService

# Настройка логирования
# W10: единая настройка — время в строке + глушение дублирующего access-лога
# werkzeug (см. backend/rest_log_config.py). Раньше здесь был голый
# basicConfig(level=INFO): строки шли без времени, а werkzeug писал вторую
# строку на каждый запрос.
from backend.rest_log_config import configure_rest_logging  # noqa: E402

configure_rest_logging()
logger = logging.getLogger("KrabEar.REST")

# ---------------------------------------------------------------------------
# M1 (спека 2026-07-16-m-series-rest-merge-design §3): DI-зависимости фабрики.
#
# Хендлеры читают коллабораторов ТОЛЬКО через _deps():
#   - внутри app-контекста — из app.config["REST_DEPS"] (у каждого app свои);
#   - вне контекста (прямой вызов хендлера из тестов) — module-глобалы,
#     как и до фабрики.
# _ModuleGlobalsDeps читает module-атрибуты ЖИВЬЁМ при каждом обращении —
# это контракт для patch.object(rest_server, "store", ...) в 12 тест-файлах.
# ---------------------------------------------------------------------------


class RestDeps(_Protocol):
    engine: _Any
    store: _Any
    transcriber: _Any
    translator: _Any
    tts_service: _Any
    metrics: _Any
    event_bus: _Any
    sse_stream: _Any


@_dataclass
class StaticDeps:
    """Используется тестами M1 для явной сборки набора deps.

    Волна M2 пошла другим путём — подменой module-level глобалов
    (см. ``adopt_external_singletons`` ниже), а не сборкой ``StaticDeps`` из
    коллабораторов BackendService. Класс остаётся живым ради тестов M1,
    которые собирают deps явно (без подмены глобалов).
    """
    engine: _Any
    store: _Any
    transcriber: _Any
    translator: _Any
    tts_service: _Any
    metrics: _Any
    event_bus: _Any
    sse_stream: _Any


class _ModuleGlobalsDeps:
    """Standalone-путь: живое чтение module-атрибутов (имена совпадают 1:1).

    Читает через globals() — ФИКСИРОВАННЫЙ словарь namespace ЭТОГО модуля,
    захваченный при определении класса — а НЕ через sys.modules[__name__]
    (переоценивается при КАЖДОМ обращении). Разница критична при reload:
    сиблинг-тест-файл в том же чанке может `sys.modules.pop("backend.rest_server")`
    + заново `import` (см. CLAUDE.md "rest_server module-level store chunk
    pollution" / reload-вариант), подменяя запись в sys.modules на НОВЫЙ
    объект модуля. Обращение через sys.modules[__name__] в этот момент тихо
    съезжает на НОВЫЙ модуль, а все patch.object(rest_server, "store", ...)
    в тестах остаются приколоты к СТАРОМУ объекту — привет из reload-класса
    багов. globals() всегда возвращает namespace ИМЕННО ЭТОГО экземпляра
    модуля, то есть воспроизводит поведение голого module-level имени
    (`store`, `engine`, ...) до M1-свипа — оно тоже читалось через
    __globals__ функции, а не через sys.modules.
    """

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None


_MODULE_DEPS = _ModuleGlobalsDeps()


def _deps() -> "RestDeps":
    if has_app_context():
        d = current_app.config.get("REST_DEPS")
        if d is not None:
            return d
    return _MODULE_DEPS


_ADOPTABLE_NAMES = ("engine", "store", "transcriber", "translator", "tts_service")


def adopt_external_singletons(
    *,
    engine,
    store,
    transcriber,
    translator,
    tts_service,
) -> None:
    """Подменяет standalone-комплект ссылками на объекты владельца процесса (M2).

    Вызывается BackendService'ом, когда REST поднимается ВНУТРИ backend-процесса:
    импорт этого модуля уже создал собственный комплект, и без подмены в одном
    процессе жили бы два AudioEngine/StateStore — ровно тот дубль тяжёлого
    состояния, ради устранения которого затевалась серия M.

    Почему подмена, а не ленивое создание: 20 тест-файлов патчат этот модуль
    либо через конструкторы вокруг импорта, либо через patch.object на
    module-атрибут. Отложенное создание ломает оба способа (объект рождается
    уже вне патч-контекста), поэтому комплект по-прежнему создаётся на импорте,
    а меняется только то, КУДА указывают имена.

    Аргументы только именованные: пять однотипных объектов подряд слишком легко
    перепутать местами, а перепутанные store и transcriber всплыли бы не здесь,
    а в обработчике запроса.
    """
    incoming = {
        "engine": engine,
        "store": store,
        "transcriber": transcriber,
        "translator": translator,
        "tts_service": tts_service,
    }
    missing = [name for name, obj in incoming.items() if obj is None]
    if missing:
        # Fail-fast: молча оставить прежний объект — значит отдать обработчику
        # standalone-состояние при уверенности вызывающего, что подмена прошла.
        raise ValueError(
            "adopt_external_singletons: не переданы зависимости: "
            + ", ".join(sorted(missing))
        )

    g = globals()
    for name in _ADOPTABLE_NAMES:
        g[name] = incoming[name]
    g["_singletons_adopted"] = True

    logger.info(
        "rest_server: standalone-комплект заменён зависимостями владельца "
        "процесса (in-process режим)"
    )


def release_external_singletons() -> None:
    """Парная операция к ``adopt_external_singletons`` (S3/Задача 9, п.3).

    Возвращает module-level имена (``engine``, ``store``, ``transcriber``,
    ``translator``, ``tts_service``) обратно к собственному standalone-
    комплекту REST-модуля и снимает флаг ``_singletons_adopted``. Без этого
    ``_rest_engine_cleanup`` (atexit ниже) при выходе процесса читала бы
    имя ``engine``, указывающее на объект ВЛАДЕЛЬЦА процесса (BackendService)
    — и закрывала бы его GigaAM-адаптер, а не собственный.

    Вызывается ``BackendService.close()`` сразу после ``rest_inprocess.stop()``.
    Идемпотентна и безопасна как no-op, если усыновления не было (рубильник
    выключен, либо сборка упала до ``adopt_external_singletons``).
    """
    g = globals()
    if not g.get("_singletons_adopted"):
        return
    for name, obj in _OWN_SINGLETONS.items():
        g[name] = obj
    g["_singletons_adopted"] = False

    logger.info(
        "rest_server: усыновлённые зависимости освобождены, "
        "module-level имена возвращены к собственному комплекту"
    )


def _base_config() -> dict:
    """Flask config values shared by every create_app() instance (M1)."""
    return {
        "MAX_CONTENT_LENGTH": 500 * 1024 * 1024,  # 500 MB max
        # flask-smorest / OpenAPI 3.0 configuration
        "API_TITLE": "Krab Ear REST API",
        "API_VERSION": "v1",
        "OPENAPI_VERSION": "3.0.3",
        "OPENAPI_URL_PREFIX": "/api",
        "OPENAPI_SWAGGER_UI_PATH": "/docs",
        "OPENAPI_SWAGGER_UI_URL": "https://cdn.jsdelivr.net/npm/swagger-ui-dist/",
    }

# ---------------------------------------------------------------------------
# CORS — разрешает кросс-доменные запросы из браузера.
# Список origins берётся из KRAB_EAR_CORS_ORIGINS.
#
# Default (wave-21 MED fix): "http://127.0.0.1,http://localhost" — явный
# allowlist, закрывающий transcript-exfiltration через любую вкладку браузера.
#
# Атака: браузер на http://evil.test открывает EventSource("http://127.0.0.1:5005/v1/events")
# → при CORS_ORIGINS="*" сервер отвечал ACAO:* → браузер читал SSE-поток с
# живыми транскриптами (simple GET, без credentials, localhost bind не спасает).
#
# F2 MED fix (W1207): when origins == "*", force supports_credentials=False.
# Combining wildcard origin + supports_credentials=True allows any browser
# tab on the same machine to make credentialed cross-origin fetches (cookies,
# auth headers), which defeats the localhost-only binding security posture.
#
# wave-21 MED fix: добавлен _is_origin_allowed() + @_block_cross_origin_reads
# для transcript-bearing эндпоинтов (/v1/events, /ws/events, /v1/vocabulary).
# ---------------------------------------------------------------------------


def _parse_cors_origins(raw: str):
    """Парсит строку origins: "*" → "*", иначе список через запятую."""
    if raw.strip() == "*":
        return "*"
    return [o.strip() for o in raw.split(",") if o.strip()]


_cors_origins = _parse_cors_origins(settings.CORS_ORIGINS)
_cors_credentials = True
if _cors_origins == "*":
    # Wildcard origin + credentials is forbidden by the CORS spec (browsers
    # refuse it) and is a security misconfiguration — force credentials off.
    _cors_credentials = False
    logger.warning(
        "CORS_ORIGINS='*' with supports_credentials=True is unsafe and "
        "violates the CORS spec. Forcing supports_credentials=False. "
        "Set CORS_ORIGINS to an explicit list to enable credentialed requests."
    )


def _init_cors(app):
    """Attach the CORS policy computed above to *app* (M1: per-instance)."""
    CORS(
        app,
        origins=_cors_origins,
        supports_credentials=_cors_credentials,
        allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    )


# ---------------------------------------------------------------------------
# Origin-gate for transcript-bearing routes (wave-21 MED fix)
#
# flask-cors emits ACAO:* when CORS_ORIGINS="*", which allows any page in the
# user's browser to call new EventSource("http://127.0.0.1:5005/v1/events")
# and read live transcripts (simple GET — no preflight, no credentials needed).
# The localhost bind does NOT defend against this because the browser itself
# is running on the same machine.
#
# Guard: if CORS_ORIGINS is literally "*" (misconfigured / explicit opt-in),
# we still check the Origin header on routes that expose transcript data.
# When Origin is present and is NOT in the effective allow-list we return 403.
# Requests without an Origin header (e.g. curl, native clients) are unaffected.
# ---------------------------------------------------------------------------

def _is_origin_allowed(origin: str) -> bool:
    """Return True when *origin* is permitted by the effective CORS config.

    An absent or empty *origin* is always allowed (non-browser callers).
    When CORS_ORIGINS is the literal "*" we still check a fixed localhost
    set so that no browser page can silently read transcript streams.
    """
    if not origin:
        return True
    # Normalize: strip trailing slash so http://localhost/ == http://localhost
    origin = origin.rstrip("/")

    allowed = _cors_origins
    if allowed == "*":
        # Wildcard — only permit actual localhost variants.
        _localhost_set = {
            "http://127.0.0.1",
            "http://localhost",
            "https://127.0.0.1",
            "https://localhost",
        }
        # Also allow port variants of localhost (e.g. http://localhost:3000)
        _localhost_prefixes = (
            "http://127.0.0.1:",
            "http://localhost:",
            "https://127.0.0.1:",
            "https://localhost:",
        )
        return (
            origin in _localhost_set
            or any(origin.startswith(p) for p in _localhost_prefixes)
        )
    # Explicit list — match exactly.
    return origin in allowed


def _block_cross_origin_reads(f):
    """Decorator: reject cross-origin requests to transcript-bearing routes.

    When a browser sends an Origin header that is NOT in the allowlist, return
    403 so no transcript data leaks via simple-GET (EventSource / fetch).
    Requests without Origin (curl, Swift client, local process) pass through.
    """
    @functools.wraps(f)
    def _wrapper(*args, **kwargs):
        origin = request.headers.get("Origin", "")
        if origin and not _is_origin_allowed(origin):
            logger.warning(
                "Blocked cross-origin transcript read from Origin=%r path=%s",
                origin, request.path,
            )
            return jsonify({"error": "cross-origin access denied"}), 403
        return f(*args, **kwargs)
    return _wrapper

# ---------------------------------------------------------------------------
# Rate limiting — flask-limiter с in-memory хранилищем.
# Отключается через KRAB_EAR_RATE_LIMIT_ENABLED=false (тесты/dev).
# ---------------------------------------------------------------------------


def _rate_limit_exceeded_handler(e):
    """Возвращает 429 JSON с retry_after вместо стандартного HTML."""
    retry_after = 60
    try:
        retry_after = math.ceil(e.description.retry_after.total_seconds())
    except Exception:
        pass
    response = jsonify({"error": "rate_limit_exceeded", "retry_after": retry_after})
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


# ---------------------------------------------------------------------------
# M-2 (W809): rate-limit storage backing.
#
# Production note: "memory://" is process-local and transient — limits reset
# on every restart and are NOT shared across workers (e.g. gunicorn multi-
# process). For production deployments set KRAB_EAR_RATE_LIMIT_STORAGE_URI to
# a stable backing store:
#   redis://localhost:6379/0          — Redis (recommended)
#   memcached://localhost:11211       — Memcached
#   file:///var/run/krabear-ratelimit — SQLite via file: URI
# The warning is suppressed when auth is entirely disabled (dev mode).
# ---------------------------------------------------------------------------

_RATE_LIMIT_STORAGE_URI: str = getattr(settings, "RATE_LIMIT_STORAGE_URI", "memory://")

if (
    settings.RATE_LIMIT_ENABLED
    and _RATE_LIMIT_STORAGE_URI == "memory://"
):
    logger.warning(
        "Rate-limit storage is 'memory://' (W809 M-2): limits are process-local "
        "and reset on every restart. In production set "
        "KRAB_EAR_RATE_LIMIT_STORAGE_URI=redis://... (or memcached/file) for "
        "stable, cross-worker enforcement."
    )

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["60 per minute"] if settings.RATE_LIMIT_ENABLED else [],
    storage_uri=_RATE_LIMIT_STORAGE_URI,
    enabled=settings.RATE_LIMIT_ENABLED,
    headers_enabled=True,
)


def _request_entity_too_large_handler(e):
    """Return 413 JSON instead of Flask's default HTML page (W1674 F2 MED fix — W1684).

    Flask returns an HTML error page when MAX_CONTENT_LENGTH is exceeded.
    JSON-only clients (e.g. Swift's JSONSerialization) cannot parse that
    response and crash silently.  This handler ensures all 413 responses
    carry Content-Type: application/json with a machine-readable body.
    """
    max_mb = _rest_mod_max_content_mb()
    response = jsonify({"error": "Файл слишком большой", "max_mb": max_mb})
    response.status_code = 413
    return response


def _rest_mod_max_content_mb() -> int:
    """Return MAX_CONTENT_LENGTH as MB for the 413 error body."""
    try:
        cfg = current_app.config if has_app_context() else app.config
        limit = cfg.get("MAX_CONTENT_LENGTH", 500 * 1024 * 1024)
        return int(limit) // (1024 * 1024)
    except Exception:
        return 500


# WebSocket heartbeat interval (seconds)
_WS_HEARTBEAT_SEC = 30
# How long to block waiting for next event before looping (keep < heartbeat)
_WS_POLL_SEC = 5.0


# ---------------------------------------------------------------------------
# Auth decorator — optional Bearer token.
# Three modes:
#   1. REST_API_AUTH_ENABLED=True  -> RestAuth token store
#   2. REST_API_KEY set (legacy)   -> single-key check
#   3. both disabled               -> pass through
# ---------------------------------------------------------------------------

# Module-level RestAuth singleton (lazy-initialized on first auth request)
_rest_auth = None


def _get_rest_auth():
    """Return or create the module-level RestAuth instance."""
    global _rest_auth
    if _rest_auth is None:
        _rest_auth = RestAuth(data_dir=str(settings.DATA_DIR))
    return _rest_auth


def _log_unauthorized(endpoint):
    logger.warning("Unauthorized REST request to %s from %s",
                   endpoint, request.remote_addr)


def require_api_key(f):
    """Decorator: enforce Bearer token auth when configured."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if getattr(settings, "REST_API_AUTH_ENABLED", False):
            # Mode 1: token-store auth
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                _log_unauthorized(request.path)
                return jsonify({"error": "Missing or invalid Authorization header"}), 401
            raw_token = auth_header[len("Bearer "):]
            meta = _get_rest_auth().verify_token(raw_token)
            if meta is None:
                _log_unauthorized(request.path)
                return jsonify({"error": "Invalid or revoked API token"}), 401
            return f(*args, **kwargs)
        # Mode 2: legacy single-key auth
        api_key = settings.REST_API_KEY
        if api_key:
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                _log_unauthorized(request.path)
                return jsonify({"error": "Missing or invalid Authorization header"}), 401
            token = auth_header[len("Bearer "):]
            try:
                match = hmac.compare_digest(
                    (token or "").encode("utf-8"),
                    (api_key or "").encode("utf-8"),
                )
            except Exception:
                match = False
            if not match:
                _log_unauthorized(request.path)
                return jsonify({"error": "Invalid API key"}), 401
        # Mode 3: auth disabled — pass through
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# EventBridge internal endpoint (spec 2026-07-07-event-bridge-design.md §2.2).
# Loopback-only + bridge-token auth — ВСЕГДА требуется, независимо от
# REST_API_AUTH_ENABLED/REST_API_KEY (require_api_key выше). Fail-closed.
# ---------------------------------------------------------------------------

_event_bridge_token_cache: str | None = None


def _get_event_bridge_token() -> str | None:
    """Ленивый кэшируемый читатель — REST НИКОГДА не создаёт токен, только читает.

    Кэшируется ТОЛЬКО успешный (непустой) результат: если IPC-процесс ещё не
    создал файл (порядок старта процессов произволен), последующие запросы
    продолжают проверять файл заново, а не залипают на None навсегда.
    """
    global _event_bridge_token_cache
    if _event_bridge_token_cache:
        return _event_bridge_token_cache
    from backend.event_bridge import read_bridge_token
    # log_source=True: REST обязан сказать, ОТКУДА читает токен. Путь сюда приходит
    # другим каналом конфигурации, чем у моста (env против CLI-аргумента), и без
    # полного пути в логе расхождение каналов невидимо — обе стороны рапортуют
    # «файл прочитан», просто файлы разные (инцидент 2026-07-12).
    token = read_bridge_token(settings.DATA_DIR, log_source=True)
    if token:
        _event_bridge_token_cache = token
    return _event_bridge_token_cache


def _bridge_tokens_match(supplied: str, expected: str) -> bool:
    """hmac.compare_digest; разная длина / не-ASCII → False, не исключение."""
    try:
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


def _require_loopback_and_bridge_token(f):
    """Декоратор: /internal/event — loopback-only (403) + bridge-токен (401).

    Независим от REST_API_AUTH_ENABLED/REST_API_KEY — этот эндпоинт ВСЕГДА
    требует токен, даже если пользовательский REST auth выключен. Fail-closed:
    любая проверка не пройдена -> f() не вызывается.
    """
    @functools.wraps(f)
    def _wrapper(*args, **kwargs):
        remote_addr = request.remote_addr or ""
        if remote_addr not in ("127.0.0.1", "::1"):
            logger.warning("event_bridge: non-loopback remote_addr=%r отклонён", remote_addr)
            return jsonify({"error": "loopback only"}), 403
        token = _get_event_bridge_token()
        if not token:
            logger.warning("event_bridge: bridge-токен недоступен на REST-стороне")
            return jsonify({"error": "bridge token unavailable"}), 401
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        supplied = auth_header[len("Bearer "):]
        if not _bridge_tokens_match(supplied, token):
            # Dual kickstart: REST успел закэшировать токен до того, как
            # backend перезаписал файл. Один re-read, не вечный 401.
            global _event_bridge_token_cache
            _event_bridge_token_cache = None
            token = _get_event_bridge_token()
            if not token or not _bridge_tokens_match(supplied, token):
                logger.warning("event_bridge: неверный bridge-токен")
                return jsonify({"error": "invalid bridge token"}), 401
        return f(*args, **kwargs)
    return _wrapper


# ---------------------------------------------------------------------------
# F1: Magic byte validation (W1213)
# Extension-only allowlists are trivially bypassed; validate actual file
# signatures so crafted payloads don't reach libsndfile/ffmpeg/mlx-whisper.
# ---------------------------------------------------------------------------

# Maximum audio duration accepted at the REST layer (seconds).  Files longer
# than this would expand to ~2 GB RAM when soundfile.read() decodes PCM — DoS
# vector (W1213 F2).
_MAX_AUDIO_DURATION_SEC = 3600  # 1 hour

# Wall-clock timeout for a single transcription call at the REST layer.
_TRANSCRIBE_TIMEOUT_SEC = 600  # 10 minutes
# W2c: optional per-request override via form-field deadline_sec (VG budget).
# Отсутствует → 600. Мусор / ≤0 / >120 → 400. (0, _DEADLINE_SEC_MIN) → clamp вверх.
#
# 🔴 Находка 3 (финальный гейт волны 2026-08-26): нижняя граница НЕ смеет
# совпадать с stt_budget.MIN_USEFUL_ATTEMPT_SEC — deadline_sec становится
# полным дедлайном запроса (stt_budget_scope(deadline_sec=...)), и к моменту
# первой проверки budget_exhausted() в каскаде (после открытия scope, чтения
# настроек, резолва пути) остаток уже < MIN_USEFUL_ATTEMPT_SEC. Легальное
# входное значение приводило к нулю попыток распознавания — контракт
# принимал число, которое не могло отработать никогда. Запас ×2 берётся от
# ЕДИНСТВЕННОГО источника истины (stt_budget), а не новой магической константой.
_DEADLINE_SEC_MIN = stt_budget.MIN_USEFUL_ATTEMPT_SEC * 2  # 10.0
_DEADLINE_SEC_MAX = 120.0


def _resolve_transcribe_deadline_sec(raw: _Any) -> tuple[float | None, str | None]:
    """Разобрать optional ``deadline_sec``. ``(None, None)`` → дефолт 600."""
    if raw is None:
        return None, None
    text = str(raw).strip()
    if text == "":
        return None, None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None, "invalid deadline_sec"
    if not math.isfinite(value) or value <= 0 or value > _DEADLINE_SEC_MAX:
        return None, "invalid deadline_sec"
    return float(max(_DEADLINE_SEC_MIN, value)), None


# P0 2026-08-16: один native STT в REST-процессе. Каждый POST раньше поднимал
# свой ThreadPoolExecutor — два /v1/stt/transcribe грузили MLX параллельно.
_STT_SINGLEFLIGHT = threading.Lock()


def try_acquire_stt_singleflight(timeout_sec: float) -> bool:
    """Захватить STT-слот. False → 503 stt_busy, transcribe не стартует."""
    return _STT_SINGLEFLIGHT.acquire(timeout=max(0.0, float(timeout_sec)))


def release_stt_singleflight() -> None:
    _STT_SINGLEFLIGHT.release()


# EX_SOFTWARE из sysexits.h. Отдельный код отличает намеренный fail-fast после
# зависшей транскрибации от обычного исключения приложения в launchd/gunicorn.
_TRANSCRIBE_TIMEOUT_EXIT_CODE = 70


def _exit_poisoned_rest_process(exit_code: int) -> None:
    """Немедленно завершает только REST-процесс, не запуская Python-finalize.

    После таймаута MLX-вызов может навсегда удерживать lock в non-daemon потоке.
    Обычный ``sys.exit`` тогда зависнет на финализации, поэтому здесь намеренно
    используется ``os._exit``. Функция выделена отдельно для безопасной подмены
    в тестах; production-вызов не возвращается.

    M2: предпосылка «процесс отдельный» верна ТОЛЬКО в двухпроцессном режиме.
    При REST внутри backend этот вызов убил бы backend целиком — вместе с
    аудио идущей диктовки или встречи, которое живёт в RAM и сохраняется лишь
    через shutdown_handler (тот же класс потери, что живой инцидент
    2026-07-22 с рестартом под запись). Поэтому в слитом процессе
    самоубийство запрещено: остаёмся с отравленным MLX-локом до штатного
    рестарта — деградация STT предпочтительнее потери записи владельца.
    """
    if bool(getattr(settings, "REST_IN_PROCESS_ENABLED", False)):
        logger.error(
            "REST transcribe timeout: MLX-локом, возможно, владеет зависший "
            "поток, но процесс НЕ завершается — REST работает внутри backend, "
            "и выход уничтожил бы идущую запись. Требуется штатный рестарт "
            "backend (scripts/safe_backend_restart.command).",
        )
        return
    try:
        from core.mlx_whisper_session import kill_mlx_whisper_session

        kill_mlx_whisper_session()
    except Exception:
        logger.debug("REST timeout: mlx_whisper worker kill failed", exc_info=True)
    os._exit(exit_code)


def _arm_timeout_exit(response: Response, temp_path: _Any) -> Response:
    """Ставит очистку upload и fail-fast на момент закрытия готового 504-ответа.

    ``call_on_close`` вызывается WSGI-сервером после выдачи тела ответа. До этой
    границы временный файл остаётся на месте и зависший decoder не получает путь,
    удалённый из-под него. После закрытия ответа файл удаляется непосредственно
    перед невозвратным завершением poisoned REST-процесса.
    """

    def _cleanup_and_exit() -> None:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(
                "Не удалось удалить временный файл перед fail-fast %s: %s",
                temp_path,
                exc,
            )
        finally:
            _exit_poisoned_rest_process(_TRANSCRIBE_TIMEOUT_EXIT_CODE)

    response.call_on_close(_cleanup_and_exit)
    return response


def _validate_audio_magic_bytes(data: bytes) -> bool:
    """Return True if *data* starts with a recognised audio file signature.

    Checks the first 16 bytes against known magic sequences for all formats
    accepted by ALLOWED_EXTENSIONS.  Rejects anything that doesn't match,
    even if the filename extension looks legitimate (W1213 F1).
    """
    if len(data) < 4:
        return False
    # WAV: "RIFF" at 0..3, "WAVE" at 8..11
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WAVE":
        return True
    # MP3: ID3 tag or sync-word frame header (\xff\xfb | \xff\xf3 | \xff\xf2)
    if data[:3] == b"ID3":
        return True
    if len(data) >= 2 and data[0] == 0xFF and data[1] in (0xFB, 0xF3, 0xF2):
        return True
    # FLAC
    if data[:4] == b"fLaC":
        return True
    # OGG (Ogg / Opus)
    if data[:4] == b"OggS":
        return True
    # WebM / Matroska: EBML magic
    if data[:4] == b"\x1A\x45\xDF\xA3":
        return True
    # M4A / AAC / MP4: "ftyp" box at offset 4
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return True
    # AAC ADTS sync word: \xff\xf1 or \xff\xf9
    if len(data) >= 2 and data[0] == 0xFF and data[1] in (0xF1, 0xF9):
        return True
    return False


ALLOWED_EXTENSIONS = {'.wav', '.mp3', '.ogg', '.m4a', '.flac', '.opus', '.webm', '.mp4', '.aac'}

VALID_QUALITY = {"fast", "balanced", "accurate"}
VALID_CLEANUP = {"off", "soft", "strict"}
VALID_DOMAIN = {"casual", "finance", "code", "conversational", "medical"}

# ---------------------------------------------------------------------------
# Marshmallow schemas
# ---------------------------------------------------------------------------


class HealthResponseSchema(Schema):
    """Pydantic schema для ответа /health endpoint — статус сервиса и текущий профиль."""
    status = ma_fields.String(metadata={"description": "Always 'ok' when alive"})
    service = ma_fields.String(metadata={"description": "Service name constant"})
    profile = ma_fields.String(metadata={"description": "Current AudioEngine quality profile"})


class ReadinessComponentsSchema(Schema):
    """Pydantic schema для компонентов готовности — статус STT, диаризации и трансляции."""
    stt = ma_fields.Boolean()
    diarization = ma_fields.Boolean()
    translation = ma_fields.Boolean()


class ReadinessResponseSchema(Schema):
    """Pydantic schema для ответа /readiness endpoint — общая готовность и статусы компонентов."""
    overall_ready = ma_fields.Boolean(metadata={"description": "True when all required components are available"})
    components = ma_fields.Dict(keys=ma_fields.String(), values=ma_fields.Boolean())


class MetricsResponseSchema(Schema):
    latency_p50_ms = ma_fields.Float(load_default=None, dump_default=None)
    latency_p95_ms = ma_fields.Float(load_default=None, dump_default=None)
    latency_p99_ms = ma_fields.Float(load_default=None, dump_default=None)
    confidence_avg = ma_fields.Float(load_default=None, dump_default=None)
    request_count = ma_fields.Integer(load_default=0, dump_default=0)
    error_count = ma_fields.Integer(load_default=0, dump_default=0)
    total_requests = ma_fields.Integer(load_default=0, dump_default=0)
    error_rate = ma_fields.Float(load_default=0, dump_default=0)
    status = ma_fields.String(load_default="waiting_data", dump_default="waiting_data")


class VocabularyResponseSchema(Schema):
    words = ma_fields.List(ma_fields.String(), metadata={"description": "Current vocabulary list"})


class VocabularyPostSchema(Schema):
    words = ma_fields.List(
        ma_fields.String(),
        required=True,
        metadata={"description": "Words to add to vocabulary"},
    )


class VocabularyUpdateResponseSchema(Schema):
    status = ma_fields.String()
    count = ma_fields.Integer(metadata={"description": "Total vocabulary size after merge"})


class ErrorSchema(Schema):
    error = ma_fields.String()


class TranscribeResponseSchema(Schema):
    status = ma_fields.String()
    text = ma_fields.String(load_default="")
    confidence = ma_fields.Float(load_default=0.0)
    duration_ms = ma_fields.Integer(load_default=0)
    engine = ma_fields.String(load_default="mlx-whisper")
    model = ma_fields.String(load_default="")
    language = ma_fields.String(load_default=None, allow_none=True)
    segments = ma_fields.List(ma_fields.Dict(), load_default=list)
    diarization = ma_fields.Dict(load_default=dict)
    history_id = ma_fields.String(load_default="")
    reason = ma_fields.String(load_default=None, allow_none=True)


# ---------------------------------------------------------------------------
# Централизованная инициализация
# Используем один AudioEngine для всех подсистем во избежание перегрузки VRAM.
#
# Wave 69: skip_gigaam_warmup=True предотвращает дублирование GigaAM subprocess.
# REST-сервер проксирует STT через BackendService IPC и не использует GigaAM напрямую.
# Только BackendService (service.py) должен быть owner'ом GigaAM worker'а.
# ---------------------------------------------------------------------------

engine = AudioEngine(skip_gigaam_warmup=True)
store = StateStore(settings.DATA_DIR)
transcriber = Transcriber(engine=engine)
translator = Translator()
tts_service = TTSService()

# S3/Задача 9, п.3: снимок СОБСТВЕННОГО standalone-комплекта модуля — до
# adopt_external_singletons() module-level имена выше указывают именно на
# эти объекты. release_external_singletons() возвращает имена сюда, а
# _rest_engine_cleanup() (atexit ниже) сверяется с флагом _singletons_adopted,
# чтобы не закрыть чужой (усыновлённый) движок.
_OWN_SINGLETONS: dict[str, _Any] = {
    "engine": engine,
    "store": store,
    "transcriber": transcriber,
    "translator": translator,
    "tts_service": tts_service,
}
_singletons_adopted = False


def _propagate_hf_token_to_env() -> None:
    """Make the user's gated-pyannote HF token reach the REST engine's diarization.

    The token is managed in the GUI and persisted by the IPC backend to the CANONICAL
    ~/Library/Application Support/KrabEar/settings.json. The REST process is separate
    and breaks two ways: (a) it may run with a different DATA_DIR, so its own
    store.load_settings() is empty; (b) its launchd plist bakes an HF_TOKEN at install
    time which goes STALE/revoked once the user rotates the token via the GUI. Both
    leave pyannote with an empty or dead token → 401 on the gated repo → diarization
    silently disabled for every REST /v1/stt/transcribe upload.

    Fix: source the token from the canonical GUI settings.json and OVERWRITE the env
    keys when a token is present — the live GUI token is the single source of truth and
    must win over a stale plist-baked env token (setdefault would let the dead token
    survive). Falls back to this process's own store only when canonical is absent.
    A token is set only when one actually exists, so env-only setups are untouched.
    The token value is NEVER logged.
    """
    def _pick(d: dict) -> str:
        return (str(d.get("hf_token", "") or "").strip()
                or str(d.get("stt_gigaam_hf_token", "") or "").strip())

    _token = ""
    # 1. Canonical GUI settings.json — authoritative (raw lowercase keys).
    try:
        from core.config import _SETTINGS_JSON_FILE
        if _SETTINGS_JSON_FILE.exists():
            _canon = json.loads(_SETTINGS_JSON_FILE.read_text())
            if isinstance(_canon, dict):
                _token = _pick(_canon)
    except Exception:
        _token = ""
    # 2. Fallback: this REST process's own store (its DATA_DIR) when canonical absent.
    if not _token:
        try:
            _token = _pick(store.load_settings())
        except Exception:
            _token = ""
    if not _token:
        return
    try:
        for _k in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN"):
            os.environ[_k] = _token  # overwrite: live GUI token beats a stale plist token
    except Exception as exc:  # null-byte in token → ValueError; never log the token
        logger.warning("REST hf_token env propagation failed: %s", type(exc).__name__)


_propagate_hf_token_to_env()


def _rest_engine_cleanup() -> None:
    """atexit cleanup — закрывает GigaAM адаптер если он был создан (graceful shutdown).

    S3/Задача 9, п.3: если ``engine`` усыновлён (``_singletons_adopted``) —
    это объект ВЛАДЕЛЬЦА процесса (BackendService), не собственный REST-движок
    модуля. Закрывать его отсюда значит закрывать чужой GigaAM-адаптер посреди
    жизненного цикла backend'а. Штатный путь (``BackendService.close()``)
    зовёт ``release_external_singletons()`` до этой точки, поэтому здесь флаг
    обычно уже снят; проверка — paranoid guard на случай падения процесса
    ДО штатного close().
    """
    if _singletons_adopted:
        logger.debug(
            "REST atexit: пропуск — engine усыновлён владельцем процесса, "
            "закрывать чужой GigaAM-адаптер здесь нельзя"
        )
        return
    try:
        if engine is not None and engine._router is not None:
            adapter = engine._router.get_gigaam_adapter()
            if adapter is not None and hasattr(adapter, "close"):
                adapter.close()
                logger.info("REST atexit: GigaAM адаптер закрыт")
    except Exception as exc:
        logger.debug("REST atexit cleanup error (non-critical): %s", exc)
    try:
        from core.mlx_whisper_session import close_mlx_whisper_session

        close_mlx_whisper_session()
        logger.info("REST atexit: mlx_whisper worker закрыт")
    except Exception as exc:
        logger.debug("REST atexit mlx_whisper cleanup error (non-critical): %s", exc)


atexit.register(_rest_engine_cleanup)

TEMP_DIR = settings.DATA_DIR / "temp_uploads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Request timing middleware
# ---------------------------------------------------------------------------

# Maximum JSON body size for POST /v1/vocabulary (wave-31 H1 MED fix).
# This fires BEFORE flask-smorest's @v1_blp.arguments() deserializes the
# body, preventing a 500 MB allocation before the 500-word / 100-char cap
# validation even runs.  The global MAX_CONTENT_LENGTH (500 MB) is for audio
# uploads — vocabulary payloads never legitimately exceed a few kilobytes.
_VOCABULARY_POST_MAX_BYTES = 512 * 1024  # 512 KB


def _check_vocabulary_post_size():
    """Reject oversized POST /v1/vocabulary bodies before JSON parse (H1 MED)."""
    if request.path == "/v1/vocabulary" and request.method == "POST":
        cl = request.content_length
        if cl is not None and cl > _VOCABULARY_POST_MAX_BYTES:
            return jsonify({"error": "Request too large"}), 413


def start_timer():
    g._request_start = time.time()
    g._request_id = str(uuid.uuid4())


def log_request(response):
    duration_ms = int((time.time() - g.get('_request_start', time.time())) * 1000)
    request_id = g.get('_request_id', str(uuid.uuid4()))

    # Attach request ID to response header for traceability
    response.headers['X-Request-ID'] = request_id

    # S3/Задача 7b, п.6: /health опрашивается раз в 30с сторожем REST
    # (rest_watchdog.py) — плюс тем же путём Swift/Voice Gateway (см. их
    # общий loopback-бакет в rest_watchdog.py:_default_probe). Здоровый ответ
    # не несёт диагностической ценности, но даёт ~2880 строк access-лога в
    # сутки НАВСЕГДА. X-Request-ID выше проставляется БЕЗУСЛОВНО — решение не
    # логировать касается только строки в access-логе, не трассировки.
    if request.path == "/health" and 200 <= response.status_code < 300:
        return response

    if settings.LOG_FORMAT == "json":
        log_record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "request_id": request_id,
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
            "ip": request.remote_addr or "",
            "content_length": request.content_length or 0,
        }
        logger.info(json.dumps(log_record))
    else:
        logger.info(
            "%s %s %s %dms [%s]",
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            request_id,
        )
    return response


# ---------------------------------------------------------------------------
# Blueprints
# ---------------------------------------------------------------------------

# ── health / metrics (no version prefix) ────────────────────────────────────
monitoring_blp = Blueprint(
    "monitoring",
    __name__,
    url_prefix="",
    description="Liveness, readiness, and metrics endpoints",
)


@monitoring_blp.route("/info", methods=["GET"])
def api_info():
    """Return API version metadata — supported versions, current default, deprecated."""
    return jsonify(get_api_info())


@monitoring_blp.route("/health", methods=["GET"])
@monitoring_blp.response(200, HealthResponseSchema)
@limiter.limit("120 per minute")
def health():
    """Liveness check — verifies the server process is running."""
    deps = _deps()
    return {"status": "ok", "service": "krab-ear", "profile": deps.engine.quality_profile}


@monitoring_blp.route("/metrics", methods=["GET"])
@monitoring_blp.response(200, MetricsResponseSchema)
@require_api_key
def get_metrics():
    """Return aggregated performance and quality metrics."""
    deps = _deps()
    return deps.metrics.get_summary()


# ---------------------------------------------------------------------------
# Prometheus text-format metrics endpoint
# ---------------------------------------------------------------------------

_SERVER_START_TIME = time.time()

# Prometheus histogram bucket boundaries (seconds)
_PROM_LATENCY_BUCKETS_SEC = [0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0]


def _build_prometheus_text(summary: dict) -> str:
    """Формирует строку в формате Prometheus text exposition format 0.0.4."""
    lines = []

    # ── transcriptions_total ─────────────────────────────────────────────────
    total = summary.get("total_requests", 0)
    lines += [
        "# HELP krab_ear_transcriptions_total Total number of transcription requests",
        "# TYPE krab_ear_transcriptions_total counter",
        f"krab_ear_transcriptions_total {total}",
    ]

    # ── errors_total ─────────────────────────────────────────────────────────
    error_rate = summary.get("error_rate", 0.0)
    errors = round(error_rate * total) if total else 0
    lines += [
        "# HELP krab_ear_errors_total Total number of failed transcription requests",
        "# TYPE krab_ear_errors_total counter",
        f"krab_ear_errors_total {errors}",
    ]

    # ── confidence_avg ───────────────────────────────────────────────────────
    stt = summary.get("stt_metrics", {})
    conf_avg = stt.get("confidence", {}).get("avg", 0.0) if stt else 0.0
    lines += [
        "# HELP krab_ear_confidence_avg Average STT confidence score (0-1)",
        "# TYPE krab_ear_confidence_avg gauge",
        f"krab_ear_confidence_avg {conf_avg:.4f}",
    ]

    # ── uptime_seconds ────────────────────────────────────────────────────────
    uptime = time.time() - _SERVER_START_TIME
    lines += [
        "# HELP krab_ear_uptime_seconds Time since the REST server process started",
        "# TYPE krab_ear_uptime_seconds gauge",
        f"krab_ear_uptime_seconds {uptime:.3f}",
    ]

    # ── stt_latency histogram ────────────────────────────────────────────────
    lines += [
        "# HELP krab_ear_stt_latency_seconds STT transcription latency",
        "# TYPE krab_ear_stt_latency_seconds histogram",
    ]

    latency_ms_data = stt.get("latency_ms", {}) if stt else {}
    p50 = latency_ms_data.get("p50")
    p95 = latency_ms_data.get("p95")
    p99 = latency_ms_data.get("p99")
    avg_ms = latency_ms_data.get("avg")

    # Build approximate bucket counts from percentile data.
    # Uses p50 / p95 / p99 as thresholds to infer cumulative counts.
    window = summary.get("window_size", 0)
    for le in _PROM_LATENCY_BUCKETS_SEC:
        le_ms = le * 1000.0
        if window == 0 or p50 is None:
            count = 0
        elif le_ms < (p50 or 0):
            count = int(window * 0.49)
        elif le_ms < (p95 or 0):
            count = int(window * 0.94)
        elif le_ms < (p99 or 0):
            count = int(window * 0.98)
        else:
            count = window
        lines.append(f'krab_ear_stt_latency_seconds_bucket{{le="{le}"}} {count}')

    lines.append(f"krab_ear_stt_latency_seconds_bucket{{le=\"+Inf\"}} {window}")
    sum_sec = (avg_ms / 1000.0 * window) if (avg_ms is not None and window) else 0.0
    lines += [
        f"krab_ear_stt_latency_seconds_sum {sum_sec:.6f}",
        f"krab_ear_stt_latency_seconds_count {window}",
    ]

    return "\n".join(lines) + "\n"


@monitoring_blp.route("/metrics/prometheus", methods=["GET"])
@require_api_key
def get_metrics_prometheus():
    """Return metrics in Prometheus text exposition format 0.0.4.

    No external dependencies — format generated manually.
    Content-Type: text/plain; version=0.0.4; charset=utf-8
    """
    deps = _deps()
    summary = deps.metrics.get_summary()
    body = _build_prometheus_text(summary)
    return Response(
        body,
        status=200,
        mimetype="text/plain; version=0.0.4; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# Health dashboard HTML page
# ---------------------------------------------------------------------------

def _status_dot_color(status: str) -> str:
    """Возвращает CSS-цвет индикатора по строке статуса."""
    green = {"ok", "healthy", "closed"}
    yellow = {"warning", "degraded", "circuit_open", "unavailable", "waiting_data"}
    if status in green:
        return "#4ade80"
    if status in yellow:
        return "#fbbf24"
    return "#f87171"


def _build_dashboard_html() -> str:
    """Строит самодостаточную HTML-страницу дашборда состояния."""
    import platform

    deps = _deps()

    try:
        import psutil  # type: ignore
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        mem_used_gb = round(mem.used / (1024 ** 3), 2)
        mem_total_gb = round(mem.total / (1024 ** 3), 2)
        mem_pct = mem.percent
        disk = psutil.disk_usage("/")
        disk_free_gb = round(disk.free / (1024 ** 3), 2)
        disk_total_gb = round(disk.total / (1024 ** 3), 2)
        sys_info_available = True
    except Exception:
        cpu_pct = mem_used_gb = mem_total_gb = mem_pct = None
        disk_free_gb = disk_total_gb = None
        sys_info_available = False

    # Health checks
    try:
        from backend.health_checker import HealthChecker
        checker = HealthChecker(store=deps.store, transcriber=deps.transcriber)
        health_data = checker.check_all()
    except Exception as exc:
        health_data = {
            "status": "error",
            "checks": {},
            "uptime_sec": round(time.time() - _SERVER_START_TIME, 1),
            "version": "unknown",
            "error": str(exc),
        }

    # Metrics summary
    try:
        metrics_summary = deps.metrics.get_summary()
    except Exception:
        metrics_summary = {}

    uptime_sec = health_data.get("uptime_sec", round(time.time() - _SERVER_START_TIME, 1))
    uptime_str = _format_uptime(uptime_sec)
    overall_status = health_data.get("status", "unknown")
    overall_color = _status_dot_color(overall_status)
    version = health_data.get("version", "unknown")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    py_version = platform.python_version()
    # F3 LOW fix (W1207): mask OS build details — expose only the OS family
    # (darwin / linux / win32) to avoid leaking kernel version and build strings.
    _raw_platform = platform.system().lower()
    if "darwin" in _raw_platform:
        platform_str = "darwin"
    elif "linux" in _raw_platform:
        platform_str = "linux"
    elif "windows" in _raw_platform or "win32" in _raw_platform:
        platform_str = "win32"
    else:
        platform_str = "unknown"

    # Build health check rows
    check_rows = ""
    for name, result in health_data.get("checks", {}).items():
        st = result.get("status", "unknown")
        color = _status_dot_color(st)
        details_parts = []
        for k, v in result.items():
            if k == "status":
                continue
            details_parts.append(f"{k}: {v}")
        details = " | ".join(details_parts) if details_parts else ""
        check_rows += f"""
            <tr>
              <td><span class="dot" style="background:{color}"></span>{name}</td>
              <td><span class="badge" style="background:{color}22;color:{color};border:1px solid {color}44">{st}</span></td>
              <td class="detail">{details}</td>
            </tr>"""

    # Build metrics section
    stt = metrics_summary.get("stt_metrics", {}) or {}
    lat = stt.get("latency_ms", {}) or {}
    conf = stt.get("confidence", {}) or {}
    p50 = lat.get("p50")
    p95 = lat.get("p95")
    p99 = lat.get("p99")
    conf_avg = conf.get("avg")
    total_req = metrics_summary.get("total_requests", 0)
    err_rate = metrics_summary.get("error_rate", 0)

    def _fmt(v, suffix=""):
        return f"{v:.1f}{suffix}" if v is not None else "—"

    metrics_rows = f"""
            <tr><td>Total requests</td><td>{total_req}</td></tr>
            <tr><td>Error rate</td><td>{_fmt(err_rate * 100 if err_rate else None, '%') if err_rate else '0%'}</td></tr>
            <tr><td>Latency p50</td><td>{_fmt(p50, ' ms')}</td></tr>
            <tr><td>Latency p95</td><td>{_fmt(p95, ' ms')}</td></tr>
            <tr><td>Latency p99</td><td>{_fmt(p99, ' ms')}</td></tr>
            <tr><td>Confidence avg</td><td>{_fmt(conf_avg)}</td></tr>"""

    # System info rows
    if sys_info_available:
        sys_rows = f"""
            <tr><td>CPU usage</td><td>{cpu_pct:.1f}%</td></tr>
            <tr><td>Memory</td><td>{mem_used_gb} / {mem_total_gb} GB ({mem_pct:.1f}%)</td></tr>
            <tr><td>Disk free</td><td>{disk_free_gb} / {disk_total_gb} GB</td></tr>"""
    else:
        sys_rows = "<tr><td colspan='2' class='detail'>psutil not available</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="30">
  <title>Krab Ear — Health Dashboard</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #0f1117;
      --surface: #1a1d27;
      --border: #2a2d3a;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent: #6366f1;
    }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
      line-height: 1.6;
      padding: 24px;
      max-width: 1000px;
      margin: 0 auto;
    }}
    header {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 28px;
      flex-wrap: wrap;
    }}
    header h1 {{
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.3px;
    }}
    .overall-badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 12px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 600;
      border: 1px solid;
    }}
    .meta {{
      margin-left: auto;
      color: var(--muted);
      font-size: 12px;
      text-align: right;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 16px;
      margin-bottom: 20px;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px;
    }}
    .card h2 {{
      font-size: 13px;
      font-weight: 600;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 14px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    td {{
      padding: 6px 4px;
      vertical-align: middle;
    }}
    tr + tr td {{
      border-top: 1px solid var(--border);
    }}
    td:last-child {{
      text-align: right;
      color: var(--muted);
    }}
    .dot {{
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      margin-right: 8px;
      vertical-align: middle;
      flex-shrink: 0;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 500;
    }}
    .detail {{
      color: var(--muted);
      font-size: 12px;
    }}
    .full-width {{
      grid-column: 1 / -1;
    }}
    .refresh-note {{
      text-align: center;
      color: var(--muted);
      font-size: 12px;
      margin-top: 24px;
    }}
    @media (max-width: 600px) {{
      body {{ padding: 12px; }}
      header {{ gap: 8px; }}
      .meta {{ margin-left: 0; }}
    }}
  </style>
</head>
<body>
  <header>
    <span style="font-size:28px">🦀</span>
    <h1>Krab Ear — Health Dashboard</h1>
    <span class="overall-badge" style="background:{overall_color}22;color:{overall_color};border-color:{overall_color}44">
      <span class="dot" style="background:{overall_color}"></span>
      {overall_status.upper()}
    </span>
    <div class="meta">
      <div>v{version} · Python {py_version}</div>
      <div>Updated: {now_str}</div>
    </div>
  </header>

  <div class="grid">
    <!-- Uptime + identity -->
    <div class="card">
      <h2>Service</h2>
      <table>
        <tr><td>Uptime</td><td>{uptime_str}</td></tr>
        <tr><td>Platform</td><td class="detail" style="font-size:11px">{platform_str}</td></tr>
        <tr><td>Quality profile</td><td>{deps.engine.quality_profile}</td></tr>
      </table>
    </div>

    <!-- System resources -->
    <div class="card">
      <h2>System Resources</h2>
      <table>{sys_rows}
      </table>
    </div>

    <!-- Metrics -->
    <div class="card">
      <h2>Recent Metrics</h2>
      <table>{metrics_rows}
      </table>
    </div>

    <!-- Health checks — full width -->
    <div class="card full-width">
      <h2>Health Checks</h2>
      <table>
        <colgroup>
          <col style="width:22%">
          <col style="width:18%">
          <col>
        </colgroup>
        <tr style="color:var(--muted);font-size:12px">
          <td>Check</td><td>Status</td><td>Details</td>
        </tr>{check_rows}
      </table>
    </div>
  </div>

  <p class="refresh-note">Auto-refreshes every 30 seconds &mdash; <a href="/health/dashboard" style="color:var(--accent)">refresh now</a></p>
</body>
</html>"""
    return html


def _format_uptime(seconds: float) -> str:
    """Форматирует секунды в читаемую строку вида '2d 3h 14m 05s'."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs:02d}s")
    return " ".join(parts)


@monitoring_blp.route("/health/dashboard", methods=["GET"])
@require_api_key
def health_dashboard():
    """Self-contained HTML health dashboard with auto-refresh every 30 seconds.

    Returns a dark-themed, responsive HTML page showing:
      - Overall service status
      - Health check results (stt_model, llm, disk_space, history_store, audio_devices)
      - System resources (CPU, memory, disk) — requires psutil
      - Recent STT metrics (latency percentiles, confidence, error rate)
      - Uptime and version info

    No external CSS/JS dependencies — everything is inlined.
    """
    html = _build_dashboard_html()
    return Response(html, status=200, mimetype="text/html; charset=utf-8")


# ── v1 APIs ──────────────────────────────────────────────────────────────────
v1_blp = Blueprint(
    "v1",
    __name__,
    url_prefix="/v1",
    description="Krab Ear v1 API — STT, vocabulary, readiness, events",
)


@v1_blp.route("/readiness", methods=["GET"])
@require_api_key
def readiness():
    """Readiness check — verifies all ML components are present and loadable.

    Unlike /health, this performs a real filesystem probe of the HuggingFace
    model cache for each component (STT, diarization, translation).
    Returns 503 when any required component is missing.

    Response 200/503: {"overall_ready": bool, "components": {stt, diarization, translation}}
    """
    report = BackendService._build_readiness_report_static()
    status_code = 200 if report["overall_ready"] else 503
    return jsonify(report), status_code


MAX_VOCABULARY_SIZE = 500
MAX_WORD_LENGTH = 100


@v1_blp.route("/vocabulary", methods=["GET"])
@v1_blp.response(200, VocabularyResponseSchema)
@require_api_key
@_block_cross_origin_reads
def get_vocabulary():
    """Return the current persistent user vocabulary."""
    deps = _deps()
    return {"words": deps.store.load_vocabulary()}


@v1_blp.route("/vocabulary", methods=["POST"])
@v1_blp.arguments(VocabularyPostSchema)
@v1_blp.response(200, VocabularyUpdateResponseSchema)
@require_api_key
def add_vocabulary(args):
    """Add words to the persistent user vocabulary.

    Words are merged into the global vocabulary stored via StateStore.
    Duplicates are silently ignored.
    Maximum vocabulary size: 500 words; maximum word length: 100 characters.
    """
    deps = _deps()
    new_words = args["words"]
    new_words = [str(w).strip()[:MAX_WORD_LENGTH] for w in new_words if str(w).strip()]
    current = deps.store.load_vocabulary()
    updated = list(set(current + new_words))
    if len(updated) > MAX_VOCABULARY_SIZE:
        from flask_smorest import abort
        abort(400, message=f"Vocabulary limit exceeded ({MAX_VOCABULARY_SIZE} words max)")
    deps.store.save_vocabulary(updated)
    return {"status": "ok", "count": len(updated)}


def _load_settings_field(key: str, default):
    """Read a single field from settings.json via the shared StateStore.

    Falls back gracefully to *default* on any read/parse error so that
    callers are never blocked by a corrupt settings file.
    """
    try:
        s = _deps().store.load_settings()
        return s.get(key, default)
    except Exception:
        return default


def _privacy_gate(f):
    """Decorator: short-circuit with 403 privacy_mode BEFORE any auth check runs.

    Must be applied ABOVE (outside) @require_api_key in the decorator stack so
    privacy_mode wins first even when REST auth is also misconfigured — e.g. a
    Voice Gateway client with a stale/wrong KRAB_EAR_REST_API_KEY would otherwise
    get 401 (not 403) while privacy_mode is on, and VG's fallback chain treats a
    plain "not success" 401 as retryable, falling through to cloud STT/TTS and
    leaking privacy-mode-protected audio/text. Mirrors the codebase-wide
    "privacy mode always wins" gate convention (see CLAUDE.md).
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if _load_settings_field("privacy_mode_enabled", False):
            return jsonify({"ok": False, "skipped": "privacy_mode"}), 403
        return f(*args, **kwargs)
    return decorated


@v1_blp.route("/tts/synthesize", methods=["POST"])
@limiter.limit("60 per minute")
@_privacy_gate
@require_api_key
def synthesize_speech():
    """Synthesize speech from text.

    Request: application/json
        - text: str (required)
        - language: "ru" | "en" | "auto" (default: auto)
        - voice: str (optional)

    Notes:
        - Использование реального TTS (Silero/Kokoro) или фоллбэка (macOS say)
          зависит от флага settings.TTS_ENABLED (и settings.TTS_FALLBACK_SAY).

    Returns 403 {"ok": false, "skipped": "privacy_mode"} when IPC privacy
    mode is active (privacy_mode_enabled=true in settings.json) — enforced by
    @_privacy_gate before this body runs (and before auth is even checked).
    """
    deps = _deps()
    req_data = request.get_json(silent=True)
    if not req_data:
        return jsonify({"error": "Invalid or missing JSON"}), 400

    if "text" not in req_data:
        return jsonify({"error": "text is required"}), 400

    params = {
        "text": req_data.get("text", ""),
        "language": req_data.get("language", "auto"),
        "voice": req_data.get("voice"),
    }

    result = deps.tts_service.handle_synthesize_speech(params)

    if not result.get("ok", True):
        return jsonify({"error": result.get("error", "Unknown TTS error")}), 400

    return jsonify({
        "wav_bytes_b64": result.get("wav_bytes_b64", ""),
        "language": result.get("language", "auto"),
        "engine": result.get("engine", "none"),
        "byte_count": result.get("byte_count", 0),
    }), 200


@v1_blp.route("/stt/transcribe", methods=["POST"])
@v1_blp.response(200, TranscribeResponseSchema)
@limiter.limit("60 per minute")
@_privacy_gate
@require_api_key
def transcribe_audio():
    """Transcribe an audio file to text.

    Request: multipart/form-data
        - file: audio file (required). Allowed: .wav .mp3 .ogg .m4a .flac .opus .webm .mp4 .aac
        - quality_profile: fast|balanced|accurate (default: balanced). NOTE
          (W1897): AudioEngine.set_quality_profile() only recognizes
          {"balanced", "max"} — "fast" and "accurate" both silently coerce to
          "balanced" (i.e. MODEL_BALANCED / whisper-large-v3-turbo). There is
          currently no distinct faster model tier; use `diarize=false` below
          to actually cut per-call latency.
        - cleanup_profile: off|soft|strict (default: soft)
        - domain: casual|finance|code|conversational|medical (default: casual)
        - lang_hint: ISO 639-1 code (optional, auto-detected when omitted)
        - vocabulary: comma-separated hint words (optional)
        - chat_id + message_id: idempotency key pair (optional)
        - diarize: "true"/"1"/"yes" or "false"/"0"/"no" (case-insensitive,
          optional). Omitted → None, falls back to settings.DIARIZATION_ENABLED
          (True by default) via Transcriber.transcribe(). Explicit override lets
          a caller skip the pyannote diarization pass per-call — e.g. Voice
          Gateway's "Разговор с AI" conversation flow, where diarization adds
          real latency to every short conversational turn for no benefit
          (single speaker, no need to separate voices).
        - persist_history: "true"/"1"/"yes" (case-insensitive) or "false"/"0"/"no"
          (default: true). When false, the transcription is still performed and
          returned in the response, but is NOT written to history.ndjson. Intended
          for callers streaming ephemeral utterances (e.g. Voice Gateway's
          "Разговор с AI" conversation turns) that should not pollute permanent
          history. privacy_mode_enabled ALWAYS wins over this flag — see below.
        - deadline_sec (W2c, optional): wall-clock бюджет Future.result вместо
          глобальных 600 с. Пусто/нет поля → 600. Число clamp [5, 120].
          Мусор / ≤0 / >120 → 400 ``invalid deadline_sec`` без транскрибации.

    Returns 403 {"ok": false, "skipped": "privacy_mode"} when IPC privacy
    mode is active (privacy_mode_enabled=true in settings.json) — enforced by
    @_privacy_gate before this body runs (and before auth is even checked).
    """
    deps = _deps()
    chat_id = request.form.get("chat_id")
    message_id = request.form.get("message_id")
    persist_history = request.form.get("persist_history", "true").strip().lower() in ("true", "1", "yes")

    # Идемпотентность
    if chat_id and message_id and deps.store.is_idempotent(chat_id, message_id):
        return jsonify({"status": "skipped", "reason": "duplicate"}), 200

    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    # F4: extract extension from ORIGINAL filename BEFORE secure_filename(),
    # which strips non-ASCII characters — "тест.wav" → "wav" (loses the dot).
    original_filename = file.filename or ""
    original_ext = os.path.splitext(original_filename)[1].lower()
    if original_ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {original_ext}"}), 400

    safe_base = secure_filename(original_filename) or "upload"
    # Ensure the safe basename carries the correct extension even for Unicode names.
    if not safe_base.endswith(original_ext):
        safe_base = os.path.splitext(safe_base)[0] + original_ext

    temp_path = TEMP_DIR / f"{uuid.uuid4().hex[:12]}_{safe_base}"
    temp_cleanup_deferred = False

    try:
        # F3 (W1766): file.save перемещён ВНУТРЬ try, чтобы finally-блок гарантированно
        # удалял частичный файл при ошибках записи (ENOSPC и т.п.).
        # Каталог мог исчезнуть после импорта из-за системной очистки или изоляции теста.
        # mkdir остаётся внутри try: ошибки прав/ENOSPC проходят общий обработчик ниже.
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        file.save(str(temp_path))

        # F1: Validate magic bytes before handing the file to any decoder.
        with open(str(temp_path), "rb") as _fh:
            _header = _fh.read(16)
        if not _validate_audio_magic_bytes(_header):
            return jsonify({"error": "File content does not match a recognised audio format"}), 400

        # F2: Reject audio exceeding 1 hour to prevent PCM-expansion OOM DoS.
        try:
            import soundfile as _sf  # type: ignore
            _info = _sf.info(str(temp_path))
            if _info.duration > _MAX_AUDIO_DURATION_SEC:
                return jsonify({
                    "error": f"Audio too long: {_info.duration:.0f}s (max {_MAX_AUDIO_DURATION_SEC}s)"
                }), 400
        except Exception:
            # soundfile.info may not support all containers (e.g. MP3); continue
            # — libsndfile will surface a proper error inside transcribe() if
            # the file is truly unreadable.
            pass

        deps.engine.normalize_audio(str(temp_path))

        quality = request.form.get("quality_profile", "balanced")
        if quality not in VALID_QUALITY:
            return jsonify({"error": f"Invalid quality_profile: {quality}"}), 400
        cleanup = request.form.get("cleanup_profile", "soft")
        if cleanup not in VALID_CLEANUP:
            return jsonify({"error": f"Invalid cleanup_profile: {cleanup}"}), 400
        domain = request.form.get("domain", "casual")
        if domain not in VALID_DOMAIN:
            return jsonify({"error": f"Invalid domain: {domain}"}), 400
        # Accept both "lang_hint" (Krab Ear native) and "language" (Voice Gateway
        # KrabEarSTTEngine sends this key) — cross-project contract drift fix.
        lang_hint = request.form.get("lang_hint") or request.form.get("language") or None

        req_vocab_raw = request.form.get("vocabulary", "")
        req_vocab = [w.strip() for w in req_vocab_raw.split(",") if w.strip()] if req_vocab_raw else []
        full_vocabulary = list(set(deps.store.load_vocabulary() + req_vocab))

        # W1897: diarize form field — explicit per-call override. Omitted (None)
        # preserves prior behavior (Transcriber.transcribe falls back to
        # settings.DIARIZATION_ENABLED). Lets ephemeral-conversation callers
        # (Voice Gateway's "Разговор с AI") skip pyannote diarization overhead
        # per-turn without touching the global setting for other callers.
        _diarize_raw = request.form.get("diarize")
        diarize_override = (
            None
            if _diarize_raw is None
            else _diarize_raw.strip().lower() in ("true", "1", "yes")
        )

        _deadline_sec, _deadline_err = _resolve_transcribe_deadline_sec(
            request.form.get("deadline_sec"),
        )
        if _deadline_err is not None:
            return jsonify({"error": "invalid deadline_sec"}), 400
        _transcribe_timeout_sec = (
            _TRANSCRIBE_TIMEOUT_SEC if _deadline_sec is None else _deadline_sec
        )

        if not try_acquire_stt_singleflight(_transcribe_timeout_sec):
            logger.warning(
                "REST STT singleflight busy after %.1fs for %s",
                _transcribe_timeout_sec,
                safe_base,
            )
            return jsonify({"error": "stt_busy"}), 503

        # F2: ограничиваем весь вызов транскрайбера wall-clock таймаутом.
        # ThreadPoolExecutor создаёт non-daemon worker, поэтому cancel_futures
        # не может остановить уже начавшийся MLX-вызов. На таймауте сначала
        # отдаём 504, а после закрытия ответа завершаем отдельный REST-процесс;
        # launchd/gunicorn поднимет чистый экземпляр без poisoned MLX lock.
        start_ts = time.monotonic()
        _transcribe_path = str(temp_path)
        _transcribe_kwargs = dict(
            quality_profile=quality,
            cleanup_profile=cleanup,
            domain=domain,
            extra_vocabulary=full_vocabulary,
            lang_hint=lang_hint,
            diarize=diarize_override,
        )
        _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _pool_shutdown_nonblocking = False
        # Спека 2026-08-26 §4.1/§6: scope открывается ВНУТРИ worker-треда
        # (call_in_scope) — ContextVar не наследуется тредом пула. Снапшот
        # настроек берётся здесь: engine в REST-процессе создан без
        # settings_get и сам настройки прочитать не может (§4.2).
        try:
            _budget_settings_snapshot = deps.store.load_settings(nowait=True)
        except Exception:
            _budget_settings_snapshot = None
        try:
            _future = _pool.submit(
                stt_budget.call_in_scope,
                deps.transcriber.transcribe,
                _transcribe_path,
                profile=stt_budget.INTERACTIVE,
                deadline_sec=_transcribe_timeout_sec,
                settings_snapshot=_budget_settings_snapshot,
                **_transcribe_kwargs,
            )
            try:
                result = _future.result(timeout=_transcribe_timeout_sec)
            except concurrent.futures.TimeoutError:
                if _future.done():
                    # Сам транскрайбер тоже может завершиться исключением
                    # TimeoutError. Это обычная завершившаяся error-ветка, а не
                    # зависший executor; повторный result() вернёт результат
                    # гонки либо пробросит исходное исключение в общий 500.
                    result = _future.result()
                elif _future.cancel():
                    # Pending Future ещё не получил worker и успешно отменён:
                    # poisoned процесса нет, поэтому обычный 504 безопасен.
                    logger.warning(
                        "Transcription timed out before worker start after %ss for %s",
                        _transcribe_timeout_sec,
                        safe_base,
                    )
                    timeout_response = jsonify({"error": "Transcription timeout"})
                    timeout_response.status_code = 504
                    return timeout_response
                elif _future.done():
                    # Future мог завершиться между первой done-проверкой и
                    # cancel(). Забираем результат гонки без ложного hard-exit.
                    result = _future.result()
                elif _future.running():
                    _pool_shutdown_nonblocking = True
                    # Уже работающую задачу Future.cancel() не прерывает.
                    # Не ждём non-daemon worker: ожидание снова превратило бы
                    # быстрый 504 в зависание Python-finalize.
                    _pool.shutdown(wait=False, cancel_futures=True)
                    logger.error(
                        "Transcription timed out after %ss for %s",
                        _transcribe_timeout_sec,
                        safe_base,
                    )
                    timeout_response = jsonify({"error": "Transcription timeout"})
                    timeout_response.status_code = 504
                    _arm_timeout_exit(timeout_response, temp_path)
                    temp_cleanup_deferred = True
                    return timeout_response
                else:
                    # Для стандартного Future ветка недостижима. Нестандартному
                    # executor не доверяем настолько, чтобы убивать процесс без
                    # доказанного running; возвращаем fail-safe 504 без ожидания.
                    _pool_shutdown_nonblocking = True
                    _pool.shutdown(wait=False, cancel_futures=True)
                    logger.error(
                        "Transcription Future entered an unknown state after %ss for %s",
                        _transcribe_timeout_sec,
                        safe_base,
                    )
                    timeout_response = jsonify({"error": "Transcription timeout"})
                    timeout_response.status_code = 504
                    return timeout_response
        finally:
            try:
                release_stt_singleflight()
            except RuntimeError:
                # release без acquire не должен маскировать таймаут/ошибку STT.
                pass
            if not _pool_shutdown_nonblocking:
                # На success/error/cancelled-pending Future нет работающей
                # задачи, поэтому wait=True гарантирует отсутствие хвоста.
                _pool.shutdown(wait=True)
        elapsed_sec = time.monotonic() - start_ts

        text = result.get("text", "")

        # F3: Respect privacy_mode_enabled — skip history persistence when active.
        # privacy_mode_enabled ALWAYS wins over persist_history (see CLAUDE.md
        # "privacy_mode_enabled ВСЕГДА побеждает"): a caller cannot use
        # persist_history=true to force a save while global privacy mode is on.
        _privacy_mode = _load_settings_field("privacy_mode_enabled", False)
        if _privacy_mode or not persist_history:
            history_item_id = ""
        else:
            history_item = deps.store.add_history_item(
                text=text,
                chat_id=chat_id or "",
                message_id=message_id or "",
                source_text=result.get("raw_text", text),
            )
            history_item_id = history_item.id

        deps.metrics.record(
            latency_ms=result.get("duration_ms", int(elapsed_sec * 1000)),
            confidence=result.get("confidence", 0.0),
        )

        response_payload = {
            "status": "ok",
            "text": text,
            "confidence": result.get("confidence", 0.0),
            "duration_ms": result.get("duration_ms", int(elapsed_sec * 1000)),
            "engine": result.get("engine", "mlx-whisper"),
            # Legacy engine у раннего empty-result исторически содержит reason.
            # Новый ключ не выдумывает имя движка до фактического STT-вызова;
            # при обычном результате он равен реальному result.engine.
            "stt_engine": result.get("stt_engine", result.get("engine", "mlx-whisper")),
            "model": result.get("model", ""),
            "language": result.get("language"),
            "segments": result.get("segments", []),
            "diarization": result.get("diarization", {}),
            "history_id": history_item_id,
        }
        # W-2026-08-19: пустой text скрывал ДВЕ разных причины (тишина vs не
        # смогли распознать — вырожденный вход/VAD-скип), клиент (Voice
        # Gateway) вынужден был гадать по длительности ответа. Причина уже
        # известна внутри (AudioEngine._empty_transcription_result "reason":
        # "empty_audio"/"vad_skip") — пробрасываем её АДДИТИВНО: поле
        # присутствует ТОЛЬКО когда текст пуст и причина известна, чтобы не
        # засорять ответ при нормальном распознавании и не ломать контракт
        # старых клиентов, не ожидающих новый ключ.
        if not text:
            _empty_reason = result.get("reason")
            if _empty_reason:
                response_payload["reason"] = _empty_reason
        return jsonify(response_payload)

    except Exception:
        logger.exception("Ошибка при обработке аудио-запроса")
        deps.metrics.record(0, 0, is_error=True)
        return jsonify({"error": "Internal processing error"}), 500

    finally:
        if not temp_cleanup_deferred and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception as e:
                logger.warning("Не удалось удалить временный файл %s: %s", temp_path, e)


@v1_blp.route("/models", methods=["GET"])
@limiter.limit("60 per minute")
@require_api_key
def list_models():
    """Best-effort catalog of all STT, cloud, and LLM models available to Krab Ear.

    No privacy gate — model names are not user data.

    Response 200:
        {
          "ok": true,
          "stt_engines": [{"name", "display_name", "available": bool,
                           "enabled": bool, "languages": [...], "type": "local"}, ...],
          "cloud_stt": [{"name": "openai"|"deepgram"|"assemblyai",
                         "available": bool, "type": "cloud"}, ...],
          "llm_models": [str, ...],
          "default_stt": str|null,
          "default_llm": str|null
        }

    Each section degrades gracefully to empty on any error — the caller always
    receives HTTP 200 with partial data, never 500.

    Voice Gateway bridge: third pillar alongside POST /v1/stt/transcribe
    and POST /v1/tts/synthesize (Phase 1.4 foundation, 2026-06-18).
    """
    deps = _deps()
    # ------------------------------------------------------------------
    # 1. STT engines via stt_router_factory
    # ------------------------------------------------------------------
    stt_engines = []
    try:
        from core.pipeline.stt_router_factory import build_router as _build_router
        _cur_settings = deps.store.load_settings() or {}
        _router = _build_router(settings_dict=_cur_settings)
        # Canonical language codes we probe per adapter (conservative list).
        _PROBE_LANGS = ("ru", "en", "es", "zh", "ja", "ko", "de", "fr")
        for _adapter in _router._adapters:
            _name = _adapter.model_id
            _display = getattr(_adapter, "display_name", _name)
            _avail = False
            try:
                _avail = bool(_adapter.is_available())
            except Exception:
                pass
            _langs = sorted(
                lang for lang in _PROBE_LANGS
                if _adapter.supports_language(lang)
            )
            # enabled = the adapter made it into the router
            # (build_router only appends adapters whose enabled flag is set)
            stt_engines.append({
                "name": _name,
                "display_name": _display,
                "available": _avail,
                "enabled": True,
                "languages": _langs,
                "type": "local",
            })
    except Exception as exc:
        logger.warning(
            "GET /v1/models: stt_engines enumeration failed — degrading to []",
            extra={"error": str(exc)},
        )

    # ------------------------------------------------------------------
    # 2. Cloud STT providers
    # ------------------------------------------------------------------
    cloud_stt = []
    try:
        _s = deps.store.load_settings() or {}
        _cloud_providers = [
            ("openai", "openai_api_key"),
            ("deepgram", "deepgram_api_key"),
            ("assemblyai", "assemblyai_api_key"),
        ]
        for _pname, _key_field in _cloud_providers:
            _api_key = str(_s.get(_key_field, "")).strip()
            cloud_stt.append({
                "name": _pname,
                "available": bool(_api_key),
                "type": "cloud",
            })
    except Exception as exc:
        logger.warning(
            "GET /v1/models: cloud_stt enumeration failed — degrading to []",
            extra={"error": str(exc)},
        )

    # ------------------------------------------------------------------
    # 3. LLM models from LM Studio /api/v1/models
    # Mirrors llm_ops_service.handle_list_llm_models but uses urllib
    # (already available) — rest_server is a separate process from IPC.
    # ------------------------------------------------------------------
    llm_models = []
    try:
        import re as _re
        import urllib.request as _urlreq
        import urllib.parse as _urlparse
        _s2 = deps.store.load_settings() or {}
        _llm_base = str(_s2.get("llm_base_url", "http://127.0.0.1:1234/v1")).rstrip("/")
        _llm_key = str(_s2.get("llm_api_key", "")).strip()
        from backend.lm_studio_lifecycle import (
            CATALOG_ENDPOINTS,
            parse_lm_studio_catalog,
        )
        # Strip the trailing /v<N> segment: каталог живёт рядом с ним, а не под ним.
        _llm_host = _re.sub(r"/v\d+$", "", _llm_base)
        for _endpoint in CATALOG_ENDPOINTS:
            _llm_url = f"{_llm_host}{_endpoint}"
            # SSRF guard: allow only http/https schemes
            _parsed = _urlparse.urlparse(_llm_url)
            if _parsed.scheme not in ("http", "https"):
                raise ValueError(f"Disallowed LM Studio URL scheme: {_parsed.scheme!r}")
            _req = _urlreq.Request(_llm_url)
            if _llm_key:
                _req.add_header("Authorization", f"Bearer {_llm_key}")
            with _urlreq.urlopen(_req, timeout=5) as _resp:  # noqa: S310
                if _resp.status != 200:
                    continue
                _data = json.loads(
                    _resp.read(512 * 1024).decode("utf-8", errors="replace")
                )
            # Формы каталога разные, ключ идентификатора тоже — разбор общий.
            _entries = parse_lm_studio_catalog(_data)
            if _entries is not None:
                llm_models = sorted(e["id"] for e in _entries)
                break
    except Exception as exc:
        logger.warning(
            "GET /v1/models: llm_models probe failed (LM Studio may be off)"
            " — degrading to []",
            extra={"error": str(exc)},
        )

    # ------------------------------------------------------------------
    # 4. Defaults from runtime settings (with static fallbacks)
    # ------------------------------------------------------------------
    default_stt = None
    default_llm = None
    try:
        _s3 = deps.store.load_settings() or {}
        _dstt = _s3.get("stt_ru_primary_model") or "mlx-community/whisper-large-v3-mlx"
        default_stt = str(_dstt).strip() or None
        _dllm = _s3.get("llm_model") or "gemma-4-e4b-it-mlx"
        default_llm = str(_dllm).strip() or None
    except Exception as exc:
        logger.warning(
            "GET /v1/models: default_stt/default_llm read failed",
            extra={"error": str(exc)},
        )

    return jsonify({
        "ok": True,
        "stt_engines": stt_engines,
        "cloud_stt": cloud_stt,
        "llm_models": llm_models,
        "default_stt": default_stt,
        "default_llm": default_llm,
    }), 200


@v1_blp.route("/events", methods=["GET"])
@require_api_key
@_block_cross_origin_reads
def events_stream():
    """Subscribe to real-time STT pipeline events via Server-Sent Events (SSE).

    Opens a long-lived GET connection that emits newline-delimited SSE frames.
    A keepalive comment (`: ping`) is emitted ~every 15 seconds when idle.

    Authentication (W809 M-4):
        When REST_API_AUTH_ENABLED=true or REST_API_KEY is set, a valid
        Bearer token is required:
          Authorization: Bearer <token>
        When auth is disabled (local dev) the endpoint is unauthenticated.

    Query params:
        filter — optional comma-separated list of event types to receive.
                 Example: ``?filter=stt.final,live_subs.result``
                 When omitted, all events are delivered (backwards-compatible).

    Event types:

        event: stt.final  →  {history_id, text, confidence, duration_sec, language}

        event: stt.failed →  {reason, duration_sec}
    """
    deps = _deps()
    event_filter = request.args.get("filter")
    return Response(
        stream_with_context(deps.sse_stream(deps.event_bus, event_filter=event_filter)),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@monitoring_blp.route("/internal/event", methods=["POST"])
@limiter.limit("600 per minute")  # щедрый лимит — легитимные батчи моста могут быть частыми
@_require_loopback_and_bridge_token
def internal_event():
    """Приём батча событий от EventBridge (IPC-процесс) -> re-emit в REST-шину.

    Body: {"events": [{"type": str, "ts": str, "data": dict}, ...]}
    Невалидный элемент — скип + WARN, не 500 (один плохой элемент не должен
    ронять весь батч).
    """
    deps = _deps()
    body = request.get_json(silent=True) or {}
    events = body.get("events")
    if not isinstance(events, list):
        return jsonify({"error": "events must be a list"}), 400

    accepted = 0
    skipped = 0
    for env in events:
        if not isinstance(env, dict):
            skipped += 1
            continue
        etype = env.get("type")
        ts = env.get("ts")
        data = env.get("data")
        if not isinstance(etype, str) or not isinstance(ts, str) or not isinstance(data, dict):
            skipped += 1
            logger.warning("event_bridge: malformed envelope skipped: %r", env)
            continue
        try:
            deps.event_bus.emit_envelope({"type": etype, "ts": ts, "data": data, "origin": "ipc"})
            accepted += 1
        except Exception:
            skipped += 1
            logger.warning("event_bridge: emit_envelope failed for type=%s", etype, exc_info=True)

    return jsonify({"ok": True, "accepted": accepted, "skipped": skipped}), 200


# ---------------------------------------------------------------------------
# Compatibility shim: keep /v1/vocabulary working for GET+POST on the same
# path without duplicate-route errors from flask-smorest
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# V2 catch-all — APIVersion.V2 is listed in SUPPORTED_VERSIONS but no V2
# routes are implemented yet.  Without this handler Flask returns 404, which
# misleads clients into thinking "this path simply doesn't exist".  501 is
# the correct HTTP status for "the server recognises the request but has not
# implemented it".  (W1350 F3 MED fix — wave1357)
# ---------------------------------------------------------------------------

_V2_PLANNED_ROUTES = [
    "GET  /v2/stt/transcribe",
    "POST /v2/stt/transcribe",
    "GET  /v2/vocabulary",
    "POST /v2/vocabulary",
    "GET  /v2/readiness",
    "GET  /v2/events",
]


# ---------------------------------------------------------------------------
# API v2 stub — 501 Not Implemented
#
# APIVersion.V2 is listed in the enum for future expansion, but no v2
# Blueprint or routes are registered yet.  Without this catch-all, Flask
# would return 404 NOT FOUND which incorrectly implies the resource might
# exist elsewhere.  501 is the correct response: the server understands the
# request but has not implemented the requested API version.
# ---------------------------------------------------------------------------

@require_api_key
@limiter.limit("60 per minute")
def v2_not_implemented(p):
    """Catch-all for /v2/* — returns 501 until v2 is implemented.

    Auth + rate-limit enforced (W1674 F1 MED fix — W1684): the /v2 stub
    carries the same auth and rate-limit posture as production v1 routes.
    Unauthenticated callers receive 401; excessive callers receive 429.
    """
    response = jsonify({
        "error": "V2 API not yet implemented",  # W1357: restored uppercase V2 for test contract
        "message": (
            "APIVersion.V2 is declared as supported but no V2 routes have "
            "been released.  Use /v1/* routes until V2 ships."
        ),
        "planned_routes": _V2_PLANNED_ROUTES,
        "use_instead": "/v1/",
        "supported_versions": ["v1"],  # W1684: keep for compatibility
    })
    response.status_code = 501
    response.headers["X-API-Version"] = "v2"
    return response


# ---------------------------------------------------------------------------
# WebSocket live-streaming endpoint
# ---------------------------------------------------------------------------

def _handle_ws_connection(ws, bus, type_filter=None):
    """Внутренняя логика WebSocket-соединения — выделена для unit-тестирования.

    Args:
        ws: объект WebSocket с методом .send(str).
        bus: EventBus для подписки.
        type_filter: set строк с допустимыми типами событий, или None — всё.
    """
    q = bus.subscribe()
    logger.info("WS /ws/events: клиент подключился, фильтр=%s", type_filter or "все")

    last_ping = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now - last_ping >= _WS_HEARTBEAT_SEC:
                try:
                    ws.send('{"type":"ping"}')
                except Exception:
                    break
                last_ping = time.monotonic()

            try:
                event = q.get(timeout=_WS_POLL_SEC)
            except queue.Empty:
                continue

            if event is None:
                # Сигнал завершения от сервера
                break

            if type_filter and event.get("type") not in type_filter:
                continue

            try:
                ws.send(json.dumps(event))
            except Exception:
                break
    except Exception:
        logger.debug("WS /ws/events: соединение прервано")
    finally:
        bus.unsubscribe(q)
        logger.info("WS /ws/events: клиент отключился")


def _ws_check_auth(ws) -> bool:
    """Enforce Bearer token auth for WebSocket connections (W809 M-4).

    Flask-Sock routes execute in the request context, so request.headers and
    request.args are available.  Auth is checked before the upgrade completes —
    if it fails we send a close frame and return False so the caller exits.

    Auth lookup order (mirrors require_api_key):
      1. Authorization: Bearer <token>  header
      2. ?api_key=<token>               query param (SSE-browser fallback)

    Returns True when auth passes or auth is disabled.
    """
    def _raw_token() -> str:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            return auth_hdr[len("Bearer "):]
        # Query-param fallback — browsers cannot set custom headers in WS
        return request.args.get("api_key", "")

    if getattr(settings, "REST_API_AUTH_ENABLED", False):
        raw = _raw_token()
        if not raw or _get_rest_auth().verify_token(raw) is None:
            _log_unauthorized("/ws/events")
            try:
                ws.send(json.dumps({"error": "unauthorized", "code": 4401}))
                ws.close(message=b"Unauthorized")
            except Exception:
                pass
            return False
        return True

    api_key = settings.REST_API_KEY
    if api_key:
        raw = _raw_token()
        try:
            ok = hmac.compare_digest(
                (raw or "").encode("utf-8"),
                (api_key or "").encode("utf-8"),
            )
        except Exception:
            ok = False
        if not ok:
            _log_unauthorized("/ws/events")
            try:
                ws.send(json.dumps({"error": "unauthorized", "code": 4401}))
                ws.close(message=b"Unauthorized")
            except Exception:
                pass
            return False

    return True


def ws_events(ws):
    """WebSocket endpoint для стриминга событий транскрибации в реальном времени.

    Подписывается на EventBus и пересылает все события клиенту в формате JSON.

    Authentication (W809 M-4):
        When REST_API_AUTH_ENABLED=true or REST_API_KEY is set, a valid
        Bearer token is required.  Two accepted forms:
          - Header:      Authorization: Bearer <token>
          - Query param: /ws/events?api_key=<token>  (browser WebSocket fallback)
        When auth is disabled the endpoint is open (local dev only).

    Query params:
        types — опциональный фильтр по типам событий через запятую.
                Пример: /ws/events?types=stt.final,translation
                Если не задан — отправляются все события.

    Протокол:
        Server → Client: JSON-строка {type, ts, data}
        Server → Client: {"type": "ping"} каждые 30 секунд (heartbeat)
        Client → Server: любые входящие данные игнорируются
        Server → Client: {"error": "unauthorized", "code": 4401} + close on auth failure
        Server → Client: {"error": "cross-origin access denied"} + close on Origin check failure
    """
    # wave-21: Origin gate — block cross-origin browser tabs from reading transcripts.
    origin = request.headers.get("Origin", "")
    if origin and not _is_origin_allowed(origin):
        logger.warning(
            "Blocked cross-origin WS transcript read from Origin=%r", origin
        )
        try:
            ws.send(json.dumps({"error": "cross-origin access denied", "code": 4403}))
            ws.close(message=b"cross-origin access denied")
        except Exception:
            pass
        return
    if not _ws_check_auth(ws):
        return
    raw_types = request.args.get("types", "")
    type_filter = {t.strip() for t in raw_types.split(",") if t.strip()} if raw_types else None
    _handle_ws_connection(ws, _deps().event_bus, type_filter)


def _ws_stream_handler(ws):
    """WebSocket endpoint для потоковой транскрипции/перевода (Stage 1)."""
    deps = _deps()
    # 🔴 Privacy-gate
    if deps.store.load_settings().get("privacy_mode_enabled", False):
        try:
            ws.send(json.dumps({"type": "error", "code": "privacy_mode_active", "message": "Privacy mode active"}))
            ws.close(message=b"privacy_mode_active")
        except Exception:
            pass
        return

    # CORS check is done by @_block_cross_origin_reads mostly, but WS does not use standard decorator nicely
    # actually _block_cross_origin_reads doesn't work for WebSocket routes directly in flask_sock because
    # it returns a Response object instead of closing WS. We need to handle it inline.
    origin = request.headers.get("Origin", "")
    if origin and not _is_origin_allowed(origin):
        logger.warning("Blocked cross-origin WS stream from Origin=%r", origin)
        try:
            ws.send(json.dumps({"type": "error", "code": "cross_origin_denied", "message": "cross-origin access denied"}))
            ws.close(message=b"cross-origin access denied")
        except Exception:
            pass
        return

    # Auth
    if not _ws_check_auth(ws):
        return

    import base64
    try:
        first_msg = ws.receive()
        if not first_msg:
            return
        config = json.loads(first_msg)
        if config.get("type") != "config":
            ws.send(json.dumps({"type": "error", "code": "invalid_config", "message": "First message must be config"}))
            ws.close(message=b"invalid_config")
            return
    except Exception as e:
        logger.error("WS /v1/stream config error: %s", e)
        try:
            ws.send(json.dumps({"type": "error", "code": "invalid_json", "message": "Invalid JSON"}))
            ws.close(message=b"invalid_json")
        except Exception:
            pass
        return

    backend = config.get("backend", "auto")
    source_lang = config.get("source_lang", "auto")
    cloud_provider_name = config.get("provider", "openai")

    mode = config.get("mode", "transcribe")
    target_lang = config.get("target_lang", "off") if mode == "translate" else "off"

    # P0e: native WS делит REST STT-слот с POST; acquire(0) внутри _process_window.
    # Cloud-ветка ниже LiveSubsService не использует. IPC BackendService gate не получает.
    live_subs = LiveSubsService(
        transcriber=deps.transcriber,
        translator=deps.translator,
        settings_get=lambda k, d=None: deps.store.load_settings().get(k, d),
        stt_acquire=try_acquire_stt_singleflight,
        stt_release=release_stt_singleflight,
    )

    cloud_audio_buffer = bytearray()
    cloud_sample_rate = 16000

    logger.info("WS /v1/stream: Client connected")

    try:
        while True:
            raw_msg = ws.receive()
            if not raw_msg:
                break

            # Privacy gate on each chunk
            if deps.store.load_settings().get("privacy_mode_enabled", False):
                ws.send(json.dumps({"type": "error", "code": "privacy_mode_active", "message": "Privacy mode active"}))
                break

            try:
                msg = json.loads(raw_msg)
            except Exception:
                ws.send(json.dumps({"type": "error", "code": "invalid_json", "message": "Invalid JSON"}))
                break

            msg_type = msg.get("type")
            if msg_type == "end":
                if backend == "cloud":
                    if cloud_audio_buffer:
                        provider = get_cloud_stt_provider(cloud_provider_name)
                        if not provider:
                            ws.send(json.dumps({"type": "error", "code": "invalid_cloud_provider", "message": "Unknown provider"}))
                        else:
                            res = provider.transcribe(bytes(cloud_audio_buffer), cloud_sample_rate, source_lang)
                            if "error" in res:
                                if res["error"] == "no_api_key":
                                    ws.send(json.dumps({"type": "error", "code": "cloud_no_api_key", "message": res.get("message", "")}))
                                else:
                                    ws.send(json.dumps({"type": "error", "code": "cloud_api_error", "message": res.get("message", "")}))
                            else:
                                text = res.get("text", "")
                                if text:
                                    resp = {
                                        "type": "final",
                                        "text": text,
                                        "lang": res.get("lang") or "ru",
                                        "confidence": res.get("confidence", 0.0),
                                    }
                                    if mode == "translate" and text and target_lang != "off":
                                        resp["translation"] = deps.translator.translate(text, target_lang)
                                    ws.send(json.dumps(resp))
                else:
                    result = live_subs.ingest(audio_bytes=b"", sample_rate=16000, target_lang=target_lang, is_final=True)
                    if result and result.get("text"):
                        resp = {
                            "type": "final",
                            "text": result.get("text", ""),
                            "lang": result.get("language_detected") or "ru",
                            "confidence": 0.0,
                        }
                        if mode == "translate" and result.get("translation"):
                            resp["translation"] = result["translation"]
                        ws.send(json.dumps(resp))
                break

            elif msg_type == "audio":
                audio_b64 = msg.get("data", "")
                sample_rate = msg.get("sample_rate", 16000)
                is_final = msg.get("is_final", False)

                try:
                    audio_bytes = base64.b64decode(audio_b64)
                except Exception as e:
                    ws.send(json.dumps({"type": "error", "code": "invalid_base64", "message": str(e)}))
                    continue

                if backend == "cloud":
                    cloud_sample_rate = sample_rate
                    cloud_audio_buffer.extend(audio_bytes)
                    # Bound the WS accumulator — flask MAX_CONTENT_LENGTH does not
                    # apply to WebSocket frames, so an unbounded stream would exhaust
                    # process memory (the buffer is later doubled by pcm16_to_wav).
                    if len(cloud_audio_buffer) > MAX_CLOUD_AUDIO_BYTES:
                        ws.send(json.dumps({"type": "error", "code": "audio_too_large", "message": "Cloud audio buffer exceeded limit"}))
                        break
                    if is_final:
                        if cloud_audio_buffer:
                            provider = get_cloud_stt_provider(cloud_provider_name)
                            if not provider:
                                ws.send(json.dumps({"type": "error", "code": "invalid_cloud_provider", "message": "Unknown provider"}))
                            else:
                                res = provider.transcribe(bytes(cloud_audio_buffer), cloud_sample_rate, source_lang)
                                if "error" in res:
                                    if res["error"] == "no_api_key":
                                        ws.send(json.dumps({"type": "error", "code": "cloud_no_api_key", "message": res.get("message", "")}))
                                    else:
                                        ws.send(json.dumps({"type": "error", "code": "cloud_api_error", "message": res.get("message", "")}))
                                else:
                                    text = res.get("text", "")
                                    if text:
                                        resp = {
                                            "type": "final",
                                            "text": text,
                                            "lang": res.get("lang") or "ru",
                                            "confidence": res.get("confidence", 0.0),
                                        }
                                        if mode == "translate" and text and target_lang != "off":
                                            resp["translation"] = deps.translator.translate(text, target_lang)
                                        ws.send(json.dumps(resp))
                        break
                else:
                    result = live_subs.ingest(
                        audio_bytes=audio_bytes,
                        sample_rate=sample_rate,
                        target_lang=target_lang,
                        is_final=is_final
                    )

                    if result and result.get("text"):
                        resp = {
                            "type": "final",
                            "text": result.get("text", ""),
                            "lang": result.get("language_detected") or "ru",
                            "confidence": 0.0,
                        }
                        if mode == "translate" and result.get("translation"):
                            resp["translation"] = result["translation"]
                        ws.send(json.dumps(resp))

                    if is_final:
                        break
            else:
                ws.send(json.dumps({"type": "error", "code": "invalid_message", "message": f"Unknown type: {msg_type}"}))

    except Exception as e:
        logger.error("WS /v1/stream loop error: %s", e)
    finally:
        live_subs.reset()
        try:
            ws.close()
        except Exception:
            pass
        logger.info("WS /v1/stream: Client disconnected")


def create_app(deps: "RestDeps | None" = None, config_mapping=None) -> Flask:
    """Настоящая фабрика (M1). deps=None → standalone module-глобалы."""
    flask_app = Flask(__name__)
    flask_app.config.update(_base_config())
    if config_mapping:
        flask_app.config.update(config_mapping)
    flask_app.config["REST_DEPS"] = deps if deps is not None else _MODULE_DEPS

    api_local = Api(flask_app)
    flask_app.after_request(api_version_header())
    _init_cors(flask_app)
    limiter.init_app(flask_app)
    flask_app.register_error_handler(429, _rate_limit_exceeded_handler)
    flask_app.register_error_handler(413, _request_entity_too_large_handler)
    flask_app.before_request(_check_vocabulary_post_size)
    flask_app.before_request(start_timer)
    flask_app.after_request(log_request)

    api_local.register_blueprint(monitoring_blp)
    api_local.register_blueprint(v1_blp)

    flask_app.add_url_rule(
        "/v2/", "v2_catchall_root", v2_not_implemented,
        defaults={"p": ""},
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    flask_app.add_url_rule(
        "/v2/<path:p>", "v2_catchall", v2_not_implemented,
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])

    sock_local = Sock(flask_app)
    sock_local.route("/ws/events")(ws_events)
    sock_local.route("/v1/stream")(_block_cross_origin_reads(_ws_stream_handler))
    flask_app.extensions["krab_sock"] = sock_local
    return flask_app


# --- standalone module-level путь (контракт категорий A/B/C: 752 теста) ---
app = create_app()
sock = app.extensions["krab_sock"]
ws_stream = _ws_stream_handler
# api: grep-проверка (KrabEar/backend/rest_server.py, KrabEar/tests/) подтвердила
# 0 обращений к `api.` вне фабрики и 0 импортов `rest_server.api` в тестах — Api
# создаётся per-app внутри create_app(), module-level алиас не нужен для чего-то,
# кроме hasattr()-контракта теста test_module_level_aliases_preserved.
api = None


# ---------------------------------------------------------------------------
# Memory Conductor T6 (спека 2026-08-19-memory-conductor-design.md §3
# C-EXECUTOR-LOCALITY, §6): REST-side idle-reaper для mlx_whisper_worker.
#
# Кто ВЛАДЕЕТ резидентом — тот его и выгружает: mlx_whisper-сессия живёт
# синглтоном в core.mlx_whisper_session ЭТОГО процесса (REST, не IPC-backend),
# поэтому реапер живёт здесь же, а не в IPC-стороннем conductor'е (тот ведает
# gigaam/rewriter/brain — другие резиденты, другой процесс).
#
# 🔴 Локус старта (round-2 констрейнт): ТОЛЬКО run-путь — блок `__main__`
# ниже И `InProcessRestServer.start()` (rest_inprocess.py), НИКОГДА на
# импорте модуля. rest_server.py импортируется chunked-тестами — daemon-тред
# на импорте протёк бы в процесс каждого файла того же чанка (класс #1782).
# ---------------------------------------------------------------------------

_WHISPER_LEDGER_OWNER = "krab_ear_rest"
_WHISPER_RESIDENT_NAME = "mlx_whisper_worker"
_WHISPER_RESIDENT_SIZE_MB = 1800
_DEFAULT_WHISPER_IDLE_UNLOAD_SEC = 900.0

# DI-шов для тестов (тот же приём, что module-level store/engine выше):
# тест подставляет сюда LedgerClient(path=tmpdir) ДО первого тика, чтобы
# реапер никогда не касался настоящего ~/.openclaw/memory_ledger.json.
_whisper_ledger_client = None
# Идемпотентность старта: RestWatchdog зовёт InProcessRestServer.start()
# повторно (restart = stop()+start()) — двойной вызов обязан дать РОВНО один
# тред за жизнь процесса, а не респавнить второй параллельно с первым.
_whisper_reaper_started = threading.Event()
_whisper_reaper_start_lock = threading.Lock()


def _whisper_ledger():
    global _whisper_ledger_client
    if _whisper_ledger_client is None:
        from backend.memory_ledger import LedgerClient

        _whisper_ledger_client = LedgerClient(owner=_WHISPER_LEDGER_OWNER)
    return _whisper_ledger_client


def _whisper_idle_reaper_tick() -> None:
    """Один тик REST idle-reaper'а: публикует леджер, затем shadow-лог или
    реальный close_if_idle (в зависимости от master-гейта + enforce-флага).

    ``peek_session()`` НИКОГДА не создаёт сессию (C-EXECUTOR-LOCALITY) — до
    первой транскрибации в этом процессе тик молча выходит, не порождая
    worker ради самого прохода.
    """
    from core.mlx_whisper_session import peek_session

    sess = peek_session()
    if sess is None:
        return

    inflight = sess.inflight
    last_used = sess.last_used_ts
    state = "active" if inflight > 0 else "idle"
    # 🔴 Шов T6↔T8 (финальный гейт, HIGH-2): last_used — MONOTONIC, а Swift и
    # кондуктор считают idle от epoch. Публикуем epoch-момент начала простоя.
    idle_since_ts = None if inflight > 0 else (time.time() - (time.monotonic() - last_used))

    try:
        _whisper_ledger().publish_own({
            _WHISPER_RESIDENT_NAME: {
                "size_mb": _WHISPER_RESIDENT_SIZE_MB,
                "state": state,
                "idle_since_ts": idle_since_ts,
                "reload_cost": "cheap",
                "pid": os.getpid(),
            }
        })
    except Exception:
        logger.exception("whisper idle-reaper: publish_own бросил")

    if state != "idle":
        return

    idle_threshold = float(
        _load_settings_field("whisper_idle_unload_sec", _DEFAULT_WHISPER_IDLE_UNLOAD_SEC)
    )
    elapsed = time.monotonic() - last_used
    if elapsed < idle_threshold:
        return

    # Global Constraint: memory_conductor_enforce=False по умолчанию — до
    # явного включения (master-гейт memory_conductor_enabled И один из
    # enforce-флагов) реапер только ЛОГИРУЕТ решение; леджер уже опубликован
    # выше в любом случае (write-only observability, не зависит от policy).
    conductor_enabled = bool(_load_settings_field("memory_conductor_enabled", False))
    enforce = bool(_load_settings_field("memory_conductor_enforce", False)) or bool(
        _load_settings_field("memory_conductor_enforce_whisper", False)
    )
    if not (conductor_enabled and enforce):
        logger.info(
            "whisper idle-reaper: SHADOW would evict mlx_whisper_worker "
            "(idle %.0fs >= %.0fs)", elapsed, idle_threshold,
        )
        return

    if sess.close_if_idle(idle_threshold):
        logger.info(
            "whisper idle-reaper: evicted mlx_whisper_worker (idle >= %.0fs)",
            idle_threshold,
        )


def _whisper_idle_reaper_loop(interval_sec: float) -> None:
    while True:
        try:
            _whisper_idle_reaper_tick()
        except Exception:
            logger.exception("whisper idle-reaper: tick бросил")
        time.sleep(interval_sec)


def start_whisper_idle_reaper(interval_sec: float = 60.0) -> None:
    """Запускает daemon idle-reaper mlx_whisper_worker (T6).

    🔴 Вызывать ТОЛЬКО из run-путей (см. докстринг секции выше) — НИКОГДА на
    импорте модуля.

    Идемпотентен через module-level ``threading.Event``: и `__main__`
    serve-блок, и ``InProcessRestServer.start()`` (тот может дёрнуться
    повторно — RestWatchdog делает restart() = stop()+start()) обязаны дать
    РОВНО один тред за жизнь процесса.
    """
    from core.mlx_whisper_session import mlx_whisper_worker_enabled

    if not mlx_whisper_worker_enabled():
        return
    with _whisper_reaper_start_lock:
        if _whisper_reaper_started.is_set():
            return
        _whisper_reaper_started.set()
        threading.Thread(
            target=_whisper_idle_reaper_loop,
            args=(interval_sec,),
            name="whisper-idle-reaper",
            daemon=True,
        ).start()


if __name__ == "__main__":
    # Запуск сервера на локальном интерфейсе.
    #
    # Wave 58 follow-up (Wave 47 B2 HIGH-1 partial closure): bind explicitly
    # to 127.0.0.1 — REMOTE attackers cannot reach this server. Any local
    # process (any user on this machine) CAN hit it without auth if neither
    # REST_API_AUTH_ENABLED nor REST_API_KEY is set.
    #
    # Defense in depth: emit a warning at startup so operator knows the
    # security posture. Full fix (require auth by default) would break
    # existing localhost clients — defer to opt-in migration wave.
    import errno as _errno
    import sys as _sys

    # Сиблинг-паритет с IPC-процессом (найдено ревью волны 2026-08-07): REST —
    # отдельный launchd-юнит, он тоже спавнит GigaAM-воркер и транскрибирует,
    # то есть имеет ту же экспозицию к нативным сбоям (MLX/onnxruntime/
    # PortAudio). Без этого его сегфолт умирал бы без единого трейсбека в
    # krab-ear-rest.err.log (dirty-маркер здесь не ведётся). Обработчик
    # сигнала из Python вешать ЗАПРЕЩЕНО — см. observability.install_signal_handlers.
    try:
        import faulthandler as _faulthandler

        _faulthandler.enable(all_threads=True)
    except Exception:  # noqa: BLE001
        logger.warning("REST: faulthandler.enable() недоступен — крэш-трейсбека не будет")

    _auth_mode = (
        "token-store" if getattr(settings, "REST_API_AUTH_ENABLED", False)
        else "legacy-key" if settings.REST_API_KEY
        else "DISABLED"
    )
    if _auth_mode == "DISABLED":
        logger.warning(
            "REST server starting on 127.0.0.1:%s with NO auth "
            "(Wave 47 B2 HIGH-1). Any local process can call /api/*. "
            "To enable auth: set REST_API_AUTH_ENABLED=true OR populate "
            "REST_API_KEY in settings.json. Localhost-only bind prevents "
            "remote attack.",
            settings.REST_SERVER_PORT,
        )
    else:
        logger.info("REST server starting on 127.0.0.1:%s with auth=%s", settings.REST_SERVER_PORT, _auth_mode)

    # F3 MED fix (W1674 / W1684): guard against EADDRINUSE.
    #
    # Without this guard, Flask's app.run() raises OSError([Errno 48] Address
    # already in use) when port 5005 is occupied, and the process exits with a
    # raw traceback — nothing is logged, the operator sees no structured error,
    # and launchd silently respawns in a tight crash-loop.
    #
    # With this guard: a structured logger.error fires (visible in
    # krab-ear-rest.err.log), Sentry captures the event if a DSN is wired,
    # and sys.exit(1) terminates cleanly so launchd does NOT tight-loop.

    # T6 Memory Conductor: серв-путь standalone REST — единственное
    # разрешённое место запуска idle-reaper'а mlx_whisper_worker в этом
    # режиме (НИКОГДА на импорте модуля, см. докстринг start_whisper_idle_reaper).
    start_whisper_idle_reaper()

    try:
        app.run(host="127.0.0.1", port=settings.REST_SERVER_PORT)
    except OSError as _e:
        if _e.errno == _errno.EADDRINUSE:
            logger.error(
                "REST server failed to start: port %s is already in use "
                "(EADDRINUSE). Another instance may be running. "
                "Stop it first: lsof -ti :%s | xargs kill -9",
                settings.REST_SERVER_PORT, settings.REST_SERVER_PORT,
                extra={"errno": _e.errno, "port": settings.REST_SERVER_PORT},
            )
            try:
                from backend.observability import capture_exception
                capture_exception(_e)
            except Exception:
                pass
            _sys.exit(1)
        raise
