"""Диагностика при старте Krab Ear backend.

Запускает набор проверок при инициализации сервиса и возвращает
структурированный StartupReport с итоговым статусом, предупреждениями и ошибками.
"""

from __future__ import annotations

import importlib
import logging
import os
import shutil
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from KrabEar.__version__ import __version__ as APP_VERSION
from backend.observability import add_breadcrumb

logger = logging.getLogger("KrabEar.Backend.StartupDiagnostics")

# Порог свободного места: критический уровень — 1 ГБ
DISK_MIN_FREE_GB = 1.0

# Sentinel status used while the background LM Studio probe is still running.
# Consumers (to_dict, aggregation) treat "pending" like "ok" (non-blocking).
_LM_STUDIO_PENDING = "pending"

# Минимальная версия Python
MIN_PYTHON_VERSION = (3, 12)

# Пакеты, обязательные для работы STT/audio
REQUIRED_PACKAGES = [
    "mlx_whisper",
    "sounddevice",
    "numpy",
    "pydantic",
]


@dataclass
class CheckResult:
    """Результат одной диагностической проверки."""

    name: str
    status: str  # "ok" | "warning" | "error"
    message: str
    duration_ms: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class StartupReport:
    """Агрегированный отчёт о запуске."""

    status: str  # "ready" | "degraded" | "critical"
    checks: list[CheckResult]
    startup_time_ms: float
    warnings: list[str]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Сериализует отчёт в JSON-совместимый словарь."""
        return {
            "status": self.status,
            "version": APP_VERSION,
            "startup_time_ms": round(self.startup_time_ms, 2),
            "warnings": self.warnings,
            "errors": self.errors,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "duration_ms": round(c.duration_ms, 2),
                    "details": c.details,
                }
                for c in self.checks
            ],
        }


class StartupDiagnostics:
    """Запускает полный набор проверок при старте бэкенда."""

    def __init__(
        self,
        data_dir: Path | str | None = None,
        socket_path: Path | str | None = None,
        cache_ttl_sec: float = 60.0,
        *,
        socket_ownership_snapshot_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._data_dir: Path | None = Path(data_dir) if data_dir else None
        self._socket_path: Path | str | None = socket_path
        # Спека 2026-08-22 socket-ownership: immutable snapshot текущего claim
        # позволяет отличить СЕБЯ (claimed/listening) от чужого listener'а.
        self._socket_ownership_snapshot_getter = socket_ownership_snapshot_getter
        self._cache_ttl_sec = cache_ttl_sec
        self._cached_report: StartupReport | None = None
        self._cache_ts: float = 0.0

        # Late-injected by BackendService after construction (Wave 490).
        # If None, startup.stt_model_cache_miss errors are not pushed to error bus.
        self._error_bus: Any | None = None

        # W1672 (W1615 F2 MED): LM Studio reachability result is populated
        # asynchronously in a daemon thread so __init__ (and run_all_checks) return
        # immediately even when LM Studio is offline.
        self._lm_studio_result: CheckResult | None = None
        self._lm_studio_lock: threading.Lock = threading.Lock()
        self._lm_studio_done: threading.Event = threading.Event()
        # Start the background check immediately so the result is available
        # as soon as possible (typically within the ~2 s connect timeout).
        self._start_lm_studio_background_check()

    # ------------------------------------------------------------------
    # Публичный метод
    # ------------------------------------------------------------------

    def run_all_checks(self, *, force: bool = False) -> StartupReport:
        """Выполняет все проверки и возвращает StartupReport.

        Каждая проверка независима — сбой одной не влияет на остальные.
        Результат кэшируется на cache_ttl_sec секунд; force=True сбрасывает кэш.

        Итоговый статус:
          "critical" — хотя бы одна проверка со статусом "error"
          "degraded"  — хотя бы одна проверка со статусом "warning"
          "ready"     — все проверки "ok"
        """
        now = time.monotonic()
        if (
            not force
            and self._cached_report is not None
            and (now - self._cache_ts) < self._cache_ttl_sec
        ):
            return self._cached_report

        suite_start = time.monotonic()

        checks: list[CheckResult] = [
            self._check_python_version(),
            self._check_required_packages(),
            self._check_data_dir_writable(),
            self._check_socket_path_available(),
            self._check_ffmpeg_available(),
            self._check_huggingface_token(),
            self._check_stt_model_cached(),
            self._check_lm_studio_reachable(),
            self._check_disk_space(),
            self._check_audio_devices(),
        ]

        suite_elapsed_ms = (time.monotonic() - suite_start) * 1000.0

        warnings: list[str] = [c.message for c in checks if c.status == "warning"]
        errors: list[str] = [c.message for c in checks if c.status == "error"]

        if errors:
            overall = "critical"
        elif warnings:
            overall = "degraded"
        else:
            overall = "ready"

        report = StartupReport(
            status=overall,
            checks=checks,
            startup_time_ms=suite_elapsed_ms,
            warnings=warnings,
            errors=errors,
        )
        add_breadcrumb(
            category="startup",
            message="run_all_checks",
            level="info" if overall == "ready" else "warning" if overall == "degraded" else "error",
            data={
                "status": overall,
                "total_checks": len(checks),
                "failed_count": len(errors),
                "warning_count": len(warnings),
                "duration_ms": round(suite_elapsed_ms),
                "ok": overall == "ready",
            },
        )
        self._cached_report = report
        self._cache_ts = time.monotonic()
        return report

    def critical_errors(self) -> list[CheckResult]:
        """Возвращает только проверки со статусом 'error' из последнего отчёта.

        Если кэш пуст — запускает run_all_checks() автоматически.
        """
        if self._cached_report is None:
            self.run_all_checks()
        assert self._cached_report is not None
        return [c for c in self._cached_report.checks if c.status == "error"]

    def invalidate_cache(self) -> None:
        """Сбрасывает кэшированный отчёт."""
        self._cached_report = None
        self._cache_ts = 0.0

    # ------------------------------------------------------------------
    # Отдельные проверки
    # ------------------------------------------------------------------

    def _check_python_version(self) -> CheckResult:
        """Python version ≥ 3.12."""
        t0 = time.monotonic()
        try:
            ver = sys.version_info
            ver_str = f"{ver.major}.{ver.minor}.{ver.micro}"
            if (ver.major, ver.minor) >= MIN_PYTHON_VERSION:
                return CheckResult(
                    name="python_version",
                    status="ok",
                    message=f"Python {ver_str}",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                    details={"version": ver_str, "required": f"{MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}"},
                )
            else:
                return CheckResult(
                    name="python_version",
                    status="error",
                    message=f"Python {ver_str} < {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]} — обновите интерпретатор",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                    details={"version": ver_str, "required": f"{MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}"},
                )
        except Exception as exc:
            return CheckResult(
                name="python_version",
                status="error",
                message=f"Не удалось определить версию Python: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

    def _check_required_packages(self) -> CheckResult:
        """Все обязательные пакеты импортируются без ошибок."""
        t0 = time.monotonic()
        missing: list[str] = []
        for pkg in REQUIRED_PACKAGES:
            try:
                importlib.import_module(pkg)
            except ImportError:
                missing.append(pkg)
        elapsed = (time.monotonic() - t0) * 1000.0
        if not missing:
            return CheckResult(
                name="required_packages",
                status="ok",
                message=f"Все {len(REQUIRED_PACKAGES)} обязательных пакетов доступны",
                duration_ms=elapsed,
                details={"packages": REQUIRED_PACKAGES},
            )
        return CheckResult(
            name="required_packages",
            status="error",
            message=f"Отсутствуют пакеты: {', '.join(missing)}",
            duration_ms=elapsed,
            details={"missing": missing, "packages": REQUIRED_PACKAGES},
        )

    def _check_data_dir_writable(self) -> CheckResult:
        """Директория данных доступна для записи.

        Предусловие: StateStore уже создал директорию до вызова диагностики,
        поэтому mkdir здесь избыточен и может породить гонку.
        Исключения разбиваются на два типа:
          - PermissionError → настоящая ошибка прав доступа (статус "error").
          - FileNotFoundError → транзиентная гонка (директория ещё создаётся);
            делаем одну попытку повтора после минимальной паузы.
        """
        t0 = time.monotonic()
        try:
            if self._data_dir is None:
                from core.config import settings
                data_dir = Path(settings.DATA_DIR)
            else:
                data_dir = self._data_dir

            test_file = data_dir / ".startup_write_test"

            def _try_write() -> None:
                test_file.write_text("ok")
                test_file.unlink(missing_ok=True)

            try:
                _try_write()
            except PermissionError as exc:
                return CheckResult(
                    name="data_dir_writable",
                    status="error",
                    message=f"Нет прав на запись в директорию данных: {exc}",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                    details={"path": str(data_dir)},
                )
            except FileNotFoundError:
                # Транзиентная гонка или первый запуск: директория ещё не создана.
                # W122: создаём директорию и повторяем попытку (восстановлено в W1707).
                try:
                    data_dir.mkdir(parents=True, exist_ok=True)
                    _try_write()
                except Exception as exc2:
                    return CheckResult(
                        name="data_dir_writable",
                        status="error",
                        message=f"Директория данных недоступна для записи (после повтора): {exc2}",
                        duration_ms=(time.monotonic() - t0) * 1000.0,
                        details={"path": str(data_dir)},
                    )

            return CheckResult(
                name="data_dir_writable",
                status="ok",
                message=f"Директория данных доступна для записи: {data_dir}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
                details={"path": str(data_dir)},
            )
        except Exception as exc:
            return CheckResult(
                name="data_dir_writable",
                status="error",
                message=f"Директория данных недоступна для записи: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

    def _check_socket_path_available(self) -> CheckResult:
        """Состояние canonical socket endpoint'а с учётом собственного claim'а.

        Спека 2026-08-22 socket-ownership: путь — ТОЧНЫЙ (переданный или
        ``default_socket_path``), «занято» отличает себя от чужого listener'а
        по snapshot'у claim'а + identity inode; probe read-only.
        """
        t0 = time.monotonic()
        from backend.socket_ownership import (
            SocketOwnershipState,
            SocketPathStatus,
            default_socket_path,
            probe_unix_socket_path,
        )

        def _result(status: str, message: str, **details: Any) -> CheckResult:
            return CheckResult(
                name="socket_path",
                status=status,
                message=message,
                duration_ms=(time.monotonic() - t0) * 1000.0,
                details=details,
            )

        try:
            if self._socket_path is not None:
                sock_path = Path(self._socket_path)
            elif self._data_dir is not None:
                sock_path = default_socket_path(self._data_dir)
            else:
                try:
                    from core.config import settings
                    sock_path = default_socket_path(Path(settings.DATA_DIR))
                except Exception:
                    sock_path = default_socket_path(
                        Path.home() / ".krab_ear_data"
                    )

            snapshot = None
            if self._socket_ownership_snapshot_getter is not None:
                try:
                    snapshot = self._socket_ownership_snapshot_getter()
                except Exception:
                    snapshot = None

            own_state = snapshot.state if snapshot is not None else None

            # Ревью F7: доказательство «слушаем мы» — БЕЗ connect'а к самому
            # себе (каждый TTL-прогон занимал бы слот семафора и handler-тред,
            # а под нагрузкой connect-таймаут давал бы ложный warning).
            # lstat + сравнение с bound_identity достаточно: state=LISTENING
            # держим мы сами, значит совпавший inode = наш живой listener.
            if (
                own_state is SocketOwnershipState.LISTENING
                and snapshot.bound_identity is not None
            ):
                try:
                    _st = os.lstat(sock_path)
                except OSError:
                    _st = None
                if _st is not None and stat.S_ISSOCK(_st.st_mode) and (
                    (_st.st_dev, _st.st_ino)
                    == (snapshot.bound_identity.device, snapshot.bound_identity.inode)
                ):
                    return _result(
                        "ok",
                        f"На {sock_path} слушаем мы (inode совпадает)",
                        path=str(sock_path),
                        path_status="listening",
                        ownership_state=own_state.value,
                        owner="self",
                        stale=False,
                        exists=True,
                    )

            probe = probe_unix_socket_path(sock_path)
            base = {
                "path": str(sock_path),
                "path_status": probe.status.value,
                "ownership_state": (
                    snapshot.state.value if snapshot is not None else None
                ),
                "owner": None,
                # Совместимость прежней схемы details (спека: ключи только
                # добавляются): stale/exists выводимы из path_status.
                "stale": probe.status is SocketPathStatus.STALE,
                "exists": probe.status is not SocketPathStatus.MISSING,
            }

            if own_state is not None and own_state is not SocketOwnershipState.UNCLAIMED:
                if (
                    own_state is SocketOwnershipState.CLAIMED
                    and probe.status is SocketPathStatus.MISSING
                ):
                    base["owner"] = "self"
                    return _result(
                        "ok",
                        f"Endpoint {sock_path} закреплён за нами (до bind)",
                        **base,
                    )
                if (
                    own_state is SocketOwnershipState.LISTENING
                    and probe.status is SocketPathStatus.LISTENING
                    and snapshot.bound_identity is not None
                    and probe.identity is not None
                    and (probe.identity.device, probe.identity.inode)
                    == (
                        snapshot.bound_identity.device,
                        snapshot.bound_identity.inode,
                    )
                ):
                    base["owner"] = "self"
                    return _result(
                        "ok",
                        f"На {sock_path} слушаем мы (inode совпадает)",
                        **base,
                    )
                # Self-state противоречит probe — не маскируем поломку.
                return _result(
                    "warning",
                    (
                        f"Состояние claim'а ({own_state.value}) противоречит "
                        f"наблюдению пути ({probe.status.value})"
                    ),
                    **base,
                )

            if probe.status is SocketPathStatus.MISSING:
                return _result(
                    "ok", f"Путь к сокету доступен: {sock_path}", **base
                )
            if probe.status is SocketPathStatus.STALE:
                return _result(
                    "ok",
                    f"Обнаружен стейловый сокет {sock_path} — будет очищен под claim'ом",
                    **base,
                )
            if probe.status is SocketPathStatus.LISTENING:
                base["owner"] = "other"
                return _result(
                    "warning",
                    f"На сокете {sock_path} уже слушает другой процесс",
                    **base,
                )
            return _result(
                "warning",
                f"Путь {sock_path} занят не-сокетом или неоднозначен ({probe.error})",
                **base,
            )
        except Exception as exc:
            return _result("warning", f"Не удалось проверить путь к сокету: {exc}")

    def _check_ffmpeg_available(self) -> CheckResult:
        """ffmpeg доступен в PATH."""
        t0 = time.monotonic()
        try:
            ffmpeg_path = shutil.which("ffmpeg")
            elapsed = (time.monotonic() - t0) * 1000.0
            if ffmpeg_path:
                return CheckResult(
                    name="ffmpeg",
                    status="ok",
                    message=f"ffmpeg найден: {ffmpeg_path}",
                    duration_ms=elapsed,
                    details={"path": ffmpeg_path},
                )
            return CheckResult(
                name="ffmpeg",
                status="warning",
                message="ffmpeg не найден в PATH — импорт аудио недоступен",
                duration_ms=elapsed,
                details={"path": None},
            )
        except Exception as exc:
            return CheckResult(
                name="ffmpeg",
                status="warning",
                message=f"Не удалось проверить ffmpeg: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

    def _check_huggingface_token(self) -> CheckResult:
        """HuggingFace токен задан (нужен для pyannote diarization)."""
        t0 = time.monotonic()
        try:
            # Проверяем через конфиг и env-переменные
            token = ""
            try:
                from core.config import settings
                token = settings.HF_TOKEN
            except Exception:
                pass
            if not token:
                token = os.environ.get("HF_TOKEN", "") or os.environ.get("HUGGINGFACE_TOKEN", "")

            elapsed = (time.monotonic() - t0) * 1000.0
            if token:
                return CheckResult(
                    name="hf_token",
                    status="ok",
                    message="HuggingFace токен задан",
                    duration_ms=elapsed,
                    details={"present": True},
                )
            return CheckResult(
                name="hf_token",
                status="warning",
                message="HuggingFace токен не задан — диаризация (pyannote) будет недоступна",
                duration_ms=elapsed,
                details={"present": False},
            )
        except Exception as exc:
            return CheckResult(
                name="hf_token",
                status="warning",
                message=f"Не удалось проверить HF_TOKEN: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

    def _check_stt_model_cached(self) -> CheckResult:
        """STT модель (mlx-whisper) присутствует в кэше Hugging Face."""
        t0 = time.monotonic()
        try:
            try:
                from core.config import settings
                model_name = settings.MODEL_BALANCED
            except Exception:
                model_name = "mlx-community/whisper-large-v3-turbo"

            # Расположение кэша HF: сначала HF_HOME env, затем дефолт ~/.cache/huggingface/hub/
            hf_home_env = os.environ.get("HF_HOME", "")
            if hf_home_env:
                hf_cache = Path(hf_home_env) / "hub"
            else:
                hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
            if not hf_cache.exists():
                # Wave 490: push startup.stt_model_cache_miss to error bus
                self._push_stt_cache_miss_error(model_name)
                return CheckResult(
                    name="stt_model_cached",
                    status="warning",
                    message=f"HF кэш не найден — модель {model_name} будет загружена при первом запуске",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                    details={"model": model_name, "cached": False, "cache_dir": str(hf_cache)},
                )

            # Ищем папку модели (формат: models--{owner}--{name})
            model_dir_name = "models--" + model_name.replace("/", "--")
            model_dir = hf_cache / model_dir_name
            cached = model_dir.exists()
            elapsed = (time.monotonic() - t0) * 1000.0
            if cached:
                return CheckResult(
                    name="stt_model_cached",
                    status="ok",
                    message=f"STT модель {model_name} присутствует в кэше",
                    duration_ms=elapsed,
                    details={"model": model_name, "cached": True, "cache_dir": str(model_dir)},
                )
            # Wave 490: push startup.stt_model_cache_miss to error bus
            self._push_stt_cache_miss_error(model_name)
            return CheckResult(
                name="stt_model_cached",
                status="warning",
                message=f"STT модель {model_name} отсутствует в кэше — первый запуск займёт больше времени",
                duration_ms=elapsed,
                details={"model": model_name, "cached": False, "cache_dir": str(hf_cache)},
            )
        except Exception as exc:
            return CheckResult(
                name="stt_model_cached",
                status="warning",
                message=f"Не удалось проверить кэш STT модели: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

    # ------------------------------------------------------------------
    # W1672: non-blocking LM Studio reachability
    # ------------------------------------------------------------------

    def _start_lm_studio_background_check(self) -> None:
        """Запускает проверку LM Studio в фоновом daemon-потоке.

        Вызывается один раз из __init__.  Повторный вызов перезапускает
        проверку — полезно для тестов, где нужно сбросить состояние.
        """
        t = threading.Thread(
            target=self._run_lm_studio_check_in_background,
            name="StartupDiag-LMStudio",
            daemon=True,
        )
        t.start()

    def _run_lm_studio_check_in_background(self) -> None:
        """Выполняет реальную TCP-проверку LM Studio и сохраняет результат."""
        result = self._do_lm_studio_check()
        with self._lm_studio_lock:
            self._lm_studio_result = result
        self._lm_studio_done.set()

    def _do_lm_studio_check(self) -> CheckResult:
        """Синхронная часть проверки LM Studio (вызывается из фонового потока)."""
        t0 = time.monotonic()
        try:
            from core.config import settings
            if not settings.LLM_ENABLED:
                return CheckResult(
                    name="lm_studio",
                    status="ok",
                    message="LLM не включён (LLM_ENABLED=False) — проверка пропущена",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                    details={"enabled": False},
                )

            base_url = settings.LLM_BASE_URL
            # Парсируем host:port из URL типа "http://localhost:1234/v1"
            import urllib.parse
            parsed = urllib.parse.urlparse(base_url)
            host = parsed.hostname or "localhost"
            port = parsed.port or 1234

            try:
                sock = socket.create_connection((host, port), timeout=2.0)
                sock.close()
                elapsed = (time.monotonic() - t0) * 1000.0
                return CheckResult(
                    name="lm_studio",
                    status="ok",
                    message=f"LM Studio доступен на {host}:{port}",
                    duration_ms=elapsed,
                    details={"enabled": True, "host": host, "port": port},
                )
            except (ConnectionRefusedError, OSError, socket.timeout):
                elapsed = (time.monotonic() - t0) * 1000.0
                return CheckResult(
                    name="lm_studio",
                    status="warning",
                    message=f"LM Studio недоступен на {host}:{port} — LLM rewriter отключится через circuit breaker",
                    duration_ms=elapsed,
                    details={"enabled": True, "host": host, "port": port, "reachable": False},
                )
        except Exception as exc:
            return CheckResult(
                name="lm_studio",
                status="warning",
                message=f"Не удалось проверить LM Studio: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

    def _check_lm_studio_reachable(self) -> CheckResult:
        """LM Studio reachability check — returns immediately (W1672).

        If the background probe has already completed the real result is
        returned; otherwise a lightweight "pending" placeholder is returned
        so that run_all_checks() is never blocked by a slow TCP connect.

        The placeholder has status "ok" (non-critical) so the overall startup
        status is not degraded just because LM Studio hasn't been probed yet.
        Callers that want the final result can call wait_lm_studio_check().
        """
        with self._lm_studio_lock:
            result = self._lm_studio_result
        if result is not None:
            return result
        # Background probe is still running — return a pending placeholder.
        return CheckResult(
            name="lm_studio",
            status="ok",
            message="LM Studio reachability check pending (проверяется в фоне)",
            duration_ms=0.0,
            details={"pending": True},
        )

    def wait_lm_studio_check(self, timeout: float = 3.0) -> CheckResult | None:
        """Ожидает завершения фонового LM Studio check и возвращает результат.

        Используется в тестах и в _handle_get_startup_diagnostics когда нужен
        финальный статус (force=True), а не pending-плейсхолдер.

        Возвращает None если таймаут истёк раньше завершения проверки.
        """
        if self._lm_studio_done.wait(timeout=timeout):
            with self._lm_studio_lock:
                return self._lm_studio_result
        return None

    def _check_disk_space(self) -> CheckResult:
        """Свободного места на диске больше 1 ГБ."""
        t0 = time.monotonic()
        try:
            if self._data_dir is not None:
                check_path = self._data_dir if self._data_dir.exists() else self._data_dir.parent
            else:
                try:
                    from core.config import settings
                    p = Path(settings.DATA_DIR)
                    check_path = p if p.exists() else p.parent
                except Exception:
                    check_path = Path.home()

            usage = shutil.disk_usage(str(check_path))
            free_gb = round(usage.free / (1024 ** 3), 2)
            elapsed = (time.monotonic() - t0) * 1000.0

            if free_gb >= DISK_MIN_FREE_GB:
                return CheckResult(
                    name="disk_space",
                    status="ok",
                    message=f"Свободно {free_gb} ГБ на диске",
                    duration_ms=elapsed,
                    details={"free_gb": free_gb, "min_gb": DISK_MIN_FREE_GB},
                )
            return CheckResult(
                name="disk_space",
                status="error",
                message=f"Мало места на диске: {free_gb} ГБ (минимум {DISK_MIN_FREE_GB} ГБ)",
                duration_ms=elapsed,
                details={"free_gb": free_gb, "min_gb": DISK_MIN_FREE_GB},
            )
        except Exception as exc:
            return CheckResult(
                name="disk_space",
                status="error",
                message=f"Не удалось проверить место на диске: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

    def _check_audio_devices(self) -> CheckResult:
        """Доступны аудиоустройства ввода (микрофон)."""
        t0 = time.monotonic()
        try:
            import sounddevice as sd  # type: ignore

            devices = sd.query_devices()
            input_devices = [d for d in devices if d.get("max_input_channels", 0) > 0]
            count = len(input_devices)
            elapsed = (time.monotonic() - t0) * 1000.0

            if count == 0:
                return CheckResult(
                    name="audio_devices",
                    status="warning",
                    message="Не найдено ни одного аудиоустройства ввода",
                    duration_ms=elapsed,
                    details={"count": 0},
                )

            default_name: str | None = None
            try:
                default_info = sd.query_devices(kind="input")
                default_name = default_info.get("name") if isinstance(default_info, dict) else None
            except Exception:
                pass

            return CheckResult(
                name="audio_devices",
                status="ok",
                message=f"Найдено {count} аудиоустройств ввода",
                duration_ms=elapsed,
                details={"count": count, "default": default_name},
            )
        except ImportError:
            return CheckResult(
                name="audio_devices",
                status="error",
                message="sounddevice не установлен — запись звука недоступна",
                duration_ms=(time.monotonic() - t0) * 1000.0,
                details={"count": 0},
            )
        except Exception as exc:
            return CheckResult(
                name="audio_devices",
                status="warning",
                message=f"Не удалось опросить аудиоустройства: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
                details={"count": 0},
            )

    # ------------------------------------------------------------------
    # Error bus helpers (Wave 490)
    # ------------------------------------------------------------------

    def _push_stt_cache_miss_error(self, model_name: str) -> None:
        """Push startup.stt_model_cache_miss to error bus. Never raises.

        Wave 490: fired when Whisper HF model is not cached locally.
        Dedupe 86400s ensures at most one toast per day.
        """
        error_bus = self._error_bus
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get("startup.stt_model_cache_miss", {})
            err = KrabError(
                severity="warn",
                component="startup",
                code="startup.stt_model_cache_miss",
                message_user=entry.get(
                    "user_msg_ru",
                    "Модель Whisper отсутствует в кэше — первая транскрибация задержится на минуты.",
                ),
                message_debug=f"STT model not cached: {model_name}",
                timestamp=datetime.now(timezone.utc),
                context={"model": model_name},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception:
            logger.exception("StartupDiagnostics: startup.stt_model_cache_miss push failed")
