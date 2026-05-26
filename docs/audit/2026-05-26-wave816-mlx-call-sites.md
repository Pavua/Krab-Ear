# MLX Call Sites Audit — Wave 816 (2026-05-26)

Follow-up to [Phase C audit (2026-05-04)](mlx-call-sites-2026-05-04.md).

## Summary

| Metric | Value |
|--------|-------|
| Total MLX inference / GPU call sites (production) | 13 |
| Fully locked (in `with mlx_lock():` or called only under lock) | 11 |
| Unlocked — SIGSEGV risk | **2** |
| `mx.clear_cache()` sites (intentionally outside lock) | 2 |
| Script sites (debug_whisper.py) | 2 — both locked |

## Unlocked Sites — Risk Assessment

### 1. `core/pipeline/stt_parakeet.py:128` — HIGH RISK

```python
# transcribe() method — lazy model load on first inference call
self._model = parakeet_mlx.from_pretrained(self._model_path)  # NOT in mlx_lock()
```

**Risk**: `parakeet_mlx.from_pretrained()` loads model weights into Metal GPU buffers via
MLX. The module docstring at line 11–13 explicitly states "parakeet-mlx uses MLX for
inference — must be wrapped in mlx_lock()". The subsequent **inference** call at line 154
is correctly wrapped in `with mlx_lock():`, but the model loading call on first use is not.

If a second thread (e.g., the live-subs path or a parallel IPC call) triggers any other
MLX operation while `from_pretrained` is allocating GPU memory, the same
`__hash_table<MTL::Resource*>` race that caused the SIGSEGV in PR #71 can occur.

**Scenario**: User triggers first Parakeet transcription (which lazy-loads the model)
while another thread runs a Whisper transcription — two concurrent Metal allocations
without the lock.

### 2. `core/pipeline/stt_parakeet.py:210` — HIGH RISK

```python
# warmup() method — explicit pre-load path
self._model = parakeet_mlx.from_pretrained(self._model_path)  # NOT in mlx_lock()
```

Same root cause as site #1. `warmup()` is called at startup by
`STTManagementService` when the Parakeet adapter is registered. Startup can be
concurrent with the Whisper warmup in `RecordingCoreService`.

## All Sites — Complete Table

| File | Line | Pattern | Locked? | Notes |
|------|------|---------|---------|-------|
| `core/engine.py` | 430 | `mlx_whisper.transcribe(…)` | ✅ yes | `warmup_stt()` — explicit `with mlx_lock():` at line 429 |
| `core/engine.py` | 1910 | `lambda: mlx_whisper.transcribe(…)` | ✅ yes | `_transcribe_model` — inside `with mlx_lock():` at line 1902; lambda executed under lock |
| `core/engine.py` | 1915 | `mlx_whisper.transcribe(…)` | ✅ yes | `_transcribe_model` (non-watchdog path) — same `with mlx_lock():` block |
| `core/engine.py` | 546 | `_mx.clear_cache()` | ⚪ N/A | `set_quality_profile` — **intentional**: cache flush post-profile-switch, not inference |
| `core/engine.py` | 921 | `_mx.clear_cache()` | ⚪ N/A | `transcribe()` — **intentional**: memory cleanup after STT+diarization completes |
| `core/audio_lang_id.py` | 226 | `mlx_whisper.load_models.load_model(…)` | ✅ yes | `_detect_with_mlx` — only called from `_run_detect()` which holds `with mlx_lock():` at line 203 |
| `core/audio_lang_id.py` | 252 | `mlx_whisper.audio.log_mel_spectrogram(…)` | ✅ yes | Same call chain as above — runs under caller's lock |
| `core/audio_lang_id.py` | 262 | `mlx_whisper.decoding.detect_language(…)` | ✅ yes | Same call chain as above |
| `core/pipeline/stt_whisper_mlx_adapter.py` | 123 | `mlx_whisper.transcribe(…)` | ✅ yes | `WhisperMLXAdapter.transcribe()` — `with mlx_lock():` at line 120 |
| `core/pipeline/stt_parakeet.py` | 128 | `parakeet_mlx.from_pretrained(…)` | 🔴 **NO** | `transcribe()` lazy model load — NOT in mlx_lock() — **HIGH RISK** |
| `core/pipeline/stt_parakeet.py` | 155 | `self._model.transcribe(audio)` | ✅ yes | `transcribe()` inference — `with mlx_lock():` at line 154 |
| `core/pipeline/stt_parakeet.py` | 210 | `parakeet_mlx.from_pretrained(…)` | 🔴 **NO** | `warmup()` model pre-load — NOT in mlx_lock() — **HIGH RISK** |
| `scripts/debug_whisper.py` | 28 | `mlx_whisper.transcribe(…)` | ✅ yes | Debug script — `with mlx_lock():` at line 27 |
| `scripts/debug_whisper.py` | 37 | `mlx_whisper.transcribe(…)` | ✅ yes | Debug script — `with mlx_lock():` at line 36 |

