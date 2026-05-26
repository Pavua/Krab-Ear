# W1205 — Security Audit: OpenWakeWordAdapter

**Date:** 2026-05-26  
**Branch:** audit/openwakeword-security-W1205  
**File audited:** `KrabEar/backend/openwakeword_adapter.py`  
**Related:** `KrabEar/backend/service.py` (wiring, lines 485, 1178-1181)

---

## Summary

`OpenWakeWordAdapter` wraps the Apache-2.0 openWakeWord library for background wake-word
detection. It is instantiated unconditionally in `BackendService.__init__` (line 485) and
exposed via four IPC handlers. The adapter operates a background thread that holds an open
`sounddevice.InputStream` -- meaning any bug that allows it to start incorrectly constitutes
a covert microphone tap.

Five findings were identified: two HIGH, two MEDIUM, one LOW.

---

## Findings

### F1 - HIGH: Threshold Not Validated (Covert Continuous Microphone Tap)

**Location:** `handle_wake_word_start` (line 209-210), `start()` (line 105)

```python
threshold = float(params.get("threshold", 0.5))
# No clamp, no bounds check
self.start(model_name, _on_detected, threshold=threshold)
```

**Impact:** Any IPC caller (including a malicious local process that can reach the Unix
socket) may pass `threshold=0.0` or a negative float. Because the detection loop fires
`on_detected(name, score)` whenever `score >= threshold` (line 317), a threshold of `0.0`
causes the callback to fire on every 80 ms audio chunk -- keeping the microphone stream open
and emitting detections continuously. There is no upper-bound check either; `threshold=2.0`
silently disables all detection with no user feedback.

**Fix:** Clamp and validate in `handle_wake_word_start` and `start()`:

```python
threshold = max(0.01, min(1.0, float(params.get("threshold", 0.5))))
```

---

### F2 - HIGH: No Privacy-Mode Gate on Microphone Activation

**Location:** `handle_wake_word_start` (line 204), `service.py` wiring (line 1178-1181)

**Impact:** The backend honours `privacy_mode_enabled` in `translation_service.py` (lines 96,
201) and `observability.py` (line 122) to suppress outbound data. However,
`handle_wake_word_start` has no corresponding gate. An IPC call `wake_word_start` succeeds
regardless of whether `privacy_mode_enabled=True` is set in user settings. The `_listen_loop`
then opens an `sd.InputStream` and continuously reads from the microphone without any
privacy check.

**Fix:** Read the privacy setting from `SettingsService` before starting:

```python
settings = self._settings_svc.get_settings()  # inject SettingsService
if settings.get("privacy_mode_enabled"):
    return {"ok": False, "error": "Wake word disabled in privacy mode"}
```

---

### F3 - MEDIUM: Custom Model Files Not Hash-Verified (Arbitrary ONNX/TFLite Load)

**Location:** `_load_model` (lines 263-273), `list_models` (lines 84-94)

```python
return OWWModel(wakeword_models=[model_path])  # model_path is caller-supplied
```

**Impact:** ONNX Runtime and TFLite model loading executes embedded operators and custom ops
at load time. A malicious `.onnx` or `.tflite` file placed in the `{data_dir}/wake_word_models/`
directory is loaded without any SHA-256 checksum or signature check.

Additionally, `list_models` calls `f.is_file()` which returns `True` for symlinks pointing
to files. An attacker with write access to `wake_word_models/` can place a symlink pointing
to an arbitrary `.onnx` outside `data_dir`; `_load_model` will load it without any path
containment check.

**Fix:**
1. Reject symlinks in `list_models` and `_resolve_model_path` (`f.is_file() and not f.is_symlink()`).
2. Maintain an optional allow-list of SHA-256 digests for known-good custom models.

---

### F4 - MEDIUM: Built-in Model Download Has No Integrity Check

**Location:** `_load_model` (line 273), comment line 272

```python
# Built-in model -- openWakeWord downloads on first run
return OWWModel(wakeword_models=[model_name])
```

**Impact:** When a built-in model name such as `"alexa"` or `"hey_jarvis"` is requested and
not already cached locally, the openWakeWord library downloads it at runtime from a remote
URL (GitHub Releases / HuggingFace Hub) with no hash verification at the adapter layer.
A MITM or a compromised CDN could serve a malicious ONNX binary. The call happens inside
the `_lock`-held `start()` path, blocking the IPC response thread during the download with
no timeout.

**Fix:** Pin built-in models to known SHA-256 hashes. Verify the downloaded file against the
hash before passing to `OWWModel`. Log a security warning and return an error if the hash
does not match.

---

### F5 - LOW: No Debounce After Detection (Rapid-Fire False Positives)

**Location:** `_listen_loop` (lines 315-318)

```python
prediction = oww.predict(flat)
for mdl_name, score in prediction.items():
    if score >= threshold and self._on_detected is not None:
        self._on_detected(mdl_name, float(score))
```

**Impact:** There is no per-model cooldown or debounce window. With a 1280-sample chunk at
16 kHz this is approximately 80 ms per iteration. A sustained noise burst or adversarial
audio (e.g. rogue Bluetooth speaker) that repeatedly scores above threshold will fire
`on_detected` on every frame. In a future integration where `on_detected` triggers
`start_recording`, this would produce hundreds of stacked recording jobs per second.

The current IPC `handle_wake_word_start` callback only logs (line 213-215), so the immediate
production impact is limited to log spam. However, because adding a recording trigger is a
one-line change, this is a latent issue.

**Fix:** Track `_last_trigger_ts: dict[str, float]` per model; enforce a minimum inter-trigger
interval (e.g. 2 s):

```python
import time
now = time.monotonic()
if now - self._last_trigger_ts.get(mdl_name, 0) < self._cooldown_sec:
    continue
self._last_trigger_ts[mdl_name] = now
self._on_detected(mdl_name, float(score))
```

---

## Out of Scope / Not Found

- **Serialization format safety:** openWakeWord uses ONNX Runtime and TFLite, not Python
  pickle. No arbitrary deserialization execution path found in the adapter.
- **Thread safety:** The `_lock` (`threading.Lock`) correctly serializes `start()`/`stop()`
  vs `_listen_loop` reads of `self._oww`. No race found.
- **HotkeyDoubleTapDetector race:** The Swift-side `HotkeyDoubleTapDetector` and
  `WakeWordListener` (Porcupine-based) are independent of `OpenWakeWordAdapter`. The Python
  adapter is not connected to the Swift double-tap path; no shared state race exists.
- **Settings hot-reload:** `OpenWakeWordAdapter` reads `threshold` only at `start()` time.
  Settings changes after start do not affect the running listener (no hot-reload), but this
  is expected behaviour given the current architecture.
- **Missing model file error handling:** `_resolve_model_path` raises `ValueError` for unknown
  names, propagated correctly through `start()`. `handle_wake_word_start` catches it and
  returns `{"ok": False, "error": ...}`. Error handling is adequate.

---

## Severity Matrix

| ID | Severity | Description |
|----|----------|-------------|
| F1 | HIGH     | Threshold=0 opens covert continuous microphone tap |
| F2 | HIGH     | Privacy mode not respected by wake word adapter |
| F3 | MEDIUM   | Symlink / unsigned custom ONNX model loaded without path containment or hash check |
| F4 | MEDIUM   | Built-in model download has no hash verification at adapter layer |
| F5 | LOW      | No per-model debounce -- rapid-fire false positives possible |
