# W1324 Re-audit: `core/silence_detector.py` — Residual Findings

**Date:** 2026-05-27
**Branch audited:** `codex/krab-ear-v2` (current tip: v2.0.5 release)
**File:** `KrabEar/core/silence_detector.py` (255 lines)
**Prior audits:** W912 (threshold unify -40 dB), W1016 (initial audit), W1018 (preserve-whisper threshold + dead line), W1099 (AudioChunker NF5 stereo claim)
**W1320 interaction:** fix-denoiser-percentile-strict-W1320 is MERGED into `codex/krab-ear-v2`

---

## W912 / W1018 Merge State

| Branch | Description | Status |
|--------|-------------|--------|
| `feature/fix-silence-threshold-W912` | Export `SILENCE_THRESHOLD_DB` / `SILENCE_THRESHOLD_AMP`; unify AudioQualityAnalyzer to -40 dB | **NOT MERGED** |
| `fix-silence-whisper-threshold-W1018` | Two-tier `SILENCE_THRESHOLD_DB_STRICT` / `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER`; remove stale `audio.shape` line | **NOT MERGED** |

Both branches were developed in worktrees. Neither has a merged PR on `codex/krab-ear-v2`.
The fixes and constants they introduce are absent from production code.

**W1320 status:** `fix-denoiser-percentile-strict-W1320` IS merged — the denoiser
percentile `<` → `<=` fix is live. However, `SilenceDetector` does not interact with
`AudioDenoiser` in any caller path (`SmartSilenceSkipper`, `RealtimeSilenceFilter`,
and `AudioChunker` all call `SilenceDetector` directly without denoising),
so W1320 has no coupling effect on the findings below.

---

## Status of W1016 Findings

| Finding | Status |
|---------|--------|
| W1016 F1 — stale `audio.shape` expression in `trim_silence` | **NOT FIXED** — W1018 branch not merged |
| W1016 F2 — missing two-tier threshold for whisper preservation | **NOT FIXED** — W1018 branch not merged |

---

## New Residual Findings (cap 5)

### R1 — HIGH: `AudioQualityAnalyzer` threshold diverges 10x from `SilenceDetector` (W912 not merged)

**Location:** `KrabEar/core/audio_quality.py` line 24; `KrabEar/core/silence_detector.py` line 50

`SilenceDetector.detect_silence()` default `threshold_db=-40.0` corresponds to RMS
amplitude `0.01` (via `_db_to_amplitude`).

`AudioQualityAnalyzer._SILENCE_RMS_THRESHOLD = 0.001` corresponds to **-60 dB** — a 10x
(20 dB) gap.

Consequence: the same recording will be reported as having different speech/silence
ratios depending on which module evaluates it. A frame with RMS=0.005 is **speech** by
`AudioQualityAnalyzer` standards but **silence** by `SilenceDetector` standards. Users
can get contradictory "silence ratio" values from `analyze_silence_file` (IPC:
`analyze_silence`) vs `get_quality_analysis` (IPC: `analyze_audio_quality`) on identical
recordings.

W912 proposed exporting `SILENCE_THRESHOLD_DB: float = -40.0` and
`SILENCE_THRESHOLD_AMP: float = 0.01` as module constants for all consumers to import,
and updating `AudioQualityAnalyzer._SILENCE_RMS_THRESHOLD` accordingly. That branch is
not merged.

**Fix:** Merge W912 or apply its changes: add the two module-level constants to
`silence_detector.py` and update `audio_quality.py` to import `SILENCE_THRESHOLD_AMP`
rather than hard-coding `0.001`.

---

### R2 — MEDIUM: Stale `audio.shape` expression still in `trim_silence` on main (W1018 not merged)

**Location:** `KrabEar/core/silence_detector.py` line 134

```python
# Current code (main branch):
        audio.shape
        mono = self._to_mono(audio)
```

The expression `audio.shape` is a no-op — it reads the attribute and discards the
result. The statement has no side effects. It was a debugging remnant that W1018
removed but W1018 is not merged.

