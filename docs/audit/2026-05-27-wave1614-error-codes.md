# Wave 1614 — First-pass audit: `backend/error_codes.py`

**Date:** 2026-05-27  
**Auditor:** W1614 sub-agent  
**File:** `KrabEar/backend/error_codes.py`  
**Registry size:** 57 entries (56 named in comments/tests — see F2)  
**Test file:** `KrabEar/tests/test_error_codes.py`

---

## Scope

First-ever direct audit of `ERROR_REGISTRY` (only `error_bus.py` was previously audited, in W1231). Checks:
- Schema completeness (`_Entry` TypedDict coverage)
- Dead codes (in registry, no call site)
- Missing codes (pushed at call site, not registered — W1231 F2 pattern)
- Test coverage health
- Naming convention compliance
- Action handler coverage

---

## Findings

### F1 HIGH — `audio.max_duration_reached` pushed but not in registry (silent degraded error)

**File:** `KrabEar/backend/recorder.py:257`  
**Pattern:** W1231 F2 repeat

`AudioRecorder._push_max_duration_error()` constructs a `KrabError` with `code="audio.max_duration_reached"` directly via `ERROR_REGISTRY.get("audio.max_duration_reached", {})` — the `.get` with `{}` default means that when the code is absent, `KrabError` is instantiated with empty `user_msg_ru`, empty `severity`, `actionable=False` (all falsy defaults from dict). The error bus still fires (Sentry gets a breadcrumb) but the Swift toast shows an empty message.

`audio.max_duration_reached` is not present in `ERROR_REGISTRY` at all. It is referenced in `recorder.py` comments (line 29) and the push helper (line 243, 257). No entry exists in `error_codes.py`.

**Fix:** Add entry to registry:
```python
"audio.max_duration_reached": {
    "user_msg_ru": "Достигнут лимит записи — запись остановлена автоматически",
    "actionable": False,
    "action_id": None,
    "action_label": "",
    "severity": "warn",
    "dedupe_seconds": 30,
},
```
Also add to `test_expected_codes_present` expected set and update count to 58.

---

### F2 HIGH — 3 active test failures in `test_error_codes.py`

**File:** `KrabEar/tests/test_error_codes.py`

Running `PYTHONPATH=KrabEar python3 -m unittest KrabEar/tests/test_error_codes.py` produces 3 FAIL:

1. **`test_expected_codes_present`** — `stt.transcribe_failed` is in `ERROR_REGISTRY` but missing from the `expected` set. Assertion `assertEqual(set(ERROR_REGISTRY.keys()), expected)` fails with: _"Items in the first set but not the second: 'stt.transcribe_failed'"_.

2. **`test_error_registry_count_matches_documentation`** — asserts `len(ERROR_REGISTRY) == 56` but actual count is 57. `stt.transcribe_failed` was added to the registry (Wave 78) without updating this test.

3. **`test_disk_critical_in_registry`** — asserts `entry["dedupe_seconds"] == 300` but `disk.critical.dedupe_seconds` is 600 in the registry. The test was written with one value; the registry was later updated with a different one (Wave 490 comment says 600s to avoid alert storm).

**Fix:**
- Add `"stt.transcribe_failed"` to the `expected` set in `test_expected_codes_present`
- Change count assertion to `assertEqual(len(ERROR_REGISTRY), 57)` (or 58 after F1 fix)
- Update `test_disk_critical_in_registry` to `assertEqual(entry["dedupe_seconds"], 600)` (registry is correct; test is stale)

---

### F3 MED — `stt.gigaam.ffmpeg_missing` violates 2-part naming convention

**Line:** `error_codes.py:423`

All 56 other codes use a `category.specific` (2-part) dotted name. `stt.gigaam.ffmpeg_missing` has 3 parts. This breaks:
- The `_Entry` TypedDict key regex used in CI audit scripts (`grep -oP '"[a-z][a-z_.]*\.[a-z_]+"'`)
- Any tooling that assumes `code.split(".")[0]` = category and `code.split(".")[1]` = specific
- Visual scanning for "all `stt.*` codes"

The code belongs in the `stt` category; `gigaam_ffmpeg_missing` is the natural 2-part form.

**Fix:** Rename to `"stt.gigaam_ffmpeg_missing"` in registry, all call sites (`service.py:376`, test), and `test_expected_codes_present`. One grep replacement.

---

