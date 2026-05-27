# W1404 Audit: `backend/transcriber.py` Post-Wave 1139/1177/1303/1391

**Date:** 2026-05-27
**Auditor:** W1404 sub-agent (read-only)
**Branch base:** `codex/krab-ear-v2`
**Scope:** `KrabEar/backend/transcriber.py` (147 lines), all callers, all test files.

---

## Summary

`Transcriber` is a thin 147-line wrapper over `AudioEngine`.  
The class is stable and correctly wires the majority of parameters.  
Five findings are documented below (two MEDIUM, three LOW).

---

## Findings

### F1 — MEDIUM: `silence_ranges` param absent from `Transcriber.transcribe()` (W1139 gap)

**File:** `KrabEar/backend/transcriber.py`, `transcribe()` method  
**Engine signature:** `engine.py:671` — `silence_ranges: list[tuple[float, float]] | None = None`

`AudioEngine.transcribe()` accepts `silence_ranges` (added by W1139) which zeroes silence segments in the PCM buffer before STT. `Transcriber.transcribe()` does not declare or forward this parameter. The same omission applies to `progress_callback`.

As a result any caller who goes through `Transcriber` cannot pass silence ranges; they have to bypass the wrapper and call `self.transcriber.engine.transcribe()` directly — which is exactly what `recording_core_service.py:1301` does for the progress-callback path, and which skips the Phase B.1 HF_TOKEN diarization guard entirely.

**Evidence:**
```python
# transcriber.py — missing params
def transcribe(
    self,
    audio_data: Any,
    quality_profile: str = "balanced",
    # NO silence_ranges
    # NO progress_callback
    ...
) -> dict[str, Any]:
    ...
    return self.engine.transcribe(
        audio_data,
        # silence_ranges NOT forwarded
        ...
    )
```

```python
# recording_core_service.py:1299–1309 — direct engine bypass
if progress_callback is not None:
    self.transcriber.engine.set_quality_profile(quality_profile)
    transcribe_payload = self.transcriber.engine.transcribe(  # bypasses guard!
        audio_path,
        ...
        progress_callback=progress_callback,
    )
```

**Fix:** Add `silence_ranges` and `progress_callback` to `Transcriber.transcribe()` signature and forward them. The engine bypass in `recording_core_service.py` could then be collapsed to a single code path.

---

### F2 — MEDIUM: HF_TOKEN guard not fired on primary stop_recording path

**File:** `KrabEar/backend/recording_core_service.py:947–955`

The Phase B.1 guard in `Transcriber.transcribe()` fires only when `settings` is passed. The primary recording-stop call does **not** pass `settings=` or `diarize=`:

```python
# recording_core_service.py:947
transcribe_payload = self.transcriber.transcribe(
    audio,
    quality_profile=quality_profile,
    cleanup_profile=cleanup_profile,
    lang_hint=lang_hint,
    extra_vocabulary=user_vocabulary if user_vocabulary else None,
    history_context=_recent_history if _recent_history else None,
    stt_hotwords=_combined_hotwords,
    # settings= NOT passed
    # diarize= NOT passed
)
```

Because `settings is None`, the diarization guard block is skipped entirely. If a user has `diarization_enabled=True` but no HF_TOKEN, no error is pushed and diarization silently fails inside the engine — the `diarization.no_token` error code never reaches the user.

The Phase B.1 tests (e.g. `test_transcriber_errors.py::TranscribeCallSiteDiarizationTests`) only test the `settings`-provided path; they do not cover the `settings=None` fallback path used in production.

**Fix:** `recording_core_service._stop_recording_phase_c` should pass `settings=sr` and `diarize=None` (letting the guard resolve from `settings["diarization_enabled"]`). This requires adding the upstream `sr` dict to that call-site.

---

### F3 — LOW: `_FakeTranscriber` signature drift in `test_transcriber_errors.py`

**File:** `KrabEar/tests/test_transcriber_errors.py:44–52`

`_FakeTranscriber.transcribe()` has an outdated signature — it lacks `settings`, `diarize`, and `skip_vad_prefilter` parameters. Because the fake returns a plain string (`return f"test #{self.counter}"`) rather than the dict that `BackendService` expects, any test that exercises the full service round-trip with this fake will silently pass if the code path only calls `.get("text")` on the result. The fake was written before W1139/W1303 added params.

