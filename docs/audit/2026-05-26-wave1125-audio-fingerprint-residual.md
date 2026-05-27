# Audit: AudioFingerprinter Residual Issues — W1125

**Date:** 2026-05-26  
**Branch audited:** `codex/krab-ear-v2` (HEAD `6c900317`)  
**File:** `KrabEar/core/audio_fingerprint.py`  
**Scope:** Post-W1078 re-audit. Verify W1078 merge status; find NEW residual issues.

---

## W1078 Merge Status: NOT MERGED

Branch `fix/audio-fingerprint-W1078` (commit `13ed2b2a`) exists on `origin` but has **not been merged into `codex/krab-ear-v2`**.

Evidence:
```bash
# W1078 commit NOT found in codex/krab-ear-v2 log
git log codex/krab-ear-v2 --oneline | grep "1078"  # no output

# W1078 branch IS on origin
git log remotes/origin/fix/audio-fingerprint-W1078 --oneline | head -1
# 13ed2b2a fix(wave1078): audio_fingerprint restrict to exact-match ...
```

As a result, `codex/krab-ear-v2` currently retains the **broken `compare()` with Hamming distance on SHA-256**, the exact CRITICAL bug identified in W1063. The W1078 fix (`equals()` method, `DeprecationWarning` on `compare()`) is absent from the main branch.

---

## Findings (5 NEW, post-W1078 scope)

### F1 — CRITICAL: W1078 unmerged; broken `compare()` still live on main branch

**Severity:** CRITICAL (unblocks W1063 regression)  
**File:** `KrabEar/core/audio_fingerprint.py` (on `codex/krab-ear-v2`)  
**Lines:** 57–83 (the `compare()` method)

The `compare()` method on `codex/krab-ear-v2` computes Hamming distance between SHA-256 hex bytes. This is statistically meaningless: any two non-identical SHA-256 hashes differ in ~50% of bits (avalanche effect), so `compare()` returns values clustered around 0.5 rather than reflecting acoustic similarity. W1063 classified this as CRITICAL.

W1078 authored the fix (branch `fix/audio-fingerprint-W1078`):
- Added `equals()` method (exact-match only)
- Changed `compare()` to a deprecated shim returning `1.0 | 0.0`
- Updated `is_duplicate_audio()` to use `equals()`
- Added `DeprecationWarning` + logger warning on `compare()`

None of these changes reached `codex/krab-ear-v2`. The fix must be merged or cherry-picked.

**Active caller chain (live on `codex/krab-ear-v2`):**
```
IPC "check_audio_duplicate"
  → audio_analytics_service.py:221  similarity = self._audio_fingerprinter.compare(fp1, fp2)
  → audio_fingerprint.py:compare()  ← BROKEN Hamming on SHA-256
```

Result: `similarity` is always ~0.5 for non-identical recordings, `is_duplicate` threshold 0.95 is never reached for real duplicates (they get 0.5, not 1.0). Duplicate detection is silently broken.

**Fix:** Merge `fix/audio-fingerprint-W1078` into `codex/krab-ear-v2` via PR.

---

### F2 — HIGH: `audio_analytics_service.py` calls deprecated `compare()` — not updated to `equals()`

**Severity:** HIGH  
**File:** `KrabEar/backend/audio_analytics_service.py`, line 221  

Even after W1078 merges, `audio_analytics_service.py:handle_check_audio_duplicate` still calls `self._audio_fingerprinter.compare(fp1, fp2)` and returns the float `similarity` to IPC callers:

```python
# audio_analytics_service.py lines 219-229
fp1 = self._audio_fingerprinter.fingerprint(audio1, sample_rate)
fp2 = self._audio_fingerprinter.fingerprint(audio2, sample_rate)
similarity = self._audio_fingerprinter.compare(fp1, fp2)   # <-- deprecated shim after W1078
return {
    "fingerprint1": fp1,
    "fingerprint2": fp2,
    "similarity": round(similarity, 6),          # <-- returns 0.0 or 1.0 only
    "is_duplicate": similarity >= threshold,
}
```

