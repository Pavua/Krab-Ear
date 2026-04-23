"""Observability helpers for Krab Ear backend.

Sentry/GlitchTip integration (Sentry-compatible self-hosted option).
All functions are no-op when DSN is not provided — safe to ship without a DSN.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_sentry_initialized = False


def init_sentry(
    dsn: str | None,
    environment: str = "production",
    release: str | None = None,
) -> bool:
    """Init Sentry (or GlitchTip) if DSN provided.

    Returns True if SDK was initialized, False if DSN absent or import fails.
    Compatible with any Sentry-protocol server (sentry.io, self-hosted GlitchTip, etc.).
    """
    global _sentry_initialized

    if not dsn:
        logger.debug("Sentry: DSN не задан — telemetry отключена")
        return False

    try:
        import sentry_sdk  # noqa: PLC0415  (lazy import — optional dep)

        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release or "krab-ear@unknown",
            traces_sample_rate=0.05,
            send_default_pii=False,  # конфиденциальность
        )
        _sentry_initialized = True
        logger.info(
            "Sentry инициализирован",
            extra={"environment": environment, "release": release or "krab-ear@unknown"},
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
