# Wave 1402 — AutoGlossary Fifth-Pass Audit

**Date:** 2026-05-27  
**Auditor:** W1402 (sub-agent, fifth pass)  
**File audited:** `KrabEar/core/auto_glossary.py`  
**Branch base:** `codex/krab-ear-v2` (HEAD `148804b2` after fetching merged PRs)

---

## Prior wave merge state (verified 2026-05-27)

| Wave | PR | State | What it fixed |
|------|----|-------|---------------|
| W1024 | #946 | **OPEN** | Atomic write via tmp+os.replace+fsync; privacy_mode guard in `_save_cache_to_disk` |
| W1104 | #1023 | **OPEN** | Wire `get_auto_glossary` + `refresh_auto_glossary` IPC handlers |
| W1288 | #1194 | **OPEN** (docs only) | Re-audit identifying F1–F5 |
| W1292 | #1201 | **MERGED** 2026-05-27 | `invalidate()` wired into `HistoryService.add_history_item` + `RecordingCoreService` persist |
| W1293 | #1196 | **MERGED** 2026-05-27 | `transcript_context.py` 560-char cap + `_MAX_PROMPT_CHARS` constant |
| W1294 | #1202 | **MERGED** 2026-05-27 | `settings_provider` constructor param; `_FILLER_STARTERS` + `_starts_with_filler()` |

W1292/W1293/W1294 were merged the same day this audit was opened. The worktree was updated
via `git pull` before analysis. W1024 and W1104 remain unmerged — their fix branches exist
(`fix-auto-glossary-atomic-privacy-W1024`, `wire-auto-glossary-ipc-W1104`) and the fixes have
been confirmed implemented there, but neither PR has landed in `codex/krab-ear-v2` yet.

---

## Current file state summary

The merged W1294 adds `settings_provider` and `_FILLER_STARTERS`/`_starts_with_filler()`.
The merged W1292 wires `invalidate()` on history persist.
The merged W1293 caps the initial_prompt at 560 chars in `transcript_context.py`.

`_save_cache_to_disk()` (line 423) still uses `path.write_text(...)` directly — the W1024
atomic-write fix is **not** in the codebase. The IPC handlers `get_auto_glossary` and
`refresh_auto_glossary` are **not** registered in `service.py` — the W1104 fix is not merged.

---

## New findings (F1–F5)

### F1 — MED — TOCTOU race: build() + invalidate() with privacy toggle

**Location:** `auto_glossary.py` lines 264–302 (`build()`) and 309–314 (`invalidate()`)

**Description:**  
The IPC server uses thread-per-connection (`ipc_server.py` line 81). Two concurrent IPC
requests — one triggering `build(force=True)` (e.g. `refresh_auto_glossary`) and another
toggling privacy mode followed by an `invalidate()` — can race:

1. Thread A starts `build()`, passes the privacy guard (privacy OFF), enters
   `_build_from_history()` (slow, scans history).
2. Thread B: user toggles privacy ON; `invalidate()` is called — sets `_cache = []`
   and `_cache_built_at = 0.0`.
3. Thread A finishes `_build_from_history()`, sets `self._cache = terms` and
   `self._cache_built_at = time.time()`.

Result: the invalidation is silently undone. The now-live cache was built from pre-privacy
history and will persist until `_refresh_hours` expires. No lock guards these two attributes.

The existing `test_concurrent_build` test (line 566 of `test_auto_glossary.py`) tests 16
concurrent `build(force=True)` calls but does NOT test concurrent `build` + `invalidate`.

**Fix:** Add `threading.RLock` to `AutoGlossaryBuilder.__init__`. Acquire it in `build()`,
`invalidate()`, `get_cached()`, `_is_cache_valid()`, and `_load_cache_from_disk()`.

---

### F2 — LOW — `_load_cache_from_disk()` accepts unvalidated `terms` value

**Location:** `auto_glossary.py` line 411

```python
self._cache = data.get("terms", [])
```

**Description:**  
The JSON is parsed with a try/except, but the value of `terms` is assigned without type
validation. A tampered or malformed `auto_glossary.json` (e.g. written by another process or
a test that left corrupted data) with `{"terms": 123}` or `{"terms": ["valid", null, 99]}`
would set `self._cache` to a non-`list[str]`, bypassing the type annotation. Downstream:

- `list(self._cache[:top_n])` succeeds silently.
- `build_initial_prompt()` in `transcript_context.py` calls `", ".join(combined_terms)`
  (line 166), which raises `TypeError: sequence item N: expected str instance, NoneType found`
  if any element is non-string.

The existing `test_corrupt_disk_cache_handled` test (line 306) only tests invalid JSON, not
structurally valid JSON with wrong-typed values.

