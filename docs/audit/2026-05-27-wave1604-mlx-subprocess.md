# Audit W1604 — `core/mlx_subprocess.py` first-pass

**Date:** 2026-05-27  
**Auditor:** sub-agent W1604 (first dedicated audit of this module)  
**Scope:** `KrabEar/core/mlx_subprocess.py` (314 LOC) — MLX inference watchdog  
**Related tests:** `tests/test_mlx_subprocess.py`, `tests/test_mlx_recovery.py`, `tests/test_error_bus_phase_b_wave78.py`

---

## Architecture summary

`mlx_subprocess.py` is NOT a subprocess-based watchdog despite its name. It is a **threading watchdog**: `MLXWatchdog.run_with_timeout()` spawns a daemon thread, runs the user-supplied `fn()` (which calls `mlx_whisper.transcribe()`), and times the join. On timeout it performs an unbounded `thread.join()` before raising `MLXTimeoutError` to prevent the W1358 SIGSEGV race (documented in-code). The name "mlx_subprocess" is a historical misnomer — the module docstring itself explains the subprocess approach was rejected in favour of this simpler threading model.

---

## Findings

### F1 HIGH — Variants loop short-circuits: only first variant ever attempts recovery

**File:** `core/engine.py` lines 1952–1964 (caller of this module)  
**Impact:** The three-variant retry loop in `_transcribe_mlx_model_data` is intended to handle different library versions by falling back from `condition_on_previous_text` + `no_speech_threshold` to simpler params. However, when `recovery_enabled=True`, the watchdog wraps only the **first** `return get_watchdog().run_with_timeout(...)` call. A `return` inside the `for params in variants` loop means the loop never advances to variants[1] or variants[2] on a successful call — which is correct — but also means that if `MLXTimeoutError` is raised it propagates immediately **out of the `with mlx_lock()` block**, skipping the remaining variants entirely. This is by design per the comment ("MLXTimeoutError — not TypeError, not caught below"), but creates an asymmetry: with `recovery_enabled=False` all three variants are attempted; with `recovery_enabled=True` a timeout on variant[0] marks the whole model unavailable without trying the simpler variants. There is no test covering this asymmetry.

**Recommendation:** Document the intentional short-circuit explicitly in the loop body, or add a test asserting that MLXTimeoutError correctly skips all remaining variants.

---

### F2 MED — Module name is a misnomer; CLAUDE.md description is incorrect

**File:** `KrabEar/core/mlx_subprocess.py` module docstring, `CLAUDE.md`  
**Impact:** CLAUDE.md states: `mlx_subprocess.py — MLX inference watchdog: runs MLX transcription in a subprocess with a configurable timeout and auto-recovery on GPU hang.` The module docstring itself documents why subprocess was rejected (lines 14–21). The module implements a **threading watchdog**, not a subprocess. This misleads new contributors into expecting fork/IPC mechanics and is likely why this module received no dedicated audit until now.

**Recommendation:** Update CLAUDE.md entry for `mlx_subprocess.py` to say "threading watchdog" instead of "subprocess". Optionally rename the module to `mlx_watchdog.py` in a future refactor (low priority, breaking change).

---

### F3 MED — Unbounded join on GPU hang stalls HealthMonitor ping → circuit breaker fires

**File:** `core/mlx_subprocess.py` lines 152–178  
**Impact:** The W1358 race-guard correctly performs `thread.join()` (unbounded) after a timed join reveals a live thread. This is intentional: the comment says "BackendSupervisor (HealthMonitor) will SIGTERM → respawn the process if that occurs." However, `HealthMonitor.swift` uses a 3-second ping (`handle_ping` IPC) with 2 consecutive failures triggering SIGTERM → respawn. A GPU hang lasting >6 seconds will cause BackendSupervisor to kill and respawn the Python backend while the watchdog is still waiting. The process death unblocks the daemon thread, but the restart loses the in-flight transcription. No test verifies interaction between a stuck watchdog join and the HealthMonitor kill cycle.

**Recommendation:** Add a note in the W1358 docstring that the unbounded join means the backend will be killed by HealthMonitor (not the watchdog). Consider emitting a `krab_restart_gateway`-style IPC notification when the watchdog enters unbounded-join mode so UI can show a spinner/warning before the respawn clears it.

---

### F4 MED — `_error_bus` wiring is runtime-injection via `globals()`, untested for teardown