After W1078 `compare()` becomes a shim returning only `{0.0, 1.0}`, the `similarity` field in IPC response will always be exactly `0.0` or `1.0`, making the float field semantically a bool. Callers relying on intermediate similarity values will get binary results without warning.

Additionally, `is_duplicate_audio()` on the fingerprinter itself (line 108) still delegates to `compare()` on `codex/krab-ear-v2`. After W1078 this path is fixed but `audio_analytics_service.py` bypasses it by calling `fingerprint()` + `compare()` directly.

**Fix:** Update `handle_check_audio_duplicate` to use `equals()` directly; rename `similarity` to `is_match` (bool) or document that it is binary post-W1078.

---

### F3 — MEDIUM: Quantization scale for `sc_std` is wrong for audio at sample rates > 16 kHz

**Severity:** MEDIUM  
**File:** `KrabEar/core/audio_fingerprint.py`, line 182  

```python
scales = [8000.0, 8000.0, 1.0, 1.0]  # sc_mean, sc_std, zcr_mean, zcr_std
```

The scale `8000.0` covers spectral centroid values for audio up to 16 kHz Nyquist (0–8000 Hz). For audio with `sample_rate > 16000` (e.g., 44100 Hz, 48000 Hz), the spectral centroid can legitimately exceed 8000 Hz. Empirical test:

```
15 kHz sine at 44.1 kHz SR → sc_mean = 14453 Hz → clipped to 8000 → quantized 65535/65535
```

Any audio above 8 kHz centroid at 44100 Hz yields the same quantized value (65535), causing false collisions between distinct high-frequency recordings.

The `sc_std` scale is similarly problematic: for broadband audio at 44100 Hz, sc_std can exceed 8000 Hz in pathological cases (though typical music/speech stays below this threshold).

Furthermore, the docstring on the class (line 31) says "квантуются в **8-битные** значения" but the actual code uses `uint16` (65535 range). This is a pre-existing inconsistency already present in both `codex/krab-ear-v2` and `fix/audio-fingerprint-W1078`.

**Fix (recommendation):** Make `scales` depend on `sample_rate` (pass `sample_rate` to `_hash_features`), using `sample_rate / 2.0` as the Nyquist limit for `sc_mean` and `sc_std` scales. Fix docstring to say 16-bit. W1078 fixes the docstring but does not fix the scale dependency.

---

### F4 — MEDIUM: IPC `check_audio_duplicate` has no file-path variant — PCM-buffer IPC is impractical for audio > ~1 minute

**Severity:** MEDIUM  
**File:** `KrabEar/backend/audio_analytics_service.py`, lines 196–229  

The `check_audio_duplicate` IPC method receives raw PCM samples as `list[float]` (JSON array). For audio typical of Krab Ear use cases:

| Duration | PCM float32 | JSON bytes |
|----------|-------------|------------|
| 1 min    | 3.7 MB      | ~11 MB     |
| 10 min   | 37 MB       | ~110 MB    |
| 1 hour   | 220 MB      | ~660 MB    |

Passing hour-long recordings through the Unix socket is impractical. The IPC message size limit (`MAX_MESSAGE_BYTES` in `backend/ipc_constants.py`) would need verification, but at minimum this creates a usability gap: the primary use case for duplicate detection (checking if a 30-min meeting recording was re-imported) cannot be served via this API.

The fingerprinter itself processes 1-hour audio in ~1.2 seconds (measured), so compute is not the bottleneck — it is IPC serialization.

**Fix (recommendation):** Add a `check_audio_duplicate_by_path` IPC variant that accepts file paths and uses `AudioConverter` to load + fingerprint server-side, bypassing the PCM transport bottleneck.

---

### F5 — LOW: `_to_mono_float32` amplitude normalization breaks fingerprint invariance across recordings of identical content

**Severity:** LOW  
**File:** `KrabEar/core/audio_fingerprint.py`, lines 118–129  

