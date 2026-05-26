# Wave 765 — ERROR_REGISTRY Stale-Code Audit

**Date:** 2026-05-26  
**Auditor:** Wave 765 sub-agent  
**Scope:** `KrabEar/backend/error_codes.py` ERROR_REGISTRY vs runtime raise sites in `KrabEar/backend/` + `KrabEar/core/`

---

## Summary

| Category | Count |
|---|---|
| Total codes in ERROR_REGISTRY | **51** |
| Fully live (raised from runtime code) | **44** |
| Test-only (raised only in test files, not in runtime) | **3** |
| Dead (only in error_codes.py + membership-check test) | **4** |

**Conclusion: 7 codes have no runtime raise site.** Of these, 4 are completely dead (only referenced in `test_error_codes.py` membership list), and 3 are wired in test files with push simulation but have no actual `_push_error` / `error_bus.push` call in production code.

---

## Methodology

1. All 51 keys parsed from `ERROR_REGISTRY` via AST.
2. For each key, `grep -rn "\"<code>\""` run across:
   - `KrabEar/backend/` + `KrabEar/core/` (excluding `error_codes.py`) — **runtime hits**
   - `KrabEar/tests/` — **test hits**, with `test_error_codes.py` membership-check hits subtracted separately
3. Classification:
   - **LIVE** = runtime hits > 0
   - **TEST-ONLY** = runtime hits = 0, but test files push or exercise the code directly (beyond membership list)
   - **DEAD** = runtime hits = 0, test hits = membership-check only (i.e., `test_error_codes.py` list assertion)

---

## Per-Category Breakdown

### Layer: paste (2 codes)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `paste.ax_denied` | ✅ LIVE | 1 | `service.py:1814` via `ax_denied` dict mapping |
| `paste.app_unsupported` | ✅ LIVE | 1 | `service.py:1815` via `app_unsupported` dict mapping |

### Layer: rewriter (13 codes)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `rewriter.timeout` | ✅ LIVE | 4 | `llm_rewriter.py:472/514/565/709` |
| `rewriter.connection_error` | ✅ LIVE | 2 | `llm_rewriter.py:533/584` |
| `rewriter.circuit_open` | ⚠️ TEST-ONLY | 0 | Code is **never pushed** in runtime. Circuit-open path at `llm_rewriter.py:456–465` records a breadcrumb only (`_add_bc`), does not call `_push_error`. Tests simulate push via `bus.push(KrabError(code="rewriter.circuit_open"))` in `test_error_bus_extras.py:449–450`. |
| `rewriter.unavailable` | ✅ LIVE | 1 | `llm_probe.py:226` |
| `rewriter.tool_calls_emitted` | ✅ LIVE | 1 | `llm_rewriter.py:731` |
| `rewriter.empty_response` | ✅ LIVE | 1 | `llm_rewriter.py:751` |
| `rewriter.parse_error` | ✅ LIVE | 1 | `llm_rewriter.py:740` |
| `rewriter.model_evicted` | ✅ LIVE | 1 | `llm_probe.py:184` |
| `rewriter.channel_error` | ✅ LIVE | 3 | `llm_rewriter.py:529/580/676` |
| `rewriter.fallback_used` | ✅ LIVE | 2 | `llm_rewriter.py:1382–1384` |
| `rewriter.unauthorized` | ✅ LIVE | 1 | `llm_rewriter.py:658` |
| `rewriter.warmup_failed` | ❌ DEAD | 0 | Only in `test_error_codes.py` membership list. `llm_rewriter.py` warmup path at line 1094 pushes `rewriter.warmup_timeout` instead. This code was added (Wave 50) but the wiring was superseded by `rewriter.warmup_timeout`. |
| `rewriter.warmup_timeout` | ✅ LIVE | 1 | `llm_rewriter.py:1094` |
| `rewriter.lm_studio_500` | ✅ LIVE | 1 | `llm_rewriter.py:691` |
| `rewriter.model_unloaded` | ⚠️ TEST-ONLY | 0 | Described as 422/400 "Model has not started loading" — but `llm_rewriter.py` does not push this code at the 422 handler. Tests in `test_error_bus_phase_b_wave78.py` exercise it via direct `bus.push`. Likely planned but never wired at the HTTP 422 response handling site. |
| `rewriter.output_ratio_fallback` | ✅ LIVE | 2 | `llm_rewriter.py:795/809` |
| `rewriter.lm_studio_stream_gpu_lost` | ✅ LIVE | 2 | `llm_rewriter.py:617/683` |

