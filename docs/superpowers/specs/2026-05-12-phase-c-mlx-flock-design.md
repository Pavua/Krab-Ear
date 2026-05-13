# Phase C Step 6 — Inter-Process MLX Serialization via POSIX flock

**Date:** 2026-05-12  
**Author:** Claude (Sonnet 4.6)  
**Status:** Draft — Wave 48 skeleton, wire-in Wave 49

---

## Problem

Wave 46 A4 performance baseline confirmed:

| Scenario | LLM rewriter latency |
|---|---|
| Isolated (LM Studio + Krab Ear, no STT) | ~1 587 ms |
| Production (STT + diarization + LM Studio concurrent) | ~9 500 ms |

Root cause: **inter-process Metal GPU contention**. LM Studio (MLX-based inference) and Krab Ear's mlx-whisper transcription both issue GPU commands to the same Metal device without coordination. macOS Metal driver serializes at the hardware level, but GPU command queues compete causing head-of-line blocking (each waits for the other's Metal buffer swaps before scheduling).

The existing intra-process `mlx_lock` (RLock, `core/mlx_lock.py`) serializes *Krab Ear's own* concurrent MLX calls (fixed PR #71). It does **not** coordinate with LM Studio, which is a separate OS process with its own Metal device handle.

---

## Approach

### Strategy: Krab Ear-side POSIX flock

We **cannot** patch LM Studio (closed external binary). Two alternative strategies:

**A) Active backoff** — poll LM Studio `/v1/models` before STT to detect LM Studio GPU activity  
**B) POSIX flock** — acquire a shared lock file before any MLX GPU dispatch; LM Studio would need to cooperate (not possible)

Since LM Studio cannot be patched, neither A nor B provides *true* mutual exclusion with LM Studio. However, **flock still provides value**:

1. **Krab Ear self-coordination** — ensures STT, diarization probes, and any future MLX adapters within Krab Ear don't collide with each other across process boundaries (e.g., if a future subprocess-based GigaAM worker or mlx_subprocess.py worker is added).
2. **Foundation for future coordination** — if LM Studio adds a lockfile protocol, Krab Ear will be ready.
3. **Active probe backoff (complementary)** — before Krab Ear STT, HTTP-probe LM Studio `/v1/models` (fast, <5 ms). If LM Studio is actively processing (queue depth heuristic via response time), apply a configurable brief delay before GPU dispatch.

**This spec implements strategy B (flock for Krab Ear processes) + optional active backoff probe.**

---

## Lock File Location

```
~/Library/Application Support/KrabEar/mlx_inter_process.lock
```

Rationale:
- Same directory as `krabear.sock` — already guaranteed writable by Krab Ear processes
- `/tmp/krab_mlx.lock` alternative rejected: tmpfs on macOS may not support flock semantics on all FS mounts; also lost on reboot (minor issue but cleaner to use persistent dir)
- `mlx_inter_process.lock` name is descriptive and unlikely to collide

---

## Module: `KrabEar/core/mlx_inter_lock.py`

```
InterProcessMLXLock
├── __init__(lock_path: Path, timeout_sec: float = 5.0)
├── __enter__() → None          # fcntl.flock(LOCK_EX) with timeout
├── __exit__(...) → None        # fcntl.flock(LOCK_UN)
└── _open_lock_file() → None    # lazy open, creates if missing
```

### Feature flag

Controlled by env var `KRAB_EAR_MLX_INTER_PROCESS_LOCK`:
- `0` or unset → no-op (default for Wave 48)
- `1` → flock enabled

This allows A/B testing in Wave 49 without code change.

### Timeout behaviour

`fcntl.flock(LOCK_EX)` blocks indefinitely by default. We use a non-blocking attempt with manual retry + timeout to avoid hanging STT permanently if a process crashes holding the lock:

```python
deadline = time.monotonic() + timeout_sec
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return  # acquired
    except BlockingIOError:
        if time.monotonic() >= deadline:
            logger.warning("mlx_inter_lock: flock timeout after %.1fs, proceeding without lock", timeout_sec)
            return  # degrade gracefully — do not block STT
        time.sleep(0.05)
```

**Timeout = graceful degradation, not hard failure.** STT must never be blocked permanently by a lock.

