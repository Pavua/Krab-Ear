# W1133 Re-audit v2: `core/audio_quality.py` — Residual Findings

**Date:** 2026-05-26
**Branch audited:** `codex/krab-ear-v2` @ `6c900317` (v2.0.5)
**File:** `KrabEar/core/audio_quality.py`
**Prior audits:** W1015 (6 findings), W1100 (5 residual findings after W1017 unmerged)
**Wave fixes under review:** W1017 (NaN/Inf), W1103 (re-apply W1017), W1107 (silence threshold)

---

## Executive Summary

All three fix branches — `fix/audio-quality-nan-W1017`, `fix/audio-quality-nan-W1103`, and
`fix/audio-quality-unify-silence-W1107` — are **NOT merged** into `codex/krab-ear-v2`. Each
branch has commits that predate the HEAD of `codex/krab-ear-v2` but none appear in the main
branch history (confirmed via `git branch --contains`).

Result: `_safe_float` and `SILENCE_THRESHOLD_AMP` are **absent from production code**.
All W1015 F1–F5 findings and W1100 R1–R5 findings remain live.

This audit identifies 5 new findings beyond previous audits, focusing on denoiser ordering
semantics, long-audio performance, and float64 memory pressure.

---

## Merge State of W1017 / W1103 / W1107

| Wave | Branch | Status in `codex/krab-ear-v2` |
|------|--------|-----------------------------|
| W1017 | `fix/audio-quality-nan-W1017` | NOT MERGED |
| W1103 | `fix/audio-quality-nan-W1103` | NOT MERGED |
| W1107 | `fix/audio-quality-unify-silence-W1107` | NOT MERGED |

Verification command:

```bash
git branch --contains 21513ad8  # W1107 commit
# Returns: fix/audio-quality-unify-silence-W1107 only — not codex/krab-ear-v2
```

---

## W1015 / W1100 Findings — Current Status

| ID | Description | Status |
|----|-------------|--------|
| F1/R4 | NaN/Inf propagation into JSON output (`rms_level`, `snr_estimate_db`) | **OPEN** |
| F2 | JSON serialization crash to Swift client | **OPEN** |
| F3/R1 | Dual silence thresholds (0.001 vs 0.01) | **OPEN** |
| F4/R2 | `snr=0.0` for audio < 4096 samples (< 256 ms at 16 kHz) | **OPEN** |
| F5/R3 | `_error_bus` never injected via IPC call path | **OPEN** |

---

## New Findings (cap 5)

### N1 — MEDIUM: `analyze_audio_quality` IPC reports quality of raw audio, not denoised audio

**Location:** `KrabEar/backend/audio_analytics_service.py:74`, `KrabEar/core/audio_quality.py:247–249`

The IPC handler `handle_analyze_audio_quality` calls `analyze_file(file_path)`, which creates
a fresh `AudioQualityAnalyzer()` and reads the raw file from disk. In the engine's `transcribe()`
path (line 842 of `engine.py`), adaptive denoising is applied to live numpy buffers **before**
STT, but there is no code path where the denoised audio is fed to `AudioQualityAnalyzer`.

As a result:
- The quality report (SNR, silence_ratio) reflects pre-denoise audio.
- The STT engine receives post-denoise audio.
- When denoising is active, the quality score can read "poor" (low SNR) even though STT
  will see improved audio. The warning "Большой dynamic range: возможно наличие щелчков"
  may fire spuriously.

This is a semantic mismatch. For file imports via IPC, denoising never runs, so the
mismatch only affects live recordings if a caller reads quality metrics before/after
recording. Currently no caller does this explicitly — but it is a latent footgun.

**Severity:** MEDIUM (misleading diagnostics; no crash)

**Fix:** If quality analysis of live recordings is needed, pass the (optionally denoised)
numpy buffer to `AudioQualityAnalyzer.analyze()` rather than the raw file path.

---

### N2 — MEDIUM: `float64` cast doubles memory for long audio files

**Location:** `AudioQualityAnalyzer.analyze`, line 71

```python
audio_data = audio_data.astype(np.float64)
```

`soundfile` returns `float32` by default via `analyze_file`. The unconditional cast to
`float64` doubles the working memory: for a 1-hour recording at 16 kHz the audio array
is ~439 MB as float32. After casting it becomes ~878 MB, and the original float32 array
is still referenced until GC — peak memory can reach ~1.3 GB just for this analysis step.

For the intended use-case (pre-flight quality check on imported 1–2 h call recordings)
this can OOM the backend on systems with limited RAM.

**Severity:** MEDIUM (OOM risk for long imports; affects production per CLAUDE.md
`MAX_AUDIO_MB` default = 1000 MB — hourly ALAC/AAC calls are common)

**Fix:** Cast only if dtype is not already float64:
```python
if audio_data.dtype != np.float64:
    audio_data = audio_data.astype(np.float64)
```
Or use float32 arithmetic throughout (sufficient precision for RMS/SNR estimation).

---

### N3 — LOW: Python loop in `_compute_silence_ratio` is O(n) and blocks the IPC thread for long audio

**Location:** `_compute_silence_ratio`, lines 157–162

```python
frames = np.array_split(audio, n_frames)
silent = sum(
    1 for f in frames
    if len(f) > 0 and np.sqrt(np.mean(f ** 2)) < _SILENCE_RMS_THRESHOLD
)
```

