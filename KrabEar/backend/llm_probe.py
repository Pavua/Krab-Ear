"""Active LM Studio HTTP probe thread.

LLMHttpProbe periodically calls rewriter.passive_health_check() to detect
whether LM Studio is reachable and has our target model loaded.  This avoids
the JIT reload churn that POST /v1/chat/completions caused (gemma-4 6.86 GB
was evicted every idle cycle → 5-7 s reload → timeout → repeat).

On state transitions it pushes KrabError events (alive→dead) and emits
EventBus events (dead→alive).  When LM Studio is reachable but the model is
not in the loaded list, a deduplicated info-severity
``rewriter.model_evicted`` event is pushed so the user gets one diagnostic
without toast spam.

Usage::

    probe = LLMHttpProbe(
        rewriter=llm_rewriter_instance,
        error_bus=error_bus_instance,
        event_bus=event_bus_instance,
        settings_provider=lambda: current_settings_dict,
    )
    probe.start()
    # ... application runs ...
    probe.stop()
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from backend.error_bus import KrabError

logger = logging.getLogger("KrabEar.Backend.LLMHttpProbe")


class LLMHttpProbe:
    """Background thread that actively probes LM Studio HTTP endpoint.

    Uses ``rewriter.passive_health_check()`` (GET /v1/models) rather than
    POST /v1/chat/completions to avoid triggering JIT model reloads.

    Parameters
    ----------
    rewriter:
        LLMRewriter (or duck-typed stand-in). Must expose:
        - ``passive_health_check() -> tuple[bool, bool]`` —
          returns (is_reachable, has_target_model); never raises.
    error_bus:
        ErrorBus instance; ``push(KrabError)`` called on alive→dead.
    event_bus:
        EventBus instance; ``emit(event_name, payload)`` called on dead→alive.
    settings_provider:
        Zero-argument callable returning a dict with at minimum::
            {"llm_rewrite_enabled": bool}
    base_interval_sec:
        Normal probe interval in seconds (default 30.0).
    cold_load_threshold_ms:
        Kept for API compatibility; no longer used since GET /models is always
        fast (~50 ms). May be removed in a future cleanup pass.
    max_interval_sec:
        Kept for API compatibility; no longer used.
    recovery_consecutive:
        Kept for API compatibility; no longer used.
    """

    def __init__(
        self,
        rewriter,
        error_bus,
        event_bus,
        settings_provider: Callable[[], dict],
        base_interval_sec: float = 30.0,
        cold_load_threshold_ms: int = 3000,
        max_interval_sec: float = 300.0,
        recovery_consecutive: int = 3,
    ) -> None:
        self._rewriter = rewriter
        self._error_bus = error_bus
        self._event_bus = event_bus
        self._settings_provider = settings_provider

        self._base_interval_sec = base_interval_sec
        # Kept for API/test compatibility; GET /models is always fast.
        self._cold_load_threshold_ms = cold_load_threshold_ms
        self._max_interval_sec = max_interval_sec
        self._recovery_consecutive = recovery_consecutive

        # Interval is now fixed — GET /models latency is ~50 ms, no adaptation needed.
        self._current_interval_sec: float = base_interval_sec
        self._fast_streak: int = 0  # kept for API compatibility

        # Probe liveness state: None = unknown, True = alive, False = dead
        self._alive: bool | None = None

        # Dedupe tracker for rewriter.model_evicted (emit at most once per 600 s)
        self._last_model_evicted_ts: float | None = None
        _MODEL_EVICTED_DEDUPE_SEC = 600
        self._model_evicted_dedupe_sec: int = _MODEL_EVICTED_DEDUPE_SEC

        # Thread management
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background probe thread. Idempotent — no-op if already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="LLMHttpProbe",
            daemon=True,
        )
        self._thread.start()
        logger.debug("LLMHttpProbe started (interval=%.1fs)", self._current_interval_sec)

    def stop(self) -> None:
        """Stop the background probe thread. Idempotent — no-op if not running."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        logger.debug("LLMHttpProbe stopped")

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main probe loop — waits for interval then calls _tick()."""
        while not self._stop_event.wait(self._current_interval_sec):
            self._tick()

    def _tick(self) -> None:
        """Single probe tick. Checks settings, calls passive_health_check,
        and handles state transitions."""
        try:
            settings = self._settings_provider()
        except Exception:
            logger.debug("LLMHttpProbe: settings_provider raised, skipping tick")
            return

        if settings.get("privacy_mode_enabled", False):
            # Privacy mode active — do not probe LM Studio (no outbound calls)
            return

        if not settings.get("llm_rewrite_enabled", False):
            # Feature disabled — skip probe entirely, don't change state
            return

        old_alive = self._alive

        # Use passive GET /v1/models — never triggers JIT model reload.
        reachable, has_model = self._rewriter.passive_health_check()
        new_alive = reachable and has_model

        # If LM Studio is up but our model was evicted, emit a deduplicated diagnostic.
        if reachable and not has_model:
            self._maybe_emit_model_evicted()

        # Handle state transitions
        if old_alive != new_alive:
            self._on_state_change(old=old_alive, new=new_alive, latency_ms=None)

        self._alive = new_alive

    def _maybe_emit_model_evicted(self) -> None:
        """Push rewriter.model_evicted info KrabError at most once per dedupe window."""
        now = time.monotonic()
        if (
            self._last_model_evicted_ts is not None
            and (now - self._last_model_evicted_ts) < self._model_evicted_dedupe_sec
        ):
            return  # dedupe — already emitted recently
        self._last_model_evicted_ts = now

        ts = datetime.now(timezone.utc).isoformat()
        err = KrabError(
            severity="info",
            component="rewriter",
            code="rewriter.model_evicted",
            message_user="LM Studio доступен, но модель выгружена из памяти",
            message_debug=(
                f"LLMHttpProbe: passive_health_check reachable=True has_model=False; ts={ts}"
            ),
            timestamp=datetime.now(timezone.utc),
            context={},
            actionable=False,
            action_id=None,
        )
        try:
            self._error_bus.push(err)
        except Exception as exc:
            logger.warning("LLMHttpProbe: error_bus.push(model_evicted) failed — %s", exc)
        logger.info("LLMHttpProbe: model evicted (reachable but not loaded)")

    def _adapt_interval(self, *, new_alive: bool, latency_ms: int | None) -> None:
        """No-op: GET /v1/models is always fast (~50 ms), no adaptive interval needed.

        Kept for API compatibility. The _current_interval_sec stays at base_interval_sec.
        """

    def _on_state_change(
        self,
        old: bool | None,
        new: bool,
        latency_ms: int | None,
    ) -> None:
        """Emit events/errors when liveness state changes.

        - alive → dead (new=False): push KrabError(code='rewriter.unavailable')
        - dead → alive (old=False, new=True): emit 'rewriter_recovered' event
        - None → True: initial discovery of alive state — no event (not a recovery)
        - None → False: initial discovery of dead state — push error
        """
        ts = datetime.now(timezone.utc).isoformat()

        if not new:
            # Alive → dead OR unknown → dead: push error
            err = KrabError(
                severity="info",
                component="rewriter",
                code="rewriter.unavailable",
                message_user="LM Studio недоступен (active probe)",
                message_debug=(
                    f"LLMHttpProbe: passive_health_check returned (False, *); "
                    f"previous_state={old!r}; ts={ts}"
                ),
                timestamp=datetime.now(timezone.utc),
                context={
                    "previous_state": str(old),
                    "latency_ms": latency_ms,
                },
                actionable=False,
                action_id=None,
            )
            try:
                self._error_bus.push(err)
            except Exception as exc:
                logger.warning("LLMHttpProbe: error_bus.push failed — %s", exc)
            logger.info("LLMHttpProbe: state %r → dead; pushed rewriter.unavailable", old)

        elif old is False and new is True:
            # Dead → alive: emit recovered event
            payload = {
                "ts": ts,
                "latency_ms": latency_ms,
            }
            try:
                self._event_bus.emit("rewriter_recovered", payload)
            except Exception as exc:
                logger.warning("LLMHttpProbe: event_bus.emit failed — %s", exc)
            logger.info(
                "LLMHttpProbe: dead → alive; emitted rewriter_recovered (latency=%s ms)",
                latency_ms,
            )
        # Note: None → True (initial alive discovery) is intentionally silent.
