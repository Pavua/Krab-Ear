"""WebhookManager — отправка событий на внешние URL (webhook-уведомления).

Поддерживает:
- Регистрацию/удаление webhook-получателей с фильтрацией по типу события.
- HMAC-SHA256 подпись тела запроса (если указан secret).
- Неблокирующую доставку через ограниченный ThreadPoolExecutor (max 4 потока).
- Retry с экспоненциальной задержкой (до 3 попыток).
- Персистентность реестра в {data_dir}/webhooks.json (chmod 0600).
- Статистику доставки на webhook.
- SSRF-защита (при регистрации И при каждой отправке):
  - Блокируются localhost, RFC1918, link-local, CGNAT (100.64.0.0/10), mDNS и cloud-metadata адреса.
  - Нестандартные нотации IP (decimal/octal/hex/IPv6-mapped) защищены через
    socket.getaddrinfo: ipaddress.ip_address() принимает только canonical dotted/colon
    нотацию и выбрасывает ValueError на decimal/hex/octal; getaddrinfo резолвит эти
    нотации в canonical IP, который затем проверяется _is_ip_safe() — http://2130706433/ невозможен.
  - DNS-rebinding полностью закрыт через IP-pinning (Gap 4 fix W1721):
    hostname резолвится ОДИН РАЗ, validated IP передаётся в pinned connection
    handler — urllib не выполняет повторную DNS-резолюцию при connect().
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
import os
import socket
import threading
import time
import http.client
import ssl
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
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

# BUG 4 fix: bounded delivery pool — prevents unbounded thread creation on event bursts.
_DELIVERY_MAX_WORKERS = 4

# MED DoS cap (wave-28): unlimited webhooks → fire() calls all of them on every event.
# 100 webhooks × 3 retry attempts × up to 10 s each = bounded CPU/thread cost.
MAX_WEBHOOKS = 100

# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

# Имена хостов, которые всегда блокируются независимо от настроек
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset({"localhost", "0.0.0.0", ""})

# Cloud metadata endpoint IPs that must always be blocked regardless of other checks
_METADATA_IPS: frozenset[str] = frozenset({
    "169.254.169.254",   # AWS / GCP / Azure IMDS
    "fd00:ec2::254",     # AWS IPv6 IMDS
})

# Gap 1 fix (W1721): CGNAT 100.64.0.0/10 is neither private nor reserved in Python's
# ipaddress module (RFC 6598 Shared Address Space).  Block it explicitly so carrier-grade
# NAT addresses cannot be used to reach internal services via a carrier's NAT gateway.
# The IPv6-mapped form ::ffff:100.64.x.x is handled by checking ipv4_mapped on IPv6Address.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_ip_safe(ip_str: str) -> tuple[bool, str | None]:
    """Check whether a single resolved or literal IP string is safe.

    Returns (is_safe, reason).  reason is None when safe.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False, f"cannot parse IP: {ip_str!r}"

    canonical = str(ip)
    if canonical in _METADATA_IPS or ip_str in _METADATA_IPS:
        return False, f"cloud metadata IP blocked ({canonical})"
    if ip.is_loopback:
        return False, f"loopback IP blocked ({canonical})"
    if ip.is_link_local:
        return False, f"link-local IP blocked ({canonical})"
    if ip.is_private:
        return False, f"private/RFC1918 IP blocked ({canonical})"
    if ip.is_multicast:
        return False, f"multicast IP blocked ({canonical})"
    if ip.is_reserved:
        return False, f"reserved IP blocked ({canonical})"
    if ip.is_unspecified:
        return False, f"unspecified IP blocked ({canonical})"
    # Gap 1 fix (W1721): CGNAT 100.64.0.0/10 (RFC 6598) — Python marks it neither
    # private nor reserved; block explicitly.  Also unwrap IPv6-mapped addresses
    # (::ffff:100.64.x.x) — ipaddress exposes the inner IPv4 via .ipv4_mapped.
    check_v4: ipaddress.IPv4Address | ipaddress.IPv6Address = ip
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        check_v4 = ip.ipv4_mapped
    if isinstance(check_v4, ipaddress.IPv4Address) and check_v4 in _CGNAT_NETWORK:
        return False, f"CGNAT/shared-address-space IP blocked ({canonical})"
    return True, None


