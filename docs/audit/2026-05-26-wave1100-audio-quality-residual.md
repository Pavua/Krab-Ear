# W1100 Re-audit: `core/audio_quality.py` — Residual Findings

**Date:** 2026-05-26  
**Branch audited:** `codex/krab-ear-v2` (post W1017 worktree, pre-merge)  
**File:** `KrabEar/core/audio_quality.py`  
**Prior audit:** W1015 (5 findings: F1–F5); W1017 wrote a fix for F1+F2 (NaN/Inf JSON safety)

---

## Executive Summary

W1017's fix (`_safe_float` coercion) was written in worktree `fix/audio-quality-nan-W1017`
but **PR #938 is still OPEN** — the fix has never merged into `codex/krab-ear-v2`.
The current main branch is identical to the pre-W1017 source. All original W1015 findings
are therefore still present in production, including F1+F2 (the HIGH ones). Beyond the
unmerged PR, 5 new residual findings are documented below.

---

## Status of W1015 Findings

| Finding | Status |
|---------|--------|
| F1 — NaN/Inf in JSON output (rms/peak/snr) | **NOT FIXED** — PR #938 open, not merged |
| F2 — JSON serialization crash to Swift client | **NOT FIXED** — same, PR #938 open |
| F3 — Dual silence thresholds | **STILL PRESENT** (see R1 below) |
| F4 — Short audio <4096 samples gets snr=0 | **STILL PRESENT** (see R2 below) |
| F5 — `_error_bus` dead in production | **STILL DEAD** (see R3 below) |

---

## New Residual Findings (cap 5)

### R1 — MEDIUM: Dual silence thresholds are still inconsistent

**Location:** lines 24, 160, 191

`_compute_silence_ratio` (line 160) classifies a frame as silent when
`RMS < _SILENCE_RMS_THRESHOLD` = **0.001**.  
`_estimate_snr` (line 191) classifies a frame as part of the noise floor when
`RMS < _SILENCE_RMS_THRESHOLD * 10` = **0.01**.

The 10× difference means a frame that `_compute_silence_ratio` treats as "speech"
(RMS = 0.003) will be used as noise floor in `_estimate_snr`, inflating noise floor
and underestimating SNR. The two methods will produce contradictory assessments of
the same frames.

**W1015 F3 status:** still present, unchanged since original audit.

**Fix:** use a single named constant `_NOISE_FLOOR_RMS_THRESHOLD = 0.01` and
keep `_SILENCE_RMS_THRESHOLD = 0.001` for silence detection, making the intentional
split explicit — or unify to a single value if the 10× heuristic is not needed.

---

### R2 — LOW: SNR returns 0.0 for any audio shorter than 0.256 s at 16 kHz

**Location:** `_estimate_snr`, line 174

```python
if n < _SILENCE_FRAME_SIZE * 4:  # 1024 * 4 = 4096 samples
    return 0.0
```

At 16 000 Hz this silently returns `snr=0.0` for recordings under **0.256 seconds**.
At 44 100 Hz the cutoff is **0.093 seconds**. The cutoff is undocumented and the
caller's `_score()` will classify these as `"poor"` (snr_db < 10), which is misleading
for a short but high-quality recording (e.g., a command word).

**W1015 F4 status:** still present, unchanged.

**Fix:** either lower the minimum to `_SILENCE_FRAME_SIZE * 2` (512 samples minimum
frame coverage), or propagate a `None` / special sentinel value so the score function
can distinguish "SNR unknown" from "SNR = 0 dB".

---

### R3 — LOW: `_error_bus` is never injected via the production IPC call path

**Location:** `AudioQualityAnalyzer.__init__` (implicit), `analyze_file`, line 249

The `stt.empty_audio_warning` error-bus push (lines 78–98) only fires when
`self._error_bus` is set. In the production IPC path (`analyze_audio_quality` →
`AudioAnalyticsService.handle_analyze_audio_quality` → `analyze_file`), `analyze_file`
instantiates `AudioQualityAnalyzer()` with no arguments, leaving `_error_bus` unset
(`getattr(self, "_error_bus", None)` returns `None`).

