# Audit: SilenceDetector (Wave 1016)

**Date:** 2026-05-26  
**Auditor:** W1016 (sub-agent)  
**Scope:** `KrabEar/core/silence_detector.py`, interaction with `VoiceActivityDetector`, `SmartSilenceSkipper`, `RealtimeSilenceFilter`, `AudioChunker`  
**Prerequisite:** W912 already unified threshold to `-40 dB` across `SilenceDetector` and `AudioQualityAnalyzer`. This audit covers residual issues only.

---

## Summary

5 findings (2 HIGH, 2 MEDIUM, 1 LOW). No critical safety regressions. Core algorithm is pure energy/RMS-based — no spectral or VAD integration. The main actionable items are: a dead statement that silently prevents multichannel trim from working correctly, a fixed threshold that misclassifies whispered speech, and tripled frame computation across the three public methods.

---

## F1 — HIGH: Dead `audio.shape` statement in `trim_silence` silently discards result

**File:** `KrabEar/core/silence_detector.py`, line 128  
**Severity:** HIGH (functional bug, silent)

```python
def trim_silence(self, audio, sample_rate, threshold_db=-40.0, min_silence_sec=0.5):
    audio.shape   # ← standalone attribute access, result discarded
    mono = self._to_mono(audio)
```

`audio.shape` returns a tuple but is immediately discarded. The line is a leftover from a deleted shape-assertion or reshape call (likely `audio = audio.reshape(-1)` or `assert audio.ndim == 1`). It has no effect at runtime, but misleads readers into thinking a shape check is performed. The *intended* guard is absent:

- If `audio` has shape `(N, C)` with C > 1 (stereo), `_to_mono` correctly produces a mono array and `trim_silence` then slices `audio[start_sample:end_sample]` — which preserves the channel dimension. The final slice at lines 169-170 correctly returns `audio[start_sample:end_sample]` regardless of ndim (same code path for both branches), so stereo trimming functionally works.
- However the dead line costs nothing to remove and creates a false sense of intent. It should be deleted.

**Recommendation:** Remove `audio.shape` on line 128. Add an `ndim` comment near the slice if intent needs documenting.

---

## F2 — HIGH: Default `-40 dB` threshold misclassifies whispered speech as silence

**File:** `KrabEar/core/silence_detector.py`, lines 50–51, 114–115, 176–177  
**Severity:** HIGH (functional, affects real transcription quality)

Empirical measurement:

| Signal level | RMS (dB) | Classified at -40 dB threshold |
|---|---|---|
| Normal speech (0.5 amplitude) | -6 dB | Speech |
| Quiet speech (0.1 amplitude) | -20 dB | Speech |
| Very quiet speech (0.003 amplitude) | ~-50 dB | **Silence** |
| Whisper (real-world typical: -45 to -55 dB) | -45 to -55 dB | **Silence** |

Whispered Russian or Spanish speech, which Krab Ear specifically targets, routinely falls between -45 dB and -55 dB normalized RMS. With the fixed `-40 dB` default:

1. `SmartSilenceSkipper` will remove whispered passages from audio before STT — they are skipped, not transcribed.
2. `RealtimeSilenceFilter` will suppress `realtime.partial_transcript` events during whisper, creating blank real-time feedback.
3. `trim_silence` will crop leading/trailing whisper from recordings.

The `VoiceActivityDetector` in `KrabEar/core/vad.py` avoids this problem entirely via its adaptive threshold (percentile-based noise floor + `margin_db`), but `SilenceDetector` is never consulted by `VoiceActivityDetector` and vice versa — they operate independently. `SmartSilenceSkipper` uses only `SilenceDetector`, not `VAD`.

**Recommendation:** Lower the default to `-50 dB` or `-55 dB` to avoid clipping whispered speech. Alternatively, expose a `SMART_SILENCE_SKIP_THRESHOLD_DB` runtime setting (analogous to `SMART_SILENCE_SKIP_ENABLED`) so users can tune it. The VAD's adaptive approach is the long-term superior solution for SmartSilenceSkipper.

---

## F3 — MEDIUM: Frame RMS computation triplicated across three public methods

**File:** `KrabEar/core/silence_detector.py`  
**Severity:** MEDIUM (performance, maintainability)

All three public methods (`detect_silence`, `trim_silence`, `get_speech_ratio`) contain identical frame-splitting and RMS computation code:

```python
n_frames = max(n_samples // _FRAME_SIZE, 1)
frames = np.array_split(audio, n_frames)
frame_rms = np.array([
    float(np.sqrt(np.mean(f.astype(np.float64) ** 2))) if len(f) > 0 else 0.0
    for f in frames
])
```

This is ~15 lines duplicated 3×. When `analyze_silence_file` calls both `detect_silence` and `get_speech_ratio` (lines 241–242), the same audio is split and RMS-computed twice. For a 5-minute recording at 16 kHz:

- Single method: ~24 ms
- All three methods combined: ~69 ms (3× linear, not amortized)

`AudioChunker` (which also uses `SilenceDetector`) calls `detect_silence` internally and then processes regions separately, so the duplication also propagates to callers.

