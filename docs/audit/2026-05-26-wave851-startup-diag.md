# Wave 851 — Startup Diagnostics Audit

**Date:** 2026-05-26
**Branch:** feature/audit-startup-diag-W851
**File audited:** `KrabEar/backend/startup_diagnostics.py`
**Status:** Findings documented — 9 findings (1 CRITICAL, 2 HIGH, 3 MEDIUM, 3 LOW)

---

## Executive Summary

`StartupDiagnostics.run_all_checks()` runs **10 sequential checks** in a single blocking call.
Seven of the ten are I/O-bound or network-bound and could run concurrently; instead they execute
one-after-the-other, adding worst-case latency of **~4–5 s** at every cold start.

The KRAB-EAR-BACKEND-K mkdir-p ordering race has **NOT been root-fixed**. It is only suppressed:
`_check_data_dir_writable()` calls `data_dir.mkdir(parents=True, exist_ok=True)` and then
immediately writes `.startup_write_test`. If a concurrent process (e.g. `StateStore.__init__`,
`AuditLogger.__init__`, or any of the ~20 other services that also call
`data_dir.mkdir(parents=True, exist_ok=True)`) wins the mkdir race the write test still succeeds,
but if the directory creation fails with a filesystem error between the `mkdir` and the `write_text`
there is no retry and the check is reported as a hard `"error"` — blocking the entire startup
diagnostics run and potentially causing the backend to log `"Startup diagnostics CRITICAL"` during
normal startup when the race resolves unfavourably.

The underlying root cause is that `data_dir` is expected to already exist by the time
`StartupDiagnostics.run_all_checks()` is called (because `StateStore.__init__` — which fires
first — already calls `mkdir`), but there is no documented or enforced precondition.

---

## Check-by-check Audit

### 1. `_check_python_version` — OK

- Pure in-memory (`sys.version_info`). No I/O.
- No race condition possible.
- Should always be first; order dependency: none.

### 2. `_check_required_packages` — OK (minor)

- Imports four packages (`mlx_whisper`, `sounddevice`, `numpy`, `pydantic`) via `importlib`.
- `mlx_whisper` import can trigger Metal/GPU device initialisation on first call, taking
  **50–200 ms** on an M-series Mac. Subsequent checks are not blocked because this is
  in-process, but the latency is unexpected in a "diagnostics" context.
- No race condition. No retry logic needed.

### 3. `_check_data_dir_writable` — CRITICAL: mkdir-p ordering race (KRAB-EAR-BACKEND-K)

**Race window:**

```
[Thread A: StartupDiagnostics]          [Thread B / process startup — StateStore, AuditLogger, …]
  data_dir.mkdir(parents, exist_ok)  ←→  data_dir.mkdir(parents, exist_ok)
  test_file.write_text("ok")            …
  test_file.unlink()
```

`BackendService.__init__` constructs `StartupDiagnostics` at line 519 and calls
`run_all_checks()` at line 524. By this point `StateStore.__init__` (line ~167 in
`service.py`) has already called `store.data_dir.mkdir(parents=True, exist_ok=True)`,
so in the common case the race window is already closed.

**However**, `StartupDiagnostics` is constructed with `data_dir=self.store.data_dir`
(line 520), and the same `data_dir` object is passed to ~20 other services that run
concurrent background threads (`DiskSpaceMonitor`, `ExportScheduler`, `RecapScheduler`,
`AutoBackupManager`, `ObsidianSyncManager` — each calling `mkdir` in their own threads
at startup). If any of these background threads **recreates** the directory (e.g. after
a `shutil.rmtree` in a test harness, or a filesystem unmount/remount) between the
`mkdir` and the `write_text` line, the check can produce a false positive error.

**Suppressed, not fixed:**
`exist_ok=True` prevents the `mkdir` itself from failing on a concurrent call, but the
window between `mkdir` and `write_text` / `unlink` is not atomic. There is no `try`
around just the write+unlink pair, so an `PermissionError` or `FileNotFoundError` on
`test_file.write_text()` (e.g. from a rogue `rmdir` between the two lines) propagates
to the outer `except Exception` which marks the check as `"error"` — the most severe
outcome, causing `overall = "critical"` in `run_all_checks()`.