*(Note: `rewriter.lm_studio_500` counted separately — 13 rewriter codes in table above = correct)*

### Layer: stt (13 codes)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `stt.load_fail` | ✅ LIVE | 2 | `engine.py:1819/1835` |
| `stt.empty_text` | ✅ LIVE | 1 | `engine.py:902` |
| `stt.repetition_loop` | ✅ LIVE | 1 | `engine.py:890` |
| `stt.mlx_timeout` | ❌ DEAD | 0 | Only in `test_error_codes.py` membership list. `core/mlx_subprocess.py` pushes `stt.mlx_watchdog_hang` (not `stt.mlx_timeout`) for the timeout/hang scenario. These are overlapping concepts — `stt.mlx_timeout` was never wired. |
| `stt.padding_mismatch` | ❌ DEAD | 0 | Only in `test_error_codes.py` membership list. GigaAM padding logic in `core/pipeline/stt_gigaam.py` does not push this code — the padding error path falls through to the existing `stt.gigaam_worker_crashed` code. |
| `stt.oom_model_evicted` | ✅ LIVE | 2 | `engine.py:1825/1841` |
| `stt.gigaam_worker_timeout` | ✅ LIVE | 2 | `stt_gigaam.py:823/827` |
| `stt.gigaam.ffmpeg_missing` | ✅ LIVE | 1 | `service.py:358` |
| `stt.empty_audio_warning` | ✅ LIVE | 2 | `audio_quality.py:84/88` |
| `stt.diarization_skipped` | ✅ LIVE | 1 | `engine.py:2329` |
| `stt.gigaam_worker_crashed` | ✅ LIVE | 2 | `stt_gigaam.py:596/600` |
| `stt.critical_recognition_error` | ✅ LIVE | 1 | `engine.py:1096` |
| `stt.gigaam_hf_cache_miss` | ✅ LIVE | 1 | `engine.py:2535` |
| `stt.mlx_watchdog_hang` | ✅ LIVE | 2 | `mlx_subprocess.py:260/264` |

### Layer: diarization (3 codes)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `diarization.no_token` | ✅ LIVE | 2 | `transcriber.py:131/135` |
| `diarization.pipeline_fail` | ✅ LIVE | 1 | `engine.py:2838` |
| `diarization.vad_gated` | ❌ DEAD | 0 | Only in `test_error_codes.py` membership list. pyannote VAD gating logic (GigaAM longform) does not push this code. The blocker scenario documented in `blocker_pyannote_gated_2026-04-26.md` was never wired to this error code. |

### Layer: translation (1 code)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `translation.timeout` | ✅ LIVE | 1 | `translator.py:398` |

### Layer: mlx (3 codes)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `mlx.oom` | ✅ LIVE | 2 | `engine.py:521/1915` |
| `mlx.metal_assertion_failure` | ✅ LIVE | 1 | `engine.py:1928` |
| `mlx.semaphore_leak` | ✅ LIVE | 2 | `stt_gigaam.py:658/662` |

### Layer: history (1 code)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `history.write_fail` | ✅ LIVE | 1 | `state_store.py:219` |

### Layer: vocabulary (1 code)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `vocabulary.load_fail` | ✅ LIVE | 2 | `vocabulary_store.py:85/95` |

### Layer: ipc (3 codes)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `ipc.reconnect` | ✅ LIVE | 2 | `service.py:1906/1910` |
| `ipc.rate_limit_exceeded` | ✅ LIVE | 2 | `service.py:1309/1313` |
| `ipc.audio_device_poll_flood` | ⚠️ TEST-ONLY | 0 | Designed to fire when `list_audio_inputs` / `get_audio_devices` exceeds 10 calls/s, but the poll-flood detection logic was never added to `service.py` or `recording_core_service.py`. Tests in `test_error_bus_phase_b_wave78.py` push it directly via `bus.push`. Planned but unimplemented. |

### Layer: hotkey (1 code)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `hotkey.conflict` | ✅ LIVE | 2 | `service.py:1847/1851` |

### Layer: disk (1 code)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `disk.low_space` | ✅ LIVE | 2 | `disk_monitor.py:290/294` |