**Recommendation:** Extract a private `_compute_frame_rms(audio, threshold_amp) -> tuple[np.ndarray, np.ndarray]` helper returning `(frame_rms, is_silent)`. The three public methods become thin wrappers around it. Matches the pattern already used by `VoiceActivityDetector._compute_frame_rms`.

---

## F4 — MEDIUM: No coordination with VAD — `SmartSilenceSkipper` and `RealtimeSilenceFilter` use energy-only detector while VAD uses adaptive threshold

**File:** `KrabEar/core/smart_silence_skipper.py`, `KrabEar/backend/realtime_silence_filter.py`  
**Severity:** MEDIUM (architectural, affects accuracy in noisy environments)

`SilenceDetector` uses a fixed-amplitude threshold converted from `threshold_db`. `VoiceActivityDetector` uses an adaptive threshold: it computes the noise floor from the quietest `quiet_percentile` percent of frames, then adds `margin_db`. This makes VAD robust to background noise — in a quiet room it sets a lower threshold; in a noisy room it raises it.

`SmartSilenceSkipper` uses `SilenceDetector` with the fixed `-40 dB` default. In a noisy recording environment (e.g., café background at -35 dB), the fixed threshold will keep all background noise as "speech", never removing any pauses, even if whispered speech is buried at -50 dB.

`RealtimeSilenceFilter` has the same dependency. It calls `detect_silence` with a configurable threshold but defaults to whatever the caller passes (the default chain ultimately resolves to -40 dB).

The two systems were built independently and have never been reconciled. There is no shared "silence oracle" — each consumer reimplements or wraps a different detection strategy.

**Recommendation (short-term):** Pass `threshold_db` through `SmartSilenceSkipper.__init__` as a runtime-configurable setting (it already has a parameter, but it is not wired to any IPC setting). **Long-term:** Replace `SilenceDetector` usage in `SmartSilenceSkipper` with `VoiceActivityDetector` — its adaptive threshold handles variable noise floors correctly.

---

## F5 — LOW: `_FRAME_SIZE = 512` gives coarse 64 ms resolution at 8 kHz phone audio

**File:** `KrabEar/core/silence_detector.py`, line 19  
**Severity:** LOW (accuracy at non-standard sample rates)

`_FRAME_SIZE = 512` is a fixed constant. At 16 kHz (standard for Whisper), this gives 32 ms resolution — acceptable. At 8 kHz (telephony audio from call recordings), resolution degrades to 64 ms per frame. Short silences (e.g., speaker turn gaps of 50–100 ms in phone calls) will not be detected at all.

Krab Ear supports call recording via `CallAutomationController`. If telephony audio arrives at 8 kHz, `SilenceDetector` will systematically miss short pauses, affecting both `SmartSilenceSkipper` compression ratios and `CallSilenceProbe` integration (though `CallSilenceProbe` in `backend/call_silence_probe.py` implements its own detection independently).

**Recommendation:** Compute `frame_size = min(512, sample_rate // 50)` at call time to cap frame duration at 20 ms regardless of sample rate. This is already the pattern used by `VoiceActivityDetector` (it accepts `frame_ms` as a parameter).

---

## Test Coverage Assessment

Test file: `KrabEar/tests/test_silence_detector.py`

Coverage is solid for the standard path:
- Pure silence, pure speech, mixed segments
- Leading/trailing silence detection and trimming
- Multichannel (stereo) input
- Custom threshold sensitivity
- Thread safety (concurrent `detect_silence` from 10 threads)
- Edge cases: zero-length audio, zero sample rate, all-loud signal

**Missing tests:**
- No test for the whisper-as-silence misclassification (F2): a signal at -45 dB being skipped by `SmartSilenceSkipper`
- No benchmark/regression test for the tripled RMS computation (F3)
- No test for 8 kHz input resolution (F5)
- No integration test of `SilenceDetector` + `SmartSilenceSkipper` round-trip (currently `test_smart_silence_skipper.py` exists but tests `SmartSilenceSkipper` in isolation)

---

## Wire Status

| Consumer | Wired | Method used | Notes |
|---|---|---|---|
| `SmartSilenceSkipper` | Yes | `detect_silence` | Fixed -40 dB threshold |
| `RealtimeSilenceFilter` | Yes | `detect_silence` | Configurable threshold |
| `AudioChunker` | Yes | `detect_silence` | Chunked STT splitting |
| `AudioAnalyticsService` | Yes | `analyze_silence_file` | Analytics endpoint |
| `VAD` | No (independent) | — | Separate adaptive algorithm |
| `CallSilenceProbe` | No (independent) | — | Own energy detection |
| `engine.py` | No | — | Uses VAD + AudioChunker, not SilenceDetector directly |

`SilenceDetector` is correctly wired in all 4 consumer paths. There are no orphan import or dead-wire issues.

---

## Clipping Safety

Verified: clipped audio (amplitude = 1.0 full scale) always reads as NOT silence regardless of threshold (tested up to -40 dB). RMS of a clipped signal is approximately -3 dB, well above any reasonable threshold. F1 does not introduce a regression here.