`np.array_split` is O(1) per view, but the Python `sum()` generator iterates `n_frames`
times in pure Python. For 1-hour audio at 16 kHz: `n_frames = 56 250` iterations.
Each calls `np.mean()` on a 1024-sample view (two numpy calls + sqrt). Estimated wall time
is 200–400 ms on M4 Max, blocking the IPC socket handler thread during analysis.

The same loop pattern exists in `_estimate_snr` (line 180), compounding to ~400–800 ms
total for a 1-hour file. There is no timeout, no progress reporting, and no background
thread offload.

**Severity:** LOW (IPC handler blocks; no crash; acceptable for typical short recordings)

**Fix:** Vectorize both methods:
```python
# _compute_silence_ratio vectorized
frames_2d = audio[:n_frames * _SILENCE_FRAME_SIZE].reshape(n_frames, _SILENCE_FRAME_SIZE)
rms_per_frame = np.sqrt(np.mean(frames_2d ** 2, axis=1))
silence_ratio = float(np.mean(rms_per_frame < _SILENCE_RMS_THRESHOLD))
```
This reduces the 56 250-iteration loop to two vectorized numpy ops (~5 ms for 1-hour audio).

---

### N4 — LOW: `analyze_file` creates a new `AudioQualityAnalyzer` with no `_error_bus`, so the empty-audio error path at line 78 is always a silent no-op via the IPC call path

**Location:** `analyze_file`, line 249; `AudioQualityAnalyzer.analyze`, line 78

This extends W1015 F5 (W1100 R3) with a new observation: not only is `_error_bus` never
set on the instance returned by `analyze_file`, but `AudioAnalyticsService.__init__`
also receives no `error_bus` parameter (confirmed in `service.py:448–454`). There is no
code path through which a production `_error_bus` reaches `AudioQualityAnalyzer`.

The `getattr(self, "_error_bus", None)` guard at line 78 is therefore always `None` in
production. The empty-audio event (`stt.empty_audio_warning`) can only fire if a caller
manually sets `analyzer._error_bus = ...` before calling `.analyze()` — which no production
code does.

**Severity:** LOW (silent monitoring gap; no crash; empty-audio events are lost)

**Fix:** Add `error_bus` parameter to `AudioAnalyticsService.__init__`, store it as
`self._error_bus`, and pass it to `AudioQualityAnalyzer` instances created inside the
service:
```python
# In AudioAnalyticsService.handle_analyze_audio_quality:
analyzer = AudioQualityAnalyzer()
analyzer._error_bus = getattr(self, "_error_bus", None)
report = analyze_file(file_path, analyzer=analyzer)
```

---

### N5 — LOW: `np.clip(NaN, -20.0, 80.0)` does not sanitize NaN — `snr_estimate_db` can be NaN even after the `_estimate_snr` guard at line 187

**Location:** `_estimate_snr`, lines 196–197 and 208

```python
snr = 20.0 * np.log10(signal_rms / noise_rms)
return float(np.clip(snr, -20.0, 80.0))
```

`np.clip(np.nan, ...)` propagates NaN unchanged. If `audio` contains NaN samples,
`np.mean(audio ** 2)` → NaN → `signal_rms` → NaN → `np.log10(NaN / noise_rms)` → NaN →
`np.clip(NaN, ...)` → NaN → `float(NaN)` → Python `nan`. The caller at line 115 assigns
this to `snr_estimate_db` without any sanitization.

This is distinct from W1017/W1103's fix scope: those fixes wrap the *return value of
`analyze()`* in `_safe_float()`, but `_estimate_snr` is also called internally to produce
`quality_score` via `_score(snr_db, ...)`. Even with W1103 merged, `_score` would receive
NaN from `_estimate_snr` before `_safe_float` is applied, and `snr_db >= 30` with NaN is
`False` — causing valid audio to silently score as `"poor"`.

**Severity:** LOW (silent misclassification only; W1017/W1103 fix is still needed to
prevent JSON crash, but `_score` receives pre-sanitized NaN regardless)

**Fix:** Add NaN guard inside `_estimate_snr` before returning:
```python
result = float(np.clip(snr, -20.0, 80.0))
return result if not (result != result) else 0.0  # NaN check
```
Or more clearly: `return result if math.isfinite(result) else 0.0`

---

## Summary Table

| ID | Severity | Description | Prior art |
|----|----------|-------------|-----------|
| N1 | MEDIUM | Quality report reflects raw audio, not denoised; semantic mismatch with STT path | New |
| N2 | MEDIUM | `float64` cast doubles RAM for long audio (1 h = ~1.3 GB peak) | New |
| N3 | LOW | Python loop in silence/SNR computation blocks IPC thread ~400–800 ms for 1-hour audio | New |
| N4 | LOW | `_error_bus` path confirmed dead — `AudioAnalyticsService` never receives `error_bus` | Extends F5/R3 |
| N5 | LOW | `np.clip` does not sanitize NaN; `_score()` receives NaN before `_safe_float` would apply | Extends F1 |

---

## Priority Action Items

1. **Merge W1103 + W1107 branches first** — they are fully tested and fix F1/F2 (HIGH) and F3.
2. **Fix N2** (float64 cast) — trivial one-liner, unblocks large file imports safely.
3. **Fix N3** (vectorize silence loops) — performance fix, important for batch re-transcription.
4. **Fix N5** — add `math.isfinite` guard inside `_estimate_snr` before returning.
5. **Wire N4** (`_error_bus` into `AudioAnalyticsService`) — monitoring improvement.
6. **N1** (denoiser ordering) — document the semantic boundary; lower priority since no
   code path currently reads quality metrics on live (denoised) recordings.