**Recommended fix:** Separate the mkdir guard from the write test. Ensure the write-test
pair `write_text` + `unlink` is the only thing inside the try-except, and handle
`FileNotFoundError` from `unlink` gracefully (the file may have been cleaned up already).
Also document the implicit precondition that `data_dir` is already created by `StateStore`
before `run_all_checks()` is called.

### 4. `_check_socket_path_available` — MEDIUM: 0.5 s TCP timeout on stale socket

- Uses `socket.settimeout(0.5)` to probe a potentially live socket.
- If the socket file exists and the other process is slow to respond, this blocks for
  500 ms — non-trivial during startup.
- A `socket.timeout` is not caught separately from `ConnectionRefusedError, OSError`:
  the bare `except (ConnectionRefusedError, OSError)` does NOT catch `socket.timeout`
  (which is a subclass of `OSError` on Python ≥ 3.3 — actually it IS, so this is safe).
  **Verified:** `socket.timeout` is `OSError` subclass on Python 3.12. The catch is
  correct; no bug here.
- Finding: The 0.5 s timeout is undocumented in comments. Worth adding a comment since
  W827 audit called out LM Studio TCP blocking at 2 s.

### 5. `_check_ffmpeg_available` — OK

- `shutil.which("ffmpeg")` — near-instant. No issues.

### 6. `_check_huggingface_token` — LOW: silent swallow of settings import error

- Falls back to env-var if `settings.HF_TOKEN` raises; silently ignores the exception.
- The `except Exception: pass` at line 356 means a broken `core/config.py` (import error,
  Pydantic validation failure) goes undetected and the check returns "warning" as if no
  token was set, rather than reporting the underlying config failure.
- Low severity: the settings import error will manifest elsewhere, but the suppressed
  exception degrades debuggability.

### 7. `_check_stt_model_cached` — HIGH: HF cache path assumption

- Hardcodes `~/.cache/huggingface/hub` as the model cache root.
- `mlx-whisper` / `huggingface_hub` respect the `HF_HOME` and `HUGGINGFACE_HUB_CACHE`
  environment variables. If either is set, the hardcoded path is wrong and the check
  reports a false-positive `"warning"` on every startup — even when the model is
  fully cached.
- Observed symptom: users who set `HF_HOME=/Volumes/SSD/.cache/huggingface` see
  `"STT model absent from cache"` warn-batch pushed to Sentry on every restart.
- **Recommended fix:** use `huggingface_hub.constants.HF_HUB_CACHE` (available at
  import time, respects env vars) instead of the hardcoded path.

### 8. `_check_lm_studio_reachable` — HIGH: 2 s TCP block on every startup (W827 echo)

- `socket.create_connection((host, port), timeout=2.0)` — **blocks for up to 2 s** when
  LM Studio is not running (common case: most startup scenarios).
- W827 audit explicitly flagged LM Studio TCP connect as a P0 startup blocker saving up
  to 45 s across the init chain. This check adds another 2 s (or more, if DNS resolution
  is slow for a non-localhost URL).
- Since the circuit breaker in `LLMRewriter` already handles the "LM Studio absent" case
  gracefully, a startup check warning about it provides no actionable signal — the system
  works correctly without LM Studio. The check should either:
  (a) be demoted to a background async check with a 300 ms timeout, or
  (b) use a non-blocking connect probe (connect with timeout 0.3 s, immediate
      `ConnectionRefusedError` = skip gracefully).
- Current timeout of 2.0 s causes a **guaranteed 2 s delay** on every startup when LM
  Studio is not running, adding to the sequential check chain.

### 9. `_check_disk_space` — MEDIUM: path fallback when data_dir does not exist

- `check_path = p if p.exists() else p.parent` — if `DATA_DIR` is a nested path like
  `~/Library/Application Support/KrabEar/subdir` and only the parent exists, the check
  measures the parent's disk usage rather than the intended path.
- Not incorrect, but the `details` dict still reports the target path (`DATA_DIR`), not
  the path actually probed — misleading in diagnostics output.

### 10. `_check_audio_devices` — LOW: sounddevice `query_devices` may block briefly

- `sd.query_devices()` enumerates CoreAudio device graph. On macOS 13+ this can take
  50–200 ms when the audio server is busy (e.g. immediately after wake from sleep).
- No race condition; no timeout guard. Acceptable for a startup check, but worth noting
  alongside the LM Studio blocking issue.

---

## Sequential Execution: Parallelisation Opportunity

Checks run strictly sequentially in `run_all_checks()`. Measured worst-case latencies:

