"""Observability helpers for Krab Ear backend.

Sentry/GlitchTip integration (Sentry-compatible self-hosted option).
All functions are no-op when DSN is not provided — safe to ship without a DSN.
"""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

_sentry_initialized = False


def release_from_git() -> str:
    """Determine release version from git describe.

    Returns a string like 'v2.0.0-87-gabcdef' or 'krab-ear@unknown' on failure.
    Runs git describe --tags --always --dirty.
    """
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            if version:
                return version
    except Exception:  # noqa: BLE001
        pass
    return "krab-ear@unknown"


def init_sentry(
    dsn: str | None,
    environment: str = "production",
    release: str | None = None,
    settings: dict | None = None,
) -> bool:
    """Init Sentry (or GlitchTip) if DSN provided.

    Returns True if SDK was initialized, False if DSN absent, privacy mode
    enabled, or import fails.
    Compatible with any Sentry-protocol server (sentry.io, self-hosted GlitchTip, etc.).

    If *release* is None, calls :func:`release_from_git` to determine the
    release string automatically from the current git commit/tag.

    Args:
        dsn: Sentry DSN string. Empty or None → no-op.
        environment: Sentry environment tag.
        release: Release string. None → determined from git.
        settings: Current settings dict. If ``privacy_mode_enabled`` is True,
                  init is skipped regardless of DSN.
    """
    global _sentry_initialized

    if settings and settings.get("privacy_mode_enabled"):
        logger.info("Sentry init skipped — privacy_mode_enabled=True")
        return False

    if not dsn:
        logger.debug("Sentry: DSN не задан — telemetry отключена")
        return False

    resolved_release = release if release is not None else release_from_git()

    try:
        import sentry_sdk  # noqa: PLC0415  (lazy import — optional dep)

        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=resolved_release,
            traces_sample_rate=0.05,
            send_default_pii=False,  # конфиденциальность
        )
        _sentry_initialized = True
        logger.info(
            "Sentry инициализирован release=%s env=%s",
            resolved_release,
            environment,
        )
        return True

    except ImportError:
        logger.warning("sentry-sdk не установлен — telemetry отключена")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sentry init failed: %s", exc)
        return False


def is_sentry_initialized() -> bool:
    """Возвращает True если Sentry SDK был успешно инициализирован."""
    return _sentry_initialized


def capture_exception(exc: Exception, component: str | None = None) -> None:
    """Отправляет исключение в Sentry если SDK инициализирован.

    No-op если DSN не задан или sentry-sdk не установлен.
    """
    if not _sentry_initialized:
        return

    try:
        import sentry_sdk  # noqa: PLC0415

        with sentry_sdk.push_scope() as scope:
            if component:
                scope.set_tag("component", component)
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        pass  # никогда не падаем из-за telemetry


# ---------------------------------------------------------------------------
# Breadcrumbs
# ---------------------------------------------------------------------------

#: IPC methods excluded from breadcrumb recording (high-frequency / low-value).
_BREADCRUMB_EXCLUDED_METHODS: frozenset[str] = frozenset({
    "ping",
    "get_recording_state",
    "get_call_assist_state",
    "live_subs_ingest",
    "get_throttle_stats",
    "get_event_log",
    "get_event_stats",
})

_PHONE_PATTERN = re.compile(r"(\+?\d[\d\s\-]{3,})(\d{4})")


def mask_phone(phone: str) -> str:
    """Маскирует номер телефона, оставляя только последние 4 цифры.

    Пример: '+34666123456' → '+34*****3456'
    Безопасно возвращает исходную строку если паттерн не совпадает.
    """
    m = _PHONE_PATTERN.fullmatch(phone.strip())
    if m:
        return "*****" + m.group(2)
    # Если не полное совпадение — маскируем всё кроме последних 4 цифр
    digits = re.sub(r"\D", "", phone)
    if len(digits) >= 4:
        return "*****" + digits[-4:]
    return "*****"


def add_breadcrumb(
    category: str,
    message: str,
    level: str = "info",
    data: dict | None = None,
) -> None:
    """Добавляет breadcrumb в Sentry для трассировки user actions до crash.

    No-op если Sentry не инициализирован или sentry-sdk не установлен.
    Privacy: не передавать текст транскрипций, только metadata.

    Args:
        category: категория события (recording, transcription, translation, ipc, call).
        message:  короткое описание действия.
        level:    severity ('debug', 'info', 'warning', 'error').
        data:     дополнительные metadata (length, language, confidence и т.п.).
    """
    if not _sentry_initialized:
        return

    try:
        import sentry_sdk  # noqa: PLC0415

        sentry_sdk.add_breadcrumb(
            category=category,
            message=message,
            level=level,
            data=data or {},
        )
    except Exception:  # noqa: BLE001
        pass  # telemetry никогда не должна ломать основной поток
