# W1468 Third-Pass Audit: `core/silence_detector.py` — Post-W1333

**Date:** 2026-05-27
**Auditor:** W1468 (sub-agent, third pass)
**Branch audited:** `codex/krab-ear-v2` (HEAD f7086279)
**File primary scope:** `KrabEar/core/silence_detector.py` (264 lines)
**Secondary scope:** `core/silence_constants.py`, `core/audio_quality.py`, `core/noise_profiler.py`
**Prior audit chain:** W1016 (initial, 5 findings), W1124 (residual), W1324 (residual, 5 findings), W1333 (shared constant fix)

---

## Prior Wave Merge State

| Wave | Branch | Description | Merge Status |
|------|--------|-------------|--------------|
| W912 | `feature/fix-silence-threshold-W912` | Export SILENCE_THRESHOLD_DB/AMP; unify AudioQualityAnalyzer to -40 dB | **NOT MERGED** |
| W1018 | `fix-silence-whisper-threshold-W1018` | Two-tier strict/preserve-whisper thresholds; remove stale `audio.shape` | **NOT MERGED** |
| W1324 | (audit doc only, PR #1231) | Re-audit finding: 10-copies divergence documented | **MERGED** (docs only) |
| W1333 | `fix-shared-silence-threshold-W1333` (PR #1237) | Create `core/silence_constants.py`; replace 10 bare `-40.0` literals across 6 files | **MERGED** |

### W1333 Coverage Scope

W1333 replaced `-40.0` literals in 6 files:
- `core/silence_detector.py` (×4 default args)
- `core/smart_silence_skipper.py` (`_DEFAULT_THRESHOLD_DB`)
- `backend/realtime_silence_filter.py` (`_DEFAULT_THRESHOLD_DB`)
- `core/audio_chunker.py` (threshold_db default)
- `backend/call_silence_probe.py` (×2 default args)
- `backend/audio_analytics_service.py` (params.get fallback)

**What W1333 did NOT fix:** `audio_quality.py` (W1324 R1 HIGH — 10x divergence), `audio.shape` dead statement (W1018/W1324 R2), and the two functional issues (W1016 F2 whisper misclassification, W1016 F3 triplication).

---

## New Findings (Post-W1333, Cap 5)

### N1 — HIGH: `audio_quality.py` still hardcodes `_SILENCE_RMS_THRESHOLD = 0.001` (-60 dB), 10× divergence from shared constant

**File:** `KrabEar/core/audio_quality.py`, line 36
**Severity:** HIGH — same-recording contradictory results, user-visible

```python
# audio_quality.py line 36 — NOT fixed by W1333:
_SILENCE_RMS_THRESHOLD = 0.001  # RMS фрейма ниже этого → тишина
```

`SILENCE_THRESHOLD_AMP` from `silence_constants.py` is `0.01` (−40 dBFS).
`audio_quality._SILENCE_RMS_THRESHOLD` is `0.001` (−60 dBFS) — a 20 dB (10×) gap.

This was explicitly identified as W1324 R1 (HIGH) but was not included in W1333's scope
(W1333 only touched files using the `-40.0` literal, not this separate `0.001` divergence).

**Runtime consequence:** A signal frame at RMS = `0.005`:
- `SilenceDetector.detect_silence()` → **silence** (0.005 < 0.01)
- `AudioQualityAnalyzer._compute_silence_ratio()` → **speech** (0.005 > 0.001)

The IPC pair `analyze_silence` and `analyze_audio_quality` will report contradictory
silence ratios for identical recordings. In addition, `_compute_snr_estimate` uses
`frame_rms < _SILENCE_RMS_THRESHOLD * 10` (line 203) as the quiet-frame mask for SNR
estimation, meaning the quiet-frame mask threshold is `0.01` — which coincidentally
equals `SILENCE_THRESHOLD_AMP`, but only by accident. Any intentional recalibration of
`SILENCE_THRESHOLD_AMP` would silently break this coincidence.

`audio_quality.py` does not import from `core.silence_constants` or `core.silence_detector`.

**Fix:** In `audio_quality.py`:
```python
from core.silence_constants import SILENCE_THRESHOLD_AMP
_SILENCE_RMS_THRESHOLD = SILENCE_THRESHOLD_AMP  # -40 dBFS (0.01)
```
Note: this will change `_compute_silence_ratio` behavior — frames with RMS in `(0.001, 0.01)` will shift from "speech" to "silence". The SNR `* 10` multiplier should then be reviewed (currently `0.001 * 10 = 0.01`; after fix becomes `0.01 * 10 = 0.1`, a 10× change to the SNR quiet-frame mask).

**W1333 test gap:** `test_shared_silence_threshold_W1333.py` checks only 6 files and does
not include `audio_quality.py` in its AST scan. A 7th file check must be added.

---

### N2 — MEDIUM: `noise_profiler.py` imports `SILENCE_THRESHOLD_AMP` from `core.silence_detector` instead of `core.silence_constants`

**File:** `KrabEar/core/noise_profiler.py`, line 16
**Severity:** MEDIUM — indirect coupling, correct value but wrong import chain

```python
# noise_profiler.py line 16 — indirect import:
from core.silence_detector import SILENCE_THRESHOLD_AMP
```

`core.silence_detector` re-exports `SILENCE_THRESHOLD_AMP` via `__all__` (added by W1333),
so the value is correct at runtime (`0.01`). However:

1. The re-export in `silence_detector.py` exists specifically for backwards compatibility,
   documented with the comment "Источник истины — core.silence_constants." The intent is
   for modules to migrate to importing directly from `core.silence_constants`.

2. `noise_profiler.py` was wired by W1132 (`fix(wave1132): noise_profiler unify silence
   threshold via SILENCE_THRESHOLD_AMP`, merged as PR #1041). W1132's fix correctly
   changed the value but used `silence_detector` as the import source, predating
   `silence_constants.py` (which was created by W1333).

3. `test_noise_profiler_silence_threshold_W1132.py` also imports from `silence_detector`,
   not `silence_constants`. Both the production code and its test use the compatibility
   re-export path rather than the canonical source.

**Fix:** Update `noise_profiler.py` import and the corresponding test:
```python
# Replace:
from core.silence_detector import SILENCE_THRESHOLD_AMP
# With:
from core.silence_constants import SILENCE_THRESHOLD_AMP
```

---

### N3 — MEDIUM: Dead `audio.shape` expression still present in `trim_silence` (W1018 not merged)

**File:** `KrabEar/core/silence_detector.py`, line 136
**Severity:** MEDIUM — misleading no-op, vulture/linter flag

```python
def trim_silence(self, audio, sample_rate, threshold_db=SILENCE_THRESHOLD_DB, min_silence_sec=0.5):
    audio.shape   # ← standalone attribute read, result discarded (line 136)
    mono = self._to_mono(audio)
```

This finding was first documented as W1016 F1 (HIGH — now downgraded to MEDIUM as
functional correctness has been confirmed), confirmed in W1124, and again in W1324 R2.
W1333 did not remove it — W1333 only replaced `-40.0` literals, not dead code.

At runtime the statement has no effect: `.shape` is an attribute read with no side
effects on `np.ndarray`. The multichannel trim case works correctly because the final
slice on line 176 (`return audio[start_sample:end_sample]`) correctly handles both 1-D
and 2-D arrays. However, the expression:
- Misleads readers into thinking a shape assertion or reshape is performed.
- Will be flagged by vulture, pyflakes, and `ruff` as a dead expression.
- Signals intent (validate shape before mono conversion) that is never enforced.

**Fix:** Remove line 136. One-line change, no logic impact.

---

### N4 — LOW: `trim_silence` has an identical duplicated return at lines 176–178

**File:** `KrabEar/core/silence_detector.py`, lines 176–178
**Severity:** LOW — dead branch, misleading

```python
        if audio.ndim > 1:
            return audio[start_sample:end_sample]  # line 177
        return audio[start_sample:end_sample]       # line 178
```

Both branches return `audio[start_sample:end_sample]`. The `if audio.ndim > 1` check
produces no behavioral difference. Both `ndim=1` and `ndim>1` slicing of a 2-D ndarray
along axis-0 preserves all channels, so the result is identical. The conditional was
probably intended to either:
- Return a mono view for 1-D (already mono) or
- Return a 2-D slice for multichannel

...but both branches are the same code. The `if` branch is dead logic.

Compare: lines 159–161 in the all-silence path correctly distinguishes the two cases:
```python
if audio.ndim > 1:
    return np.zeros((0, audio.shape[1]), dtype=audio.dtype)  # preserves channel shape
return np.zeros(0, dtype=audio.dtype)                         # 1-D zero
```
The trim case should mirror this pattern.

**Fix:** Collapse to a single `return audio[start_sample:end_sample]`, or if channel
shape preservation matters, use:
```python
if audio.ndim > 1:
    return audio[start_sample:end_sample, :]
return audio[start_sample:end_sample]
```

---

### N5 — LOW: `test_shared_silence_threshold_W1333.py` scope gap — `audio_quality.py` absent from AST scan

**File:** `KrabEar/tests/test_shared_silence_threshold_W1333.py`
**Severity:** LOW — test coverage gap, allows silent regression

`_TARGET_FILES` in `TestAllModulesReferenceSharedConstant` enumerates exactly 6 files
(lines matching `_TARGET_FILES = [...]`). `audio_quality.py` is not in the list.

The test `test_all_modules_reference_shared_constant` checks that all 6 listed files
import `silence_constants`. Because `audio_quality.py` is excluded, its persistent
hardcoded `0.001` divergence (N1) passes the test suite without any failure.

Adding `audio_quality.py` to `_TARGET_FILES` would immediately surface N1 as a failing
test — making the gap self-enforcing.

**Fix:** Add `_KRAB_EAR / "core" / "audio_quality.py"` to `_TARGET_FILES` and add a
corresponding `test_no_legacy_0001_literal_in_audio_quality` test checking that
`0.001` does not appear as a standalone silence threshold constant (distinct from it
being used as a coefficient elsewhere).

---

## Summary Table

| # | Sev | Title | Prior Wave | Status |
|---|-----|-------|------------|--------|
| N1 | HIGH | `audio_quality._SILENCE_RMS_THRESHOLD = 0.001` (−60 dB), 10× divergence from SILENCE_THRESHOLD_AMP | W1324 R1 | **Unresolved** — W912/W1333 both missed it |
| N2 | MED | `noise_profiler` imports from `silence_detector` instead of `silence_constants` | W1132 (introduced) | **Unresolved** — stale import path |
| N3 | MED | Dead `audio.shape` expression in `trim_silence` line 136 | W1016 F1, W1324 R2 | **Unresolved** — W1018 not merged |
| N4 | LOW | Duplicate identical `return` branches in `trim_silence` lines 176–178 | New | **New finding** |
| N5 | LOW | `test_shared_silence_threshold_W1333.py` missing `audio_quality.py` in AST scope | New | **New finding** (test gap) |

---

## W1333 Divergence Fix Status

| Location | W1333 Fixed? | Current State |
|----------|-------------|---------------|
| `silence_detector.py` (×4 default args) | Yes | Import from `silence_constants` |
| `smart_silence_skipper.py` (`_DEFAULT_THRESHOLD_DB`) | Yes | Import from `silence_constants` |
| `realtime_silence_filter.py` (`_DEFAULT_THRESHOLD_DB`) | Yes | Import from `silence_constants` |
| `audio_chunker.py` (threshold_db default) | Yes | Import from `silence_constants` |
| `call_silence_probe.py` (×2 default args) | Yes | Import from `silence_constants` |
| `audio_analytics_service.py` (params.get fallback) | Yes | Import from `silence_constants` |
| `audio_quality.py` (`_SILENCE_RMS_THRESHOLD = 0.001`) | **No** | Still hardcoded 0.001 (−60 dB) |
| `noise_profiler.py` (`_SILENCE_RMS_THRESHOLD`) | Partial | Correct value, wrong import source |

10 of 11 divergent copies resolved. 1 remaining (audio_quality.py) — value-level divergence (different threshold, not just bare literal).

---

## W1016 Outstanding Findings Status

| W1016 Finding | Status Post-W1333 |
|---------------|-------------------|
| F1 — dead `audio.shape` in `trim_silence` | **Still unresolved** (W1018 not merged) → now N3 |
| F2 — fixed -40 dB misclassifies whisper | **Still unresolved** — separate architectural issue, not a constant |
| F3 — frame RMS triplicated across 3 methods | **Still unresolved** — no extraction attempted |
| F4 — no coordination with VAD | **Still unresolved** — architectural |
| F5 — `_FRAME_SIZE=512` coarse at 8 kHz | **Still unresolved** |
