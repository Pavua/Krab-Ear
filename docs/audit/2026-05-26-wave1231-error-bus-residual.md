# W1231 — Error Bus Residual Audit

**Date:** 2026-05-26  
**Branch:** `audit/error-bus-residual-W1231`  
**Scope:** `KrabEar/backend/error_bus.py`, `error_codes.py`, all `_push_error` callsites  
**Status:** 5 findings

---

## Summary

Post-Wave-82 audit of the error bus subsystem. The core `ErrorBus`/`WarnBatcher`/`KrabError`
machinery is solid: thread safety is correct, Sentry tier routing matches spec (info=skip,
warn=batch, error/critical=immediate), and `WarnBatcher` window-flush logic is tested. However,
three categories of residual issues were found.

---

## Finding 1 — Wave 82 codes missing from ERROR_REGISTRY (CRITICAL)

**Files:** `KrabEar/backend/error_codes.py`, `KrabEar/backend/disk_monitor.py`,
`KrabEar/backend/startup_diagnostics.py`, `KrabEar/backend/service.py`

**Severity:** Critical — fallback to empty dict silently degrades

The three codes added by Wave 490 (Phase B Wave 82) — `disk.critical`,
`system.proc_cmdline_permission`, and `startup.stt_model_cache_miss` — are **absent from
`ERROR_REGISTRY`** in `error_codes.py`. The runtime at `codex/krab-ear-v2` has exactly 51
entries; none of the Wave 82 additions are present.

All three callsites do `ERROR_REGISTRY.get("disk.critical", {})` / `.get("startup.stt_model_cache_miss", {})`, which means they silently build a `KrabError` with
`message_user=""`, `actionable=False`, `action_id=None`, and `dedupe_seconds` defaulting to
the bus's global `default_dedupe_window_sec=30s` instead of the intended 600 / 86400s. The
tests in `test_error_bus_phase_b_wave82.py` call `assertIn("disk.critical", ERROR_REGISTRY)` —
they will all fail at runtime because the registry lacks the entries.

**Reproduction:** `python3 -c "from backend.error_codes import ERROR_REGISTRY; print('disk.critical' in ERROR_REGISTRY)"` → `False`.

**Fix:** Add the three entries to `ERROR_REGISTRY` in `error_codes.py`. The correct values
are documented in `test_error_bus_phase_b_wave82.py`:

| code | severity | actionable | dedupe_seconds |
|------|----------|------------|----------------|
| `disk.critical` | critical | True (`open_logs`) | 600 |
| `system.proc_cmdline_permission` | error | False | 3600 |
| `startup.stt_model_cache_miss` | warn | False | 86400 |

---

## Finding 2 — Two active callsites push unregistered codes (HIGH)

**Files:** `KrabEar/backend/llm_rewriter.py` lines 644, 704

**Severity:** High — errors pushed with degraded metadata, no dedupe window

`llm_rewriter.py` calls `_push_error()` with two codes that do not appear in `ERROR_REGISTRY`:

1. `rewriter.mlx_token_bug` (line 644) — fires when LM Studio returns HTTP 500 with
   `"cannot access local variable 'token'"` in the body (mlx_lm UnboundLocalError). Code
   is mentioned in the comment on `rewriter.lm_studio_500` but never defined as its own
   registry entry.

2. `rewriter.gpu_stream_error` (line 704) — fires when LM Studio returns HTTP 400 with
   `"RuntimeError: There is no Stream(gpu, N) in current thread"` (Metal CommandStream
   corrupted by concurrent GPU pressure). A similar but distinct variant
   `rewriter.lm_studio_stream_gpu_lost` (HTTP 500, Stream(gpu)) **is** registered and
   handled at line 683 — this second variant for HTTP 400 slipped through.

Both fall back to `ERROR_REGISTRY.get(code, {})` → empty dict. The effect is:
`message_user="Rewriter ошибка"` (hardcoded fallback), `actionable=False`, dedupe window
falls back to 30s default. Sentry still receives the event (error-tier) but with no
`user_msg_ru` and no dedupe metadata, causing Sentry noise on repeated hits.

**Fix:** Add `rewriter.mlx_token_bug` (severity=warn, dedupe 300s) and
`rewriter.gpu_stream_error` (severity=error, dedupe 60s) to `ERROR_REGISTRY`.

---

## Finding 3 — WarnBatcher has no flush_all() / shutdown drain (MEDIUM)

**Files:** `KrabEar/backend/error_bus.py`, `KrabEar/backend/shutdown_handler.py`

**Severity:** Medium — warn-tier Sentry events lost on clean shutdown

`WarnBatcher` accumulates warn errors in memory per-code until either `batch_size` (10)
or `window` (30s) is reached. `GracefulShutdownHandler.shutdown()` in
`backend/shutdown_handler.py` has no reference to `ErrorBus` or `WarnBatcher` — it does
not call any flush method before process exit.

