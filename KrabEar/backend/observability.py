"""Средства наблюдаемости backend Krab Ear.

Модуль связывает backend с Sentry/GlitchTip. Без DSN все функции безопасно
превращаются в no-op, поэтому локальная поставка не зависит от телеметрии.
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

# ---------------------------------------------------------------------------
# PII / path redaction for Sentry events (W1193 F4)
# ---------------------------------------------------------------------------

#: Pattern matching absolute /Users/<username>/... paths (spaces allowed in sub-paths).
_HOME_PATH_RE = re.compile(r"/Users/[^/\"']+(/[^\"']*)")

#: Marker used when a transcript path is stripped entirely.
_TRANSCRIPT_REDACTED = "<transcript-path-redacted>"

#: Prefix fragment that identifies KrabEar transcript paths.
_TRANSCRIPT_PATH_FRAGMENT = "KrabEar/transcripts/"


def _redact_string(value: str) -> str:
    """Redact file-system paths and transcript filenames from *value*.

    Two rules applied in order:
    1. Any path (absolute ``/Users/…`` or home-relative ``~/…``) that
       contains the ``KrabEar/transcripts/`` fragment is replaced entirely
       with ``<transcript-path-redacted>``.
    2. Remaining ``/Users/<username>/...`` absolute paths are collapsed to
       ``~/...`` (home-relative form).
    """
    # Rule 1 — drop transcript paths entirely (both /Users/... and ~/... forms).
    # Note: paths like ~/Library/Application Support/KrabEar/transcripts/...
    # may contain spaces, so we use [^\"']* rather than [^\s\"']*.
    if _TRANSCRIPT_PATH_FRAGMENT in value:
        value = re.sub(
            r"/Users/[^/\s]+/[^\"']*KrabEar/transcripts/[^\"']*",
            _TRANSCRIPT_REDACTED,
            value,
        )
        value = re.sub(
            r"~/[^\"']*KrabEar/transcripts/[^\"']*",
            _TRANSCRIPT_REDACTED,
            value,
        )

    # Rule 2 — collapse remaining /Users/<name>/... → ~/...
    value = _HOME_PATH_RE.sub(r"~\1", value)
    return value


def _redact_value(obj: object) -> object:
    """Recursively redact strings inside dicts, lists, and plain strings."""
    if isinstance(obj, str):
        return _redact_string(obj)
    if isinstance(obj, dict):
        return {k: _redact_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_value(item) for item in obj]
    return obj


def _sentry_before_send(event: dict, hint: object) -> dict | None:  # noqa: ARG001
    """``before_send`` callback: redact PII / local paths from Sentry events.

    - Replaces ``/Users/<username>/...`` with ``~/...``.
    - Removes ``KrabEar/transcripts/...`` filenames entirely.
    - Returns *event* (possibly mutated) or ``None`` to drop the event.

    Called by sentry-sdk synchronously before the event is enqueued for
    transmission — safe to mutate the dict in-place and return it.

    Fields walked (W1483 F1+F2):
    - ``exception.values[].stacktrace.frames[]`` — filename, abs_path, module, vars
    - ``extra``, ``contexts``, ``tags`` — top-level metadata dicts
    - ``message`` — top-level message string
    - ``breadcrumbs.values[]`` — each crumb's ``data`` dict and ``message`` string
    - ``logentry`` — ``message`` string and ``params`` (F2)
    - ``request`` — ``data``, ``query_string``, ``cookies`` (F2)
    """
    try:
        # Walk exception values → stacktrace frames → filename / abs_path / vars.
        exception = event.get("exception") or {}
        for exc_value in (exception.get("values") or []):
            stacktrace = exc_value.get("stacktrace") or {}
            for frame in (stacktrace.get("frames") or []):
                for key in ("filename", "abs_path", "module"):
                    if key in frame and isinstance(frame[key], str):
                        frame[key] = _redact_string(frame[key])
                # Redact vars dict (local variables in the frame).
                if "vars" in frame:
                    frame["vars"] = _redact_value(frame["vars"])

        # Walk top-level extra / contexts dicts.
        for top_key in ("extra", "contexts", "tags"):
            if top_key in event:
                event[top_key] = _redact_value(event[top_key])

        # Redact message string if present.
        if "message" in event and isinstance(event["message"], str):
            event["message"] = _redact_string(event["message"])

        # W1483 F1 — Walk breadcrumbs: each crumb's data dict and message string.
        breadcrumbs = event.get("breadcrumbs") or {}
        for crumb in (breadcrumbs.get("values") or []):
            if isinstance(crumb, dict):
                if "message" in crumb and isinstance(crumb["message"], str):
                    crumb["message"] = _redact_string(crumb["message"])
                if "data" in crumb and isinstance(crumb["data"], dict):
                    crumb["data"] = _redact_value(crumb["data"])

        # W1483 F2 — Walk logentry: message string and params.
        logentry = event.get("logentry")
        if isinstance(logentry, dict):
            if "message" in logentry and isinstance(logentry["message"], str):
                logentry["message"] = _redact_string(logentry["message"])
            if "params" in logentry:
                logentry["params"] = _redact_value(logentry["params"])

        # W1483 F2 — Walk request: data, query_string, cookies.
        request = event.get("request")
        if isinstance(request, dict):
            for req_key in ("data", "query_string", "cookies"):
                if req_key in request:
                    request[req_key] = _redact_value(request[req_key])

    except Exception:  # noqa: BLE001
        # Never let redaction break crash reporting.
        pass

    return event


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
        # W1601 / W1599 F1 HIGH: if Sentry was already initialised in a previous
        # call, clear the flag now so that capture_exception() and add_breadcrumb()
        # become no-ops immediately.  The Sentry SDK keeps its own internal state
        # but our guard flag is what prevents new data from being forwarded.
        if _sentry_initialized:
            logger.info(
                "init_sentry: privacy_mode now ON — clearing _sentry_initialized"
            )
            _sentry_initialized = False
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
            include_local_variables=False,  # W1193 F4: hide local file paths
            before_send=_sentry_before_send,  # W1193 F4: redact PII / paths
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


def flush_sentry(timeout: float = 2.0) -> None:
    """Flush pending Sentry events to the server.

    No-op if Sentry SDK is not initialized or not installed.
    Safe to call from signal handlers — never raises.

    Args:
        timeout: максимальное время ожидания отправки (секунд).
    """
    if not _sentry_initialized:
        return
    try:
        import sentry_sdk  # noqa: PLC0415

        sentry_sdk.flush(timeout=timeout)
    except Exception:  # noqa: BLE001
        pass  # телеметрия не должна мешать штатному завершению


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

    Пример: '+34666123456' → '*****3456' (код страны тоже скрыт — приватнее).
    wave-1770: docstring приведён в соответствие с фактическим поведением
    (раньше показывал ошибочный пример '+34*****3456').
    Никогда не возвращает исходную строку — даже при несовпадении паттерна
    маскирует всё кроме последних 4 цифр (fail-safe для приватности).
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
    """Установить Sentry-aware обработчики SIGABRT/SIGSEGV.

    SIGTERM намеренно исключён: ``service.main()`` только просит IPC-loop выйти,
    после чего единый ``finally`` выполняет IPC → workers → metadata → Sentry.

    Перед передачей аварийного сигнала default-handler-у отправляет событие
    Sentry с именем сигнала.

    Идемпотентен; без инициализированного Sentry SDK ничего не делает.
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
        # Возвращаем default-handler, чтобы аварийный сигнал завершил процесс.
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    # SIGTERM здесь нет: им владеет signal-safe callback из service.main().
    for sig in (signal.SIGABRT, signal.SIGSEGV):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # В некоторых контекстах (например, не-main thread) установка
            # SIGSEGV / SIGABRT запрещена самим Python/runtime.
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
