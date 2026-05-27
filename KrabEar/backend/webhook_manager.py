"""WebhookManager — отправка событий на внешние URL (webhook-уведомления).

Поддерживает:
- Регистрацию/удаление webhook-получателей с фильтрацией по типу события.
- HMAC-SHA256 подпись тела запроса (если указан secret).
- Неблокирующую доставку через threading.Thread.
- Retry с экспоненциальной задержкой (до 3 попыток).
- Персистентность реестра в {data_dir}/webhooks.json.
- Статистику доставки на webhook.
- SSRF-защита: блокируются localhost, RFC1918, link-local и mDNS адреса.
- Защита от redirect-SSRF (W1349 F1): allow_redirects=False — все 3xx трактуются
  как ошибка, redirect не следуется. Злоумышленник не может зарегистрировать
  https://attacker.com/redir → 302 → http://127.0.0.1/admin обход.
- Ограничение тела ответа: не более _MAX_RESPONSE_BYTES байт.
- Privacy-mode gate: fire_webhook пропускается при включённом privacy mode.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request
from urllib.error import HTTPError

logger = logging.getLogger("KrabEar.Backend.WebhookManager")

_WEBHOOKS_FILE = "webhooks.json"
_MAX_RETRIES = 3
_BACKOFF_BASE_SEC = 1.0  # 1s → 2s → 4s
_REQUEST_TIMEOUT_SEC = 10
_MAX_RESPONSE_BYTES = 64 * 1024  # 64 KB cap on response body (F2 fix)

# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

# Имена хостов, которые всегда блокируются независимо от настроек
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset({"localhost", "0.0.0.0", ""})


def _is_safe_webhook_url(url: str, allow_local: bool = False) -> tuple[bool, str | None]:
    """Проверяет URL на безопасность для использования в качестве webhook.

    Блокирует:
    - localhost / 0.0.0.0 / пустой хост
    - mDNS .local домены
    - IPv4 loopback (127.0.0.0/8), private (RFC1918), link-local (169.254.0.0/16)
    - IPv6 loopback (::1) и link-local
    - Схемы кроме http/https

    Args:
        url: URL для проверки.
        allow_local: если True — пропускает проверку SSRF (для dev-режима).

    Returns:
        (is_safe, reject_reason) — is_safe=True если URL прошёл все проверки.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "only http/https allowed"

    if allow_local:
        return True, None

    host = (parsed.hostname or "").lower()

    if host in _BLOCKED_HOSTNAMES:
        return False, f"localhost/empty host blocked ({host!r})"

    if host.endswith(".local"):
        return False, "mDNS .local hosts blocked"

    # Попытка разобрать как IP-адрес (literal)
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback:
            return False, f"loopback IP blocked ({ip})"
        if ip.is_link_local:
            return False, f"link-local IP blocked ({ip})"
        if ip.is_private:
            return False, f"private/RFC1918 IP blocked ({ip})"
    except ValueError:
        pass  # не IP-литерал — продолжаем как hostname

    return True, None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTPRedirectHandler, который блокирует ВСЕ 3xx редиректы (allow_redirects=False).

    W1349 F1 fix (Option 1): urllib.request.urlopen по умолчанию следует 3xx-редиректам.
    Этот handler перехватывает любой редирект и возбуждает HTTPError с кодом редиректа,
    так что вызывающий код видит статус 3xx вместо выполнения редиректа.

    Это эквивалент requests.post(..., allow_redirects=False) для urllib.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise HTTPError(
            req.full_url,
            code,
            f"Redirect blocked (allow_redirects=False): {newurl!r}",
            headers,
            fp,
        )


# Keep old name as alias for backwards compatibility with existing tests
_SafeRedirectHandler = _NoRedirectHandler


