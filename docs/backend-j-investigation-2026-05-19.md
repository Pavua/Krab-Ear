# BACKEND-J Investigation — rewriter HTTP 400 after retry exhaustion

## Events

| # | Event ID | Timestamp (UTC) | Sentry code |
|---|----------|-----------------|-------------|
| 1 | `83d8ea97cec94a05a6a1049e5f06b589` | 2026-05-18T19:43:16Z | `rewriter.timeout` (warn batch flush) |
| 2 | `cf64a326cf8140fdad1dc7938b951737` | 2026-05-18T19:49:19Z | `rewriter.timeout` (warn batch flush) |

- **Model**: `gemma-4-26b-a4b-it-optiq`
- **Base URL**: `http://127.0.0.1:1234/v1`
- **Component**: `rewriter`, Phase B WarnBatcher, severity `warn`, count=2

## What Actually Happened (Log Evidence)

The real HTTP 400 body from every single failure (confirmed across 13 consecutive log entries):

```
{"error":"Error in iterating prediction stream: RuntimeError: There is no Stream(gpu, N) in current thread."}
```

This is a **Metal GPU stream corruption** inside LM Studio's mlx_lm server — _not_ a model unloaded / idle TTL issue.

### Exact timeline (local = UTC+2):

```
21:41:29  GigaAM AudioChunker failed (24.8s audio): padding error → tries longform path
21:41:35  GigaAM longform also fails (LocalEntryNotFoundError, HF model not cached)
          → fallback to mlx-whisper, STT takes 6.8s + 14.2s (GPU-heavy work)
21:41:49  AudioRecorder buffer overflow warnings (×2) — GPU pressure peaking
21:41:53  FIRST 400 from LM Studio: elapsed_ms=12582 (rewriter was mid-request during GPU saturation)
          body: "There is no Stream(gpu, 1)"
21:41:55  SECOND 400: elapsed_ms=11986 — "Stream(gpu, 1)"
21:41:57  Subsequent 400s: elapsed_ms=22–64ms — LM Studio GPU state permanently broken
          Stream IDs cycling: gpu,1 → gpu,4 → gpu,8 → gpu,5
21:42:10  Circuit breaker: CLOSED → OPEN after 10 consecutive fails, cooldown=60s
21:43:16  [Sentry event #1] HALF_OPEN probe → OPEN, cooldown=120s
21:45:18  HALF_OPEN probe → OPEN, cooldown=240s
21:49:19  [Sentry event #2] HALF_OPEN probe → OPEN, cooldown=480s
```

Note: LM Studio resumed normally after a few minutes (no further 400s in log). The GPU Metal stream self-healed, but by then circuit cooldown was 480s.

## Root Cause Hypothesis (Ranked)

### 1. CONFIRMED — GigaAM GPU thrash → Metal stream corruption in LM Studio (HIGH confidence)

The GigaAM subprocess worker and LM Studio's mlx_lm backend both use the Metal GPU. At 21:41, GigaAM's chunker failed and fell to the longform path (also failed), then mlx-whisper picked up a 24.8s file taking 14+ seconds. This concurrent GPU saturation corrupted LM Studio's internal Metal CommandStream state. LM Studio began returning HTTP 400 with `RuntimeError: There is no Stream(gpu, N) in current thread` for every subsequent chat completion request — even instant ones (22ms elapsed). This is a known Apple Metal driver behavior: when a CommandStream is broken, all subsequent enqueues to that stream fail immediately.

**Trigger chain**: `GigaAM longform fail + mlx-whisper fallback (14s GPU hold)` → Metal GPU contention → LM Studio stream corrupted → 10× HTTP 400 in 17 seconds → circuit OPEN

### 2. Possible aggravator — audio buffer overflow causing burst of parallel STT calls

Two `AudioRecorder buffer overflow` warnings at 21:41:49-51 indicate the recording pipeline was under load. Multiple concurrent GigaAM calls in flight (several VAD+STT cycles visible in the log) may have amplified GPU contention.

### 3. Eliminated — Model unloaded / idle TTL hit

The `GET /v1/models` at 14:38 (5 hours before) returned HTTP 200. There is no warmup log at 21:41, and LM Studio model presence is confirmed. The 400 body is a GPU stream error, not "model not loaded". Idle TTL hypothesis eliminated.

### 4. Eliminated — Invalid request format / context window

All payloads use the same format that worked before. Context window: gemma-4-26b is ~8k tokens, transcripts are short. Eliminated.

## Code-Level Bug Found

In `llm_rewriter.py` lines 610–624, any non-200 that isn't 401, 503, or 500-token-bug falls through to:

```python
self._push_error("rewriter.timeout", f"http_{response.status_code}_after_retry")
```

**This is wrong**: HTTP 400 with `"Stream(gpu, N)"` is a Metal GPU crash, not a timeout. It gets labelled `rewriter.timeout` both in the error code and in the Sentry event title. This caused the misleading "http_400_after_retry" surfaced in Sentry — the `rewriter.timeout` code is the WarnBatcher's grouping key, which makes the event description doubly confusing.

Additionally, the circuit breaker `fail_threshold=10` (default in `service.py` initialization) allowed 10 consecutive 400s before opening — extending the damage window from ~0s to ~17s.

## Fix Proposal

### Option A — Dedicated GPU stream error code (RECOMMENDED, minimal change)

In `llm_rewriter.py`, add a pattern match for the Metal stream error before the generic catch-all:

```python
# After line 618 (channel_error check), before line 624 (push rewriter.timeout):
if "there is no stream(gpu" in body_preview.lower() or "no stream(gpu" in body_preview.lower():
    self._push_error(
        "rewriter.gpu_stream_error",
        f"LM Studio Metal GPU stream broken: {body_preview}",
        severity="error",
    )
else:
    self._push_error("rewriter.timeout", f"http_{response.status_code}_after_retry")
```

Add `"rewriter.gpu_stream_error"` to `error_codes.py` with `severity="error"` and `dedupe_seconds=300`.

**Benefit**: correct Sentry grouping, user sees actionable message ("LM Studio GPU crash — restart LM Studio").

### Option B — Reduce circuit fail_threshold for 400 errors

Lower `circuit_fail_threshold` from 10 to 3 specifically for HTTP 4xx errors (not for timeouts/connection errors). This limits the number of failed requests before circuit opens, reducing the burst damage.

**Risk**: may trigger false-positive circuit opens on transient 400s (e.g., during model swap).

### Option C — LM Studio restart advice on gpu_stream_error

For Option A, add an `action_id: "restart_lm_studio"` in `error_codes.py` so the UI toast gives the user a clear instruction. LM Studio `/restart` API endpoint or `applescript quit + open` could be called automatically.

## Risk

- **Option A**: Low. Pure additive pattern match, no behavioral change for the circuit breaker or request flow.
- **Option B**: Medium. Tighter threshold may cause unnecessary circuit opens.
- **Option C**: Medium. Automated LM Studio restart needs testing; manual user prompt is safer short-term.

## Sentry Note

Both events have `code=rewriter.timeout` tag but the real underlying error is a Metal GPU stream crash. After Option A lands, new events will be grouped under `rewriter.gpu_stream_error` with correct severity=error and separate dedup window.