```python
class _FakeTranscriber:
    def transcribe(self, audio_data, quality_profile="balanced",
                   cleanup_profile="soft", domain="casual",
                   extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None) -> str:  # returns str, not dict!
        ...
        return f"test #{self.counter}"
```

**Fix:** Update `_FakeTranscriber` to accept `settings=None, diarize=None, skip_vad_prefilter=False` and return a minimal dict `{"text": ..., "confidence": 0.0}`.

---

### F4 — LOW: `transcribe_preview()` ignores `quality_profile` parameter

**File:** `KrabEar/backend/transcriber.py:96–100`

```python
def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> dict[str, Any]:
    """Быстрая транскрибация для realtime-превью (всегда в balanced режиме)."""
    self.engine.set_quality_profile("balanced")          # hardcoded
    return self.engine.transcribe(audio_data, cleanup_profile="soft", is_preview=True)
```

The method accepts `quality_profile` as a parameter but unconditionally overrides it with `"balanced"`. The docstring says this is intentional ("always balanced for minimum latency"), but the public parameter is misleading — callers who pass a non-default value get silent override. This is tested and passing (`test_preview_accepts_quality_profile_param` asserts `balanced` is enforced), so behaviour is correct, but the parameter itself should be removed or the docstring should explicitly say it is ignored.

**Fix (low-effort):** Remove the `quality_profile` parameter from `transcribe_preview()` or rename it to `_quality_profile=` with a deprecation note.

---

### F5 — LOW: `recording_core_service` engine bypass skips diarization guard (related to F1/F2)

**File:** `KrabEar/backend/recording_core_service.py:1299–1309`

When `progress_callback is not None` (audio import with progress reporting), the code calls `self.transcriber.engine.transcribe(...)` directly, bypassing the Transcriber wrapper entirely. This means:

1. The Phase B.1 HF_TOKEN diarization guard does not fire.
2. The Sentry breadcrumbs added in `Transcriber._push_diarization_no_token_if_needed` are skipped.
3. If future logic is added to `Transcriber.transcribe()` (e.g. rate limiting, privacy mode), it will be silently skipped for all audio imports that pass a progress callback.

This bypass was introduced because `Transcriber.transcribe()` lacks the `progress_callback` parameter (F1). Closing F1 would eliminate this bypass.

**Fix:** Add `progress_callback` to `Transcriber.transcribe()` and forward it to the engine (see F1). The bypass at line 1299–1309 becomes redundant and can be removed.

---

## Test Coverage Summary

| Area | Coverage | Notes |
|------|----------|-------|
| `__init__` injection paths | Good | `test_transcriber.py` covers all 5 init variants |
| `transcribe()` param forwarding | Good | `test_transcriber_diarization.py`, `test_transcriber_edge_cases.py` |
| `transcribe_preview()` | Good | profile-reset behaviour asserted |
| HF_TOKEN guard (`_push_diarization_no_token_if_needed`) | Good | 8 tests in `test_transcriber_diarization.py`, 6 in `test_transcriber_errors.py` |
| Error passthrough (engine raises) | Good | 5 exception types covered |
| `silence_ranges` forwarding | **Missing** | No test verifies this param reaches engine |
| `progress_callback` forwarding | **Missing** | No test at all (param not on Transcriber) |
| Primary stop_recording path (no `settings=`) | **Missing** | guard bypass not tested |
| `recording_core_service` engine bypass path | **Missing** | no test for progress_callback import path |

---

## Non-Findings

- **W1163 semantic_search remove (delete cascade):** `SemanticSearcher` is alive in `service.py` (handlers at lines 666–722); no cascade into `Transcriber`.
- **W1303 engine fallback chain:** Transcriber correctly exposes the quality_profile switch via `set_quality_profile()`; the fallback chain is internal to `AudioEngine` and not exposed through the wrapper (intentional).
- **W1391 preprocess order reorder:** affects `AudioEngine` internals only; `Transcriber` is unaffected.
- **W1177 STT crash recovery:** engine-internal; no change to Transcriber signature or error propagation contract.

---

## Verdict

The wrapper is architecturally sound but has two concrete gaps (F1, F2) where engine features cannot be accessed through the wrapper, forcing callers to bypass it. The bypass in turn defeats Phase B.1 safety guards. F3, F4, F5 are low-severity maintenance items. No critical bugs found.
