# Phase B Wave 78 — Error Code Candidates

**Date:** 2026-05-19  
**Log analyzed:** `logs/krab-ear-backend.out.log` (2026-04-08 → 2026-05-19, 86 680 lines)  
**Current registry size:** 46 codes (through Wave 171 / PR #519)

---

## Summary

Five new error code candidates identified from production logs. All five patterns appear at
significant frequency (`>= 5` occurrences) and are **not** currently wired to `error_bus.push`.

---

## Candidate 1 — `stt.gigaam_hf_cache_miss`

**Trigger frequency:** 306 total occurrences; peak 2026-04-26 (~100 hits/day during active call session)

**Log line example (anonymized):**
```
2026-04-26 19:28:59 [KrabEar.Engine] WARNING: GigaAM transcribe failed (duration=50.0s, longform=True):
  _GigaAMSubprocessSession: transcribe failed: transcribe_failed:
  LocalEntryNotFoundError: An error happened while trying to locate the file on the Hub
  and we cannot find the requested files in the local cache.
```

**Root cause:** GigaAM subprocess worker attempts a HuggingFace Hub network fetch when the
local model cache (`~/.cache/huggingface/hub`) is missing or invalidated. Falls through to
Whisper fallback silently. User sees degraded RU transcription quality with no toast.

**Distinct from:** `stt.gigaam_worker_timeout` (code 390) — that covers subprocess start/crash;
this covers HF cache miss which causes `LocalEntryNotFoundError` during transcription.
`stt.gigaam.ffmpeg_missing` (code 406) — different failure path entirely.

**Severity rationale:** `warn` — GigaAM fallback to Whisper works, but user should know RU
accuracy is degraded and fix the cache.

**Suggested action_id:** `"open_huggingface_cache"` (new action: open
`~/.cache/huggingface/hub` in Finder so user can inspect/re-download)

**Proposed entry:**
```python
"stt.gigaam_hf_cache_miss": ErrorEntry(
    code="stt.gigaam_hf_cache_miss",
    component="engine",
    severity="warn",
    sentry_tier="breadcrumb",
    dedupe_window_sec=300,
    user_msg_ru=(
        "GigaAM модель не найдена в локальном кэше — "
        "используется Whisper как запасной вариант. "
        "Переподключитесь к интернету или обновите кэш HuggingFace."
    ),
    user_msg_en=(
        "GigaAM model missing from local cache — falling back to Whisper. "
        "Reconnect to the internet or refresh the HuggingFace cache."
    ),
    action_id=None,
),
```

**Wiring location:** `core/engine.py` line ~2460 (in `_transcribe_gigaam`, catch branch that
logs `"GigaAM transcribe failed"` when `"LocalEntryNotFoundError"` in `str(exc)`).

---

## Candidate 2 — `rewriter.model_unloaded`

**Trigger frequency:** 36 total occurrences (cluster: 2026-05-05, 51 rewriter timeouts that day
of which 36 are actually this distinct 400 error mislabeled as `rewriter.timeout`)

**Log line example:**
```
2026-05-05 19:19:41 [KrabEar.Backend.LLMRewriter] WARNING: LLM rewriter failure:
  kind=http_error model=gemma-4-e4b-it-mlx base_url=http://127.0.0.1:1234/v1
  elapsed_ms=901 status=400 body={"error":"Model has not started loading/has been unloaded."}
```

**Root cause:** LM Studio returns HTTP 400 with body `"Model has not started loading/has
been unloaded."` — the model was evicted from GPU memory (memory pressure or manual unload).
Current code at `llm_rewriter.py:632` falls through to `rewriter.timeout` for all non-500
non-channel_error HTTP errors, mislabeling this as a timeout. User sees generic "Rewriter
недоступен" without actionable info about the actual cause.

**Severity rationale:** `error` — model is unloaded, user must act in LM Studio to reload it.
Distinct actionability from a transient timeout.

**Suggested action_id:** `"open_lm_studio_settings"` (already exists)

**Proposed entry:**
```python
"rewriter.model_unloaded": ErrorEntry(
    code="rewriter.model_unloaded",
    component="rewriter",
    severity="error",
    sentry_tier="event",
    dedupe_window_sec=120,
    user_msg_ru=(
        "LM Studio выгрузил модель из памяти. "
        "Откройте LM Studio и загрузите модель повторно."
    ),
    user_msg_en=(
        "LM Studio unloaded the model from memory. "
        "Open LM Studio and reload the model."
    ),
    action_id="open_lm_studio_settings",
),
```

**Wiring location:** `backend/llm_rewriter.py` line ~623, add `elif response.status_code == 400
and "not started loading" in body_preview` branch before the generic fallthrough on line 632.

---

## Candidate 3 — `stt.mlx_watchdog_hang`

**Trigger frequency:** 5 total occurrences (2026-05-08 to 2026-05-09, cluster during extended
call session)

**Log line example:**
```
2026-05-08 22:00:53 [core.mlx_subprocess] ERROR: [MLXWatchdog] inference timed out after
  60.1s (model=mlx-community/whisper-large-v3-mlx, total_crashes=1). Signalling fallback.
```

**Root cause:** `MLXWatchdog` detects Whisper inference thread hung past the 60-second
watchdog timeout (Metal GPU stuck). Currently calls `_notify_sentry_timeout` (Sentry only)
and raises `MLXTimeoutError` — there is **no** `error_bus.push`. User sees no UI toast and
has no indication the GPU hung (which may require an LM Studio or process restart).

**Distinct from:** `stt.mlx_timeout` (code 268) — that covers the Whisper model taking long to
load (warmup timeout). This covers a true GPU hang during active inference with a watchdog kill.

**Severity rationale:** `error` — GPU hang is serious, total_crashes counter increments, user
needs to know.

**Suggested action_id:** `None` (watchdog auto-recovers; if `total_crashes >= 3` an additional
`critical` push with `action_id="restart_backend"` could be added in a follow-up).

**Proposed entry:**
```python
"stt.mlx_watchdog_hang": ErrorEntry(
    code="stt.mlx_watchdog_hang",
    component="engine",
    severity="error",
    sentry_tier="event",
    dedupe_window_sec=60,
    user_msg_ru=(
        "Metal GPU завис во время распознавания (таймаут 60с). "
        "Автоматически переключился на резервную модель."
    ),
    user_msg_en=(
        "Metal GPU hung during STT inference (60s watchdog). "
        "Automatically switched to fallback model."
    ),
    action_id=None,
),
```

**Wiring location:** `core/mlx_subprocess.py` line ~146, after the `logger.error(...)` call in
`MLXWatchdog._run_with_timeout`, before `raise MLXTimeoutError(...)`.

---

## Candidate 4 — `rewriter.output_ratio_fallback`

**Trigger frequency:** 38 total occurrences (spread across sessions Apr 26 – May 9)

**Log line example:**
```
2026-04-26 20:11:54 [KrabEar.Backend.LLMRewriter] WARNING: LLM output too short
  (6% of input), falling back to original
```

**Root cause:** Length ratio guard in `llm_rewriter.py` detects output < 35% of input length
(hallucination/truncation indicator) and silently falls back to raw text. Code sets
`self._last_error = "output_too_short"` but does **not** push to `error_bus`. User has no
visibility that the rewrite was discarded. Useful signal for model quality degradation.

**Severity rationale:** `warn` — fallback to raw text is safe; but repeated occurrences indicate
the model is truncating or responding in the wrong language.

**Suggested action_id:** `"disable_rewriter"` (already exists)

**Proposed entry:**
```python
"rewriter.output_ratio_fallback": ErrorEntry(
    code="rewriter.output_ratio_fallback",
    component="rewriter",
    severity="warn",
    sentry_tier="breadcrumb",
    dedupe_window_sec=120,
    user_msg_ru=(
        "Rewriter вернул слишком короткий ответ — вставлен оригинальный текст. "
        "Модель может неправильно обрабатывать входные данные."
    ),
    user_msg_en=(
        "Rewriter output was too short — original text used. "
        "The model may be mishandling the input."
    ),
    action_id="disable_rewriter",
),
```

**Wiring location:** `backend/llm_rewriter.py` line ~703, in the `if ratio < 0.35:` block,
after `self._last_error = "output_too_short"`.

---

## Candidate 5 — `ipc.audio_device_poll_flood`

**Trigger frequency:** 417 occurrences of `list_audio_inputs` rate limit hits total; peak
1412 hits on 2026-04-23 (top method after `get_call_assist_state` on that date).

**Log line example:**
```
2026-04-23 02:38:22 [KrabEar.Backend.Service] WARNING: IPC rate limit exceeded:
  method=list_audio_inputs wait=0.28s
```

**Root cause:** Swift `HistoryPanelController` settings tab polls `list_audio_inputs` at a
faster interval than the IPC throttle allows (token bucket rate limiter). Each burst of hits
generates a `WARNING` but is **not** pushed to `error_bus`. Unlike `get_call_assist_state`
(expected high-frequency polling), `list_audio_inputs` is a heavyweight `sounddevice.query`
call. This flood indicates the GUI poll loop for the audio device dropdown is misconfigured
(should be on-open or debounced, not continuous).

**Distinct from:** `ipc.reconnect` (code 217) — that covers socket disconnects, not rate limiting.

**Severity rationale:** `warn` — no data loss, but causes unnecessary CPU load and indicates
a Swift-side polling bug that should be fixed.

**Suggested action_id:** `None`

**Proposed entry:**
```python
"ipc.audio_device_poll_flood": ErrorEntry(
    code="ipc.audio_device_poll_flood",
    component="ipc",
    severity="warn",
    sentry_tier="breadcrumb",
    dedupe_window_sec=300,
    user_msg_ru=None,  # silent — internal diagnostic only
    user_msg_en=None,
    action_id=None,
),
```

**Wiring location:** `backend/service.py` line ~1093, in the `IPC rate limit exceeded` branch
for `list_audio_inputs` specifically (or all non-`get_call_assist_state` methods to filter
expected poll flood separately from unexpected throttling).

---

## Wiring Priority

| Priority | Code | Reason |
|---|---|---|
| 1 | `stt.gigaam_hf_cache_miss` | 306 occurrences, silent quality degradation, actionable |
| 2 | `rewriter.model_unloaded` | Currently mislabeled as `rewriter.timeout`, confusing diagnosis |
| 3 | `rewriter.output_ratio_fallback` | 38 hits, silent fallback, quick to wire |
| 4 | `stt.mlx_watchdog_hang` | GPU hang severity warrants user visibility |
| 5 | `ipc.audio_device_poll_flood` | Internal diagnostic; low user-facing priority |

---

## Bonus Finding — `error_bus.push` Silently Failing (Bug)

**21 occurrences** of:
```
[KrabEar.Backend.LLMRewriter] ERROR: error_bus.push failed for code=rewriter.timeout
```
All on 2026-05-05. The `_push_error()` guard catches exceptions from `error_bus.push()` and
logs them instead of raising. The root cause is likely a `KrabError` model validation failure
or an `ErrorBus` internal exception. This is a **separate bug** (not a new code candidate) —
the existing `rewriter.timeout` code is registered correctly but the push itself fails. Requires
investigation of the `ErrorBus.push()` deduplication or ring-buffer logic on that date.
