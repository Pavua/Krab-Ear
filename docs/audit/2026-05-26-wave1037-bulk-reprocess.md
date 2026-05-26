# Wave 1037 — BulkReprocessor Audit

**Date:** 2026-05-26  
**File:** `KrabEar/backend/bulk_reprocess.py`  
**Class:** `BulkReprocessor`  
**Auditor:** W1037 (sub-agent)

## Summary

BulkReprocessor is a well-structured module for batch re-transcribing history entries with the current STT settings. It has solid coverage (17 test cases, 652 lines in `test_bulk_reprocess.py`) and correct cancellation/dry-run semantics. However, **five significant gaps** were identified — two are HIGH severity.

---

## Findings

### F1 — HIGH: No concurrency guard between reprocess() and active recording

**Location:** `bulk_reprocess.py:207` (main loop), `backend/service.py` (no wiring)

`BulkReprocessor.reprocess()` calls `self.transcriber.transcribe()` synchronously on the calling thread. This delegates to `AudioEngine.transcribe()` which acquires `mlx_lock()` internally. The problem is that when a live recording is in progress, `AudioEngine` is simultaneously consuming the same MLX GPU resources. There is no guard at the `BulkReprocessor` level to check if a recording is active before starting or to pause/abort when one begins mid-run.

The existing `test_concurrent_start` test only checks that two sequential `reprocess()` calls on the same `BulkReprocessor` instance complete independently — it does not test interleaving with an active `AudioRecorder` session.