**File:** `core/mlx_subprocess.py` lines 279–313; `backend/service.py` lines 272–277  
**Impact:** `_push_watchdog_hang()` reads `globals().get("_error_bus")` — a module-level global injected by `BackendService.__init__` via `_mlx_sub._error_bus = self._error_bus`. If `BackendService` is garbage-collected or recreated (e.g., during test teardown or a restart), the old `_error_bus` reference stays in `mlx_subprocess._error_bus` pointing to the dead bus. Pushing an error to the old bus either silently succeeds (ring buffer accepts it) or raises on a closed queue. The bare `except Exception: pass` in `_push_watchdog_hang` swallows this, so it is safe but produces silent gaps in error telemetry after a backend restart within a single Python process lifetime. The `test_error_bus_phase_b_wave78.py` tests save/restore `_error_bus` correctly, but no test verifies the stale-reference scenario.

**Recommendation:** Consider weakref for `_error_bus` injection, or document the stale-reference gap explicitly as a known limitation.

---

### F5 LOW — No IPC method exposes watchdog `get_stats()` to the diagnostics panel

**File:** `core/mlx_subprocess.py` lines 81–93  
**Impact:** `MLXWatchdog.get_stats()` returns `crashes_count`, `total_calls`, `success_count`, and `avg_response_time_sec` — useful for detecting GPU flakiness trends. However, `get_diagnostics` IPC (`HealthCheckService`) does not include watchdog stats. There is no way for the Swift diagnostics panel or a support session to see watchdog crash history without connecting to the Python process directly. Given that 5 production GPU hang events were recorded (per error_codes.py comment), this data would be valuable for monitoring.

**Recommendation:** Add watchdog stats to `get_diagnostics` response under a `"mlx_watchdog"` key. Low-risk, high-observability win.

---

### F6 LOW — `MLX_CRASH_RECOVERY_ENABLED=False` path has no test in the recovery test suite

**File:** `core/engine.py` line 1963; `tests/test_mlx_recovery.py` lines 224–247  
**Impact:** `TestMLXWatchdogDisabledFlag` in `test_mlx_recovery.py` does not actually exercise the `MLX_CRASH_RECOVERY_ENABLED=False` code path in `engine.py`. It simulates "watchdog not called" by calling `direct_fn()` without going through the engine at all. The real code path — `getattr(settings, "MLX_CRASH_RECOVERY_ENABLED", True)` returning False → `mlx_whisper.transcribe(audio_data, **params)` direct call — has no test coverage. A misconfiguration or future refactor could silently break the disabled-watchdog branch.

**Recommendation:** Add a test that patches `settings.MLX_CRASH_RECOVERY_ENABLED = False` and calls `_transcribe_mlx_model_data` (with mlx_whisper mocked) to verify the watchdog is bypassed.

---

### F7 LOW — Sentry throttle threshold set is fixed; no way to tune without code change

**File:** `core/mlx_subprocess.py` lines 214–224  
**Impact:** `_SENTRY_REPORT_THRESHOLDS = frozenset({1, 5, 25, 125, 625})` is a module-level constant. For a production system where GPU hangs are frequent (e.g., older Metal drivers), this means Sentry will receive a report at crash_count=1 and then silence until 5, 25, etc. There is no way to tune the thresholds via settings or env var without a code change. The exponential spacing may be too aggressive for long-running sessions: 626th hang would never be reported again.

**Recommendation:** Cap the sentinel loop at the max threshold (625) so hangs above 625 are re-reported every 625 occurrences. One-liner fix: `return crash_count in _SENTRY_REPORT_THRESHOLDS or crash_count % 625 == 0`. Low priority.

---

## Test coverage summary

| Path | Status |
|------|--------|
| Success path (returns result) | Covered — `TestMLXWatchdogSuccess` |
| Exception propagation (ValueError, RuntimeError) | Covered — `TestMLXWatchdogExceptionPassthrough` |
| Timeout → `MLXTimeoutError` | Covered — `TestMLXWatchdogTimeout` |
| W1358 unbounded-join race guard | Covered — `TestMLXWatchdogLockRaceGuard` |
| Concurrent stats thread-safety | Covered — `TestMLXWatchdogConcurrency` |
| Sentry breadcrumb on timeout | Covered (mock) — `TestMLXWatchdogSentryIntegration` |
| `stt.mlx_watchdog_hang` error bus push | Covered — `test_error_bus_phase_b_wave78.py` |
| Variants-loop short-circuit asymmetry | **Not covered (F1)** |
| `MLX_CRASH_RECOVERY_ENABLED=False` in engine | **Not covered (F6)** |
| Stale `_error_bus` after backend restart | **Not covered (F4)** |
| Watchdog stats in `get_diagnostics` | **Not applicable (no wiring yet, F5)** |

Overall coverage is strong for the happy path and the critical W1358 race-guard. The gaps are concentrated in engine-integration scenarios and observability wiring.