## Non-MLX Adapters (do NOT need mlx_lock)

These adapters use PyTorch + MPS — Metal access goes through PyTorch's own
serialization layer, not through `libmlx.dylib`:

| File | Adapter | Backend |
|------|---------|---------|
| `core/pipeline/stt_gigaam.py` | GigaAM | PyTorch+MPS (subprocess) |
| `core/engine.py` (SenseVoice path) | SenseVoice | PyTorch+MPS |
| `core/engine.py` (WhisperX path) | WhisperX | PyTorch+CPU |

## Delta Since Previous Audit (2026-05-04)

The [2026-05-04 audit](mlx-call-sites-2026-05-04.md) found 8 sites and marked all
compliant. Two new MLX adapters were added in Phase D.2 (post-audit):

- **`core/pipeline/stt_whisper_mlx_adapter.py`** (Phase D.2) — `WhisperMLXAdapter`:
  correctly wraps inference in `mlx_lock()`. ✅ Compliant.
- **`core/pipeline/stt_parakeet.py`** (Phase D.2.1) — `ParakeetSTTAdapter`:
  wraps inference correctly but **misses the lock on model loading** (lines 128, 210).
  🔴 Two unlocked sites introduced.

Additionally, the `mx.clear_cache()` calls (lines 546 and 921 in `engine.py`) were
intentionally added outside the lock (Wave 63, PR #405) for post-inference memory
management. This is architecturally correct — cache flushing after the lock is released
is safe and avoids holding the lock longer than necessary.

## Recommended Fix

Wrap both `parakeet_mlx.from_pretrained()` calls in `with mlx_lock():`:

```python
# stt_parakeet.py — transcribe() lazy load (line ~124)
try:
    from core.mlx_lock import mlx_lock
except ImportError:
    import contextlib
    mlx_lock = contextlib.nullcontext

with mlx_lock():
    self._model = parakeet_mlx.from_pretrained(self._model_path)
```

Apply the same pattern in `warmup()` (line ~207).

**Note**: Do NOT auto-apply this fix without reviewing whether Parakeet's `from_pretrained`
actually calls MLX GPU ops. If it is pure Python / disk I/O only, the lock is unnecessary
but harmless (RLock, no deadlock risk). However the library name (`parakeet-mlx`) and the
module's own docstring confirm it uses MLX, so wrapping is the conservative safe choice.

## References

- Previous audit: `docs/audit/mlx-call-sites-2026-05-04.md`
- Original SIGSEGV crash: `~/Library/Logs/DiagnosticReports/Python-2026-04-19-213636.ips`
- Lock implementation: `core/mlx_lock.py` (RLock, reentrant — safe for nested calls)
- PR #71 (2026-04-19): initial `_transcribe_model` lock
- PR #405 (Wave 63): `mx.clear_cache()` memory leak fix
- CLAUDE.md: "ALL MLX inference must be serialized through `core.mlx_lock.mlx_lock()`"
