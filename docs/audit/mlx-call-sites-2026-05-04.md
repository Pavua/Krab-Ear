# MLX Call Sites Audit (2026-05-04)

## Summary

- **Total MLX inference call sites**: 8
- **Already wrapped (in `with mlx_lock()`)**: 7
- **Unwrapped — fixed in this PR**: 1 (`scripts/debug_whisper.py`)
- **In subprocess / separate GPU context**: 0 (gigaam uses PyTorch+MPS, not MLX)

## Detail

| File:line | Function | Has mlx_lock? | Notes |
|---|---|---|---|
| `core/engine.py:1756` | `_transcribe_model` | ✅ yes | Entire for-loop inside `with mlx_lock():` at line 1756; wraps both watchdog path and direct path. Wrapped 2026-04-19 (PR #71). |
| `core/engine.py:1764` | `_transcribe_model` (watchdog lambda) | ✅ yes | Lambda passed to `MLXWatchdog.run_with_timeout()` executes inside same `with mlx_lock():` block. |
| `core/engine.py:1769` | `_transcribe_model` (direct path) | ✅ yes | Same `with mlx_lock():` block as above. |
| `core/audio_lang_id.py:220` | `_detect_with_mlx` — `load_model` | ✅ yes | Called only from `_run_detect()` which acquires `with mlx_lock():` at line 203 before dispatching to `_detect_with_mlx`. |
| `core/audio_lang_id.py:246` | `_detect_with_mlx` — `log_mel_spectrogram` | ✅ yes | Same caller lock as above. |
| `core/audio_lang_id.py:256` | `_detect_with_mlx` — `detect_language` | ✅ yes | Same caller lock as above. |
| `scripts/debug_whisper.py:16` | `test_whisper_direct` — path variant | 🔴 → ✅ fixed | Debug script; no lock. Fixed in this PR: wrapped in `with mlx_lock():`. |
| `scripts/debug_whisper.py:24` | `test_whisper_direct` — numpy variant | 🔴 → ✅ fixed | Same function as above; fixed together. |

## Non-MLX STT adapters (do NOT need mlx_lock)

These adapters use PyTorch + MPS (Apple Silicon GPU via Metal but through PyTorch, not libmlx.dylib):

| File | Adapter | Backend | Notes |
|---|---|---|---|
| `core/pipeline/stt_gigaam.py` | GigaAM | PyTorch+MPS | Explicitly documented: "mlx_lock НЕ требуется" |
| `core/engine.py:1508` | SenseVoice | PyTorch+MPS | Documented: "mlx_lock НЕ нужен" |
| `core/engine.py:2228` | Parakeet / WhisperX | PyTorch+MPS | Documented: "mlx_lock НЕ нужен" |

## Subprocess call sites

None. GigaAM worker (`gigaam_worker.py`, referenced in `core/pipeline/stt_gigaam.py`) runs in a **subprocess** with its own GPU context — cross-process coordination is out of scope for the in-process RLock.

## Action items completed

- [x] Wrapped `scripts/debug_whisper.py:16,24` in `with mlx_lock():` (both path and numpy variants)
- [x] Regression test added: `KrabEar/tests/test_mlx_concurrency.py`

## References

- Original crash: `~/Library/Logs/DiagnosticReports/Python-2026-04-19-213636.ips`
- Fix PR: #71 (2026-04-19) — wrapped `_transcribe_model` in `core/engine.py`
- `audio_lang_id.py` already wrapped at creation (post-PR #71 policy)
- This audit: Phase C C.3 (2026-05-04)
