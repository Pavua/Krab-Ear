# Wave 1147 — LiveSubsService audit

**Date:** 2026-05-26
**File:** `KrabEar/backend/live_subs_service.py`
**Branch:** `audit/live-subs-service-W1147`
**Scope:** backpressure, buffer bounds, latency budget, language-detector false positives, privacy_mode interaction, error handling, test coverage, W947 calendar_link wire status.

---

## Summary

`LiveSubsService` is a compact, well-structured 214-line module. Core flush logic (3 s threshold + `is_final` flag) is sound and well tested. However, six actionable gaps were found across production-correctness and safety dimensions.

---

## Findings

### F1 — NO backpressure: unbounded buffer accumulation (MEDIUM)

**Location:** `live_subs_service.py:41, 61`

`self._buffer: list[np.ndarray]` has no upper bound. If the IPC caller sends chunks faster than the STT pipeline can flush them (e.g., Swift `SystemAudioCapture` streams 48 kHz at ~20 ms per chunk), the list grows indefinitely between flushes.

In the normal case a flush fires every 3 s, but if STT + translation take longer than the inter-chunk interval (which they can at cold-start or under load), the next chunk arrives and extends the buffer before the previous flush completes — because `_flush()` is called **synchronously inside `ingest()`**, and while it runs no other IPC connection is blocked (thread-per-connection model), but additional `live_subs_ingest` calls from a second connection **can** queue. Under prolonged STT stalls (>3 s) the buffer list can grow by several seconds of PCM arrays before OOM pressure manifests.

**Recommendation:** Add a hard cap (e.g., `_MAX_BUFFER_SEC = 30`) and drop + warn when exceeded.

```python
_MAX_BUFFER_SEC = 30.0

# inside ingest(), before appending:
if self._buffer_samples / max(sample_rate, 1) >= _MAX_BUFFER_SEC:
    logger.warning("LiveSubsService: buffer cap exceeded, dropping chunk")
    return None
```

---

### F2 — NO thread-safety lock on shared buffer state (HIGH)

**Location:** `live_subs_service.py:41-66`

`_buffer` and `_buffer_samples` are plain Python attributes. The IPC server uses a **thread-per-connection** model (`service.py:3655`): each incoming Unix socket connection gets its own daemon thread. If two connections both call `live_subs_ingest` concurrently (e.g., a retry from Swift while the first flush is executing), `self._buffer.append()` and `self._buffer_samples +=` race with `_reset()`. Python's GIL prevents torn writes to simple integers but does **not** prevent list/array structural corruption across `np.concatenate` + `_reset()` interleaved with `append`.

The test `TestConcurrentIngest.test_concurrent_ingest_thread_safe` asserts no exceptions are raised but does not assert correctness of `_buffer_samples` post-race — it can silently produce wrong counts.

**Recommendation:** Add `threading.RLock` (reentrant because `_flush` calls `_reset` internally):

```python
import threading
self._lock = threading.RLock()

def ingest(self, ...):
    with self._lock:
        ...
```

---

### F3 — Latency budget: 3 s flush + synchronous STT/translate can exceed 6+ s p95 (MEDIUM)

**Location:** `live_subs_service.py:65-66, 148-165`

Total end-to-end subtitle latency = buffer accumulation time + STT inference + translation:

- Buffer accumulation: up to 3 s (threshold) + remaining chunk duration.
- STT (`transcriber.transcribe`, which delegates to `AudioEngine` → `mlx_whisper`): on 3 s of audio, warm Whisper `balanced` model = ~0.8–1.2 s; cold start = 3–5 s.
- Translation (`translator.translate`, offline): ~0.05–0.2 s.

**p95 worst case (cold STT):** 3 s buffer + 5 s cold STT = **8 s latency** before the subtitle appears on screen. There is no timeout on the STT call inside `_flush()`. If the MLX GPU hangs (known issue, tracked in mlx_subprocess watchdog), `_flush()` blocks forever, freezing this service's state.

**Recommendation:**
1. Add a `transcribe` timeout (wrap in `concurrent.futures.ThreadPoolExecutor` with `timeout=10`).
2. Expose p95 flush latency via `MetricsCollector` so the overlay UI can show a "processing…" spinner beyond 2 s.

---

### F4 — Language-detector false positives: W1019 language detection not consulted; target_lang is caller-supplied (LOW)

**Location:** `live_subs_service.py:84, 156`

`target_lang` is taken directly from the IPC param (`params.get("target_lang", "off")`) without any runtime adjustment based on the detected language. `language_detected` is extracted from the STT result (line 152) but is only logged and emitted in the event payload — it is never used to skip or adapt translation.

