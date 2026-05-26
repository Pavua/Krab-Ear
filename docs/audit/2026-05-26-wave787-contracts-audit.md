# Wave 787 — Contracts Registry Audit

**Date:** 2026-05-26  
**Scope:** `KrabEar/contracts/registry.py` — `EventType` enum + `EVENT_SCHEMA_MAP`  
**Goal:** Identify live vs dead typed events and surface untyped `emit()` calls that lack a contract schema.

---

## Summary

| Metric | Count |
|--------|-------|
| Total EventType members | 9 |
| Live (emitted via `emit_typed`) | 6 |
| Dead (never emitted via `emit_typed`) | 3 |
| Untyped `emit()` calls (string literals, no schema) | 14 |

---

## EventType Members

Defined in `KrabEar/contracts/registry.py`:

```
STT_PARTIAL          = "stt.partial"
STT_FINAL            = "stt.final"
STT_FAILED           = "stt.failed"
TRANSLATION_COMPLETED= "translation.completed"
TRANSLATION_FAILED   = "translation.failed"
MARKDOWN_EXPORT      = "markdown_export"
AUTO_SUMMARY         = "auto_summary"
HOTWORD_DETECTED     = "hotword.detected"
LIVE_SUBS_RESULT     = "live_subs.result"
```

All 9 are present in `EVENT_SCHEMA_MAP` (one entry per member — no map gaps).

---

## Live Event Types

Emitted via `event_bus.emit_typed(EventType.X, ...)` in production code:

| EventType | Emitter file | Line(s) |
|-----------|-------------|---------|
| `STT_PARTIAL` | `backend/recording_core_service.py` | 633 |
| `STT_FINAL` | `backend/recording_core_service.py` | 1177 |
| `STT_FAILED` | `backend/recording_core_service.py` | 1001 |
| `TRANSLATION_COMPLETED` | `backend/recording_core_service.py` | 1034 |
| `TRANSLATION_FAILED` | `backend/recording_core_service.py` | 1044 |
| `LIVE_SUBS_RESULT` | `backend/live_subs_service.py` | 175 |

---

## Dead Candidates

These `EventType` members are defined and have schema map entries but are **never emitted** via `emit_typed` in any production file (confirmed by exhaustive grep across `KrabEar/backend/`, `KrabEar/core/`, and `KrabEar/contracts/`):

| EventType | Value | Schema model | Notes |
|-----------|-------|-------------|-------|
| `MARKDOWN_EXPORT` | `"markdown_export"` | `MarkdownExportEvent` | No emit site found. `HistoryService` handles export internally but does not fire this event. |
| `AUTO_SUMMARY` | `"auto_summary"` | `AutoSummaryEvent` | No emit site found. `call_auto_summary` is a settings flag, not an event trigger. |
| `HOTWORD_DETECTED` | `"hotword.detected"` | `HotwordDetected` | No emit site found. `backend/hotword_detector.py` contains the detector but emits nothing. |

**Action:** Do NOT remove these from the enum or map — they may be reserved for planned work. Mark for future wiring in the relevant service (hotword_detector, history_service, call_assist_service).

---

## Untyped `emit()` Calls — No Schema Coverage

These calls use `event_bus.emit(string_literal, dict)` without a corresponding `EventType` entry or `EVENT_SCHEMA_MAP` registration. They are the audit's primary concern for incremental typing.

### Priority 1 — High-frequency / consumer-facing

| String key | File | Lines | Suggested EventType name |
|-----------|------|-------|--------------------------|
| `"realtime.partial_transcript"` | `backend/realtime_partial.py` | 169 (via `_REALTIME_PARTIAL_TYPE` const) | `REALTIME_PARTIAL` |
| `"realtime.final_transcript"` | `backend/recording_core_service.py` | 1186 | `REALTIME_FINAL` |
| `"recording.audio_level"` | `backend/service.py` | 171 | `RECORDING_AUDIO_LEVEL` |
| `"app.status"` | `backend/recording_core_service.py` (338), `backend/obsidian_sync.py` (136, 155, 184, 200) | multiple | `APP_STATUS` |
| `"krab_error"` | `backend/error_bus.py` | 189 | `KRAB_ERROR` |