def _resolve_and_check_host(host: str, strict: bool = False) -> tuple[bool, str | None]:
    """Resolve *host* via getaddrinfo and check every resolved IP.

    BUG 2 fix: two-stage check handles all non-standard IP notations:

    Stage 1 — ipaddress.ip_address(host): handles canonical dotted-decimal IPv4
    (e.g. "127.0.0.1"), standard IPv6 (e.g. "::1"), and IPv6-mapped IPv4 notation
    (e.g. "::ffff:127.0.0.1", "::ffff:169.254.169.254").  It does NOT parse
    decimal-encoded (2130706433), hex-encoded (0x7f000001), or octal-encoded
    (017700000001) addresses — those raise ValueError and fall through to stage 2.

    Stage 2 — socket.getaddrinfo(host): the OS resolver handles ALL alternate
    encodings including decimal/hex/octal (e.g. http://2130706433/ → 127.0.0.1).
    The resolved canonical IP is then validated by _is_ip_safe().

    WARNING: the socket.getaddrinfo() fallback (stage 2) is load-bearing for SSRF
    protection against decimal/hex/octal-encoded IPs.  Do NOT remove it — removing
    getaddrinfo would silently re-open the bypass for those notations.

    Together these two stages catch bypasses like:
      http://2130706433/           → 127.0.0.1  (decimal — caught by getaddrinfo)
      http://0x7f000001/           → 127.0.0.1  (hex     — caught by getaddrinfo)
      http://[::ffff:127.0.0.1]/  → IPv6-mapped loopback (caught by ipaddress)
      http://[::ffff:169.254.169.254]/ → IPv6-mapped metadata (caught by ipaddress)

    BUG 1 fix (DNS rebinding): called at both registration time and fire time
    (_post_once).  At fire time, ``strict=True`` is passed — a DNS resolution
    failure is treated as a hard block (cannot confirm the target is safe).
    At registration time (``strict=False``, the default), a transient DNS failure
    is allowed through; the fire-time check will catch rebinding when it fires.

    Returns (is_safe, reason).
    """
    # Step 1: try literal IP parse (handles canonical dotted IPv4, standard IPv6,
    # and IPv6-mapped IPv4 like ::ffff:127.0.0.1).
    # NOTE: ipaddress.ip_address() raises ValueError for decimal/hex/octal-encoded
    # IPs (e.g. 2130706433, 0x7f000001) — those fall through to step 2.
    try:
        ip = ipaddress.ip_address(host)
        return _is_ip_safe(str(ip))
    except ValueError:
        pass  # not a canonical literal — fall through to DNS resolution

    # Step 2: DNS resolve → check every A/AAAA record.
    # Gap 2 fix (W1721): broaden the catch to cover ALL resolution errors, not just
    # socket.gaierror.  socket.getaddrinfo can raise:
    #   UnicodeError  — invalid IDNA hostname (e.g. surrogate characters)
    #   OverflowError — port/address value out of OS range
    #   OSError       — base of socket.gaierror; catch for safety
    #   ValueError    — unexpected input to getaddrinfo
    # None of these are subclasses of socket.gaierror, so they propagated out of the
    # guard before this fix — reaching the downstream broad `except Exception` in
    # _deliver_with_retry rather than triggering a fail-closed decision here.
    # Gap 3 fix (W1721): fail-closed at registration time too (strict=False) — a host
    # whose DNS cannot be resolved at registration is rejected immediately rather than
    # deferred to fire-time.  The only exception is a purposeful opt-in via allow_local
    # (handled upstream in _is_safe_webhook_url before reaching here).
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError, ValueError) as exc:
        # Always fail-closed: at fire-time (strict=True) AND at registration (strict=False).
        # Registering an unresolvable host opens a gap where fire-time may receive a
        # transiently valid IP after an attacker engineers a brief DNS outage window.
        reason = f"DNS resolution failed for {host!r}: {exc}"
        logger.debug("WebhookManager: %s (fail-closed at %s)", reason,
                     "fire-time" if strict else "registration")
        return False, reason

    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]  # (ip, port[, flow[, scope]])
        safe, reason = _is_ip_safe(ip_str)
        if not safe:
            return False, f"resolved IP {ip_str!r} for host {host!r}: {reason}"

    return True, None


