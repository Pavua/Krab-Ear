"""Active LM Studio HTTP probe thread.

LLMHttpProbe periodically calls rewriter.warmup() to detect whether LM Studio
is reachable. On state transitions it pushes KrabError events (alive→dead) and
emits EventBus events (dead→alive). Interval adapts when cold-load latency is
detected.

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
from datetime import datetime, timezone
from typing import Callable

from backend.error_bus import KrabError

logger = logging.getLogger("KrabEar.Backend.LLMHttpProbe")


class LLMHttpProbe:
    """Background thread that actively probes LM Studio HTTP endpoint.

    Parameters
    ----------
    rewriter:
        LLMRewriter (or duck-typed stand-in). Must expose:
        - ``warmup() -> None`` — may raise on failure
        - ``_last_latency_ms: int | None`` — last measured latency in ms
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
        If ``rewriter._last_latency_ms`` exceeds this value after a successful
        warmup, the probe interval is extended (default 3000 ms).
    max_interval_sec:
        Upper bound for the adaptive interval (default 300.0 s).
    recovery_consecutive:
        Number of consecutive fast-response ticks required before the interval
        resets back to ``base_interval_sec`` (default 3).
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
        self._cold_load_threshold_ms = cold_load_threshold_ms
        self._max_interval_sec = max_interval_sec
        self._recovery_consecutive = recovery_consecutive

        # Adaptive interval state
        self._current_interval_sec: float = base_interval_sec
        self._fast_streak: int = 0

        # Probe liveness state: None = unknown, True = alive, False = dead
        self._alive: bool | None = None

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
        """Single probe tick. Checks settings, calls warmup, adapts interval,
        and handles state transitions."""
        try:
            settings = self._settings_provider()
        except Exception:
            logger.debug("LLMHttpProbe: settings_provider raised, skipping tick")
            return

        if not settings.get("llm_rewrite_enabled", False):
            # Feature disabled — skip probe entirely, don't change state
            return

        old_alive = self._alive
        latency_ms: int | None = None

        try:
            self._rewriter.warmup()
            new_alive = True
            # Read latency after successful warmup
            latency_ms = getattr(self._rewriter, "_last_latency_ms", None)
        except Exception as exc:
            new_alive = False
            logger.debug("LLMHttpProbe: warmup failed — %s", exc)

        # Adapt interval based on latency
        self._adapt_interval(new_alive=new_alive, latency_ms=latency_ms)

        # Handle state transitions
        if old_alive != new_alive:
            self._on_state_change(old=old_alive, new=new_alive, latency_ms=latency_ms)

        self._alive = new_alive

    def _adapt_interval(self, *, new_alive: bool, latency_ms: int | None) -> None:
        """Adjust probe interval based on latency.

        - If latency > threshold (cold load): extend interval (×10, capped at max).
        - Otherwise: track fast streak; reset to base after recovery_consecutive ticks.
        """
        if not new_alive:
            # Dead — don't touch interval
            return

        if latency_ms is not None and latency_ms > self._cold_load_threshold_ms:
            self._current_interval_sec = min(
                self._current_interval_sec * 10,
                self._max_interval_sec,
            )
            self._fast_streak = 0
            logger.debug(
                "LLMHttpProbe: cold-load latency %dms → interval extended to %.1fs",
                latency_ms,
                self._current_interval_sec,
            )
        else:
            self._fast_streak += 1
            if self._fast_streak >= self._recovery_consecutive:
                if self._current_interval_sec != self._base_interval_sec:
                    logger.debug(
                        "LLMHttpProbe: %d consecutive fast ticks → interval reset to %.1fs",
                        self._fast_streak,
                        self._base_interval_sec,
                    )
                self._current_interval_sec = self._base_interval_sec

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
                    f"LLMHttpProbe: rewriter.warmup() raised; "
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
