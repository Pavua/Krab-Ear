# Wave 885 — Audio Utils Audit

**Date:** 2026-05-26  
**Scope:** `core/audio_chunker.py`, `core/audio_converter.py`, `core/audio_quality.py`, `core/audio_denoiser.py`, `core/silence_detector.py`  
**Focus:** ffmpeg invocation safety, silence threshold accuracy, denoising side-effects, file leak risk

---

## Summary

| # | Severity | File | Finding |
|---|----------|------|---------|
| 1 | MEDIUM | `audio_converter.py` | Temp file leaked on `OSError` before `subprocess.run` |
| 2 | MEDIUM | `audio_converter.py` | No timeout on `subprocess.run` — hung ffmpeg blocks caller thread indefinitely |
| 3 | LOW | `audio_converter.py` | `get_audio_info` crashes with `AttributeError` when `sf is None` |
| 4 | LOW | `audio_converter.py` | `output_format` not sanitised — path traversal risk if caller passes `../../foo` |
| 5 | LOW | `audio_denoiser.py` | Multichannel input silently downmixed to mono; original shape not restored |
| 6 | LOW | `audio_denoiser.py` | Noise floor always uses first 200 ms — wrong when recording starts mid-speech |
| 7 | LOW | `silence_detector.py` | `trim_silence` contains dead statement `audio.shape` (no-op, line 128) |
| 8 | LOW | `silence_detector.py` | dB→amplitude conversion uses amplitude formula for power quantities (20 log vs 10 log) — likely correct for RMS, but inconsistent with `audio_quality.py` which computes RMS directly |
| 9 | INFO | `audio_chunker.py` | `_compute_split_points` skips silence regions whose `start_sec < cursor` but does not advance the `usable_silences` list — O(n²) on long recordings with many silences |
| 10 | INFO | `audio_quality.py` | `_error_bus` attribute injected externally; no type annotation or setter — fragile duck-typing |

---

## Detailed Findings

### F1 — Temp file leaked on OSError (MEDIUM)
**File:** `audio_converter.py`, lines 151–172

```python
handle = tempfile.NamedTemporaryFile(prefix="krab_ear_conv_", suffix=f".{fmt}", delete=False)
handle.close()
dst = handle.name
...
try:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
except (FileNotFoundError, OSError) as exc:
    Path(dst).unlink(missing_ok=True)   # ← OK, cleaned up here
```

The cleanup on `OSError` covers the `subprocess.run` call. However, if an unexpected exception (e.g. `MemoryError`, `KeyboardInterrupt`) fires between the `NamedTemporaryFile` creation and the `try` block, `dst` is never deleted. Pattern is safe for the explicit caught exceptions but not for the general case.

**Recommendation:** wrap temp-file creation and ffmpeg call in a single `try/finally`:

```python
handle = tempfile.NamedTemporaryFile(...)
handle.close()
dst = handle.name
try:
    result = subprocess.run(...)
    if result.returncode != 0:
        raise RuntimeError(...)
    return dst
except Exception:
    Path(dst).unlink(missing_ok=True)
    raise
```

---

### F2 — No subprocess timeout (MEDIUM)
**File:** `audio_converter.py`, line 168

```python
result = subprocess.run(cmd, capture_output=True, text=True, check=False)
```

No `timeout=` argument. If ffmpeg hangs (corrupted input, pipe stall), the call blocks the IPC handler thread indefinitely. The import audio path uses this converter (service.py import flow).

**Recommendation:** add `timeout=300` (5 min) covering worst-case large files, and catch `subprocess.TimeoutExpired` to clean up the temp file.

---

### F3 — `get_audio_info` crashes when soundfile unavailable (LOW)
**File:** `audio_converter.py`, lines 83–110

```python
# Top of file
try:
    import soundfile as sf
except Exception:
    sf = None

def get_audio_info(self, path: str) -> AudioInfo:
    ...
    info = sf.info(str(p))   # AttributeError: 'NoneType' has no attribute 'info'
```

The module gracefully sets `sf = None` when soundfile is absent but `get_audio_info` does not guard against `sf is None`, raising `AttributeError` instead of a clear `RuntimeError`.

**Recommendation:** add guard at entry of `get_audio_info`:

```python
if sf is None:
    raise RuntimeError("soundfile не установлен — get_audio_info недоступен")
```

---

### F4 — `output_format` not sanitised (LOW)
**File:** `audio_converter.py`, lines 149–157

```python
fmt = output_format.lower().lstrip(".")
...
suffix=f".{fmt}"
dst = handle.name   # e.g. /tmp/krab_ear_conv_XXXX.../../etc/passwd
```

`tempfile.NamedTemporaryFile` appends the suffix literally. A suffix containing `/` would truncate at the tempdir boundary on most systems, but a carefully crafted `output_format` like `wav/../../etc/cron.d/evil` could write to an unintended path if `output_path` is provided instead of temp.

**Recommendation:** validate `output_format` against an allowlist (`{"wav", "mp3", "ogg", "flac", "m4a"}`) before use.

---

### F5 — Multichannel audio silently downmixed; original shape not restored (LOW)
**File:** `audio_denoiser.py`, lines 96–118

```python
mono = audio
if audio.ndim > 1:
    mono = audio.mean(axis=1)   # (samples,) — original channels discarded
...
return denoised.astype(audio.dtype)   # comment says "остаётся моно" — CORRECT
```