**Risk:** Under load, bulk reprocess competing with live STT causes MLX GPU lock contention and potential SIGSEGV (same class of bug as PR #71). On M4 Max with simultaneous active inference, memory pressure can force a reboot (see `feedback_mlx_memory_constraint.md`).

**Fix:** Before entering the main loop, check `self.transcriber.engine` recording state (or inject a `is_recording_fn` callback). If recording is active, return immediately with `{"error": "recording_in_progress"}` or defer until idle.

---

### F2 — HIGH: mlx_lock not acquired at BulkReprocessor level — relies solely on engine internals

**Location:** `bulk_reprocess.py:236-243`

The `reprocess()` loop calls `self.transcriber.transcribe(audio_data, ...)` with no explicit `mlx_lock` wrapping at the bulk-reprocess layer. The engine does acquire `mlx_lock` internally for the MLX path, but:

1. The engine can fall back to non-MLX adapters that do not use `mlx_lock` (SenseVoice, Parakeet, WhisperX via PyTorch+MPS — these are explicitly noted as not needing the lock in `engine.py:1646`).
2. However, `_load_audio()` uses `soundfile.sf.read` + numpy `np.interp` in a tight loop for potentially 1000 audio files — this is done completely outside any lock. If another thread also loads audio (e.g., realtime partial transcriber), the I/O is safe, but the lack of inter-call delay means 1000 consecutive MLX calls can cause GPU memory fragmentation.

This is a documentation/enforcement gap rather than an immediate crash risk (the engine lock is present), but it violates the stated project rule: *"ALL MLX inference must be serialized through `core.mlx_lock.mlx_lock()`"* at the call site level.

**Fix:** Either add a comment referencing that `transcriber.transcribe()` handles the lock internally, or wrap each call: `with mlx_lock(): result = self.transcriber.transcribe(...)` to make the invariant explicit and guard against future refactors that move the lock.

---

### F3 — MEDIUM: Privacy mode not checked before reprocessing history

**Location:** `bulk_reprocess.py:134-200` (filter section)

`BulkReprocessor` has no awareness of `privacy_mode_enabled` (a config setting in `DEFAULT_SETTINGS` at `core/config.py:987`). When privacy mode is active, sending historical audio through the STT engine could re-expose data the user intended to protect. The existing filters only check `is_protected`, `audio_path` presence, and item age.

There is no `privacy_mode_fn` callback parameter or any `settings_get` injection in the constructor.

**Fix:** Accept an optional `settings_get: Callable[[str, Any], Any] | None` parameter (matching the pattern used in `Transcriber.__init__`). At the start of `reprocess()`, check `settings_get("privacy_mode_enabled", False)` and early-return with an error if True.

---

### F4 — MEDIUM: No IPC wire — BulkReprocessor is instantiated nowhere in service.py

**Location:** `KrabEar/backend/service.py` (not present), `KrabEar/core/config.py:902`

`DEFAULT_SETTINGS` contains `"bulk_reprocess_batch_size": 5` and the IPC_API_REFERENCE.md previously listed `bulk_reprocess_start` / `bulk_reprocess_cancel` / `bulk_reprocess_status` handlers — but these were removed in Wave 65. The `BulkReprocessor` class currently has **no integration point in `service.py`** and no IPC handlers.

`grep` across all `backend/*.py` files (excluding the class itself) finds zero imports or instantiations of `BulkReprocessor`. The `test_ipc_dispatch_invariants.py` does reference `"_bulk_reprocessor"` as an expected attribute on `BackendService` (line 188), but the attribute is never created, meaning that invariant test would fail if run against the live service.

**Risk:** The feature is completely dead from a user perspective — the module exists and is tested in isolation but is not exposed. If the invariant test references it, it would silently pass only because the invariant test uses a `getattr(..., None)` pattern.

**Fix:** Either re-wire `BulkReprocessor` into `service.py` with IPC handlers (`bulk_reprocess_start`, `bulk_reprocess_cancel`, `bulk_reprocess_status`) or remove the `_bulk_reprocessor` reference from `test_ipc_dispatch_invariants.py` and mark the module as explicitly pending integration.

---

### F5 — LOW: Memory pressure for 1000-item reprocess not bounded per-item

**Location:** `bulk_reprocess.py:85-104` (`_load_audio`), `bulk_reprocess.py:189-196` (hard limit)

The hard limit of 1000 items prevents unbounded candidate lists, but within the loop each `_load_audio()` call reads the entire audio file into RAM as a float32 numpy array. A 1-hour ALAC recording at 44.1 kHz can be ~600 MB in float32. For 1000 such files processed sequentially, each audio array is loaded, transcribed, then falls out of scope — but numpy arrays are not immediately freed by the Python GC. On M4 Max with 36 GB RAM this is unlikely to OOM, but it is a latent risk on smaller systems and could trigger the MLX GPU memory pressure described in `feedback_mlx_memory_constraint.md`.

No explicit `del audio_data` or `gc.collect()` is called after each transcription in the loop.

**Fix:** Add `del audio_data` immediately after the `result = self.transcriber.transcribe(...)` call and consider `import gc; gc.collect()` every `batch_size` items to keep RSS stable during long runs (consistent with the `mx.clear_cache()` pattern from Wave 63).

---

## Test Coverage Assessment

| Area | Covered? |
|---|---|
| Dry-run (no STT, no store update) | Yes |
| Actual reprocess (version save + store update) | Yes |
| Confidence filter (skip high-confidence items) | Yes |
| Cancellation mid-loop | Yes |
| Hard limit truncation | Yes |
| Progress events (EventBus emit) | Yes |
| Protected items skip | Yes |
| Age filter (< 1 hour skipped) | Yes |
| Concurrent `reprocess()` calls (same instance) | Yes (sequential simulation) |
| **Concurrent reprocess + active recording** | **No** |
| **Privacy mode check** | **No** |
| **IPC integration** | **No (handlers removed Wave 65, not re-added)** |
| Memory cleanup after each item | No |

---

## Verdict

5 findings (2 HIGH, 2 MEDIUM, 1 LOW). The module's core logic is correct and well-tested in isolation. The blocking issues are the missing recording-active guard (F1) and the dead IPC wiring (F4). F2 (explicit mlx_lock annotation), F3 (privacy mode), and F5 (memory cleanup) are lower-risk but should be addressed before the feature is re-exposed to users.