class WebhookManager:
    """Менеджер webhook-уведомлений Krab Ear.

    Структура webhooks.json:
    {
        "<webhook_id>": {
            "url": str,
            "events": [str, ...],   # пустой список = все события
            "secret": str,          # пустая строка = без подписи
            "created_at": ISO8601,
            "enabled": bool
        },
        ...
    }

    Статистика хранится in-memory и сбрасывается при перезапуске.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        self._webhooks_path = self._data_dir / _WEBHOOKS_FILE
        self._lock = threading.Lock()
        # Реестр: webhook_id → конфиг
        self._webhooks: dict[str, dict[str, Any]] = {}
        # Статистика in-memory: webhook_id → {deliveries, failures, last_status, last_ts}
        self._stats: dict[str, dict[str, Any]] = {}
        # Privacy mode: если True — fire_webhook не отправляет события (F3 gate)
        self._privacy_mode: bool = False
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Загружает реестр из файла (при первом старте создаёт пустой)."""
        if not self._webhooks_path.exists():
            self._webhooks = {}
            return
        try:
            raw = self._webhooks_path.read_text(encoding="utf-8").strip()
            if raw:
                self._webhooks = json.loads(raw)
            else:
                self._webhooks = {}
        except Exception as exc:
            logger.warning("WebhookManager: не удалось загрузить %s: %s", self._webhooks_path, exc)
            self._webhooks = {}

    def _save(self) -> None:
        """Атомарно сохраняет реестр в файл (под _lock)."""
        tmp = self._webhooks_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._webhooks, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._webhooks_path)
        except Exception as exc:
            logger.error("WebhookManager: не удалось сохранить %s: %s", self._webhooks_path, exc)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def register_webhook(
        self,
        url: str,
        events: list[str],
        secret: str = "",
        allow_local: bool = False,
    ) -> str:
        """Регистрирует новый webhook и возвращает его ID.

        Args:
            url: целевой URL (должен начинаться с http:// или https://).
            events: список типов событий для фильтрации; [] = все события.
            secret: секрет для HMAC-SHA256; "" = без подписи.
            allow_local: отключить SSRF-проверку (только для dev/self-hosted окружений).
                         Соответствует настройке ``webhook_allow_local`` в settings.

        Returns:
            webhook_id (UUID4 строка).

        Raises:
            ValueError: если url пустой, не является HTTP(S) URL, или отклонён SSRF-защитой.
        """
        url = url.strip()
        if not url:
            raise ValueError("URL не может быть пустым")
        safe, reason = _is_safe_webhook_url(url, allow_local=allow_local)
        if not safe:
            raise ValueError(f"URL отклонён защитой SSRF ({reason}): {url!r}")

        webhook_id = str(uuid.uuid4())
        entry: dict[str, Any] = {
            "url": url,
            "events": list(events),
            "secret": secret,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "enabled": True,
            # Persist allow_local so fire_webhook can pass it to _deliver_with_retry
            "allow_local": allow_local,
        }
        with self._lock:
            self._webhooks[webhook_id] = entry
            self._save()

        logger.info("WebhookManager: зарегистрирован webhook %s → %s (события: %s)", webhook_id, url, events or "все")
        return webhook_id

    def unregister_webhook(self, webhook_id: str) -> bool:
        """Удаляет webhook по ID.

        Returns:
            True если удалён, False если не найден.
        """
        with self._lock:
            if webhook_id not in self._webhooks:
                return False
            del self._webhooks[webhook_id]
            self._stats.pop(webhook_id, None)
            self._save()

        logger.info("WebhookManager: удалён webhook %s", webhook_id)
        return True

    def list_webhooks(self) -> list[dict[str, Any]]:
        """Возвращает список всех зарегистрированных webhook-ов.

        Секрет не включается в вывод (возвращается has_secret: bool).
        """
        with self._lock:
            result = []
            for wid, cfg in self._webhooks.items():
                stats = self._stats.get(wid, {})
                result.append({
                    "webhook_id": wid,
                    "url": cfg["url"],
                    "events": cfg["events"],
                    "has_secret": bool(cfg.get("secret", "")),
                    "enabled": cfg.get("enabled", True),
                    "created_at": cfg.get("created_at", ""),
                    "deliveries": stats.get("deliveries", 0),
                    "failures": stats.get("failures", 0),
                    "last_status": stats.get("last_status", None),
                    "last_ts": stats.get("last_ts", None),
                })
        return result

    def set_privacy_mode(self, enabled: bool) -> None:
        """Включает / выключает privacy mode.

        Когда privacy mode активен, fire_webhook пропускает все доставки
        (события не покидают устройство). F3 gate fix.
        """
        self._privacy_mode = enabled
        logger.info("WebhookManager: privacy_mode=%s", enabled)

    def fire_webhook(self, event_type: str, data: dict[str, Any]) -> None:
        """Отправляет событие всем подходящим webhook-ам (неблокирующий POST).

        Фильтрация: если у webhook указан список events, отправляем только
        если event_type в этом списке. Пустой список = принимает все события.

        Privacy gate (F3): если включён privacy mode — события не отправляются.
        """
        # F3: privacy mode gate — не отправлять события при включённом privacy mode
        if self._privacy_mode:
            logger.debug(
                "WebhookManager: fire_webhook(%s) пропущен (privacy mode активен)", event_type
            )
            return

        payload = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data": data,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        with self._lock:
            targets = [
                (wid, dict(cfg))
                for wid, cfg in self._webhooks.items()
                if cfg.get("enabled", True)
                and (not cfg.get("events") or event_type in cfg["events"])
            ]

        for wid, cfg in targets:
            t = threading.Thread(
                target=self._deliver_with_retry,
                args=(wid, cfg["url"], cfg.get("secret", ""), body, event_type),
                kwargs={"allow_local": cfg.get("allow_local", False)},
                daemon=True,
            )
            t.start()

    def get_webhook_stats(self, webhook_id: str) -> dict[str, Any]:
        """Возвращает статистику доставки для webhook.

        Returns:
            dict с ключами: deliveries, failures, last_status, last_ts, webhook_id.

        Raises:
            KeyError: если webhook не найден.
        """
        with self._lock:
            if webhook_id not in self._webhooks:
                raise KeyError(f"Webhook не найден: {webhook_id}")
            stats = self._stats.get(webhook_id, {})
            return {
                "webhook_id": webhook_id,
                "deliveries": stats.get("deliveries", 0),
                "failures": stats.get("failures", 0),
                "last_status": stats.get("last_status", None),
                "last_ts": stats.get("last_ts", None),
            }

    # ------------------------------------------------------------------
    # IPC-обработчики (следуют паттерну handle_* из других сервисов)
    # ------------------------------------------------------------------

    def handle_register_webhook(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: register_webhook."""
        url = str(params.get("url", "")).strip()
        events = params.get("events", [])
        if not isinstance(events, list):
            raise RuntimeError("events должен быть списком строк")
        events = [str(e) for e in events]
        secret = str(params.get("secret", ""))
        # webhook_allow_local: opt-in для dev-окружений с самохостинговыми сервисами
        allow_local: bool = bool(params.get("webhook_allow_local", False))
        webhook_id = self.register_webhook(url=url, events=events, secret=secret, allow_local=allow_local)
        return {"webhook_id": webhook_id}

    def handle_unregister_webhook(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: unregister_webhook."""
        webhook_id = str(params.get("webhook_id", "")).strip()
        if not webhook_id:
            raise RuntimeError("Необходим параметр webhook_id")
        removed = self.unregister_webhook(webhook_id)
        return {"removed": removed}

    def handle_list_webhooks(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: list_webhooks."""
        return {"webhooks": self.list_webhooks()}

    # ------------------------------------------------------------------
    # Внутренняя логика доставки
    # ------------------------------------------------------------------

    def _deliver_with_retry(
        self,
        webhook_id: str,
        url: str,
        secret: str,
        body: bytes,
        event_type: str,
        allow_local: bool = False,
    ) -> None:
        """Делает POST с retry (экспоненциальный backoff, до 3 попыток)."""
        last_status: int | str | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                last_status = self._post_once(url=url, body=body, secret=secret, allow_local=allow_local)
                logger.debug(
                    "WebhookManager: %s → %s HTTP %s (попытка %d)",
                    event_type, url, last_status, attempt,
                )
                # 2xx — успех
                if isinstance(last_status, int) and 200 <= last_status < 300:
                    self._record_success(webhook_id, last_status)
                    return
                # 3xx — W1349 F1: redirect не следуется (allow_redirects=False).
                # Логируем предупреждение и считаем доставку неудачной.
                if isinstance(last_status, int) and 300 <= last_status < 400:
                    logger.warning(
                        "WebhookManager: %s вернул %s (3xx redirect не следуется, "
                        "возможная SSRF-попытка через redirect — игнорируем)",
                        url, last_status,
                    )
                    self._record_failure(webhook_id, last_status)
                    return
                # 4xx — не ретраить (постоянная ошибка клиента)
                if isinstance(last_status, int) and 400 <= last_status < 500:
                    logger.warning(
                        "WebhookManager: %s вернул %s (4xx, не ретраим)", url, last_status
                    )
                    self._record_failure(webhook_id, last_status)
                    return
            except Exception as exc:
                last_status = str(exc)
                logger.warning(
                    "WebhookManager: ошибка доставки на %s (попытка %d/%d): %s",
                    url, attempt, _MAX_RETRIES, exc,
                )

            if attempt < _MAX_RETRIES:
                delay = _BACKOFF_BASE_SEC * (2 ** (attempt - 1))  # 1s, 2s, 4s
                time.sleep(delay)  # sync context OK: runs in threading.Thread

        self._record_failure(webhook_id, last_status)

    def _post_once(self, url: str, body: bytes, secret: str, allow_local: bool = False) -> int:
        """Выполняет один HTTP POST. Возвращает HTTP status code.

        W1349 F1 fix: использует _NoRedirectHandler (allow_redirects=False) — все 3xx
        возвращаются как статус без следования редиректу, предотвращая SSRF через
        redirect chain (attacker.com/redir → 302 → 127.0.0.1/admin).
        F2 fix: читает не более _MAX_RESPONSE_BYTES байт из тела ответа.

        Raises:
            URLError / Exception при сетевой ошибке.
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "KrabEar-Webhook/1.0",
            "X-KrabEar-Event": "webhook",
        }
        if secret:
            sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()  # type: ignore[attr-defined]
            headers["X-KrabEar-Signature"] = f"sha256={sig}"

        req = Request(url, data=body, headers=headers, method="POST")
        # W1349 F1: _NoRedirectHandler blocks ALL 3xx redirects (allow_redirects=False).
        # HTTPError with the 3xx code is raised — caught below and returned as status.
        redirect_handler = _NoRedirectHandler()
        opener = urllib.request.build_opener(redirect_handler)
        try:
            with opener.open(req, timeout=_REQUEST_TIMEOUT_SEC) as resp:
                # F2: ограничиваем чтение тела ответа (64 KB)
                resp.read(_MAX_RESPONSE_BYTES)
                return resp.status
        except HTTPError as exc:
            return exc.code

    def _record_success(self, webhook_id: str, status: int) -> None:
        with self._lock:
            s = self._stats.setdefault(webhook_id, {"deliveries": 0, "failures": 0})
            s["deliveries"] = s.get("deliveries", 0) + 1
            s["last_status"] = status
            s["last_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _record_failure(self, webhook_id: str, status: Any) -> None:
        with self._lock:
            s = self._stats.setdefault(webhook_id, {"deliveries": 0, "failures": 0})
            s["failures"] = s.get("failures", 0) + 1
            s["last_status"] = status
            s["last_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