On a clean shutdown triggered by SIGTERM (e.g. HealthMonitor SIGTERM of the Python backend,
or user-initiated restart via launchd), all accumulated warn batches are silently discarded.
Production scenario: `rewriter.timeout` accumulates 7 hits in 29s, then user restarts the
backend — the 7 events are lost.

`WarnBatcher` also has no `flush_all()` public method — only `_flush_locked(code)` which
is private and per-code. `ErrorBus` likewise exposes no `flush()` surface.

**Fix:** Add `flush_all()` to `WarnBatcher` that iterates all buffered codes and calls
`_flush_locked`. Add `flush_warn_batches()` on `ErrorBus` that delegates to
`self._warn_batcher.flush_all()`. Wire it into `GracefulShutdownHandler.shutdown()` before
`sentry_sdk.flush()`.

---

## Finding 4 — 9 registry entries have no callsite (dead / forward-declared) (LOW)

**Files:** `KrabEar/backend/error_codes.py`

The following codes appear in `ERROR_REGISTRY` but are never passed to `_push_error()` or
`error_bus.push()` in any non-test production source file:

| code | notes |
|------|-------|
| `rewriter.circuit_open` | Circuit-open path in `llm_rewriter.py` logs a Sentry breadcrumb but never pushes to error_bus. |
| `rewriter.warmup_failed` | Warmup failure in `service.py` logs `logger.warning` only; no `_push_error` call. |
| `stt.mlx_timeout` | MLX subprocess timeout is handled by `core/mlx_subprocess.py` which pushes `stt.mlx_watchdog_hang` — `stt.mlx_timeout` is redundant. |
| `stt.padding_mismatch` | GigaAM padding mismatch falls back without a bus push. |
| `diarization.vad_gated` | No callsite found; intended for pyannote gate but never wired. |
| `ipc.audio_device_poll_flood` | No callsite; the rate-limit check in `IPCThrottle` pushes `ipc.rate_limit_exceeded` instead. |
| `paste.ax_denied` | Pushed indirectly via `_handle_report_paste_failure` (IPC method) when Swift calls `report_paste_failure` — Swift side has not been confirmed to call this. |
| `paste.app_unsupported` | Same as above. |
| `rewriter.model_unloaded` | HTTP 422/400 "model not loaded" path not confirmed wired. |

These are not immediately broken (the registry is a declaration; missing callsites mean the
code is never surfaced, not that it crashes). However they inflate the registry count (9/51 =
18%) and confuse the invariant tests that count "wired" codes. The `paste.*` codes rely on
Swift calling `report_paste_failure` via IPC — verify that path exists before treating them
as dead.

---

## Finding 5 — CLAUDE.md ERROR_REGISTRY count stale (TRIVIAL)

**File:** `CLAUDE.md`

`CLAUDE.md` states **"47 codes wired runtime"** in the Phase B section. The actual
`ERROR_REGISTRY` at `codex/krab-ear-v2` has **51 entries** (Wave 60 +5, Wave 61 +3,
Wave 64 +5, Wave 78 +7 = 44 initially, minus overlaps; actual count confirmed at runtime).
The Wave 82 three-code block was never added to either `error_codes.py` or `CLAUDE.md`,
so both lag. The count in the note should read **"51 codes in registry; 3 Wave 82 entries
pending addition (disk.critical, system.proc_cmdline_permission, startup.stt_model_cache_miss)"**
once Finding 1 is resolved.

---

## Checks passing (not findings)

- **Ring buffer overflow:** `deque(maxlen=ring_buffer_size)` auto-evicts on overflow. The W1027
  + W1148 fix is confirmed present and correct — no manual overflow handling needed.
- **Thread safety on concurrent push:** `ErrorBus._lock` is a `threading.Lock()`, protects
  `_last_emitted` dict and `_ring.append`. `_route_to_sentry` is called outside the lock
  to prevent event_bus callback deadlock — correct. `WarnBatcher` has its own independent
  lock. No TOCTOU issues found.
- **WarnBatcher correctness:** `batch_size` flush and `window` flush are both correctly
  tested in `test_error_bus_extras.py::WarnBatcherTests`. The window-flush test uses
  `time.sleep(0.05)` with `window=0.01` — not mocked but reliable at this scale.
- **Sentry tier routing:** info→skip, warn→WarnBatcher, error/critical→immediate
  `capture_message`. Implementation matches spec. Tested by `SentryTierTests`.
- **Dedupe key collision:** dedupe is keyed by `err.code` (a namespaced string). Different
  errors with the same code string would collide, but that is by design (one toast per
  code-window). No two distinct errors share a code in the registry.
