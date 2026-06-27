"""ModelDownloader — фоновая загрузка STT-моделей из HuggingFace с прогрессом.

Решает проблему «тихого провала» первого STT-запроса на свежей установке:
модель отсутствует в кэше → записи нет → транскрипция молча падает.

IPC API (регистрируется в BackendService._build_dispatch_table):
  download_stt_model   {model_id?} → {ok, status: "started"|"already_cached", model_id}
  get_stt_model_status {model_id?} → {model_id, cached, downloading, status, pct, path}

EventBus event:
  "model_download.progress" payload: {model_id, status, pct, downloaded, total}
    status: "downloading" | "done" | "error"
    pct: 0..100 float
    downloaded/total: byte counts (int, 0 if unknown)
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.event_bus import EventBus

logger = logging.getLogger("KrabEar.Backend.ModelDownloader")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum interval between progress EventBus emits (seconds) to avoid flooding.
_PROGRESS_THROTTLE_S: float = 0.5
# Minimum pct change to trigger a new emit regardless of throttle.
_PROGRESS_MIN_PCT_DELTA: float = 1.0


# ---------------------------------------------------------------------------
# Internal progress tqdm subclass
# ---------------------------------------------------------------------------

def _make_tqdm_class(downloader: "ModelDownloader", model_id: str):  # type: ignore[return]
    """Returns a tqdm subclass that forwards update() calls to the downloader.

    We import tqdm lazily so tests can avoid the import without trouble.
    """
    try:
        from tqdm import tqdm as _tqdm  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover — tqdm always installed in prod
        return None

    class _ProgressTqdm(_tqdm):  # type: ignore[misc]
        """Subclasses tqdm.update() to emit EventBus progress events."""

        def update(self, n: int = 1) -> bool | None:
            result = super().update(n)
            # self.n = current bytes, self.total = total bytes (may be None)
            downloaded = self.n or 0
            total = self.total or 0
            pct = (downloaded / total * 100.0) if total > 0 else 0.0
            downloader._on_progress(model_id, downloaded, total, pct)
            return result

    return _ProgressTqdm


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class _DownloadState:
    """Thread-safe download state for a single model_id."""

    __slots__ = (
        "model_id", "status", "pct", "downloaded", "total",
        "error_msg", "cache_path", "_lock",
    )

    def __init__(self, model_id: str) -> None:
        self.model_id: str = model_id
        self.status: str = "idle"  # idle | downloading | done | error
        self.pct: float = 0.0
        self.downloaded: int = 0
        self.total: int = 0
        self.error_msg: str = ""
        self.cache_path: str = ""
        self._lock = threading.Lock()

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "model_id": self.model_id,
                "status": self.status,
                "pct": self.pct,
                "downloaded": self.downloaded,
                "total": self.total,
                "error_msg": self.error_msg,
                "cache_path": self.cache_path,
            }

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class ModelDownloader:
    """Manages background HuggingFace model downloads with EventBus progress.

    Thread-safety:
      - Only one download runs at a time (serialised by self._dl_lock).
      - State for each model_id is stored in self._states (dict access under
        self._states_lock).
      - Progress emit is throttled (≥_PROGRESS_THROTTLE_S or ≥1% change).
    """

    def __init__(self, event_bus: "EventBus | None" = None) -> None:
        # One download at a time (idempotent: cached models skip immediately).
        self._dl_lock = threading.Lock()
        # Per-model state registry.
        self._states: dict[str, _DownloadState] = {}
        self._states_lock = threading.Lock()
        self._event_bus = event_bus
        # Progress throttle per model_id: {model_id: (last_emit_time, last_pct)}
        self._throttle: dict[str, tuple[float, float]] = {}

    # ------------------------------------------------------------------
    # Public API called by IPC handlers
    # ------------------------------------------------------------------

    def start_download(self, model_id: str) -> str:
        """Start a background download for *model_id*.

        Returns:
            "already_cached" if the model is already in the HF cache.
            "started" if a background thread was launched.
            "in_progress" if a download for this model_id is already running.
        """
        if self._is_cached(model_id):
            logger.info("ModelDownloader: модель %s уже в кэше — пропускаем", model_id)
            state = self._get_or_create_state(model_id)
            cache_path = self._model_cache_path(model_id)
            state.update(status="done", pct=100.0, cache_path=str(cache_path))
            return "already_cached"

        state = self._get_or_create_state(model_id)
        with state._lock:
            if state.status == "downloading":
                logger.info("ModelDownloader: загрузка %s уже идёт", model_id)
                return "in_progress"
            state.status = "downloading"
            state.pct = 0.0
            state.error_msg = ""

        t = threading.Thread(
            target=self._download_worker,
            args=(model_id,),
            name=f"ModelDL-{model_id.replace('/', '-')}",
            daemon=True,
        )
        t.start()
        logger.info("ModelDownloader: фоновая загрузка %s запущена", model_id)
        return "started"

    def get_status(self, model_id: str) -> dict[str, Any]:
        """Return status dict for *model_id* — cached + downloading + state fields."""
        cached = self._is_cached(model_id)
        state = self._get_or_create_state(model_id)
        state_dict = state.to_dict()
        # Sync status=done when cached externally (e.g. user ran prefetch manually).
        if cached and state_dict["status"] not in ("done", "downloading"):
            state.update(status="done", pct=100.0, cache_path=str(self._model_cache_path(model_id)))
            state_dict = state.to_dict()
        return {
            "model_id": model_id,
            "cached": cached,
            "downloading": state_dict["status"] == "downloading",
            "status": state_dict["status"],
            "pct": state_dict["pct"],
            "downloaded": state_dict["downloaded"],
            "total": state_dict["total"],
            "error_msg": state_dict["error_msg"],
            "path": state_dict["cache_path"],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_state(self, model_id: str) -> _DownloadState:
        with self._states_lock:
            if model_id not in self._states:
                self._states[model_id] = _DownloadState(model_id)
            return self._states[model_id]

    def _is_cached(self, model_id: str) -> bool:
        """Check HuggingFace hub cache presence (same logic as startup_diagnostics)."""
        hf_home_env = ""
        try:
            import os
            hf_home_env = os.environ.get("HF_HOME", "")
        except Exception:
            pass

        if hf_home_env:
            hf_cache = Path(hf_home_env) / "hub"
        else:
            hf_cache = Path.home() / ".cache" / "huggingface" / "hub"

        model_dir_name = "models--" + model_id.replace("/", "--")
        model_dir = hf_cache / model_dir_name
        # A model is considered cached when the snapshots sub-dir exists and is non-empty.
        snapshots = model_dir / "snapshots"
        if not snapshots.exists():
            return False
        try:
            return any(True for _ in snapshots.iterdir())
        except OSError:
            return False

    def _model_cache_path(self, model_id: str) -> Path:
        """Return the expected model folder path in the HF cache (may not exist)."""
        import os
        hf_home_env = os.environ.get("HF_HOME", "")
        if hf_home_env:
            hf_cache = Path(hf_home_env) / "hub"
        else:
            hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
        return hf_cache / ("models--" + model_id.replace("/", "--"))

    # ------------------------------------------------------------------
    # Progress throttling + EventBus emit
    # ------------------------------------------------------------------

    def _on_progress(
        self,
        model_id: str,
        downloaded: int,
        total: int,
        pct: float,
    ) -> None:
        """Called by tqdm subclass on each byte-level update."""
        state = self._get_or_create_state(model_id)
        state.update(pct=pct, downloaded=downloaded, total=total)

        now = time.monotonic()
        last_time, last_pct = self._throttle.get(model_id, (0.0, -999.0))
        pct_delta = abs(pct - last_pct)

        if now - last_time < _PROGRESS_THROTTLE_S and pct_delta < _PROGRESS_MIN_PCT_DELTA:
            return

        self._throttle[model_id] = (now, pct)
        self._emit(model_id, "downloading", pct, downloaded, total)

    def _emit(
        self,
        model_id: str,
        status: str,
        pct: float,
        downloaded: int = 0,
        total: int = 0,
    ) -> None:
        if self._event_bus is None:
            return
        try:
            self._event_bus.emit(
                "model_download.progress",
                {
                    "model_id": model_id,
                    "status": status,
                    "pct": round(pct, 2),
                    "downloaded": downloaded,
                    "total": total,
                },
            )
        except Exception:
            logger.debug("ModelDownloader: не удалось эмитировать событие прогресса", exc_info=True)

    # ------------------------------------------------------------------
    # Download worker
    # ------------------------------------------------------------------

    def _download_worker(self, model_id: str) -> None:
        """Background thread: acquire lock, download, update state, emit events."""
        state = self._get_or_create_state(model_id)

        with self._dl_lock:
            # Double-check after acquiring the lock (another thread may have finished).
            if self._is_cached(model_id):
                logger.info("ModelDownloader: %s уже кэширован после получения блокировки", model_id)
                cache_path = self._model_cache_path(model_id)
                state.update(status="done", pct=100.0, cache_path=str(cache_path))
                self._emit(model_id, "done", 100.0)
                return

            # Emit initial progress.
            self._emit(model_id, "downloading", 0.0)
            logger.info("ModelDownloader: начинаем snapshot_download для %s", model_id)

            try:
                from huggingface_hub import snapshot_download  # type: ignore[import-untyped]
            except ImportError as exc:
                msg = f"huggingface_hub не установлен: {exc}"
                logger.error("ModelDownloader: %s", msg)
                state.update(status="error", error_msg=msg)
                self._emit(model_id, "error", 0.0)
                return

            try:
                import os
                hf_token: str | None = os.environ.get("HF_TOKEN") or None

                tqdm_cls = _make_tqdm_class(self, model_id)
                kwargs: dict[str, Any] = {
                    "repo_id": model_id,
                    "repo_type": "model",
                    "token": hf_token,
                    # Skip non-MLX weights (same filter as prefetch_whisper_models.command).
                    "ignore_patterns": ["*.bin", "*.pt", "*.ot"],
                }
                if tqdm_cls is not None:
                    kwargs["tqdm_class"] = tqdm_cls

                local_path = snapshot_download(**kwargs)

                cache_path = str(local_path)
                state.update(status="done", pct=100.0, cache_path=cache_path)
                self._emit(model_id, "done", 100.0)
                logger.info("ModelDownloader: %s успешно загружен → %s", model_id, cache_path)

            except Exception as exc:
                msg = str(exc)
                logger.error("ModelDownloader: ошибка загрузки %s: %s", model_id, msg)
                state.update(status="error", error_msg=msg)
                self._emit(model_id, "error", state.pct)