The docstring at line 82 says "Многоканальное аудио автоматически усредняется в моно" and the inline comment at line 117 is accurate. However, callers in `engine.py` may be unaware of this shape change and downstream code may assume the original channel count is preserved. If the caller passed stereo `(N, 2)` and receives `(N,)` back, that could cause shape mismatches in numpy slicing.

**Recommendation:** document this explicitly in the return-type docstring and add an assertion or warning log when input is multichannel.

---

### F6 — Noise floor estimate assumes silent start (LOW)
**File:** `audio_denoiser.py`, lines 133–134, 175–176

Both `noisereduce` and spectral-gating backends take:

```python
noise_clip = audio[:_NOISE_FLOOR_SAMPLES]   # first ~200 ms @ 16 kHz
```

If the recording begins mid-sentence (e.g., hotkey pressed while speaking), the "noise" estimate contains speech energy, causing the denoiser to suppress the actual signal. `_NOISE_FLOOR_SAMPLES = 3200` at 16 kHz = exactly 200 ms — common microphone on-time is 50–100 ms.

**Recommendation:** either expose a `noise_ref` parameter for callers to pass an explicit background sample, or select the quietest 200 ms window from the full recording rather than always the first 200 ms.

---

### F7 — Dead statement in `trim_silence` (LOW)
**File:** `silence_detector.py`, line 128

```python
def trim_silence(self, audio: np.ndarray, ...):
    audio.shape          # ← dead expression, result discarded
    mono = self._to_mono(audio)
```

`audio.shape` is evaluated but the tuple is discarded. This is a leftover debug statement. It has no runtime effect but constitutes dead code and could confuse readers into thinking a validation is happening.

**Recommendation:** remove line 128 (`audio.shape`).

---

### F8 — dB→amplitude formula consistency note (LOW)
**File:** `silence_detector.py`, line 23

```python
def _db_to_amplitude(db: float) -> float:
    return 10.0 ** (db / 20.0)
```

This is the correct formula for converting dB (referenced to amplitude) to linear amplitude. The function name is accurate. However, `audio_quality.py` uses `_SILENCE_RMS_THRESHOLD = 0.001` directly as a linear amplitude without going through dB conversion, making the two silence-detection code paths use different threshold representations. The default `-40 dB` in `SilenceDetector` maps to `0.01` amplitude, while `audio_quality.py`'s `_SILENCE_RMS_THRESHOLD = 0.001` is equivalent to `-60 dB` — a 20 dB discrepancy between the two analyzers' definitions of "silence".

**Recommendation:** align the silence threshold constants. Either derive `_SILENCE_RMS_THRESHOLD` in `audio_quality.py` from a dB constant, or document the intentional difference.

---

### F9 — O(n²) silence scan in `_compute_split_points` (INFO)
**File:** `audio_chunker.py`, lines 279–292

```python
for region in usable_silences:
    if region.start_sec < cursor:
        continue           # ← skipped but not removed from list
    ...
```

On each iteration of the outer `while` loop, the inner `for` loop re-scans all `usable_silences` from the beginning, skipping regions before `cursor`. For a 1-hour recording with many silence regions this is O(n²). In practice the list is rarely large enough to matter, but an index pointer or slicing would make this O(n).

**Recommendation:** pass a mutable index into the silence list, or use `bisect` to find the first region after `cursor`.

---

### F10 — `_error_bus` duck-type injection in `AudioQualityAnalyzer` (INFO)
**File:** `audio_quality.py`, lines 78–98

```python
_error_bus = getattr(self, "_error_bus", None)
```

`_error_bus` is expected to be set on the instance externally (by `BackendService` presumably), but there is no `__init__` that declares the attribute, no type annotation, and no setter method. This is fragile — a `hasattr` check hides the absence; mypy cannot track it.

**Recommendation:** add a typed `__init__` that accepts an optional `error_bus` parameter:

```python
def __init__(self, error_bus=None) -> None:
    self._error_bus = error_bus
```

---

## Risk Matrix

| Finding | Impact | Effort to fix |
|---------|--------|---------------|
| F1 – temp file leak | File system accumulation under load | Low |
| F2 – no subprocess timeout | Thread starvation / IPC hang | Low |
| F3 – sf=None crash | AttributeError shown to user | Trivial |
| F4 – format not sanitised | Path traversal if output_path used | Low |
| F5 – shape change | Downstream shape mismatch | Low (doc) |
| F6 – wrong noise ref | Degraded STT quality on hot-start | Medium |
| F7 – dead statement | Code clarity | Trivial |
| F8 – threshold inconsistency | Different silence sensitivity across modules | Low |
| F9 – O(n²) scan | CPU spike on long recordings | Low |
| F10 – duck-type error_bus | Type-safety, mypy blind spot | Low |

---

## Files Audited

- `KrabEar/core/audio_chunker.py` — 338 lines
- `KrabEar/core/audio_converter.py` — 180 lines
- `KrabEar/core/audio_quality.py` — 251 lines
- `KrabEar/core/audio_denoiser.py` — 204 lines
- `KrabEar/core/silence_detector.py` — 256 lines

Total: 1 229 lines audited. No external service calls required. No secrets found.
