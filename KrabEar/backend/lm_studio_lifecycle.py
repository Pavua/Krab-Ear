"""LM Studio lifecycle management — load/unload моделей через REST + CLI fallback.

Цель: на M4 Max 36GB unified memory можно держать одновременно:
  - rewriter (always-on, 4-9B Q4, ~5 GB)
  - brain (on-demand, 30-35B-A3B Q4, ~19-20 GB) — для Voice Assistant

Когда STT (Whisper + pyannote) активен, brain должен быть выгружен чтобы
освободить ~19 GB Metal-памяти. После окончания записи brain reload'ится.

Strategy:
  1. LM Studio REST API endpoints (newer versions 0.3.x):
     - POST /api/v0/models/{model_id}/unload
     - POST /api/v0/models/{model_id}/load
  2. Fallback: shell `lms unload <model>` / `lms load <model>` (CLI tool).
  3. Last resort: log warning, не падаем (recording flow важнее).

Безопасность: если LM Studio недоступен или endpoint не поддерживается,
мы НЕ ломаем recording. Hook fire-and-forget с timeout 1 сек.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

logger = logging.getLogger("KrabEar.LMStudioLifecycle")

# Короткие таймауты — мы не блокируем recording flow, fire-and-forget.
_REST_TIMEOUT_SEC = 1.5
_CLI_TIMEOUT_SEC = 2.0

# Safety cap: model IDs longer than this are rejected before any network call.
_MODEL_ID_MAX_LEN = 256

# ---------------------------------------------------------------------------
# SSRF guard (Wave 1768) — mirrors _validate_llm_url() in llm_rewriter.py.
#
# base_url приходит из settings.llm_base_url, а settings_validator его НЕ
# валидирует. Локальный процесс мог бы выставить
#   set_settings {"llm_base_url": "file:///etc/passwd"}
# и следующий start/stop_recording дёрнул бы load/unload с этим URL. Дефолтный
# urllib opener включает FileHandler + HTTPRedirectHandler, поэтому открылись бы
# и `file://`, и `302 → file://`. Защита двухслойная:
#   1. Явный allowlist схемы (http/https) до любого сетевого вызова.
#   2. Кастомный opener БЕЗ FileHandler/FTPHandler + redirect-handler, который
#      повторно валидирует схему на каждом 30x — так блокируются и редиректы в
#      file://. localhost/LAN адреса разрешены намеренно (LM Studio 127.0.0.1).
# ---------------------------------------------------------------------------
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _scheme_allowed(base_url: str) -> bool:
    """True если схема base_url входит в allowlist (http/https)."""
    return urlparse(base_url).scheme.lower() in _ALLOWED_SCHEMES


class _SchemeCheckingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTPRedirectHandler, отклоняющий 30x-редиректы на запрещённую схему.

    Защищает от `302 → file:///etc/passwd`: даже если сервер ответит редиректом
    на file://, urllib попытается построить Request с этой схемой, а opener
    (см. ниже) не имеет FileHandler — но мы блокируем ещё раньше, явной ошибкой.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme.lower() not in _ALLOWED_SCHEMES:
            raise urllib.error.HTTPError(
                newurl, code,
                f"redirect to disallowed scheme blocked: {newurl!r}",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_safe_opener() -> urllib.request.OpenerDirector:
    """Opener только с HTTP(S) + scheme-checking redirect handler.

    Намеренно НЕ содержит FileHandler/FTPHandler/DataHandler, поэтому
    file://, ftp://, data:// не обрабатываются вовсе (в т.ч. через редирект).
    """
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.HTTPHandler())
    opener.add_handler(urllib.request.HTTPSHandler())
    opener.add_handler(_SchemeCheckingRedirectHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    return opener


# Один разделяемый opener — потокобезопасен для конкурентных open().
_SAFE_OPENER = _build_safe_opener()


def _rest_body_is_error(raw_body: bytes) -> bool:
    """Return True если тело ответа содержит JSON с ключом "error".

    Некоторые версии LM Studio отвечают HTTP 200 на неизвестные endpoints,
    но тело содержит {"error": "Unexpected endpoint or method. (POST /api/v0/...)"}.
    Такой ответ — НЕ успех; нужно упасть на CLI fallback.
    """
    try:
        parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
        return isinstance(parsed, dict) and "error" in parsed
    except (ValueError, AttributeError):
        return False


def _try_rest_unload(base_url: str, model_id: str) -> bool:
    """LM Studio REST: POST /api/v0/models/{model}/unload. Returns True on 2xx + no error body."""
    # SSRF guard (Wave 1768): отклоняем file://, ftp://, data:// и пр. до сети.
    if not _scheme_allowed(base_url):
        logger.warning(
            "LM Studio: refusing unload — base_url scheme not in %s: %r",
            sorted(_ALLOWED_SCHEMES), base_url,
        )
        return False
    # base_url типа "http://localhost:1234/v1" — отбрасываем "/v1"
    api_root = base_url.rstrip("/").removesuffix("/v1")
    url = f"{api_root}/api/v0/models/{quote(model_id, safe='')}/unload"
    try:
        req = urllib.request.Request(url, method="POST")
        with _SAFE_OPENER.open(req, timeout=_REST_TIMEOUT_SEC) as resp:
            if not (200 <= resp.status < 300):
                return False
            body = resp.read()
            if _rest_body_is_error(body):
                logger.debug(
                    "LM Studio REST unload %s: HTTP 200 but body contains error — falling back to CLI",
                    model_id,
                )
                return False
            return True
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return False  # endpoint недоступен — silent fallback
        logger.debug("LM Studio REST unload %s: HTTP %s", model_id, e.code)
        return False
    except Exception as exc:
        logger.debug("LM Studio REST unload %s: %s", model_id, exc)
        return False


def _try_rest_load(base_url: str, model_id: str) -> bool:
    """LM Studio REST: POST /api/v0/models/load. Returns True on 2xx + no error body."""
    # SSRF guard (Wave 1768): отклоняем file://, ftp://, data:// и пр. до сети.
    if not _scheme_allowed(base_url):
        logger.warning(
            "LM Studio: refusing load — base_url scheme not in %s: %r",
            sorted(_ALLOWED_SCHEMES), base_url,
        )
        return False
    api_root = base_url.rstrip("/").removesuffix("/v1")
    url = f"{api_root}/api/v0/models/load"
    try:
        req_body = json.dumps({"model": model_id}).encode()
        req = urllib.request.Request(
            url, data=req_body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _SAFE_OPENER.open(req, timeout=_REST_TIMEOUT_SEC) as resp:
            if not (200 <= resp.status < 300):
                return False
            body = resp.read()
            if _rest_body_is_error(body):
                logger.debug(
                    "LM Studio REST load %s: HTTP 200 but body contains error — falling back to CLI",
                    model_id,
                )
                return False
            return True
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return False
        logger.debug("LM Studio REST load %s: HTTP %s", model_id, e.code)
        return False
    except Exception as exc:
        logger.debug("LM Studio REST load %s: %s", model_id, exc)
        return False


_MODEL_ID_SAFE_RE = re.compile(r"[A-Za-z0-9._:/-]{1,256}$")


def _try_cli(action: str, model_id: str) -> bool:
    """Fallback на `lms <action> -- <model_id>` shell command.

    Защита от flag-injection (MED, wave-22): model_id, начинающийся с '-',
    или содержащий недопустимые символы, отклоняется ДО вызова subprocess.
    POSIX «--» separator вставляется явно чтобы lms CLI не мог разобрать
    model_id как флаг даже при будущих изменениях валидации.
    """
    lms = shutil.which("lms")
    if not lms:
        return False
    # Reject leading-dash values (would be parsed as CLI flags) and unsafe chars.
    if model_id.startswith("-") or not _MODEL_ID_SAFE_RE.fullmatch(model_id):
        logger.warning(
            "LM Studio CLI: model_id rejected (flag-injection guard): %r",
            model_id,
        )
        return False
    try:
        result = subprocess.run(
            [lms, action, "--", model_id],
            timeout=_CLI_TIMEOUT_SEC,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception as exc:
        logger.debug("LM Studio CLI %s %s: %s", action, model_id, exc)
        return False


def load_model_sync(base_url: str, model_id: str, timeout_sec: float = 90.0) -> bool:
    """Синхронная загрузка модели в LM Studio с ожиданием завершения.

    В отличие от load_model_async (fire-and-forget), этот метод блокирует
    вызывающий поток до фактической загрузки модели или истечения таймаута.

    Стратегия:
      1. Пробуем REST POST /api/v0/models/load — возвращает True сразу если endpoint
         существует и тело не содержит {"error": ...}.
      2. Fallback: `lms load -- <model_id>` (синхронный subprocess).
         PATH может не содержать ~/.lmstudio/bin, поэтому ищем lms через shutil.which,
         а при неудаче пробуем абсолютный путь ~/.lmstudio/bin/lms.

    Args:
        base_url: LLM_BASE_URL из settings (например "http://localhost:1234/v1").
        model_id: идентификатор модели в LM Studio.
        timeout_sec: максимальное ожидание subprocess `lms load` (секунды).

    Returns:
        True если модель успешно загружена (REST или CLI), False в противном случае.
    """
    if not model_id:
        return False
    if len(model_id) > _MODEL_ID_MAX_LEN:
        logger.warning(
            "LM Studio load_model_sync: model_id too long (%d chars) — skipped",
            len(model_id),
        )
        return False

    # 1. REST attempt (быстро — возвращает 200 сразу при успехе)
    if _try_rest_load(base_url, model_id):
        logger.info("LM Studio: load_model_sync '%s' via REST", model_id)
        return True

    # 2. CLI fallback — `lms load -- <model_id>` (синхронный, ждём завершения)
    # PATH в launchd-процессах часто не включает ~/.lmstudio/bin
    lms = shutil.which("lms") or os.path.expanduser("~/.lmstudio/bin/lms")
    if not os.path.isfile(lms):
        logger.debug("LM Studio load_model_sync: lms CLI not found — cannot load '%s'", model_id)
        return False

    # Flag-injection guard (зеркало _try_cli)
    if model_id.startswith("-") or not _MODEL_ID_SAFE_RE.fullmatch(model_id):
        logger.warning(
            "LM Studio load_model_sync: model_id rejected (flag-injection guard): %r",
            model_id,
        )
        return False

    try:
        result = subprocess.run(
            [lms, "load", "--", model_id],
            timeout=timeout_sec,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info("LM Studio: load_model_sync '%s' via CLI (returncode=0)", model_id)
            return True
        logger.debug(
            "LM Studio load_model_sync CLI failed: returncode=%d stderr=%s",
            result.returncode, result.stderr[:200],
        )
        return False
    except subprocess.TimeoutExpired:
        logger.warning(
            "LM Studio load_model_sync: CLI timed out after %.0fs for model '%s'",
            timeout_sec, model_id,
        )
        return False
    except Exception as exc:
        logger.debug("LM Studio load_model_sync CLI error for '%s': %s", model_id, exc)
        return False


# ---------------------------------------------------------------------------
# Каталог моделей LM Studio: три живые формы одного ответа
# ---------------------------------------------------------------------------
# 🔴 Замер 03.09.2026 (LM Studio 0.4.x, 105 моделей на диске, токен обязателен):
#
#   GET /api/v0/models → {"data":   [{"id":  ..., "state": "loaded"|"not-loaded"}]}
#   GET /api/v1/models → {"models": [{"key": ..., "loaded_instances": [...]}]}
#   GET /v1/models     → {"data":   [{"id":  ...}]}            (OpenAI-совместимый)
#
# Нативный v1 несёт идентификатор в "key", а список — в "models"; парсер
# ``data[].id`` видел на нём пустоту и годами отвечал "модели нет" вместо
# "не разобрал ответ". Отсюда вечное has_model=false в probe_llm_http и
# всегда-None в model_loaded. Wave 68 (PR #415) померила этот же endpoint без
# токена и записала "200 с пустым списком" — пусто было тело 401, а не каталог.
CATALOG_ENDPOINTS: tuple[str, ...] = ("/api/v0/models", "/api/v1/models", "/v1/models")

# 🔴 Наличие в каталоге ≠ загружена: каталог перечисляет ВСЁ скачанное.
# Признак загрузки есть только у двух нативных форм, поэтому проверка
# "загружена?" не смеет опрашивать OpenAI-совместимый endpoint — он ответил бы
# полным каталогом, и любая скачанная модель выглядела бы загруженной.
LOADED_STATE_ENDPOINTS: tuple[str, ...] = ("/api/v0/models", "/api/v1/models")


def parse_lm_studio_catalog(payload: object) -> list[dict] | None:
    """Нормализует любую из трёх форм каталога в ``[{"id": str, "loaded": bool|None}]``.

    🔴 Возврат тоже трёхзначный, и это ядро функции: ``None`` — форму не
    распознал ("не смог оценить"), ``[]`` — распознал, каталог действительно
    пуст. Схлопнуть их в один пустой список значит повторить ошибку
    ``SNR=0.0``: sentinel "не смог" неотличим от измерения "ноль", и слепой
    парсер выглядит как честный ответ "моделей нет".

    ``loaded`` — трёхзначное по той же причине: True/False, когда форма несёт
    состояние, и None, когда не несёт (OpenAI-совместимый список). None
    означает "не смог оценить", а не "не загружена" — вызывающая сторона
    обязана fail-safe.
    """
    if not isinstance(payload, dict):
        return None
    items = payload.get("models")
    id_key, loaded_key = "key", "loaded_instances"
    if not isinstance(items, list):
        items = payload.get("data")
        id_key, loaded_key = "id", "state"
    if not isinstance(items, list):
        return None

    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = item.get(id_key)
        if not isinstance(model_id, str) or not model_id:
            continue
        raw_state = item.get(loaded_key)
        if loaded_key == "loaded_instances":
            loaded = bool(raw_state) if isinstance(raw_state, list) else None
        else:
            loaded = (raw_state == "loaded") if isinstance(raw_state, str) else None
        out.append({"id": model_id, "loaded": loaded})
    return out


def model_loaded(
    base_url: str,
    model_id: str,
    timeout: float = 5.0,
    api_key: str | None = None,
) -> bool | None:
    """Проверяет "модель загружена в LM Studio?" пассивным GET по каталогу.

    Опрашивает ``LOADED_STATE_ENDPOINTS`` по порядку — только те формы, что
    реально несут состояние загрузки (см. комментарий у константы). Разбор —
    общий ``parse_lm_studio_catalog``. Нормализация base_url — существующая
    конвенция этого модуля (``.rstrip("/").removesuffix("/v1")``), та же что в
    ``_try_rest_load``/``_try_rest_unload``. Никогда не триггерит
    JIT-перезагрузку модели (только GET).

    Три состояния (C-EFFECT-CHECK, docs/superpowers/specs/2026-08-19-memory-conductor-design.md §3):
      - ``True``  — модель есть в каталоге И помечена загруженной.
      - ``False`` — LM Studio отвечает 200 с валидным каталогом, а модель в нём
        либо отсутствует, либо помечена незагруженной.
      - ``None``  — состояние НЕИЗВЕСТНО: сеть недоступна/таймаут/не-2xx/битый JSON/
        неразобранная форма ответа/форма без признака загрузки/запрещённая схема
        base_url/пустой model_id.

    🔴 None ≠ False. Вызывающая сторона НЕ ДОЛЖНА трактовать None как "не
    загружена" — так же, как SNR=0.0 не значит "тишина", а значит "не смог
    оценить" (см. reference_snr_zero_is_sentinel_not_measurement). Решение,
    основанное на None, обязано fail-safe в сторону "не знаю, не действую".

    Args:
        base_url: LLM_BASE_URL из settings (например "http://localhost:1234/v1").
        model_id: идентификатор модели в LM Studio.
        timeout: таймаут HTTP-запроса в секундах (default 5.0 — та же величина,
            что у ``passive_health_check``: /models — быстрый metadata call).
        api_key: LLM_API_KEY из settings. LM Studio с включённой аутентификацией
            отвечает 401 на запрос без Bearer — без ключа проба слепа.

    Returns:
        True/False/None — см. выше.
    """
    if not model_id:
        return None
    # SSRF guard (Wave 1768, зеркало _try_rest_load/_try_rest_unload): отклоняем
    # file://, ftp://, data:// и пр. до любого сетевого вызова.
    if not _scheme_allowed(base_url):
        logger.warning(
            "LM Studio: refusing model_loaded probe — base_url scheme not in %s: %r",
            sorted(_ALLOWED_SCHEMES), base_url,
        )
        return None
    api_root = base_url.rstrip("/").removesuffix("/v1")
    for endpoint in LOADED_STATE_ENDPOINTS:
        url = f"{api_root}{endpoint}"
        try:
            req = urllib.request.Request(url, method="GET")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
            with _SAFE_OPENER.open(req, timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                entries = parse_lm_studio_catalog(
                    json.loads(resp.read().decode("utf-8", errors="replace"))
                )
        except Exception as exc:
            logger.debug("LM Studio model_loaded probe %s (%s): %s", model_id, endpoint, exc)
            continue
        if entries is None:
            # Форму не разобрали — это молчание, а не ответ "модели нет".
            continue
        for entry in entries:
            if entry["id"] == model_id:
                # loaded=None (форма без состояния) обязано остаться None.
                return entry["loaded"]
        # Модель отсутствует в полном каталоге — её точно не загрузили.
        return False
    return None


_EMBEDDING_TYPE_MARKERS = frozenset({"embeddings", "embedding"})


def _is_embedding_model(model_id: str, type_hint: str = "") -> bool:
    hint = (type_hint or "").strip().lower()
    if hint in _EMBEDDING_TYPE_MARKERS:
        return True
    lowered = (model_id or "").lower()
    return "embed" in lowered


def probe_loaded_chat_models(
    base_url: str,
    timeout: float = 5.0,
    api_key: str | None = None,
) -> list[str] | None:
    """Какие chat-модели сейчас загружены в LM Studio (без JIT/load).

    Только GET по ``LOADED_STATE_ENDPOINTS``. Embeddings пропускаются.
    Три состояния, как у ``model_loaded``:
      - ``[]``     — каталог разобран, загруженных chat-моделей нет;
      - ``[id…]``  — есть хотя бы одна загруженная chat-модель;
      - ``None``   — неизвестно (сеть/таймаут/неразобранная форма).

    ``None`` нельзя читать как «пусто» — POST в этом случае не должен
    самовольно грузить модель, но и не должен притворяться, что слот пуст.
    """
    if not _scheme_allowed(base_url):
        logger.warning(
            "LM Studio: refusing loaded-chat probe — base_url scheme not in %s: %r",
            sorted(_ALLOWED_SCHEMES), base_url,
        )
        return None
    api_root = base_url.rstrip("/").removesuffix("/v1")
    for endpoint in LOADED_STATE_ENDPOINTS:
        url = f"{api_root}{endpoint}"
        try:
            req = urllib.request.Request(url, method="GET")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
            with _SAFE_OPENER.open(req, timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                entries = parse_lm_studio_catalog(payload)
        except Exception as exc:
            logger.debug("LM Studio loaded-chat probe (%s): %s", endpoint, exc)
            continue
        if entries is None:
            continue
        type_by_id: dict[str, str] = {}
        if isinstance(payload, dict):
            raw_items = payload.get("data")
            if not isinstance(raw_items, list):
                raw_items = payload.get("models")
            if isinstance(raw_items, list):
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    mid = item.get("id") or item.get("key")
                    if isinstance(mid, str) and mid:
                        type_by_id[mid] = str(
                            item.get("type") or item.get("model_type") or ""
                        )
        loaded: list[str] = []
        for entry in entries:
            if entry.get("loaded") is not True:
                continue
            mid = str(entry.get("id") or "")
            if not mid or _is_embedding_model(mid, type_by_id.get(mid, "")):
                continue
            loaded.append(mid)
        return loaded
    return None


def unload_model_async(base_url: str, model_id: str) -> None:
    """Fire-and-forget unload. Не блокирует caller, errors silently logged.

    Args:
        base_url: LLM_BASE_URL из settings (например "http://localhost:1234/v1").
        model_id: model identifier как видит LM Studio (lowercase, e.g. "qwen3.6-35b-a3b").
    """
    if not model_id:
        return
    if len(model_id) > _MODEL_ID_MAX_LEN:
        logger.warning(
            "LM Studio: model_id too long (%d chars, max %d) — unload skipped",
            len(model_id), _MODEL_ID_MAX_LEN,
        )
        return

    def _worker() -> None:
        if _try_rest_unload(base_url, model_id):
            logger.info("LM Studio: unloaded brain model '%s' via REST", model_id)
            return
        if _try_cli("unload", model_id):
            logger.info("LM Studio: unloaded brain model '%s' via CLI", model_id)
            return
        logger.debug(
            "LM Studio: unload '%s' not supported (REST 404 + no CLI). "
            "Brain memory will be freed on TTL eviction.",
            model_id,
        )

    threading.Thread(
        target=_worker,
        name=f"LMStudio-unload-{model_id[:20]}",
        daemon=True,
    ).start()


def load_model_async(base_url: str, model_id: str) -> None:
    """Fire-and-forget load. Не блокирует caller.

    Используется после stop_recording чтобы pre-warm brain к моменту когда
    user может открыть Voice Assistant.
    """
    if not model_id:
        return
    if len(model_id) > _MODEL_ID_MAX_LEN:
        logger.warning(
            "LM Studio: model_id too long (%d chars, max %d) — load skipped",
            len(model_id), _MODEL_ID_MAX_LEN,
        )
        return

    def _worker() -> None:
        if _try_rest_load(base_url, model_id):
            logger.info("LM Studio: pre-loaded brain model '%s' via REST", model_id)
            return
        if _try_cli("load", model_id):
            logger.info("LM Studio: pre-loaded brain model '%s' via CLI", model_id)
            return
        logger.debug(
            "LM Studio: load '%s' not supported. Will load on first request (cold start)",
            model_id,
        )

    threading.Thread(
        target=_worker,
        name=f"LMStudio-load-{model_id[:20]}",
        daemon=True,
    ).start()