No existing code in `service.py`, `audio_analytics_service.py`, or any caller sets
`_error_bus` on `AudioQualityAnalyzer`. The dead-code branch was present before W1017
and remains dead post-W1017.

**W1015 F5 status:** still dead.

**Fix (minimal):** add `error_bus` parameter to `AudioQualityAnalyzer.__init__`, pass it
from `AudioAnalyticsService.__init__`, and supply it via `analyze_file(path, analyzer=...)`
in `handle_analyze_audio_quality`.

---

### R4 — HIGH: NaN/Inf input audio propagates to `rms_level` / `peak_level` — JSON safety regression not blocked

**Location:** `analyze()` lines 104–105

PR #938 (W1017) adds `_safe_float()` coercion at the **output** of `analyze()`.
However, the PR has not merged. In the current `codex/krab-ear-v2`:

```python
rms_level = float(np.sqrt(np.mean(audio_data ** 2))) if n_samples > 0 else 0.0
peak_level = float(np.max(np.abs(audio_data))) if n_samples > 0 else 0.0
```

When `audio_data` contains `NaN` or `Inf` values (e.g., from a corrupt or malformed
audio file read by `soundfile`), these expressions produce Python `float('nan')` or
`float('inf')`. Both values pass through `round()` unchanged and land in `to_dict()`:

```
{"rms_level": NaN, "peak_level": Infinity, ...}
```

Python's `json.dumps` emits the non-RFC-7159 tokens `NaN` and `Infinity`. Swift's
`JSONDecoder` rejects these with a parse error, causing the `analyze_audio_quality`
IPC call to crash the Swift panel.

**Confirmed by live test:**
```python
>>> report = AudioQualityAnalyzer().analyze(np.array([float('nan')], dtype=np.float32), 16000)
>>> report.rms_level
nan
>>> json.dumps(report.to_dict())
'{"rms_level": NaN, ...}'  # invalid JSON
```

**Fix:** merge PR #938 (adds `_safe_float` guard) and optionally add an early input
sanitization pass (`np.nan_to_num(audio_data, nan=0.0, posinf=1.0, neginf=-1.0)`) before
processing to avoid silently hiding corrupt input.

---

### R5 — LOW: No test coverage for NaN/Inf input audio

**Location:** `KrabEar/tests/test_audio_quality.py`

The test suite covers empty arrays (`np.array([], ...)`) but has no test for audio
arrays containing `NaN` or `Inf` samples. The `test_empty_audio_does_not_raise` test
explicitly checks `n_samples == 0` but not `n_samples > 0 with NaN values`.

Given that R4 demonstrates a live JSON-crash from NaN input, the absence of a
regression test means future changes could silently re-introduce the same bug.

Missing test cases:
- `test_nan_input_audio_no_crash_json_safe` — `rms_level` and `peak_level` must be
  finite floats after W1017 merge.
- `test_inf_input_audio_no_crash_json_safe` — same for `Inf`.
- `test_snr_zero_boundary_at_4095_samples` — confirm `snr == 0.0` for the boundary
  case documented in R2.
- `test_silence_threshold_consistency` — unit test that verifies both silence-ratio
  and SNR noise-floor thresholds are documented and not accidentally unified.

---

## Summary Table

| ID | Severity | Description | Fixed by W1017? |
|----|----------|-------------|-----------------|
| R1 | MEDIUM | Dual silence thresholds 0.001 vs 0.01 still inconsistent | No (pre-existing) |
| R2 | LOW | SNR=0 for any audio <4096 samples (~0.256s @ 16kHz) | No (pre-existing) |
| R3 | LOW | `_error_bus` dead in all production call paths | No (pre-existing) |
| R4 | HIGH | NaN/Inf input audio → invalid JSON, Swift crash | No — PR #938 not merged |
| R5 | LOW | No regression tests for NaN/Inf input audio | No |

**Action required:** merge PR #938 immediately (R4 HIGH). R1–R3, R5 are follow-ups.
