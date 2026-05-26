"""Observability helpers for Krab Ear backend.

Sentry/GlitchTip integration (Sentry-compatible self-hosted option).
All functions are no-op when DSN is not provided — safe to ship without a DSN.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import signal
import subprocess

logger = logging.getLogger(__name__)

_sentry_initialized = False


def _read_version_from_plist() -> str | None:
    """Read CFBundleShortVersionString from the app bundle's Info.plist.

    Production source of truth for the release version — set at bundle build
    time and read by both Swift (Bundle.main) and Python here.

    Returns None if the plist cannot be found or read (dev / test runs).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    plist_path = os.path.join(repo_root, "Krab Ear.app", "Contents", "Info.plist")
    if not os.path.isfile(plist_path):
        return None
    try:
        with open(plist_path, "rb") as fh:
            data = plistlib.load(fh)
        version = data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")
        return str(version) if version else None
    except Exception:  # noqa: BLE001
        return None


def get_release_string() -> str:
    """Return the canonical Sentry release string ``krab-ear@<version>``.

    Priority:
      1. ``KRAB_EAR_RELEASE`` env var (CI / staging override; passes verbatim
         when it already starts with ``krab-ear@``).
      2. ``CFBundleShortVersionString`` from ``Krab Ear.app/Contents/Info.plist``
         (production source of truth).
      3. ``__version__`` from ``KrabEar/__version__.py`` (dev / test fallback).

    Wave 704 fix (W701): replaces inline ``f"krab-ear@{APP_VERSION}"`` so that
    Sentry release tag tracks the actual shipped Info.plist version instead of
    the (historically stale) hardcoded ``__version__.py``.
    """
    env_ver = os.environ.get("KRAB_EAR_RELEASE", "").strip()
    if env_ver:
        return env_ver if env_ver.startswith("krab-ear@") else f"krab-ear@{env_ver}"

    plist_ver = _read_version_from_plist()
    if plist_ver:
        return f"krab-ear@{plist_ver}"

    try:
        from KrabEar.__version__ import __version__ as _ver  # noqa: PLC0415
        return f"krab-ear@{_ver}"
    except ImportError:
        try:
            from __version__ import __version__ as _ver  # noqa: PLC0415
            return f"krab-ear@{_ver}"
        except ImportError:
            return "krab-ear@unknown"


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
        if dsn:
            # Записываем в privacy audit log что Sentry был заблокирован
            try:
                from backend.privacy_audit import get_privacy_audit_logger  # noqa: PLC0415
                get_privacy_audit_logger().log_event(
                    category="sentry",
                    action="blocked",
                    details={"reason": "privacy_mode_enabled"},
                )
            except Exception:  # noqa: BLE001
                pass  # privacy audit никогда не должен ломать основной поток
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


def install_signal_handlers() -> None:
    """Install Sentry-aware handlers for SIGABRT/SIGSEGV/SIGTERM.

    Sends a Sentry event with the signal name before propagating the signal
    to the default handler so the process terminates normally.

    Idempotent — multiple calls don't re-install handlers.
    No-op if Sentry SDK is not initialized (DSN not set).
    """
    if getattr(install_signal_handlers, "_installed", False):
        return
    install_signal_handlers._installed = True  # type: ignore[attr-defined]

    def _handler(signum: int, frame: object) -> None:
        signame = signal.Signals(signum).name
        try:
            import sentry_sdk  # noqa: PLC0415

            if _sentry_initialized:
                with sentry_sdk.push_scope() as scope:
                    scope.set_tag("signal", signame)
                    scope.set_level("fatal")
                    sentry_sdk.capture_message(f"Backend received {signame}")
                    sentry_sdk.flush(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass  # телеметрия не должна мешать штатному завершению
        # Re-raise via default handler so the process actually terminates.
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGABRT, signal.SIGSEGV):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # SIGSEGV / SIGABRT may not be settable in all contexts
            # (e.g. inside threads or restricted environments).
            pass


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