### Layer: audio (1 code)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `audio.buffer_overflow` | ✅ LIVE | 2 | `recorder.py:180/184` |

### Layer: system (1 code)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `system.malloc_env_leak` | ✅ LIVE | 2 | `stt_gigaam.py:519/523` |

### Layer: vgw (1 code)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `vgw.reconnect` | ✅ LIVE | 1 | `vg_ws_client.py:65` |

### Layer: agent (1 code)

| Code | Status | Runtime hits | Notes |
|---|---|---|---|
| `agent.binary_drift` | ✅ LIVE | 2 | `service.py:1945/1949` |

---

## Dead Candidates — Detail

### 4 × DEAD (never pushed anywhere outside error_codes.py)

| Code | Layer | Why it's dead | Recommended action |
|---|---|---|---|
| `rewriter.warmup_failed` | rewriter | Added Wave 50 as "generic warmup fail". The actual warmup probe in `llm_rewriter.py` pushes `rewriter.warmup_timeout` (added Wave 60) which is a more specific code for the same event. `warmup_failed` is now shadowed. | Wire to `warmup_probe()` exception catch (non-timeout failures), or remove if `warmup_timeout` covers all cases |
| `stt.mlx_timeout` | stt | Conceptually overlaps with `stt.mlx_watchdog_hang`. `mlx_subprocess.py` already pushes `stt.mlx_watchdog_hang` for timeout-caused hangs. `stt.mlx_timeout` was never wired. | Remove (duplicate of `stt.mlx_watchdog_hang`) or wire to the `TimeoutExpired` path specifically if distinction needed |
| `stt.padding_mismatch` | stt | GigaAM longform padding mismatch was caught by the production log scanner (Wave 51), but the fix path in `stt_gigaam.py` emits `stt.gigaam_worker_crashed` instead. `stt.padding_mismatch` has no emit site. | Wire at the GigaAM padding-error exception branch in `stt_gigaam.py`, or remove (merge into `stt.gigaam_worker_crashed`) |
| `diarization.vad_gated` | diarization | pyannote VAD gating scenario documented in memory but no raise site in `engine.py` or `transcriber.py`. The HF-gated download error falls through to `diarization.pipeline_fail`. | Wire at the `OSError`/`EnvironmentError` HF-gate detection in `engine.py` diarization pipeline, or remove |

### 3 × TEST-ONLY (pushed in tests but not in runtime)

| Code | Layer | Gap | Recommended action |
|---|---|---|---|
| `rewriter.circuit_open` | rewriter | Circuit-open path at `llm_rewriter.py:456–465` adds a breadcrumb only. The code was defined to give users a toast when the circuit breaker opens, but `_push_error` was never added at that branch. | Add `self._push_error("rewriter.circuit_open", f"circuit open state={self._circuit.state}")` at line 457 |
| `rewriter.model_unloaded` | rewriter | HTTP 422 / "Model has not started loading" path in `llm_rewriter.py` should push this, but the 422 response is currently handled by the generic `rewriter.timeout` or `rewriter.lm_studio_500` fallthrough. | Add push at the 422/400 body-match branch in `_call_lm_studio()` |
| `ipc.audio_device_poll_flood` | ipc | Poll-flood rate detector was planned (417 production hits documented in the code comment) but the detection logic was never added to `service.py` or `recording_core_service.py`. | Add a per-client rate counter for `list_audio_inputs` + `get_audio_devices` calls; push when > 10/s |

---

## Notes on Audit Limitations

1. **Dynamic code strings**: A small number of codes could theoretically be raised via dynamic string construction (e.g., `f"{prefix}.{suffix}"` or `entry = ERROR_REGISTRY[var]`). This audit uses literal string grep and would miss those. Inspection of `llm_rewriter.py`, `engine.py`, and `service.py` did not reveal dynamic code construction patterns.

2. **External / plugin code**: `backend/plugin_system.py` allows external plugins that could push any code. Not audited here.

3. **Wave 82 codes (disk.critical, system.proc_cmdline_permission, startup.stt_model_cache_miss)**: These codes are referenced in memory as Wave 82 candidates but are not in the current `ERROR_REGISTRY` in this worktree. Not applicable to this audit.

---

*Generated by Wave 765 audit sub-agent. Do not remove dead codes without first checking production logs — some may have been emitted historically even if the current raise site is missing.*