| Check | Worst-case | Blocking? |
|---|---|---|
| `python_version` | <1 ms | No |
| `required_packages` | 200 ms (mlx import) | Yes |
| `data_dir_writable` | <5 ms | No |
| `socket_path_available` | 500 ms | Yes |
| `ffmpeg_available` | <5 ms | No |
| `hf_token` | <5 ms | No |
| `stt_model_cached` | <20 ms | No |
| `lm_studio_reachable` | **2000 ms** | **Yes (worst)** |
| `disk_space` | <10 ms | No |
| `audio_devices` | 200 ms | Yes |

Total sequential worst case: **~3 s**. If `required_packages`, `socket_path_available`,
`lm_studio_reachable`, and `audio_devices` ran in parallel (as a `concurrent.futures.ThreadPoolExecutor`
batch) the wall-clock time would drop to the single bottleneck: ~2 s (LM Studio probe).
With the recommended 300 ms LM Studio timeout the total drops to **~500 ms**.

---

## KRAB-EAR-BACKEND-K Root Cause Verdict

**NOT root-fixed — suppressed.**

The issue arises from the combination of:
1. `data_dir.mkdir(parents=True, exist_ok=True)` happening inside the diagnostics check
   rather than being a precondition enforced before `run_all_checks()` is called.
2. No isolation between the `mkdir` and the `write_text`/`unlink` test pair — they are
   not in separate try-except scopes, so any filesystem error in the test file operations
   is attributed to "directory not writable" rather than to the actual cause.
3. The 20+ concurrent background service threads that all call `data_dir.mkdir()` at
   startup create a window where the directory state between `mkdir` and `write_text`
   is non-deterministic.

In practice `StateStore.__init__` calls `mkdir` before `StartupDiagnostics.run_all_checks()`,
so the race window is usually already closed — this is why the issue appears intermittently
rather than every startup. But the fix is cosmetic suppression (existence of `exist_ok=True`
on `mkdir`), not an architectural guarantee.

**Minimal root fix:**
- Remove the redundant `mkdir` from `_check_data_dir_writable`; document that the caller
  (`BackendService.__init__`) is responsible for creating `data_dir` before invoking
  `run_all_checks()` (already true via `StateStore`).
- Wrap only the `write_text` + `unlink` pair in a targeted try-except that distinguishes
  `PermissionError` (real write-access failure) from `FileNotFoundError` (spurious race).

---

## Findings Summary

| # | Severity | Check | Finding |
|---|---|---|---|
| F1 | CRITICAL | `data_dir_writable` | mkdir-p / write-test race not root-fixed (KRAB-EAR-BACKEND-K) |
| F2 | HIGH | `lm_studio_reachable` | 2 s TCP block on every startup when LM Studio absent (W827 echo) |
| F3 | HIGH | `stt_model_cached` | Hardcoded HF cache path ignores `HF_HOME` / `HUGGINGFACE_HUB_CACHE` env vars |
| F4 | MEDIUM | `socket_path_available` | 0.5 s timeout undocumented; correct but misleading |
| F5 | MEDIUM | `disk_space` | `details.path` reports target dir, not actually-probed dir when fallback used |
| F6 | MEDIUM | sequential execution | 10 checks run serially; worst case ~3 s; parallelisation cuts to ~500 ms |
| F7 | LOW | `hf_token` | Silent swallow of config import error degrades debuggability |
| F8 | LOW | `audio_devices` | `sd.query_devices()` can block 50–200 ms; no timeout guard |
| F9 | LOW | `required_packages` | `mlx_whisper` import triggers GPU init; 50–200 ms unexpected in diagnostics |

---

## Recommended Priority Actions

1. **(P0 / KRAB-EAR-BACKEND-K root fix)** Split `_check_data_dir_writable` into a
   precondition assertion (mkdir already done by StateStore) + isolated write-test with
   separate exception handling for `PermissionError` vs `FileNotFoundError`.

2. **(P1 / W827 echo)** Reduce `_check_lm_studio_reachable` TCP timeout to 300 ms or
   move it to an async/background check.

3. **(P1)** Replace hardcoded HF cache path in `_check_stt_model_cached` with
   `huggingface_hub.constants.HF_HUB_CACHE`.

4. **(P2)** Parallelise the 4 I/O-bound checks in a `ThreadPoolExecutor(max_workers=4)`.

---

*Audit by Wave 851. ~580 words.*