### Priority 2 — Operational / monitoring

| String key | File | Lines | Suggested EventType name |
|-----------|------|-------|--------------------------|
| `"rewriter_recovered"` | `backend/llm_probe.py` | 253 | `REWRITER_RECOVERED` |
| `"preset.changed"` | `backend/settings_service.py` | 338 | `PRESET_CHANGED` |
| `"bulk_reprocess_progress"` | `backend/bulk_reprocess.py` | 119 | `BULK_REPROCESS_PROGRESS` |
| `"playback.seek"` | `backend/bookmarks.py` | 266 | `PLAYBACK_SEEK` |
| `"recording.silence_detected"` | `backend/realtime_silence_filter.py` | 158 | `RECORDING_SILENCE_DETECTED` |

### Priority 3 — Disk / infra

| String key | File | Lines | Suggested EventType name |
|-----------|------|-------|--------------------------|
| `f"disk.{level}"` (→ `"disk.warning"` / `"disk.critical"`) | `backend/disk_monitor.py` | 228 | `DISK_WARNING`, `DISK_CRITICAL` |
| `"disk.history_large"` | `backend/disk_monitor.py` | 263 | `DISK_HISTORY_LARGE` |
| `"disk.auto_cleanup_requested"` | `backend/disk_monitor.py` | 340 | `DISK_AUTO_CLEANUP_REQUESTED` |

### Dynamic / passthrough (intentionally untyped)

| Pattern | File | Notes |
|---------|------|-------|
| `bus.emit(event_type, event_data)` | `backend/vg_ws_client.py` | 57 | Forwards Voice Gateway WS frames verbatim — event_type is a dynamic string from the VG protocol; intentionally passthrough, typing not applicable. |

---

## Recommendations

1. **Wire the 3 dead events** by adding `emit_typed` calls at the natural emit sites:
   - `HOTWORD_DETECTED` — in `backend/hotword_detector.py` when a match is found.
   - `MARKDOWN_EXPORT` — in `backend/history_service.py` after a successful markdown export.
   - `AUTO_SUMMARY` — in `backend/call_assist_service.py` when a call auto-summary completes.

2. **Type the Priority 1 untyped emits** first (5 events). These are the highest-traffic paths and the most likely to drift silently. Add schema models in new files (e.g., `contracts/realtime_events.py`, `contracts/app_events.py`) and register in `EVENT_SCHEMA_MAP`.

3. **Type Priority 2 emits** in a follow-up wave — lower traffic but improves observability for LLM probe, bulk reprocess, and settings changes.

4. **Leave `vg_ws_client.py` passthrough untyped** — typing a dynamic forwarding layer adds no value and would require a catch-all schema.

5. **Disk events** (Priority 3) could use a single `DiskStatusEvent` Pydantic model with a `level` discriminator to avoid 4 separate enum entries.

---

## File references

- `KrabEar/contracts/registry.py` — enum + schema map
- `KrabEar/backend/recording_core_service.py` — primary emit_typed site
- `KrabEar/backend/live_subs_service.py` — LIVE_SUBS_RESULT
- `KrabEar/backend/hotword_detector.py` — dead HOTWORD_DETECTED
- `KrabEar/backend/history_service.py` — dead MARKDOWN_EXPORT
- `KrabEar/backend/call_assist_service.py` — dead AUTO_SUMMARY
- `KrabEar/backend/disk_monitor.py` — 3 untyped disk events
- `KrabEar/backend/error_bus.py` — untyped krab_error
- `KrabEar/backend/realtime_partial.py` — untyped realtime.partial_transcript
