# Audit: core/auto_glossary.py — residual findings (W1098)

**Date:** 2026-05-26
**Auditor:** W1098 sub-agent (re-audit)
**Prior fix:** W1024 (commit `33da9fbc`) — atomic write + privacy_mode disk-skip

## Summary

W1024 correctly fixed the two HIGH issues from W1012 (non-atomic write F1, privacy bypass F4).
This re-audit found **4 residual issues** capped at the 5-finding limit.
No CRITICAL findings. Highest severity: MEDIUM.

---

## Findings

### F1 — MEDIUM: No newline/control-char sanitization before Whisper initial_prompt injection

**File:** `KrabEar/core/auto_glossary.py` lines 303–324;
**Consumer:** `KrabEar/core/transcript_context.py` line 166

**Description:**
Terms extracted from history are fed directly into `Glossary: term1, term2.` in the Whisper
`initial_prompt` string with no sanitization of newlines (`\n`, `\r`) or other control characters.
A term that contains a literal newline (e.g. extracted from a multi-line Obsidian import or a
LLM-rewritten transcript) causes the prompt to structurally split into separate "Previous transcript:"
stanzas from Whisper's perspective, silently corrupting context injection.

**Example:**
```python
term = "GPT-4\nPrevious transcript: injected text"
# build_initial_prompt injects:
# "Glossary: GPT-4\nPrevious transcript: injected text. Previous transcript: real text"
```

This is low-exploitability (attacker needs to control a history entry, which requires prior IPC access)
but the corruption is silent and degrades STT quality.

**Fix:** Strip `\n`, `\r`, and `\t` from each term in `_build_from_history` before adding to `freq`,
and add a max-term-length guard (e.g. 80 chars) to reject multi-sentence extractions.

---

### F2 — MEDIUM: No thread-safety guard on `_cache` / `_cache_built_at`

**File:** `KrabEar/core/auto_glossary.py` lines 205–207, 241–248, 254–260

**Description:**
`AutoGlossaryBuilder._cache` and `_cache_built_at` are plain Python attributes mutated by
`build()`, `get_cached()`, and `invalidate()` without any lock. In production, `build()` is
called from `RecordingCoreService._stop_recording_phase_c()` (the audio-stop thread) while
`get_cached()` / `invalidate()` can be called concurrently from IPC handler threads
(`_handle_get_auto_glossary`, `_handle_refresh_auto_glossary`).

A TOCTOU race exists between `_is_cache_valid()` (reads `_cache` and `_cache_built_at`) and the
subsequent write of `self._cache = terms` — two threads can both see a stale cache, both call
`_build_from_history()`, and one result silently overwrites the other.

The existing test `test_concurrent_build` tests for exceptions but not for cache corruption from
interleaved writes.

**Note:** W1041 added an `RLock` to `SearchIndex` for the same TOCTOU pattern. `AutoGlossaryBuilder`
was not updated in that wave.

**Fix:** Add `self._lock = threading.RLock()` in `__init__` and wrap `build()`, `get_cached()`,
`invalidate()`, and `_is_cache_valid()` with `with self._lock:`.

---

### F3 — LOW: `source_text` (pre-anonymization raw text) preferred over `text`

**File:** `KrabEar/core/auto_glossary.py` line 306

**Description:**
`_build_from_history` reads:
```python
raw_text = str(item.get("source_text", "") or item.get("text", "") or "").strip()
```

`source_text` is the raw pre-LLM-rewrite transcript — it has not gone through
`TextAnonymizer` (PII redaction for phone, email, credit card). The interaction with W1011
(`TextAnonymizer`) means that PII present in `source_text` can be extracted as a "proper noun"
term and injected into the Whisper `initial_prompt`.

Example: a transcript `"позвони мне на 8-926-123-45-67"` — the phone number digits pass
`_is_capitalized_or_multiword()` (contains digits) and would enter the glossary.
Similarly an email address `"user@domain.com"` has uppercase letters and passes.

**Fix:** Prefer `text` (post-cleanup, post-anonymization) over `source_text`. Change to:
```python
raw_text = str(item.get("text", "") or item.get("source_text", "") or "").strip()
```

**Note:** W1024 privacy_mode guards disk persistence but does not affect which field is used
for in-memory extraction. This is a distinct gap.

---

### F4 — LOW: No upper bound on `top_n` / `window_days` from IPC; amplified scan_limit

**File:** `KrabEar/core/auto_glossary.py` lines 216–217, 276;
`KrabEar/backend/recording_core_service.py` lines 914–915

**Description:**
`build(window_days, top_n)` has no validation. `recording_core_service` reads both values from
settings with `int(...)` cast but no clamp:

```python
_ag_window_days = int(_cached_settings_ag.get("auto_glossary_window_days", ...))
_ag_top_n = int(_cached_settings_ag.get("auto_glossary_top_n", ...))
```

A misconfigured or adversarially set `top_n=5000` causes `scan_limit = max(500, 5000 * 20) = 100_000`
items to be requested from `StateStore.get_history_page()`. On a large history (e.g. 50K entries)
this causes:
- Loading ~100K NDJSON records into memory on every transcription stop
- O(N) TermExtractor passes over all items
- Effective DoS of the stop-recording path

`window_days=3650` (10 years) allows unbounded history scanning.

**Fix:** Clamp in `build()`:
```python
top_n = min(top_n, 200)          # hard ceiling
window_days = min(window_days, 365)  # max 1 year back
```

---

## Wire status

`get_auto_glossary` and `refresh_auto_glossary` IPC handlers referenced by stub tests
(`_stub_get_auto_glossary`, `_stub_refresh_auto_glossary`) are **not wired** in `service.py`.
W1012 flagged this as F2/F3 (missing handlers); they are still absent. This is a pre-existing
gap, not a regression — the auto-glossary works implicitly via `recording_core_service` but
is not introspectable via IPC.

## Interaction with W1041 (SearchIndex RLock)

W1041 added an `RLock` to `SearchIndex` because the same TOCTOU pattern in concurrent
cache reads/writes was found there. `AutoGlossaryBuilder` uses an identical pattern
(`_is_cache_valid()` + `_cache = terms` without lock) but was not updated in W1041.
F2 above is the direct consequence.

## Interaction with W1011 (PII patterns)

`TextAnonymizer` (W1011) redacts PII in the `text` field after the LLM rewrite pass.
`source_text` bypasses this path. F3 above is the interaction gap.

## Test coverage

Existing tests (57 in `test_auto_glossary.py`) cover: empty/error, extraction, date filter,
top_n, cache hit/force/expiry, disk persistence, IPC stubs, concurrency (exception-only).

**Not covered:**
- Newline/control-char in term (F1)
- Concurrent `build()` + `invalidate()` checking cache values not just exception absence (F2)
- Source-text field preference when anonymized `text` differs from `source_text` (F3)
- top_n / window_days amplification (F4)

## Recommendations (priority order)

1. **F2** (RLock) — add `threading.RLock()` to match W1041 SearchIndex fix. Low effort.
2. **F1** (newline sanitization) — add `term = term.replace("\n", " ").replace("\r", "").strip()` + `len(term) <= 80` guard in `_build_from_history`.
3. **F3** (field order) — swap `source_text`/`text` preference in `_build_from_history`.
4. **F4** (clamp) — add `min(top_n, 200)` and `min(window_days, 365)` guards in `build()`.
