# Wave 1630 — mlx_inter_lock.py First-Pass Audit

**Date:** 2026-05-27
**File:** `KrabEar/core/mlx_inter_lock.py`
**Class:** `InterProcessMLXLock` / `mlx_inter_process_lock()`
**Auditor:** W1630 (sub-agent)

## Summary

`mlx_inter_lock.py` implements a POSIX `flock`-based cross-process serialization layer for MLX GPU
access. It complements the intra-process `mlx_lock` (RLock in `core/mlx_lock.py`). The code has
correct graceful-degradation on timeout and clean fd management. However, **five significant gaps**
were identified — two are HIGH severity.

---

## Findings

### F1 — HIGH: Feature flag OFF by default — engine.py and audio_lang_id.py never hold the inter-process lock

**Location:** `core/engine.py:473,607,984,1956`; `core/audio_lang_id.py:289`; `mlx_inter_lock.py:11`

The module docstring and `mlx_inter_process_lock()` explicitly default to no-op unless
`KRAB_EAR_MLX_INTER_PROCESS_LOCK=1`. Every `with mlx_lock():` block in `engine.py` (four sites)
and `audio_lang_id.py` (one site) uses only the intra-process RLock with no inter-process guard
at all, even when the feature flag is enabled — because those call sites never call
`mlx_inter_process_lock()`:

```python
# engine.py:473 — no inter-process guard
with mlx_lock():
    result = mlx_whisper.transcribe(audio, ...)
```

The only call site that uses the correct dual-lock pattern (`stt_parakeet.py:164-165`) is the
Parakeet adapter, added later. The primary whisper path (used by every default transcription) has
no inter-process coordination even when `KRAB_EAR_MLX_INTER_PROCESS_LOCK=1`.

**Risk:** If the REST server (`rest_server.py`) and the IPC backend run in separate OS processes
and both trigger mlx-whisper simultaneously (concurrent transcription + bulk reprocess or live-subs
ingest), enabling the feature flag gives no protection on the most-used code path. The GPU
corruption SIGSEGV documented in PR #71 and the memory-pressure reboot scenario
(feedback_mlx_memory_constraint.md) remain possible even with the flag set.

**Fix:** Wrap all four `with mlx_lock():` blocks in `engine.py` and the one in `audio_lang_id.py`
in `with mlx_inter_process_lock():` as the outer guard, following the Parakeet pattern. Also apply
to `bulk_reprocess.py:248` which uses `mlx_lock()` directly.

---

### F2 — HIGH: Timeout degradation silently proceeds without the lock — TOCTOU race window

**Location:** `mlx_inter_lock.py:86-106`

When flock acquisition times out, the code logs a warning and allows the calling thread to proceed
without holding the lock:

```python
# Graceful degradation: log and proceed without lock
logger.warning("mlx_inter_lock: flock timeout after %.1fs ...")
break
# acquired = False, but __enter__ returns self — caller continues
```

The `acquired` flag is checked only for a debug log at line 104. The caller has no way to detect
that it is running *without* the lock — `__enter__` returns `self` in both the acquired and
timed-out cases. This means under high contention (e.g., three concurrent transcription requests
at startup), all three threads/processes can end up in the critical MLX section simultaneously
after their timeouts elapse, defeating the entire purpose of the lock.

This is a TOCTOU race: the caller believes it is protected because `with mlx_inter_process_lock():`
entered without raising, but it may not hold the lock at all.

**Risk:** Exactly the same concurrent-GPU-access SIGSEGV the lock was designed to prevent. Under
load, graceful degradation converts the lock from "serialization guarantee" to "best-effort hint."

**Fix:** Two options:
1. Raise `InterProcessMLXLockTimeout` (custom exception) on timeout and let callers decide whether
   to retry or abort — this is the safer semantic.
2. Expose `lock.acquired` as a readable property so callers can check and log a higher-severity
   error (Sentry `mlx.lock_timeout` error code) before proceeding.

Option 1 is cleaner; the docstring already says "graceful continue" is intentional for STT
availability, so Option 2 at minimum should push a Sentry warning via `error_bus`.

---

### F3 — MED: No stale-lock cleanup — orphan fd from crashed process permanently blocks

**Location:** `mlx_inter_lock.py:77-81` (`_open_lock_file`)

`flock()` is advisory and automatically released when all fds to the file are closed — including
on process crash (the kernel closes all fds). So the "stale lock" concern from the docstring
warning is actually handled by the OS for process death. However, there is a subtler issue:
if a process hangs (not crashes) while holding the flock — e.g., MLX GPU hang causing infinite
`mlx_whisper.transcribe()` with no watchdog timeout — the lock is held for the duration, and
all contenders will timeout after 5 s and proceed without the lock (F2 above).

