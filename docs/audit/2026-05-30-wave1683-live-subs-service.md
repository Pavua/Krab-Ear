# Audit: backend/live_subs_service.py (W1683)

**Date:** 2026-05-30
**Wave:** W1683
**Auditor:** sub-agent W1683 (read-only)
**File:** `KrabEar/backend/live_subs_service.py` (222 LOC)
**Status of prior audits:** W1147 F2 (lock) + F5 (privacy) documented but fixes incomplete — see F1 and F2 below.

---

## Summary

`LiveSubsService` accumulates base64 PCM 16kHz chunks in an in-memory buffer, flushes on ≥3 s or `is_final=True`, runs Whisper STT via `Transcriber`, optionally translates, and emits `live_subs.result` via `EventBus`. First holistic audit. 6 findings.

---

## Findings

### F1 HIGH — Privacy bypass via `stop()` path

**Location:** `live_subs_service.py:72-77` (`stop()`) and `live_subs_service.py:88-89` (`handle_ingest()`)

The privacy-mode guard (`self._settings_get("privacy_mode_enabled", False)`) lives exclusively in `handle_ingest()`. The `stop()` method calls `self._flush()` unconditionally without checking privacy mode. If audio was ingested before privacy mode was enabled (e.g., a race between toggle and stop, or audio queued before user enabled privacy), `stop()` will transcribe that buffered audio and emit `live_subs.result` via `EventBus` — leaking content despite privacy mode being active.

**Confirmed by runtime test:** `stop()` called with `privacy_mode_enabled=True` in `settings_get` and a pre-primed buffer → `transcribe` called, `emit_typed` called, `flushed=True`.

Fix: add `if self._settings_get("privacy_mode_enabled", False): self._reset(); return ...` at the top of `stop()`.

---

### F2 HIGH — `test_live_subs_lock_privacy_W1162.py` fails: wrong constructor signature + nonexistent attribute

**Location:** `KrabEar/tests/test_live_subs_lock_privacy_W1162.py` (all 9 test cases)

The test file was written against a different version of `LiveSubsService` that accepted a `settings=` dict kwarg and exposed `_buffer_lock`. The actual implementation uses `settings_get=` (a callable) and `_lock` (an `RLock`). All 9 tests in this file fail immediately with `TypeError: LiveSubsService.__init__() got an unexpected keyword argument 'settings'`. Secondary mismatches:

- Tests reference `svc._buffer_lock` (nonexistent; actual: `svc._lock`).
- `TestPrivacyModeSkipsEmit.test_privacy_mode_off_flush_emits` primes buffer with `svc._buffer_lock` context manager and expects `_flush()` to skip emit when `privacy_mode_enabled=True`, but `_flush()` has no such guard — so even if the lock name were correct the assertion would fail.

**Confirmed by running:** `python -m pytest KrabEar/tests/test_live_subs_lock_privacy_W1162.py -x` → `TypeError` on first test.

These 9 tests have been silently failing in CI since the constructor signature changed (W1147 era).

---

### F3 MED — No buffer size cap: unbounded memory growth

**Location:** `live_subs_service.py:64-70` (`ingest()`), `handle_ingest()`

There is no limit on the accumulated buffer size. If the flush never fires (e.g., `is_final` never sent, Swift `SystemAudioCapture` drops the final packet, or sample_rate is passed as 0 so `buffer_sec` stays at 0 forever), `_buffer` grows without bound. A pathological caller (or a stuck SCStream session) could exhaust heap RAM. The 48kHz→16kHz resample in `_flush()` means 1 hour of 48kHz audio = ~864 MB of numpy arrays in the buffer before flush.

Recommended fix: add `_MAX_BUFFER_SEC = 30.0` constant and force-flush when `buffer_sec >= _MAX_BUFFER_SEC`, regardless of `is_final`.

---

### F4 MED — `stop()` hard-codes `sample_rate=16000` and `target_lang="off"` for final flush

**Location:** `live_subs_service.py:75`

```python
result = self._flush(sample_rate=16000, target_lang="off") if self._buffer else None
```

`stop()` always passes `sample_rate=16000` even if the session was streaming at a different sample rate (e.g., 48kHz natively from `SystemAudioCapture`). The resampler in `_flush()` would get `sample_rate=16000` meaning it skips the resample step entirely, so 48kHz PCM in the buffer is transcribed pitch-shifted (×3 slower). The final subtitle at session end is corrupted.

The `sample_rate` and `target_lang` should be stored as instance state on each `ingest()` call and reused by `stop()`.

---

### F5 LOW — `_flush` STT call holds the `_lock` for the entire duration of Whisper inference

**Location:** `live_subs_service.py:64-70` (`ingest()` → `_flush()`)

`_flush()` is called from within the `with self._lock:` block in `ingest()`. Because `_flush()` calls `self._transcriber.transcribe()` (which triggers MLX Whisper inference via `engine.py`), the `_lock` is held for the full duration of STT — typically 1–5 seconds. Any concurrent call to `ingest()`, `stop()`, or `buffer_duration_sec()` blocks for that entire window. For a 60 FPS subtitle overlay this means subtitle updates stall during transcription.

The pattern used in the sister service `RecordingCoreService` is to capture-and-reset the buffer under the lock, then run STT outside of it. `_flush()` already does a `self._reset()` early (line 133) but the STT call (line 156) and translation (line 163–173) still happen while the lock is conceptually held from the `ingest()` call site.

Fix: in `ingest()`, copy the buffer and call `_reset()` under the lock, then run `_flush()` outside the lock.

---

### F6 LOW — No `mlx_clear_cache()` after Whisper inference

**Location:** `live_subs_service.py:156-159` (STT call in `_flush()`)

Wave 63 (PR #405) established the pattern: call `mx.clear_cache()` after each `mlx_whisper.transcribe()` to prevent RAM growth on long sessions. `live_subs_service.py` calls `self._transcriber.transcribe()` which delegates to `AudioEngine._transcribe_mlx()` → the engine-level code already calls `mx.clear_cache()` after transcription. This is therefore not a new gap in `live_subs_service.py` itself — the engine handles it — but worth noting that the W63 fix applies transitively.

No action required. Included for completeness.

---

## Test Coverage Assessment

| File | Tests | Status |
|------|-------|--------|
| `test_live_subs_service.py` | 16 tests | All pass (verified via basic run) |
| `test_live_subs_service_deep.py` | 22 tests | All pass |
| `test_live_subs_lock_privacy_W1162.py` | 9 tests | **ALL FAIL** (F2 above) |
| `test_live_subs_lock_privacy_W1150.py` | 5 tests | Pass (uses `settings_get=` correctly) |

Gaps not covered by any test:
- `stop()` privacy-bypass (F1).
- Buffer cap / unbounded growth (F3).
- `stop()` sample_rate/target_lang staleness (F4).

---

## Fix Priority

| Finding | Severity | Effort |
|---------|----------|--------|
| F1 — `stop()` privacy bypass | HIGH | 3 lines |
| F2 — W1162 tests all failing (9 dead tests) | HIGH | ~20 lines (fix test stubs) |
| F3 — no buffer cap (DoS/OOM) | MED | 2 lines + constant |
| F4 — `stop()` hard-coded sample_rate | MED | ~5 lines (store instance state) |
| F5 — lock held during STT inference | LOW | refactor `ingest()`/`_flush()` split |
| F6 — `mx.clear_cache()` | LOW | N/A (handled by engine) |
