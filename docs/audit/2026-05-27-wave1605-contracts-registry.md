# W1605 — First-pass audit: `contracts/registry.py`

Date: 2026-05-29 | Status: NEW | 5 findings (1 HIGH, 2 MEDIUM, 2 LOW)

## Scope

`KrabEar/contracts/registry.py` and the surrounding `contracts/` package:
`envelope.py`, `export.py`, `__init__.py`, `stt_events.py`, `translation_events.py`,
`live_subs_events.py`, `history_events.py`, `hotword_events.py`, `contracts/schemas/`.

Cross-checked against all production `event_bus.emit`/`emit_typed` call sites in
`KrabEar/backend/` and the test suite in `KrabEar/tests/`.

---

## Summary

The registry is structurally sound: 9 `EventType` members, each mapped 1-to-1 to a
Pydantic model in `EVENT_SCHEMA_MAP`, schema files committed for all 9 types, and a
multi-file test suite with drift guards. No schema/map mismatch found. However, five
issues were identified ranging from dead-code risk to a privacy concern.

---

## Findings

### F1 — HIGH: `MARKDOWN_EXPORT` and `AUTO_SUMMARY` have no production emitter

**Files:** `KrabEar/contracts/registry.py` lines 22-23, `KrabEar/contracts/history_events.py`

`EventType.MARKDOWN_EXPORT` and `EventType.AUTO_SUMMARY` are registered with Pydantic
models (`MarkdownExportEvent`, `AutoSummaryEvent`) but a full-codebase grep reveals
**zero production call sites** that emit either event through `event_bus.emit()` or
`emit_typed()`. The only references outside `contracts/` are in test files
(`test_contracts*.py`, `test_history_contracts.py`).

Consequence: the schemas are tested in isolation but never exercised end-to-end. If a
future emitter is added with a subtly different payload shape the drift tests will pass
(because the Pydantic model has not changed) while the actual emitter will silently
produce a non-conforming event.

Similarly, `HotwordDetected` has no production emitter in the backend — the
`HotwordDetector` class (`backend/hotword_detector.py`) scans transcripts but does not
call `event_bus.emit_typed(EventType.HOTWORD_DETECTED, ...)`.

**Recommendation:** Either wire the emitters or mark these EventType members as
`RESERVED` in a docstring and add a CI check (grep) that fails if the registry member
count exceeds the number of active emitters.

---

### F2 — MEDIUM: `__init__.py` `__all__` is incomplete — 4 of 9 models missing

**File:** `KrabEar/contracts/__init__.py` lines 18-30

`__all__` exports only the STT + Translation model classes. The four models registered
in `EVENT_SCHEMA_MAP` since Wave 162 (`LiveSubsResult`, `MarkdownExportEvent`,
`AutoSummaryEvent`, `HotwordDetected`) are not listed:

```python
# Missing from __all__:
"LiveSubsResult",
"MarkdownExportEvent",
"AutoSummaryEvent",
"HotwordDetected",
```

Code that does `from contracts import LiveSubsResult` works only because Python
resolves it via `contracts/__init__.py`'s star-import chain, not through `__all__`. Any
tool or linter that respects `__all__` (e.g. `mypy --strict`, `pylint`, `pdoc`) will
treat these as private. Downstream consumers of the public API may miss these types.

**Recommendation:** Add the four missing names to `__all__` in `contracts/__init__.py`
and add corresponding `from contracts.X import Y` import lines.

---

### F3 — MEDIUM: Privacy — 5 models carry raw transcript text with no annotation

**Files:** `contracts/stt_events.py`, `contracts/translation_events.py`,
`contracts/live_subs_events.py`, `contracts/hotword_events.py`

The following fields contain verbatim user speech or translations:

| Model | Field |
|---|---|
| `SttPartial` | `text` |
| `SttFinal` | `text`, `segments[].text` |
| `TranslationCompleted` | `source_text`, `translated_text` |
| `TranslationFailed` | `source_text` |
| `LiveSubsResult` | `text`, `translation` |
| `HotwordDetected` | `text`, `matches[].context` |
| `AutoSummaryEvent` | `summary` |

None of these fields carry a privacy annotation (no `Field(json_schema_extra={"pii":
True})` or docstring marker). Events flow through `EventBus` to SSE endpoints and to
`EventReplayManager`'s ring buffer. When Sentry breadcrumbs are captured
(`backend/observability.py`), the current code does **not** pass event payloads to
Sentry, but there is no structural guard preventing a future caller from doing so.

The existing Sentry breadcrumb policy (method name + duration only, no transcript) is
documented in CLAUDE.md but is not enforced at the schema level.

**Recommendation:** Add a `_pii_fields` class variable or `Field(..., json_schema_extra={"pii": True})`
annotation to all transcript-bearing fields. This enables an automated scrubber for
Sentry / replay export without changing runtime behaviour.

---

### F4 — LOW: No schema versioning mechanism

