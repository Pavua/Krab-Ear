# GigaAM Worker Memory Analysis — 2026-05-05

## Context

Phase C C.1-infra (PR #367, commit `a8ca6c2`) shipped `scripts/memory_baseline.py`.
A single snapshot showed `gigaam_worker = 1570 MB` RSS while idle between requests.
A single STT subprocess should not need 1.5 GB of steady-state RAM.

---

## Architecture Overview

### Process topology

```
KrabEar backend (main venv, Python 3.14)
  └─ GigaAMAdapter (stt_gigaam.py)
       └─ _GigaAMSubprocessSession
            └─ subprocess.Popen(
                 [~/.venv_krab_ear_gigaam/bin/python, gigaam_worker.py],
                 stdin=PIPE, stdout=PIPE, stderr=PIPE
               )
                 └─ gigaam_worker.py  ← this is the 1570 MB process
                      └─ _MODEL: gigaam model + torch state
```

### IPC protocol

Communication is line-delimited JSON over stdin/stdout.
Worker lifecycle:

1. `{"op": "load", "mode": "rnnt", "device": "mps"}` — loads model into `_MODEL` global
2. `{"op": "transcribe", "audio_path": "/tmp/x.wav"}` — runs inference, returns text
3. `{"op": "shutdown"}` — process exits (or EOF closes it)

Worker stays alive between requests, keeping model loaded permanently.
stderr is not drained during idle — only read on OOM crash exit.

---

## Memory Growth Hypotheses

### H1 (Highest probability) — PyTorch/MPS Metal tensor accumulation

**Evidence:** GigaAM uses PyTorch + MPS (Apple Silicon). MPS backend allocates Metal
buffers that are pooled rather than immediately freed after inference. PyTorch MPS
does not aggressively release GPU-side buffers between calls — the allocator holds
them for future re-use. After a few transcriptions the pool stabilizes but peaks
can be much higher if inference runs without explicit cache clearing.

**Key locations:**
- `gigaam_worker.py:_handle_transcribe()` — calls `_MODEL.transcribe(audio_path)` or
  `_MODEL.transcribe_longform(audio_path)`. No `torch.mps.empty_cache()` call after.
- `stt_gigaam.py:_get_model()` — moves model to MPS via `model.to(torch.device("mps"))`.
  Model weights remain pinned on Metal for the lifetime of the process.

**Why 1570 MB:** GigaAM RNNT v2 weights alone are ~400–600 MB (Conformer + RNNT decoder).
MPS pre-allocates Metal buffers proportional to the largest seen input at each layer
(similar to CUDA caching allocator). After the first longform transcription the
per-layer buffer pool can reach 1–1.5 GB and stays there.

### H2 (Medium probability) — Audio buffer leakage in longform mode

**Evidence:** `_handle_transcribe` with `longform=True` calls
`_MODEL.transcribe_longform(audio_path)` which internally uses pyannote.audio for VAD
segmentation. pyannote loads its own Silero VAD model (another ~50–100 MB PyTorch
graph) into the subprocess. Segments are returned as a list of dicts — if longform is
called repeatedly the internal pyannote state may accumulate references.

No `gc.collect()` or `del segments` is called after joining segment texts.

**Key location:** `gigaam_worker.py:_handle_transcribe()` lines 122–133:
```python
segments = _MODEL.transcribe_longform(audio_path)
text = "\n\n".join(...)
# segments list remains live until next GC cycle
```

### H3 (Lower probability) — stdin pipe buffer backpressure

**Evidence:** `_GigaAMSubprocessSession._send()` uses `readline()` on `stdout=PIPE`
with a threading.Timer for timeout. `stderr=PIPE` is not drained during normal
operation — only read on OOM exit (`_check_proc_oom_on_exit`).

If gigaam or pyannote write warning/debug messages to stderr during inference (common
for HuggingFace Hub), the OS pipe buffer (typically 64 KB on macOS) fills up. Once
full, the subprocess blocks on stderr writes, preventing the response from being sent.

However this would manifest as hangs rather than memory growth, so it is less likely
to explain 1.5 GB RSS directly. It is still worth draining stderr asynchronously.

### H4 (Structural) — Model never unloaded (by design)

The worker holds `_MODEL` as a module-level global for the subprocess lifetime. There
is no mechanism to unload or reload the model without restarting the subprocess. If the
subprocess is long-lived (which it is — it is reused across all transcriptions), weights
remain pinned in RSS permanently.

**This is intentional but combined with H1 (MPS buffer pool) accounts for the full
1570 MB:** ~500 MB weights + ~1000 MB MPS buffer pool after warm-up.

---

## Instrumentation Plan

### Where to add tracemalloc

1. **Startup** (`gigaam_worker.py:main()`, before the readline loop):
   ```python
   if os.environ.get("KRAB_EAR_TRACE_GIGAAM_MEM") == "1":
       import tracemalloc
       tracemalloc.start()
   ```

2. **After each transcribe** (`gigaam_worker.py:_handle_transcribe()`):
   ```python
   # Log RSS after inference
   import resource
   rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on Linux, KB on macOS
   sys.stderr.write(f"[mem] rss_after_transcribe={rss}\n")
   ```

3. **Every 10 requests** (counter in `main()` loop):
   ```python
   if _trace_enabled and request_count % 10 == 0:
       snap = tracemalloc.take_snapshot()
       top = snap.statistics("lineno")[:10]
       for stat in top:
           sys.stderr.write(f"[tmalloc] {stat}\n")
   ```

4. **After inference, before returning** — explicit MPS cache clear (candidate fix):
   ```python
   try:
       import torch
       if torch.backends.mps.is_available():
           torch.mps.empty_cache()
   except Exception:
       pass
   ```

### Where NOT to instrument

- Do not add tracemalloc to `stt_gigaam.py` (parent process) — it measures the wrong
  process. The 1570 MB is in the subprocess.
- Do not call `gc.collect()` inside every transcribe — GC is already automatic and
  adding it may mask the real issue.

---

## Suggested Fix (for followup commit)

After confirming H1 via `profile_gigaam_worker.command` run:

1. **Add `torch.mps.empty_cache()` after each transcribe** in `_handle_transcribe()`.
   This releases MPS pooled buffers back to the Metal allocator. Expected reduction:
   300–700 MB during idle.

2. **Add `gc.collect()` after `del segments`** in longform path. Ensures pyannote
   intermediate segment tensors are reclaimed promptly.

3. **Consider subprocess restart after N requests** via a request counter in `main()`.
   If MPS pool keeps growing after cache clear, periodic restart (every 50–100
   requests) bounds the maximum resident size. Add `max_requests` parameter to the
   `load` command.

4. **Drain stderr asynchronously** in `_GigaAMSubprocessSession` using a background
   thread (similar to `subprocess.DEVNULL` but capturing for logging). This prevents
   pyannote/HF Hub warning flood from filling the 64 KB pipe buffer.

---

## Instrumentation Added (this commit)

`KrabEar/core/workers/gigaam_worker.py` now supports opt-in tracing via:

```bash
KRAB_EAR_TRACE_GIGAAM_MEM=1 python gigaam_worker.py
```

When enabled:
- `tracemalloc.start()` called at process startup
- RSS logged to stderr after every transcribe request
- Top-10 tracemalloc allocations logged every 10 requests

Zero overhead when env var absent (env check at module level).

---

## How to Run the Profile

```bash
# Install psutil in main venv if needed
source .venv_krab_ear/bin/activate && pip install psutil

# Run profiling script (50 cycles by default)
chmod +x scripts/profile_gigaam_worker.command
CYCLES=50 OUTPUT=gigaam-mem-profile.csv scripts/profile_gigaam_worker.command

# View results
column -t -s , gigaam-mem-profile.csv
```

Expected output columns: `timestamp, pid, name, rss_mb, vsz_mb`

Watch for `gigaam_worker` RSS trend — if it grows linearly with transcription count
(H2), vs stabilizes after warm-up (H1), the fix differs.

---

## References

- `KrabEar/core/workers/gigaam_worker.py` — subprocess worker source
- `KrabEar/core/pipeline/stt_gigaam.py` — adapter + `_GigaAMSubprocessSession`
- `scripts/memory_baseline.py` — baseline snapshot tool (PR #367)
- `scripts/profile_gigaam_worker.command` — this commit, drives profiling run
- `memory/reference_gigaam_bench_2026-04-26.md` — GigaAM vs Whisper bench data