**Fix:** After parsing, validate: `isinstance(data.get("terms"), list)` and filter to strings.

---

### F3 — LOW — `"корректор"` hallucination pattern is too broad

**Location:** `auto_glossary.py` line 65

```python
"корректор",
```

**Description:**  
`_looks_like_hallucination()` uses a substring match (line 149):
```python
for pattern in _HALLUCINATION_PATTERNS:
    if pattern in t:
        return True
```
`"корректор"` is a single common Russian word meaning "proofreader" or "corrector". Any
transcript that legitimately contains this word (publishing workflows, grammar checking
discussions, OCR software discussions) will cause `_looks_like_hallucination()` to return
`True` for that term and all multiword terms containing it (e.g. `"орфографический
корректор"`, `"корректор правописания"`).

Unlike `"субтитры подготовил"` or `"редактор субтитров"` which are clearly subtitle
artefacts, `"корректор"` is genuine Russian vocabulary used in professional contexts. The
W1024 comment says this is from `https://github.com/openai/whisper/discussions/928`, but
the actual Whisper artefact there is `"корректор субтитров"` — the shorter form over-fires.

**Fix:** Replace `"корректор"` with `"корректор субтитров"` (the specific subtitle artefact
form), or remove it and rely on `"субтитры подготовил"` / `"редактор субтитров"` which
already cover the subtitle-credit artefact pattern.

---

### F4 — LOW — `invalidate()` skips privacy guard before writing disk

**Location:** `auto_glossary.py` lines 309–314

```python
def invalidate(self) -> None:
    self._cache = []
    self._cache_built_at = 0.0
    if self._data_dir:
        self._save_cache_to_disk()
```

**Description:**  
`invalidate()` unconditionally writes `{terms: [], built_at: 0.0}` to disk even when
privacy mode is active. While writing an empty list is not a user-data leak, it is
inconsistent with `build()`'s belt-and-suspenders pattern of double-checking privacy
before any disk write (lines 289–300). More importantly, if the disk cache previously
contained terms (from before privacy mode was enabled), `invalidate()` should overwrite it
with an empty file — this is actually the correct privacy-preserving behavior. However,
the absence of a privacy check means that the intent is unclear and future refactors could
introduce regressions. The fix is trivially: check `_is_privacy_mode_active()` before the
disk write in `invalidate()`, mirroring the guard in `build()`.

Note: the `_is_privacy_mode_active()` helper exists in the W1024 worktree but is not yet
merged; once W1024 merges this becomes a one-line addition.

---

### F5 — LOW — No test for `source_text`-preferred term extraction path

**Location:** `auto_glossary.py` line 359–361; `tests/test_auto_glossary.py`

```python
raw_text = str(
    item.get("source_text", "") or item.get("text", "") or ""
).strip()
```

**Description:**  
`_build_from_history()` prefers `source_text` over `text`. In production, both fields are
set to the same normalized transcript (pre-LLM-rewrite) by `RecordingCoreService` — but the
field semantics imply `source_text` could diverge from `text` (e.g. if LLM rewriting modifies
`text`). The current test helper `_make_item()` always sets `source_text = text` (line 33),
so the preference logic is never exercised in tests. A test with `source_text != text` should
verify that (a) `source_text` takes priority, and (b) term extraction uses the right content.

This also serves as documentation: the intent (use raw STT output for term extraction rather
than LLM-rewritten text) should be made explicit with a comment or test.

---

## Test coverage gaps noted (not new findings)

- No test covers concurrent `build()` + `invalidate()` racing (F1 above).
- No test covers `_load_cache_from_disk()` with structurally valid but wrong-typed JSON (F2).
- The `"корректор"` hallucination pattern is not tested for false-positive suppression (F3).

---

## Summary table

| ID | Severity | File | Description |
|----|----------|------|-------------|
| F1 | MED | `core/auto_glossary.py` | TOCTOU race: concurrent `build()` + `invalidate()` — missing `threading.RLock` |
| F2 | LOW | `core/auto_glossary.py` | `_load_cache_from_disk()` assigns unvalidated `terms` — downstream TypeError risk |
| F3 | LOW | `core/auto_glossary.py` | `"корректор"` hallucination pattern too broad — legitimate RU word false-positive |
| F4 | LOW | `core/auto_glossary.py` | `invalidate()` lacks privacy guard before disk write — inconsistency with `build()` |
| F5 | LOW | `core/auto_glossary.py` + tests | No test for `source_text != text` priority path in `_build_from_history()` |

**Still-open from prior waves:** W1024 (atomic write, PR #946 OPEN), W1104 (IPC handlers, PR #1023 OPEN).
