# GigaAM STT Adapter Audit — W1216

**Date:** 2026-05-26
**Scope:** `KrabEar/core/pipeline/stt_gigaam.py`, `KrabEar/core/workers/gigaam_worker.py`, `KrabEar/core/pipeline/stt_gigaam_adapter.py`
**Branch:** `audit/audio-quality-residual-W1100`
**Auditor:** W1216 sub-agent

---

## Summary

5 findings. 2 HIGH, 2 MED, 1 LOW. No critical secrets exposed. The worker singleton guard (W525 flock) and memory management (W63/H1-H3 fixes) are solid. Main gaps are in crash recovery, concurrent access, and subtle IPC token exposure.

---

## F1 — HIGH: No auto-restart after worker crash/timeout

**File:** `stt_gigaam.py:287-318` (`_get_subprocess_session`), `stt_gigaam.py:802-812` (`_timeout_kill`)

After `_timeout_kill` fires (or the worker crashes via OOM), `_proc.terminate()` is called but `self._subprocess` is **not set to None**. On the next `transcribe()` call, `_get_subprocess_session()` sees `self._subprocess is not None` and returns the dead session. `_GigaAMSubprocessSession.is_loaded()` returns `False` (because `self._proc.poll() is not None`), causing an immediate `RuntimeError("worker not started or crashed")` — with no re-spawn attempt.

Result: **one timeout permanently disables GigaAM for the adapter lifetime**, forcing all subsequent calls to fall back to Whisper silently. No recovery without restarting the backend.

**Fix:** In `_timeout_kill`, after `self._proc.terminate()`, also call `self._loaded = False` and have `_get_subprocess_session` clear `self._subprocess = None` when `is_loaded()` is False so the next call re-spawns.

```python
# stt_gigaam.py _get_subprocess_session
def _get_subprocess_session(self) -> "_GigaAMSubprocessSession":
    if self._subprocess is not None and self._subprocess.is_loaded():
        return self._subprocess
    if self._subprocess is not None:
        # Dead session — clear it so we re-spawn below.
        self._subprocess = None
    ...
```

---

## F2 — HIGH: Race condition on subprocess spawn (no adapter-level lock)

**File:** `stt_gigaam.py:287-318` (`_get_subprocess_session`)

`GigaAMAdapter._get_subprocess_session()` is not protected by any lock at the adapter level. The check-then-act idiom:

```python
if self._subprocess is not None:   # line 289
    return self._subprocess
...
session = _GigaAMSubprocessSession(...)
session.start()                    # spawns Popen
self._subprocess = session         # line 317
```

If two threads concurrently call `transcribe()` when `_subprocess is None`, both pass the guard and spawn separate `_GigaAMSubprocessSession` objects. The gigaam_worker flock-singleton guard kills the **second** `Popen` process immediately (exit 0), but `start()` still sends a `load` command and waits up to 180 seconds for a response that never arrives, eventually timing out and calling `close()`. The adapter then has `self._subprocess` pointing to the last winner — but the race is non-deterministic; on fast machines the load can complete before the lock fires.

`_GigaAMSubprocessSession._send()` does serialize with `self._lock`, but that only helps once a single session is established. It does not protect the session creation path in the parent adapter.

**Fix:** Add a `threading.Lock` to `GigaAMAdapter.__init__` and wrap `_get_subprocess_session` body:

```python
self._spawn_lock = threading.Lock()

def _get_subprocess_session(self):
    with self._spawn_lock:
        if self._subprocess is not None and self._subprocess.is_loaded():
            return self._subprocess
        ...
```

---

## F3 — MED: HF token travels plaintext in JSON stdin/stdout IPC

**File:** `stt_gigaam.py:615-618` (`_GigaAMSubprocessSession.transcribe`), `gigaam_worker.py:253-263`

When `longform=True` and `hf_token` is non-empty, the token is included verbatim in the JSON request line written to the worker's stdin:

```python
request["hf_token"] = hf_token
self._proc.stdin.write(json.dumps(request, ...) + "\n")
```

The worker pipes are `text=True` subprocess pipes; on macOS these can appear in `lsof`/pipe metadata and are readable by processes with matching UID. More importantly, if DEBUG-level logging is active anywhere in the call path that captures the raw request dict (e.g. via `logger.debug("%s", request)`), the token appears in log files.