def _is_safe_webhook_url(url: str, allow_local: bool = False, strict: bool = False) -> tuple[bool, str | None]:
    """Проверяет URL на безопасность для использования в качестве webhook.

    Блокирует:
    - localhost / 0.0.0.0 / пустой хост
    - mDNS .local домены
    - IPv4 loopback (127.0.0.0/8), private (RFC1918), link-local (169.254.0.0/16)
    - IPv6 loopback (::1) и link-local
    - Cloud metadata (169.254.169.254, fd00:ec2::254)
    - Нестандартные нотации IP: decimal (2130706433), hex (0x7f000001),
      IPv6-mapped (::ffff:127.0.0.1) — BUG 2 fix: нормализация через getaddrinfo
      (decimal/hex/octal) и ipaddress (IPv6-mapped); getaddrinfo load-bearing для SSRF
    - Схемы кроме http/https

    BUG 1 (DNS rebinding) и BUG 2 (IP notation bypass) оба исправлены через
    _resolve_and_check_host: вызывается при регистрации и при каждой отправке.

    Args:
        url: URL для проверки.
        allow_local: если True — пропускает проверку SSRF (для dev-режима).
        strict: если True — DNS resolution failure блокирует (fire-time check);
                если False — DNS failure разрешается (registration-time, fire-time проверит).

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

    # BUG 2 + BUG 1 fix: resolve + canonicalise — catches all non-standard notations
    # and also serves as the fire-time re-check entry point.
    return _resolve_and_check_host(host, strict=strict)


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


# ---------------------------------------------------------------------------
# IP-pinning: close the TOCTOU DNS-rebinding window (Gap 4 fix, W1721)
# ---------------------------------------------------------------------------
# urllib resolves the hostname independently when it opens the TCP connection,
# creating a check-vs-connect race: we validate getaddrinfo result #1, then
# urllib does getaddrinfo result #2 (which may differ with TTL=0 rebinding).
#
# Fix: resolve the hostname ONCE in _post_once, validate the result, then
# connect using the pinned IP directly.  The original hostname is preserved
# as the HTTP Host header and the TLS server_hostname so that cert validation
# and SNI still work correctly against the intended server's certificate.
#
# This completely closes the rebinding window — urllib never re-resolves.


def _resolve_pinned_ip(host: str, port: int, scheme: str) -> tuple[str, socket.AddressFamily]:
    """Resolve *host* to the first safe IP, returning (ip_str, address_family).

    Raises ValueError if resolution fails or every resolved IP is blocked.
    """
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError, ValueError) as exc:
        raise ValueError(f"DNS resolution failed for {host!r}: {exc}") from exc

    if not infos:
        raise ValueError(f"No addresses resolved for {host!r}")

    for family, _type, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        safe, reason = _is_ip_safe(ip_str)
        if not safe:
            raise ValueError(
                f"Pinned IP {ip_str!r} for {host!r} blocked: {reason}"
            )
        return ip_str, family

    raise ValueError(f"All resolved IPs for {host!r} were blocked")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a pre-validated IP, not a re-resolved hostname.

    The Host header and (for HTTPS) TLS SNI / server_hostname retain the original
    hostname so certificate validation works as expected.
    """

    def __init__(self, host: str, port: int | None, pinned_ip: str, **kwargs: Any) -> None:
        super().__init__(host, port, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        # Connect to the pinned IP (already validated) instead of re-resolving the hostname.
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection variant — pins IP, preserves SNI/cert validation on original hostname."""

    def __init__(self, host: str, port: int | None, pinned_ip: str, **kwargs: Any) -> None:
        super().__init__(host, port, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        # Create raw TCP socket to the pinned IP.
        raw_sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        # Wrap in TLS — server_hostname=self.host preserves SNI and cert validation
        # against the ORIGINAL hostname, not the IP.
        ctx = self._context if hasattr(self, "_context") and self._context else ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    """urllib handler that injects the pre-pinned IP into every new connection."""

    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self._pinned_ip = pinned_ip

    def http_open(self, req):  # type: ignore[override]
        return self.do_open(
            lambda host, **kw: _PinnedHTTPConnection(host, None, self._pinned_ip, **kw),
            req,
        )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib HTTPS handler that injects the pre-pinned IP into every new connection."""

    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self._pinned_ip = pinned_ip

    def https_open(self, req):  # type: ignore[override]
        return self.do_open(
            lambda host, **kw: _PinnedHTTPSConnection(host, None, self._pinned_ip, **kw),
            req,
        )


class WebhookManager:
    """Менеджер webhook-уведомлений Krab Ear.

    Структура webhooks.json (chmod 0600 — BUG 3 fix: secrets restricted):
    {
        "<webhook_id>": {
            "url": str,
            "events": [str, ...],   # пустой список = все события
            "secret": str,          # пустая строка = без подписи
                                    # WARNING: secret stored plaintext; file is 0600
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
        # BUG 4 fix: bounded delivery pool — caps concurrent webhook delivery threads.
        # max_workers=4 prevents unbounded thread creation under event bursts.
        self._executor = ThreadPoolExecutor(
            max_workers=_DELIVERY_MAX_WORKERS,
            thread_name_prefix="webhook-deliver",
        )

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
        else:
            self._sanitize_loaded_allow_local()

    def _sanitize_loaded_allow_local(self) -> None:
        """Не доверяем персистированному ``allow_local`` (SSRF defence-in-depth).

        ``allow_local`` НИКОГДА не выставляется через IPC (`handle_register_webhook`
        жёстко передаёт False) — единственный путь True это прямой Python-вызов в dev.
        Подложенная/затампленная ``webhooks.json`` запись с ``allow_local: true`` +
        внутренним URL обошла бы весь SSRF-pinning на fire-time. Поэтому при загрузке
        с диска: если URL небезопасен по non-local правилам, снимаем allow_local —
        fire-time SSRF guard снова заблокирует внутренний адрес. Публичные URL (где
        allow_local и так безвреден) не затрагиваются.
        """
        if not isinstance(self._webhooks, dict):
            return
        for wid, cfg in self._webhooks.items():
            if not isinstance(cfg, dict) or not cfg.get("allow_local"):
                continue
            url = cfg.get("url", "")
            safe, _reason = _is_safe_webhook_url(url, allow_local=False)
            if not safe:
                cfg["allow_local"] = False
                logger.warning(
                    "WebhookManager: stripped persisted allow_local=True for webhook %s "
                    "(url not safe under non-local rules; SSRF guard re-engaged)", wid
                )

    def _save(self) -> None:
        """Атомарно сохраняет реестр в файл (под _lock), chmod 0600.

        BUG 3 fix: HMAC secrets are stored in plaintext — restrict file permissions
        to 0600 (owner read/write only) to prevent other local users reading secrets.
        Full at-rest encryption is out of scope; 0600 is the safe minimum.
        The permission is applied to the tmp file before rename so there is no window
        where secrets are world-readable.
        """
        tmp = self._webhooks_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._webhooks, ensure_ascii=False, indent=2), encoding="utf-8")
            # BUG 3 fix: harden before rename — no world-readable window
            os.chmod(tmp, 0o600)
            tmp.replace(self._webhooks_path)
            # Also harden destination in case it existed with loose perms
            try:
                os.chmod(self._webhooks_path, 0o600)
            except OSError:
                pass  # best-effort
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
                         Только для программного вызова из доверенного Python-кода.
                         IPC-обработчик handle_register_webhook всегда передаёт False
                         (wave1763 MED SSRF-bypass fix).

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
        # wave-1770 LOW: enforce minimum HMAC secret length.
        # Empty secret means "no signature" (legitimate — some endpoints don't support it).
        # A non-empty but short secret (< 16 chars) is effectively weak and trivially brutable.
        _MIN_SECRET_LEN = 16
        if secret and len(secret) < _MIN_SECRET_LEN:
            raise ValueError(
                f"HMAC secret слишком короткий ({len(secret)} симв.); "
                f"минимум {_MIN_SECRET_LEN} символов или пустая строка (без подписи)"
            )

        # MED DoS cap (wave-28): reject if already at MAX_WEBHOOKS.
        # fire() iterates all webhooks on every event — unbounded registration is a
        # CPU/thread DoS.  SSRF guard (allow_local=False IPC enforcement) still holds.
        with self._lock:
            if len(self._webhooks) >= MAX_WEBHOOKS:
                raise ValueError(
                    f"webhook_limit_reached: максимум {MAX_WEBHOOKS} webhook-ов"
                )

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

        BUG 4 fix: использует bounded ThreadPoolExecutor (max 4 workers) вместо
        unbounded per-request daemon threads — burst событий не создаёт сотни потоков.
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
            # BUG 4 fix: submit to bounded pool instead of spawning an unbounded daemon thread
            self._executor.submit(
                self._deliver_with_retry,
                wid,
                cfg["url"],
                cfg.get("secret", ""),
                body,
                event_type,
                cfg.get("allow_local", False),
            )

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
        """IPC: register_webhook.

        Безопасность: параметр ``webhook_allow_local`` намеренно игнорируется.
        SSRF-защита (localhost / RFC1918 / cloud-metadata) ВСЕГДА активна для
        webhook-ов, зарегистрированных через IPC, — allow_local нельзя отключить
        через тело запроса (MED SSRF-bypass, wave1763).

        Для регистрации webhook на локальный self-hosted сервис вызовите
        register_webhook() напрямую из Python-кода с явным allow_local=True.
        """
        url = str(params.get("url", "")).strip()
        events = params.get("events", [])
        if not isinstance(events, list):
            raise RuntimeError("events должен быть списком строк")
        events = [str(e) for e in events]
        secret = str(params.get("secret", ""))
        # Исправление SSRF (wave1763 MED): allow_local НЕ читается из IPC-параметров.
        # Недоверенный IPC-клиент не может обойти SSRF-защиту, передав
        # webhook_allow_local=True в теле запроса. Всегда False.
        try:
            webhook_id = self.register_webhook(url=url, events=events, secret=secret, allow_local=False)
        except ValueError as exc:
            # MED DoS cap (wave-28): limit reached → structured error response.
            msg = str(exc)
            if "webhook_limit_reached" in msg:
                return {"ok": False, "reason": "webhook_limit_reached"}
            raise
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
                time.sleep(delay)  # sync context OK: runs in ThreadPoolExecutor worker

        self._record_failure(webhook_id, last_status)

    def _post_once(self, url: str, body: bytes, secret: str, allow_local: bool = False) -> int:
        """Выполняет один HTTP POST. Возвращает HTTP status code.

        Gap 4 fix (W1721) — IP-pinning closes TOCTOU DNS-rebinding window:
        Previously the fire-time SSRF guard called getaddrinfo (resolution #1) and
        validated the result, but then urllib called getaddrinfo again independently
        (resolution #2) when opening the TCP connection.  With TTL=0 rebinding, #2
        could return a different (internal) address.

        Fix: _resolve_pinned_ip() resolves and validates ONCE, then
        _PinnedHTTP[S]Handler injects the validated IP into every HTTPConnection.connect()
        call so urllib never re-resolves.  The original hostname is preserved for the
        HTTP Host header and TLS SNI/server_hostname so cert validation still works.

        W1349 F1 fix: использует _NoRedirectHandler (allow_redirects=False) — все 3xx
        возвращаются как статус без следования редиректу, предотвращая SSRF через
        redirect chain (attacker.com/redir → 302 → 127.0.0.1/admin).
        F2 fix: читает не более _MAX_RESPONSE_BYTES байт из тела ответа.

        Raises:
            URLError / Exception при сетевой ошибке.
            ValueError: если URL не проходит fire-time SSRF check или DNS resolve fails.
        """
        parsed = urlparse(url)
        scheme = parsed.scheme  # "http" or "https"
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if scheme == "https" else 80)

        if not allow_local:
            # Gap 4 fix: resolve and validate in ONE call; the returned pinned_ip is used
            # for the actual TCP connection — urllib will not re-resolve.
            # _resolve_pinned_ip raises ValueError (fail-closed) on any DNS / safety error.
            try:
                pinned_ip, _ = _resolve_pinned_ip(host, port, scheme)
            except ValueError as exc:
                raise ValueError(
                    f"WebhookManager: fire-time SSRF check failed for {url!r}: {exc}"
                ) from exc
            # Build handlers: pinned connection handler + no-redirect handler.
            if scheme == "https":
                conn_handler: urllib.request.BaseHandler = _PinnedHTTPSHandler(pinned_ip)
            else:
                conn_handler = _PinnedHTTPHandler(pinned_ip)
        else:
            conn_handler = None  # type: ignore[assignment]

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
        handlers: list[urllib.request.BaseHandler] = [redirect_handler]
        if conn_handler is not None:
            handlers.append(conn_handler)
        opener = urllib.request.build_opener(*handlers)
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

    # ------------------------------------------------------------------
    # Privacy purge
    # ------------------------------------------------------------------

    def purge_all(self) -> None:
        """Полная очистка всех webhook-ов и секретов (privacy-wipe).

        #7 (MED W1766): webhooks.json хранит HMAC-секреты в открытом виде.
        Метод:
        1. Захватывает _lock.
        2. Очищает in-memory реестр и статистику.
        3. Вызывает _save() — записывает пустой JSON-объект в webhooks.json.
        4. Удаляет webhooks.json с диска (missing_ok=True).

        Гарантирует отсутствие plaintext секретов после возврата.
        Идемпотентен: повторный вызов при отсутствии файла не бросает исключений.
        """
        with self._lock:
            self._webhooks.clear()
            self._stats.clear()
            # Перезаписываем файл пустым объектом (атомарно через tmp + rename),
            # затем сразу удаляем — так не остаётся окна, где файл непустой.
            self._save()
            try:
                self._webhooks_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("WebhookManager.purge_all: не удалось удалить %s: %s",
                               self._webhooks_path, exc)
        logger.info("WebhookManager.purge_all: реестр и секреты очищены")