```python
peak = np.max(np.abs(arr))
if peak > 1e-7:
    arr = arr / peak
```

This peak normalization is intended to make the fingerprint invariant to recording volume. However, it introduces a subtle edge case: if two recordings of identical content have different peak amplitude due to leading/trailing silence or noise floor differences, normalization divides by different values, potentially shifting quantized feature values.

Empirically, amplitude invariance does hold for pure audio (tested: 2× gain → identical fingerprint). But for recordings where the loudest frame is noise rather than signal (e.g., mic pops, clipping artifacts), the normalization factor changes, and so does the fingerprint.

More importantly: the docstring (`_to_mono_float32`) does not mention that this normalization makes it **impossible** to use the fingerprinter to detect gain-normalized vs non-normalized versions of the same recording stored at different levels — which is exactly the scenario in import pipelines where `GainNormalizer` may have been applied to one copy but not the other. In that case, two acoustically identical recordings will correctly produce the same fingerprint (since both get normalized to peak=1.0), but this behavior is undocumented.

**MFCC-based perceptual hash (future option):** For fuzzy/near-duplicate detection (not the current exact-match use case), an MFCC-based perceptual fingerprint would be more appropriate. This would compute 13–20 MFCC coefficients per frame, quantize them to a bit-array, and compare via Hamming distance on the bit-array (not SHA-256). This is the approach used by tools like `chromaprint` (Acoustid) and is robust to minor transcoding and 1ms temporal shifts. However, implementing this adds a dependency on `librosa` or `scipy.signal`, and it changes the semantic from "exact duplicate" to "perceptually similar" — which must be documented clearly to avoid re-introducing the W1063 confusion. This remains a FUTURE improvement; the current exact-match semantic is correct given the SHA-256 architecture.

---

## Summary Table

| ID | Severity | Description | File | Action |
|----|----------|-------------|------|--------|
| F1 | CRITICAL | W1078 NOT merged into `codex/krab-ear-v2`; `compare()` Hamming still live | `audio_fingerprint.py` | Merge W1078 PR |
| F2 | HIGH | `audio_analytics_service.py` calls deprecated `compare()`, not updated to `equals()` | `audio_analytics_service.py:221` | Update to `equals()` post-W1078 |
| F3 | MEDIUM | `sc_mean/sc_std` scale hardcoded to 8000 Hz; wrong for SR > 16 kHz; docstring says 8-bit but uses uint16 | `audio_fingerprint.py:182,31` | Parametrize scale by Nyquist; fix docstring |
| F4 | MEDIUM | `check_audio_duplicate` IPC requires raw PCM — impractical for >1 min recordings (~11 MB/min JSON) | `audio_analytics_service.py:196` | Add `check_audio_duplicate_by_path` variant |
| F5 | LOW | Peak-normalization side effect undocumented; MFCC perceptual hash not supported (future) | `audio_fingerprint.py:118` | Add docstring note; defer MFCC |

---

## Temporal Sensitivity Measurement

Empirical test on `codex/krab-ear-v2`:

| Perturbation | Same fingerprint? | Note |
|---|---|---|
| 1 ms time shift (16 samples at 16 kHz) | **No** | Frame-alignment sensitive |
| 512-sample shift (exact window boundary) | **No** | Feature averages differ |
| 2× amplitude scaling | **Yes** | Peak normalization works |
| Same PCM, declared SR=8k vs SR=16k | **No** | `freqs` array changes |
| Resampled audio (44k→16k decimated) | **No** | Expected; different samples |
| 1-hour audio processing time | 1.18 s | O(N) frames loop, acceptable |

Key conclusion: the fingerprinter is **frame-aligned**, not time-invariant. A 1-sample offset changes all frame boundaries → different centroids → different hash. This is expected behavior for an exact-duplicate detector (same bytes = same result), but it must be documented clearly so callers understand that re-encoded or time-trimmed recordings will never match, even if the content is perceptually identical.