While harmless at runtime, the statement is misleading. A reader may assume it performs
a shape validation or is the body of a conditional guard. Any linter or dead-code
scanner (e.g. vulture) will flag it. It also means the intent to validate input shape
before mono conversion is signalled but never enforced.

**Fix:** Remove line 134 (`audio.shape`). This is the exact one-line change in W1018's
diff; cherry-pick it or re-apply it.

---

### R3 — MEDIUM: Six independent copies of `-40.0` threshold constant, no authoritative source

**Location:** `silence_detector.py` (×4 default args), `smart_silence_skipper.py` (×1 module const),
`realtime_silence_filter.py` (×1 module const), `audio_chunker.py` (×1 default arg),
`call_silence_probe.py` (×2 default args), `audio_analytics_service.py` (×1 inline)

```
KrabEar/core/silence_detector.py:         threshold_db: float = -40.0  (×4 occurrences)
KrabEar/core/smart_silence_skipper.py:    _DEFAULT_THRESHOLD_DB: float = -40.0
KrabEar/backend/realtime_silence_filter.py: _DEFAULT_THRESHOLD_DB: float = -40.0
KrabEar/core/audio_chunker.py:            threshold_db: float = -40.0
KrabEar/backend/call_silence_probe.py:    threshold_db: float = -40.0  (×2)
KrabEar/backend/audio_analytics_service.py: float(params.get("threshold_db", -40.0))
```

None of these reference a shared constant. A future threshold recalibration (e.g. for
low-gain microphones in noisy environments) requires editing 10 locations across 6
files — with no compile-time guarantee of consistency.

W912's proposed `SILENCE_THRESHOLD_DB` export would resolve this by giving callers a
single import point.

**Fix:** Add `SILENCE_THRESHOLD_DB: float = -40.0` to `silence_detector.py` (W912's
approach). Update `smart_silence_skipper._DEFAULT_THRESHOLD_DB` and
`realtime_silence_filter._DEFAULT_THRESHOLD_DB` to import from `silence_detector`.
Update `audio_chunker` and `call_silence_probe` default arg annotations to reference the
constant.

---

### R4 — LOW: `detect_silence` accepts int16/unnormalized audio and silently misclassifies silence as speech

**Location:** `KrabEar/core/silence_detector.py` `detect_silence`, `get_speech_ratio`, `trim_silence`

All three methods assume `audio` is normalized float in `[-1, 1]`. The docstring for
`detect_silence` documents this (`нормализованный в [-1, 1]`), but there is no runtime
assertion or dtype check.

When int16 audio is passed (e.g., from `wave.open` without explicit normalization):

```python
int_audio = np.ones(1024, dtype=np.int16) * 10  # amplitude=10, very quiet
det.detect_silence(int_audio, 16000)  # returns [] — classified as speech
# RMS = 10.0 >> threshold_amp = 0.01 -> every frame is "speech"
```

An int16 signal with amplitude 10 out of 32767 (~-70 dB in human terms) will always
report zero silence regions, because RMS=10 exceeds the float threshold 0.01.
`trim_silence` will return audio untrimmed even when it is actually all silence.

In practice, Krab Ear's pipeline loads audio via `soundfile.read(dtype="float32")` or
`AudioEngine.record()` (which produces float32 NumPy arrays directly from
`sounddevice`), so this path is not triggered in production. However, `analyze_silence_file`
and callers in tests could be exposed if dtype is not controlled.

**Fix:** Add an early assertion or `numpy.asarray(audio, dtype=np.float32)` coercion at
the start of `_to_mono` or in each public method, with a docstring note that int
inputs are not supported.

---

### R5 — LOW: `to_dict()` produces `duration_sec=0.0` for sub-ms audio at high sample rates

**Location:** `KrabEar/core/silence_detector.py` `SilenceRegion.to_dict()` line 36

