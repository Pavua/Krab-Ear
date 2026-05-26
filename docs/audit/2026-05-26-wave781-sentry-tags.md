# Sentry Tag Coverage Audit — Wave 781

**Date:** 2026-05-26  
**Branch:** feature/sentry-tag-check-W781  
**Scope:** `KrabEar/backend/` + `KrabEar/core/`

## Summary

Two `sentry_sdk.set_tag()` calls wired in this wave. Five additional locations documented as needing tags.

---

## Baseline: existing Sentry tag usage (pre-W781)

| File | Tag(s) set | Context |
|------|-----------|---------|
| `backend/observability.py` | `component` (via `capture_exception` helper), `signal` | `capture_exception(exc, component=)` wrapper; signal handler |
| `core/mlx_subprocess.py` | `component=mlx_watchdog`, `mlx_model` | Push-scope in timeout watchdog — best existing example |
| `backend/service.py` | _(none via set_tag — uses `capture_message` directly)_ | `send_diagnostics_to_sentry` handler |

No other `set_tag` calls found across 100+ backend/core modules before this wave.

---

## Tags wired in W781

### 1. `stt_engine` — `core/stt_router.py` · `STTRouter.select_model()`

**Location:** after model_id is finalized (post adapter_factory fallback), before `return model_id`.

```python
try:
    import sentry_sdk
    sentry_sdk.set_tag("stt_engine", model_id)
except Exception:
    pass
```

**Value examples:** `mlx-community/whisper-large-v3-mlx`, `gigaam`, `mlx-community/whisper-medium-mlx`

**Why useful:** STT engine is the single biggest determinant of crash class (MLX SIGSEGV,
GigaAM subprocess hang, adapter timeout). Filtering Sentry issues by `stt_engine` immediately
narrows blast radius.

---

### 2. `model_name` — `backend/llm_rewriter.py` · `LLMRewriter._rewrite_impl()`

**Location:** after input validation + `add_breadcrumb(rewrite_start)`, before circuit-breaker check.

```python
try:
    import sentry_sdk as _sentry_sdk
    _sentry_sdk.set_tag("model_name", self._model)
except Exception:
    pass
```

**Value examples:** `qwen3-4b-abliterated`, `gemma-4-26b-a4b-it`, `lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit`

**Why useful:** rewriter errors (`rewriter.timeout`, `rewriter.channel_error`, `circuit_open`)
are LM Studio model-specific. `model_name` tag lets you filter "did this timeout start after
switching to a larger model?" directly in Sentry.

---

## Tags documented as needed (not wired in W781)

### 3. `recording_state` — `backend/recording_core_service.py`

**Recommended location:** `handle_start_recording()` and `handle_stop_recording()` — set tag on
entering each phase, clear on exit.

```python
# In handle_start_recording(), after recorder.start():
try:
    import sentry_sdk
    sentry_sdk.set_tag("recording_state", "recording")
except Exception:
    pass

# In _stop_recording_phase_a(), after audio is captured:
try:
    import sentry_sdk
    sentry_sdk.set_tag("recording_state", "transcribing")
except Exception:
    pass

# In _stop_recording_phase_e(), after saving to store:
try:
    import sentry_sdk
    sentry_sdk.set_tag("recording_state", "idle")
except Exception:
    pass
```

**Value examples:** `idle`, `recording`, `transcribing`

**Why useful:** AppHang reports (AGENT-H, AGENT-M) were harder to triage because we didn't know
whether the crash happened during recording or post-processing. This tag would immediately answer
that question on any future AppHang / SIGSEGV.

**Blast radius concern:** `recording_core_service.py` is 1350+ LOC with 5 phase helpers —
touch is non-trivial. Recommend as a separate wave (W790+).

---

### 4. `component=engine` — `core/engine.py` · `AudioEngine.transcribe()`

**Current state:** `capture_exception(e, "_push_error_internal")` is called (line 488) but
`_push_error_internal` is a sentinel string that does NOT set a Sentry tag — it only logs.
The `capture_exception` helper in `observability.py` does accept a `component` kwarg, but
`engine.py` passes it as a positional arg to a `_push_error_internal` string, not the
`component` param.

**Fix needed:**
```python
# Replace:
capture_exception(e, "_push_error_internal")

# With:
capture_exception(e, component="engine")
```

**Why useful:** `engine.py` hosts MLX inference, pyannote diarization, and the fallback chain —
the most crash-prone path. Without `component=engine`, exceptions from here blend with all
other `capture_exception` calls.

---

### 5. `component=translator` — `backend/translator.py`

Same pattern as #4: `capture_exception(e, "_push_error_internal")` on line 133.

```python
# Fix:
capture_exception(e, component="translator")
```

---

### 6. `component=vg_ws_client` — `backend/vg_ws_client.py`

Same pattern on line 103.

```python
capture_exception(e, component="vg_ws_client")
```

---

### 7. `component=state_store` — `backend/state_store.py`

Same pattern on line 103.

```python
capture_exception(e, component="state_store")
```

---

## Implementation notes

- All `set_tag` calls use lazy `import sentry_sdk` inside try/except to preserve the
  existing "no-op when DSN absent" contract.
- Items 4–7 are low-risk mechanical fixes: 1-line change each in existing `except` blocks.
  Recommended bundling into a single follow-up PR (W790 or similar).
- Item 3 (`recording_state`) requires a dedicated pass since it spans 5 phase-helper methods.

## Tag taxonomy proposed

| Tag key | Setter | Values |
|---------|--------|--------|
| `stt_engine` | `STTRouter.select_model` | model id string |
| `model_name` | `LLMRewriter._rewrite_impl` | LM Studio model id string |
| `recording_state` | `RecordingCoreService` start/stop phases | `idle`, `recording`, `transcribing` |
| `component` | `capture_exception()` calls | `engine`, `translator`, `vg_ws_client`, `state_store`, `mlx_watchdog`, `startup` |