### Integration with existing `mlx_lock()`

`mlx_lock()` (intra-process RLock) is NOT replaced. The inter-process lock wraps around it:

```
acquire inter-process flock  ← outer
  acquire intra-process RLock  ← inner (existing)
    mlx_whisper.transcribe()
  release RLock
release flock
```

This preserves the existing thread-safety guarantee while adding cross-process coordination.

### Updated `core/mlx_lock.py`

Add helper `mlx_inter_process_lock()` that returns either the real `InterProcessMLXLock` or a no-op context manager depending on the feature flag. This keeps call sites clean:

```python
with mlx_inter_process_lock():
    with mlx_lock():
        result = mlx_whisper.transcribe(...)
```

---

## Active LM Studio Backoff Probe (complementary, optional)

```python
def lm_studio_is_busy(base_url: str, threshold_ms: int = 500) -> bool:
    """Heuristic: if GET /v1/models takes >threshold_ms, LM Studio is under GPU load."""
```

If `KRAB_EAR_LM_STUDIO_BACKOFF=1`, engine.py checks this before STT dispatch. If busy, sleep up to `KRAB_EAR_LM_STUDIO_BACKOFF_MAX_SEC` (default 3.0) before proceeding.

**Not implemented in Wave 48 skeleton** — flagged as Wave 49 enhancement.

---

## Wire-in Plan (Wave 49)

1. `engine.py` lines 1804-1818 (main STT call site):
   ```python
   with mlx_inter_process_lock():
       with mlx_lock():
           result = mlx_whisper.transcribe(...)
   ```
2. `engine.py` line 414 (warmup probe site):
   ```python
   with mlx_inter_process_lock():
       with mlx_lock():
           mlx_whisper.transcribe(...)
   ```
3. Any future subprocess workers in `core/mlx_subprocess.py` that do MLX inference

**Do NOT wrap `llm_rewriter.py`** — LM Studio is HTTP, runs in its own process, no MLX lock needed on Krab Ear side. The flock would only help if LM Studio also acquired it (impossible).

---

## Limitations

- **Does not solve LM Studio contention** — LM Studio will not acquire `mlx_inter_process.lock`. The 9500ms regression is primarily Metal driver-level contention with LM Studio's inference queue, not Krab Ear self-contention. Real fix for LM Studio contention = STT pipeline timing (schedule STT before or after LLM rewrite, not concurrently) — tracked separately.
- **flock is advisory** — any process that doesn't cooperate bypasses it. Only effective for Krab Ear's own processes.
- **NFS/APFS caveat** — `fcntl.flock` behavior on network mounts is undefined. Lock file is in `~/Library/Application Support/KrabEar/` which is local APFS — safe.

---

## Files Created / Modified

| File | Action |
|---|---|
| `KrabEar/core/mlx_inter_lock.py` | NEW — InterProcessMLXLock + no-op fallback |
| `KrabEar/core/mlx_lock.py` | MODIFIED — add `mlx_inter_process_lock()` helper |
| `KrabEar/tests/test_mlx_inter_lock.py` | NEW — 3 unit tests |

Wave 49 additions (not in this Wave):
| `KrabEar/core/engine.py` | wrap STT call sites |

---

## Open Questions for Wave 49

1. **Measure actual improvement** — run A4 benchmark with `KRAB_EAR_MLX_INTER_PROCESS_LOCK=1`. If intra-Krab contention is negligible vs LM Studio GPU pressure, the flock may show <5% improvement, which would change priority.
2. **STT pipeline sequencing** — the higher-ROI fix may be to make `BackendService` schedule LLM rewrite *after* STT completes (currently they run concurrently in thread pool). This requires no lock at all.
3. **Lock file path from settings** — should `lock_path` come from `settings.data_dir` or be hardcoded? If backend runs with `--data-dir`, the path must match.
4. **Timeout tuning** — 5.0s default is conservative. After Wave 49 benchmarks, may reduce to 2.0s or increase to 10.0s depending on observed contention duration.
5. **GigaAM worker** — `core/mlx_subprocess.py` may spawn subprocess workers. They need to acquire the same flock path — must coordinate on the lock file location before implementing.