`to_dict()` rounds all fields to 4 decimal places. For audio shorter than ~0.05 ms
(e.g., 1 sample at 44100 Hz), `end_sec = 1/44100 ≈ 0.0000226` rounds to `0.0000`:

```python
# Observed:
det.detect_silence(np.zeros(1, dtype=np.float32), 44100)
# → [SilenceRegion(start_sec=0.0, end_sec=0.0, duration_sec=0.0)]
# to_dict() → {'start_sec': 0.0, 'end_sec': 0.0, 'duration_sec': 0.0}
```

The region IS returned (indicating silence was found), but `duration_sec=0.0` makes
`sum(r.duration_sec for r in regions)` in `analyze_silence_file` report
`total_silence_sec=0.0` while `silence_region_count=1` — a self-contradictory result.

Callers that use `total_silence_sec` as the primary signal will undercount silence.
In practice, Krab Ear only processes 16 kHz audio and the minimum practical chunk is
>= 32 ms (one frame = 512 samples), so this edge case is not triggered in production.

**Fix:** In `to_dict()`, increase rounding precision from 4 to 6 decimal places for
`end_sec` and `duration_sec`, or filter out zero-duration regions in
`analyze_silence_file`.

---

## Test Coverage Assessment (post W1016 / W1018)

| Scenario | Covered |
|----------|---------|
| detect_silence basic cases | Yes (TestDetectSilence) |
| trim_silence leading/trailing | Yes (TestTrimSilence) |
| get_speech_ratio 0/1/partial | Yes (TestGetSpeechRatio) |
| multi-channel detect + trim | Yes (TestDetectSilenceEdgeCases, TestTrimSilence) |
| custom threshold_db values | Yes (TestCustomThreshold, TestThresholdSensitivity) |
| concurrent thread safety | Yes (TestConcurrentDetect) |
| zero sample_rate | Yes (TestDetectSilenceEdgeCases, TestGetSpeechRatioEdge) |
| analyze_silence_file (real file path) | No — only mocked in test_audio_analytics_service.py |
| int16/unnormalized audio input | No |
| 1-sample sub-ms audio (zero-duration region) | No |
| negative sample_rate in trim_silence | No |
| SILENCE_THRESHOLD_DB / SILENCE_THRESHOLD_AMP constants | N/A — not yet exported |

Key gap: `analyze_silence_file` has no unit test exercising the real code path (only
`patch`-mocked in `test_audio_analytics_service.py`). The function's error path
(`FileNotFoundError`) and the `soundfile` integration are untested.

---

## W1320 / W1099 Interaction Assessment

**W1320 (denoiser percentile strict-lt fix):** `AudioDenoiser` is not used in any
`SilenceDetector` call chain. `SmartSilenceSkipper`, `RealtimeSilenceFilter`, and
`AudioChunker` all call `SilenceDetector.detect_silence()` on raw (undenoised) PCM.
No interaction with W1320 findings.

**W1099 (AudioChunker stereo claim):** `AudioChunker` uses
`SilenceDetector._to_mono(audio)` before calling `detect_silence` (line 124 in
`audio_chunker.py`). The stereo memory finding in W1099 refers to full stereo copy
kept in memory during chunking — not to `_to_mono` itself. `_to_mono` correctly
averages channels for `(N, C)` channels-last layout (as produced by `soundfile`).
No new interaction issue.

---

## Summary Table

| # | Severity | Title | Status |
|---|----------|-------|--------|
| R1 | HIGH | AudioQualityAnalyzer threshold 10x divergence (W912 unmerged) | Unresolved |
| R2 | MEDIUM | Stale `audio.shape` expression in `trim_silence` (W1018 unmerged) | Unresolved |
| R3 | MEDIUM | 10 independent copies of `-40.0` threshold constant across 6 files | Unresolved |
| R4 | LOW | int16/unnormalized audio silently misclassifies silence as speech | Unresolved |
| R5 | LOW | `to_dict()` zero-duration for sub-ms audio at 44100 Hz | Unresolved |