Interaction with W1019 (`core/language_detector.py`): W1019's heuristic detector can return false positives on short clips (e.g., 3 s of music or silence tagged as "ru"). When the STT result is empty (`text = ""`), translation is correctly skipped (line 156 guards `if text`). However, if Whisper produces a low-confidence hallucination (non-empty `text`) in the wrong language, the service will still pass it to the translator with the caller's `target_lang`, potentially producing a nonsense subtitle.

**Recommendation (low priority):** Surface `language_detected` to the Swift `LiveSubtitlesOverlay` so the UI can display the detected language alongside the subtitle. No code change needed in `live_subs_service.py` — the field is already in the emitted payload; the gap is in Swift consuming it.

---

### F5 — Privacy mode not enforced: live_subs_ingest bypasses privacy gate (MEDIUM)

**Location:** `KrabEar/backend/service.py:1159`, `live_subs_service.py` (no privacy check)

The `handle_request` dispatcher in `service.py` does **not** have a centralized privacy-mode gate — each handler must check privacy individually. `live_subs_service.py` contains zero references to `privacy_mode`, `privacy_enabled`, or `get_privacy_audit_logger`. This means:

- When `privacy_mode` is enabled (user explicitly enabled it to suppress recording to disk), `live_subs_ingest` still performs STT on system audio and emits `live_subs.result` events.
- Audio captured from system audio (YouTube, Zoom calls, etc.) is transcribed and potentially translated without the privacy gate being consulted.
- The `privacy_audit` log records `enable/disable/purge` events but does not record `live_subs` inference events.

**Recommendation (MEDIUM):** Add a privacy check at the top of `handle_ingest`:

```python
def handle_ingest(self, params: dict[str, Any]) -> dict[str, Any]:
    if self._privacy_mode_check():  # injected or checked via settings
        return {"status": "rejected", "reason": "privacy_mode_enabled"}
    ...
```

Alternatively, the dispatcher in `service.py` should gate `live_subs_ingest` alongside `start_recording`.

---

### F6 — W947 CalendarLink: instantiated but NEVER called (LOW — dead wire)

**Location:** `KrabEar/backend/service.py:125, 508`

`CalendarLinker` is imported and instantiated as `self._calendar_linker = CalendarLinker(...)` but there are **zero** further references to `self._calendar_linker` anywhere in `service.py`. No IPC handler delegates to it; no internal call site uses it.

The W947 calendar_link integration with `live_subs` was specified but never wired. `CalendarLinker.find_overlapping_event()` (which looks up active Calendar.app events via `osascript`) is never invoked from the live subs flush path or from any regular transcription handler.

**Status: NOT WIRED.** `CalendarLinker` is dead code at the `BackendService` level. `StateStore` has a `calendar_links_path` field, and `CalendarLinker` itself is functional, but the integration point is missing.

**Recommendation:** Either wire `_calendar_linker.find_overlapping_event()` in the `_handle_add_history_item` / transcription completion path (where a history item is created), or remove the dead import+instantiation and track in backlog.

---

## Test coverage summary

| Area | Covered | Gaps |
|------|---------|------|
| Buffer accumulation / flush threshold | Yes (both test files) | No test for buffer cap overflow |
| `is_final` flush | Yes | — |
| Translation error resilience | Yes (`translate_raises=True`) | — |
| STT error / exception in `_flush` | No | No test for STT raising exception |
| Thread safety (concurrent ingest) | Partial (no-exception only) | No correctness assertion post-race |
| Privacy mode gate | No | No test |
| Buffer hard cap | No | No test |
| Resample path (non-16000 Hz) | No | `sample_rate != 16000` branch untested |
| `stop()` with pending buffer | Yes | — |
| EventBus emit payload fields | Yes | — |

**Total test methods:** 41 across `test_live_subs_service.py` (21) and `test_live_subs_service_deep.py` (20). Coverage is good on the happy-path; gaps are in error injection, concurrency correctness, and the resample path.

---

## Priority matrix

| # | Finding | Severity | Fix effort |
|---|---------|----------|-----------|
| F2 | No threading lock on buffer state | HIGH | S (add RLock) |
| F5 | Privacy mode not enforced | MEDIUM | S (add guard in handle_ingest) |
| F1 | No backpressure / buffer cap | MEDIUM | S (add cap + drop) |
| F3 | No STT timeout in flush | MEDIUM | M (ThreadPoolExecutor wrap) |
| F4 | Language-detector false positives (passive) | LOW | XS (Swift UI only) |
| F6 | W947 CalendarLink dead wire | LOW | M (wire or remove) |