### F4 MED — 7 registry entries with zero call sites (planned-but-unwired codes)

The following codes appear in `ERROR_REGISTRY` but no production call site (`_push_error(...)` or `KrabError(code=...)`) exists anywhere in `KrabEar/`:

| Code | Comment in registry |
|------|---------------------|
| `diarization.vad_gated` | Wave 50 — pyannote HF-gated VAD |
| `ipc.audio_device_poll_flood` | Wave 78 — audio device picker polling loop |
| `rewriter.warmup_failed` | Wave 50 — generic warmup fail |
| `stt.mlx_timeout` | Wave 50 — MLXWatchdog timeout |
| `stt.padding_mismatch` | Wave 50 — GigaAM padding error |
| `stt.transcribe_failed` | Wave 78 — unexpected STT exception |
| `system.proc_cmdline_permission` | Wave 82 — /proc cmdline not readable |

These are "pre-registered but not yet wired" codes — valid pattern when the call site is added later. However, they contribute to the test count mismatch (F2) and make the registry feel larger than it is. The docstring comment at line 6 says "Wire `error_bus.push(...)` at the call site" as step 3 — these are stalled at step 1.

**Recommendation (not a breaking fix):** Add a `TODO` comment section in registry grouping unwired codes, or use a `wired: bool` field. At minimum, wire or remove `stt.mlx_timeout` and `stt.padding_mismatch` which have corresponding log warnings already in the codebase (`core/mlx_subprocess.py`, `core/engine.py`).

---

### F5 LOW — Non-alphabetical ordering within categories reduces readability

The registry has 15 codes that appear out of natural category order:

- `rewriter.*` codes are split across 4 non-contiguous sections (lines 40-127, 238-248, 253-263, 325-339, 513-726)
- `stt.*` codes split across 3 sections
- `diarization.*`, `mlx.*`, `ipc.*` codes all appear scattered between unrelated Wave comment blocks

This is a maintainability issue: when adding a new `rewriter.*` code, the correct insertion point is unclear, and reviewers must scan the full file to check for duplicates.

**Recommendation:** Sort entries alphabetically within category blocks (paste → rewriter → stt → diarization → translation → mlx → history → vocabulary → ipc → hotkey → audio → agent → disk → system → vgw → startup). A one-time reorder PR.

---

### F6 LOW — `_Entry` TypedDict undocumented fields and no i18n envelope

**Line:** `error_codes.py:12-18`

`_Entry` has 6 fields: `user_msg_ru`, `actionable`, `action_id`, `action_label`, `severity`, `dedupe_seconds`.

Two gaps:
1. The module docstring at line 6 lists `message_debug?` as an optional key (step 1 instructions), but `_Entry` does not define it. No entry uses it. The docstring is stale — `message_debug` lives in the `KrabError` instance, not in the registry. Minor doc drift.
2. No `user_msg_en` field exists. The app is bilingual (RU/ES/EN per CLAUDE.md). If English toasts are ever needed (e.g., for non-RU users), there is no place to register them without an `_Entry` schema change. Not blocking now but an architectural gap.

**Recommendation:** Remove `message_debug?` from the step 1 comment (it's a call-site concern, not a registry concern). Optionally add `user_msg_en: str | None = None` to `_Entry` TypedDict for future use.

---

## Summary

| # | Severity | Finding | Fix effort |
|---|----------|---------|------------|
| F1 | HIGH | `audio.max_duration_reached` pushed but absent from registry (silent toast) | 1 entry + test update |
| F2 | HIGH | 3 test failures in `test_error_codes.py` (count 57≠56, `stt.transcribe_failed` missing from expected, `disk.critical` dedupe stale) | 3 test line changes |
| F3 | MED | `stt.gigaam.ffmpeg_missing` uses non-standard 3-part name | 1 rename across 3 files |
| F4 | MED | 7 registry codes have no production call site (unwired) | Wire or document |
| F5 | LOW | Registry ordering: codes scattered across 4+ non-contiguous sections per category | One-time reorder |
| F6 | LOW | `_Entry` TypedDict missing `user_msg_en`, stale `message_debug?` in docstring | Docstring + optional field |

**Action coverage:** All 12 `action_id` values in the registry have corresponding handlers in `ACTION_HANDLERS` — no gaps.  
**Schema coverage:** All 57 entries pass `_Entry` required-key check, severity validity, and `actionable → non-null action_id` rule.