More practically: `_open_lock_file` opens with `O_CREAT | O_RDWR` but never deletes the file on
shutdown (no `__del__`, no cleanup in `GracefulShutdownHandler`). The file accumulates across
restarts. On macOS `~/Library/Application Support/KrabEar/` this is benign (tiny file), but the
lock_path directory check `self._lock_path.parent.mkdir(parents=True, exist_ok=True)` at line 79
runs on every `__enter__` — a minor stat() cost on every transcription when the flag is enabled.

**Fix:** Cache the directory-exists check at construction time, not in `__enter__`. Register lock
file cleanup in `GracefulShutdownHandler`. Document that OS-level flock release handles process
crash.

---

### F4 — MED: Thread-unsafety warning buried in class docstring — not enforced

**Location:** `mlx_inter_lock.py:57-64` (class docstring), `mlx_inter_process_lock():124`

The class docstring states: "Thread safety: один fd на объект — не использовать один экземпляр
из нескольких тредов. Для multi-thread создавать отдельный экземпляр на тред или использовать
`mlx_inter_process_lock()`."

However, `mlx_inter_process_lock()` returns `_NOOP` (a module-level singleton) when the flag is
OFF, and creates a new `InterProcessMLXLock` per call when ON. If the flag is OFF (the default),
concurrent threads all share `_NOOP` which is safe. If the flag is ON, concurrent threads each
get their own `InterProcessMLXLock` instance (correct), but nothing prevents a caller from
creating a single instance and reusing it across threads — the class provides no `threading.Lock`
to guard its own `_fd` attribute. A thread calling `__enter__` and another calling `__exit__` on
the same instance races on `self._fd`.

The `_NOOP` singleton is also not thread-safe for `__exit__` (though `_NoOpContext.__exit__`
returns `False` immediately with no state, so it is safe in practice).

**Fix:** Add a `threading.Lock` to `InterProcessMLXLock.__init__` protecting `_fd` access, or
enforce single-use by raising if `_fd is not None` at `__enter__` entry. At minimum add a runtime
assertion.

---

### F5 — LOW: No Sentry / error_bus wiring on timeout or unlock failure

**Location:** `mlx_inter_lock.py:96-101,113-114`

Both the flock timeout warning (line 96-101) and the `LOCK_UN` failure (line 113-114) use
`logger.warning()` only. Given that this project has `error_bus.py` with `mlx.*` error codes and
Sentry integration, a lock timeout is a signal worth surfacing:
- It indicates either a hung process (needs HealthMonitor attention) or severe GPU contention.
- `LOCK_UN` failure after acquiring means the fd was closed or the file was deleted externally —
  a state corruption event.

Neither event currently pushes to the ErrorBus or triggers a Sentry breadcrumb.

**Fix:** On timeout (when `acquired=False`), push `error_bus.push(KrabError(code="mlx.lock_timeout",
severity="warn", ...))`. On `LOCK_UN` failure, push `error_bus.push(KrabError(code="mlx.lock_release_failed",
severity="error", ...))` — add both codes to `error_codes.py` if not present.

---

## Coverage Assessment

| Call site | Uses inter-process lock | Uses intra-process mlx_lock | Verdict |
|---|---|---|---|
| `engine.py` (4 sites) | No | Yes | GAP (F1) |
| `audio_lang_id.py` | No | Yes | GAP (F1) |
| `pipeline/stt_parakeet.py` | Yes (outer) | Yes (inner) | CORRECT |
| `pipeline/stt_whisper_mlx_adapter.py` | No | Yes | GAP (F1) |
| `backend/bulk_reprocess.py` | No | Yes | GAP (F1) |
| `scripts/debug_whisper.py` | No | Yes | Acceptable (dev script) |

Test coverage: 3 test classes / 8 test methods in `test_mlx_inter_lock.py`. Covers acquire/release,
feature flag toggle, and timeout degradation. Missing: concurrent subprocess test verifying actual
cross-process exclusion; test for lock ordering (inter-outer, intra-inner); test for the
`LOCK_UN` failure path.

## Lock Ordering

The prescribed ordering from both `mlx_lock.py` and the class docstring is:
```
with mlx_inter_process_lock():  # outer — cross-process flock
    with mlx_lock():            # inner — intra-process RLock
```

`stt_parakeet.py` follows this correctly. All other MLX sites use only `mlx_lock()` with no outer
inter-process guard — see F1 above. There is no risk of deadlock from inverted ordering since no
call site currently holds both locks in reversed order; the risk is coverage gaps, not inversion.

## macOS vs Linux Behavior Note

`fcntl.flock()` on macOS (BSD-derived) is per-open-file-description, not per-process. Forking a
process that holds a flock duplicates the fd and the lock is shared. `mlx_subprocess.py` spawns
subprocesses for MLX watchdog; if the parent holds the inter-process flock when forking, the child
inherits it and the flock is effectively held by two processes simultaneously. This is not currently
a problem because the feature flag is OFF by default and `mlx_inter_lock` is not held at fork
time, but it becomes a risk if F1 is fixed and the flag enabled without fork-safety audit.