**Files:** all `contracts/*.py`

The `KrabEventEnvelope` contract (`{type, ts, data}`) is described as
`EVENT_CONTRACT_V1` in CLAUDE.md and in the `event_bus.py` docstring but this version
string is not embedded in any code artifact. There is no `CONTRACT_VERSION` constant,
no `schema_version` field on models, and no migration path documented.

If a breaking field rename is needed (e.g. `SttFinal.history_id` → `SttFinal.id` for
alignment with a Voice Gateway schema), there is no mechanism to detect downstream
consumers that have not been updated. The `test_contracts_schema_drift.py` guard only
catches changes to committed schema files after `python -m contracts.export` is re-run;
it does not catch breaking renames that happen to keep the same JSON structure.

**Recommendation:** Add `CONTRACT_VERSION = "1"` to `registry.py` and emit it as part
of the `event_bus.emit` envelope (e.g. as `{"type": ..., "ts": ..., "v": "1", "data":
...}`), or at minimum document the versioning policy in a `CONTRACTS.md` file.

---

### F5 — LOW: Hard-coded count `9` in test suite will fail silently on removal

**File:** `KrabEar/tests/test_contracts_coverage.py` line 68

```python
def test_event_type_count_equals_9(self):
    self.assertEqual(len(EventType), 9)
```

This guard prevents accidental removal of an EventType member but produces a confusing
failure message when a new type is legitimately added ("Expected 9, got 10"). The
`_EXPECTED_EVENT_TYPE_VALUES` set in `test_contracts_registry.py` is a better pattern
because it names every expected member explicitly, making the diff obvious. The count
test adds no additional safety over the set-equality test already present.

**Recommendation:** Remove `test_event_type_count_equals_9` and rely on
`TestEventTypeAllValues.test_all_known_values_present` in `test_contracts_registry.py`
as the single authoritative completeness check.

---

## What was checked and found healthy

- **EventType completeness vs. production emitters (typed path):** `STT_PARTIAL`,
  `STT_FINAL`, `STT_FAILED`, `TRANSLATION_COMPLETED`, `TRANSLATION_FAILED`,
  `LIVE_SUBS_RESULT` all have `emit_typed` call sites in `recording_core_service.py`
  and `live_subs_service.py`.
- **EVENT_SCHEMA_MAP correctness:** Every enum member maps to the correct Pydantic
  class; no swapped entries.
- **Schema drift guard:** `test_contracts_schema_drift.py` exports fresh schemas and
  diffs against committed files — will catch any field change that survives a rebuild.
- **Orphan schema files:** None. All 9 `contracts/schemas/*.schema.json` files match an
  active EventType.
- **`parse_and_validate` raises `UnknownEventType` for foreign domains:** Confirmed;
  Voice Gateway events (`voice_gateway.*`, `tts.*`) correctly raise `UnknownEventType`.
- **`EventType` is a `str` subclass:** `EVENT_SCHEMA_MAP.get("stt.final")` returns the
  correct class — the `vg_ws_client.py` str-key lookup works correctly despite using a
  raw string (no runtime bug).
- **Cross-project boundary:** Clear. Krab Ear owns `stt.*`, `translation.*`,
  `live_subs.*`, `hotword.*`, `markdown_export`, `auto_summary`. Voice Gateway events
  rejected by `UnknownEventType` in `parse_and_validate`.
- **emit_typed validation:** `emit_typed` calls `payload.model_dump(mode="json")` which
  runs Pydantic's full validation; malformed payloads raise `ValidationError` before
  reaching subscribers.
- **`python -m contracts.export` still works:** Schema export generates all 9 files
  with correct `{"type": "object", "properties": ...}` structure.
- **No Sentry hooks on contract violations:** No automatic Sentry capture on
  `ValidationError` in `parse_and_validate` or on `UnknownEventType`. This is
  acceptable given the no-op DSN default, but could be added opportunistically.

---

## Files read

- `KrabEar/contracts/registry.py`
- `KrabEar/contracts/envelope.py`
- `KrabEar/contracts/export.py`
- `KrabEar/contracts/__init__.py`
- `KrabEar/contracts/stt_events.py`
- `KrabEar/contracts/translation_events.py`
- `KrabEar/contracts/live_subs_events.py`
- `KrabEar/contracts/history_events.py`
- `KrabEar/contracts/hotword_events.py`
- `KrabEar/backend/event_bus.py`
- `KrabEar/backend/vg_ws_client.py`
- `KrabEar/backend/recording_core_service.py` (emit_typed sites)
- `KrabEar/backend/live_subs_service.py` (emit_typed sites)
- `KrabEar/backend/realtime_partial.py` (untyped emit)
- `KrabEar/tests/test_contracts.py`
- `KrabEar/tests/test_contracts_registry.py`
- `KrabEar/tests/test_contracts_schema_drift.py`
- `KrabEar/tests/test_contracts_coverage.py`