The worker correctly restores env vars after each call (SEC MED-1 in `gigaam_worker.py:255-311`), but there is no masking when the token is serialized for transport or if JSON parse errors cause the raw line to be logged (`stt_gigaam.py:716`).

**Fix:** Hash or truncate the token in any error/debug log paths. Consider passing the HF token only via environment variable at subprocess spawn time (already done for `MALLOC_STACK_LOGGING`), rather than per-request via JSON.

---

## F4 — MED: Hardcoded confidence 0.9 — quality signal fully absent

**File:** `stt_gigaam.py:175-180`, `gigaam_worker.py:329`

GigaAM does not expose per-segment log-probabilities, so the adapter always returns `"confidence": 0.9`. This constant is used by:

- `TranscriptionScorer` to compute the A–F quality grade shown in UI.
- `MetricsCollector` p95 confidence window.
- Fallback chain: `_build_fallback_chain` in engine uses confidence threshold to decide whether to try the next adapter.

With a hardcoded 0.9, the scorer always grades GigaAM output highly regardless of actual quality (short noise clips, silence, heavily accented speech). Users see an "A" grade for unintelligible transcripts. The fallback chain never switches to Whisper based on confidence because 0.9 exceeds any reasonable threshold.

**Fix:** Implement a heuristic confidence proxy: ratio of non-whitespace characters to audio duration (WPM proxy), or check the text-length-vs-duration ratio used in `LLMRewriter.length_ratio_guard`. Even a simple `min(0.95, len(text.split()) / max(1, duration_sec / 0.5) / 3.0)` would differentiate empty/garbled results from real transcripts.

---

## F5 — LOW: Tmp WAV leak on abnormal process termination

**File:** `stt_gigaam.py:148-165`

```python
with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
    tmp_path = tmp.name

try:
    self._write_wav(tmp_path, audio_16k)
    ...
finally:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
```

The `NamedTemporaryFile` context manager exits before the `try` block — so `tmp_path` is created and closed (but not deleted, because `delete=False`). If the Python process receives `SIGKILL` between the `with` block exiting and the `finally` executing, the tmp file is never deleted.

On a busy system with frequent GigaAM calls (each producing one 16 kHz WAV file), leaked files can accumulate in `/tmp`. For a 30-second clip at 16 kHz int16: 30 × 16000 × 2 bytes ≈ 960 KB per call. A session with 100 calls = ~94 MB leaked.

The fix is standard: use `tempfile.TemporaryDirectory` or register a `signal.signal(SIGTERM, ...)` cleanup handler, or use Python's `atexit` module. Alternatively, use `delete=True` and keep the file open until the `finally` block.

---

## Already Resolved Items (for reference)

- **Worker singleton / duplicate leak (W69):** flock-based guard in `gigaam_worker.py:49-86`. Solid.
- **stderr pipe-full backpressure (H3):** daemon drain thread + ring buffer in `stt_gigaam.py:719-763`. Solid.
- **MPS buffer pool leak (H1):** `torch.mps.empty_cache()` + `gc.collect()` after every transcribe. Solid.
- **pyannote segments leak (H2):** explicit `del segments` + `gc.collect()` in longform path. Solid.
- **pyannote VAD gated dependency:** gated HF model requires manual TOS accept at `huggingface.co/pyannote/segmentation-3.0`. Documented. AudioChunker is the preferred non-gated path (engine.py:2480-2504). Status: known, documented, not a new finding.
- **MALLOC_STACK_LOGGING env leak (W64):** stripped from subprocess env before Popen. Solid.
- **HF token persistence per-call (SEC MED-1):** env vars restored in `finally` block in both worker and in-process paths. Solid.

---

## Files Audited

- `/KrabEar/core/pipeline/stt_gigaam.py` (848 lines)
- `/KrabEar/core/workers/gigaam_worker.py` (411 lines)
- `/KrabEar/core/pipeline/stt_gigaam_adapter.py` (112 lines)
- `/KrabEar/core/engine.py` (lines 2401-2563, GigaAM dispatch)
- `/KrabEar/backend/settings_backup.py` (redaction list)
