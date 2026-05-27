# Audit: core/auto_glossary.py — seventh-pass (W1493)

**Date:** 2026-05-27
**Auditor:** W1493 sub-agent (Sonnet 4.6)
**File:** `KrabEar/core/auto_glossary.py`
**Base branch:** `codex/krab-ear-v2` (HEAD `707afb5f`)

---

## Merge state matrix — all prior waves

| Wave | Description | Commit / PR | On `codex/krab-ear-v2`? |
|------|-------------|-------------|------------------------|
| W1012 | Initial audit doc (6 findings) | `1939234c` | YES (doc only) |
| W1024 | Atomic write + privacy_mode guard (W1012 F1+F4) | `33da9fbc` / #946 OPEN | **NO** — branch `fix-auto-glossary-atomic-privacy-W1024`, never merged |
| W1098 | Residual audit doc (4 findings post-W1024) | `5fe74671` / #1013 | YES (doc only) |
| W1104 | IPC handler wiring | — / #1023 OPEN | **NOT FOUND** — branch `wire-auto-glossary-ipc-W1104`, never merged |
| W1288 | Residual re-audit doc (5 findings) | `5b233714` / #1194 | YES (doc only) |
| W1292 | Wire `AutoGlossary.invalidate()` into history+recording persist | `1aeef4b3` / #1201 | YES (MERGED) |
| W1293 | transcript_context 560-char cap | `6fb82d5a` / #1196 | YES (MERGED) |
| W1294 | Settings_provider + filler bigrams | `30270d63` / #1202 | YES (MERGED) |
| W1402 | Fifth-pass audit doc (5 findings) | `cb624970` / #1303 | YES (doc only) |
| W1417 | RLock + type validation (W1402 F1+F2) | `09707e3a` / no PR | **NO** — branch `fix-auto-glossary-rlock-W1417`, never PR'd |
| W1418 | Remove bare "корректор" from hallucination patterns | `5aa39e98` / #1316 | YES (MERGED) |
| W1450 | Sixth-pass audit doc (5 findings) | `45e50590` / #1343 | YES (doc only) |
| W1456 | Strict type validation in `_load_cache_from_disk()` (W1450 F1 HIGH) | `23a7a26a` / #1346 | YES (MERGED) |

**Still-open from prior waves:** W1024 (atomic write), W1417 (RLock + concurrent TOCTOU), W1104 (IPC wiring).

---

## W1456 fix verification

W1456 (commit `23a7a26a`, PR #1346) added strict validation in `_load_cache_from_disk()`:

```python
terms = data.get("terms", [])
if not isinstance(terms, list):
    # logs warning, resets _cache = [], returns
    ...
# Per-entry: accepts plain strings (with truthiness check) and dicts with 'term' str key
for entry in terms:
    if isinstance(entry, str) and entry:
        validated.append(entry)
    elif isinstance(entry, dict) and isinstance(entry.get("term"), str):
        validated.append(entry["term"])   # ← F1 of this audit
```

The `isinstance(terms, list)` guard correctly rejects dict-typed `"terms"` (prevents
`KeyError: slice(None, 30, None)` from W1450 F1). The plain-string guard
(`isinstance(entry, str) and entry`) correctly rejects empty strings.

However, the dict-entry guard (`isinstance(entry.get("term"), str)`) accepts
**any** string including the empty string `""` — no truthiness check. This is F1 below.

---

## New findings (W1493)

### F1 — LOW: W1456 dict-entry guard admits empty-string `"term"` into cache

**Severity:** LOW
**File:** `KrabEar/core/auto_glossary.py`, line 431–432 (`_load_cache_from_disk`)

The W1456 per-entry validation loop (line 431–432):
```python
elif isinstance(entry, dict) and isinstance(entry.get("term"), str):
    validated.append(entry["term"])
```
accepts `{"term": ""}` because `isinstance("", str)` is `True`. The empty string
is appended to `validated` and assigned to `self._cache`.

Confirmed with Python 3.12:
```python
# write cache with dict entry that has empty "term"
cache_file.write_text(json.dumps({
    "terms": ["ValidTerm", {"term": ""}, {"term": "AnotherValid"}],
    "built_at": 9999999999.0,
}))
builder = AutoGlossaryBuilder(store=FakeStore(), data_dir=...)
builder.get_cached()
# → ['ValidTerm', '', 'AnotherValid']   ← empty string in cache!
```

`transcript_context.py` line 167–169 strips and skips empty terms before injecting into
the Whisper `initial_prompt` (`w = w.strip(); if not w: continue`), so no STT-quality
regression occurs. However, the empty string:
1. Pollutes `self._cache` and is written back to disk via `_save_cache_to_disk()`.
2. Makes `len(self._cache)` misleading (inflates the count in debug logs).
3. Is returned by `get_cached()` and from `build()` to any IPC callers inspecting the list.

The companion guard for plain strings does this correctly: `isinstance(entry, str) and entry`.
The dict-entry branch simply needs the same truthiness check on the extracted value.

**Fix:** Change line 431–432 to:
```python
elif isinstance(entry, dict):
    term_val = entry.get("term")
    if isinstance(term_val, str) and term_val:
        validated.append(term_val)
```

---

### F2 — MED: TOCTOU race in `build()` / `invalidate()` — W1417 still unmerged (carry-over)

**Severity:** MED (unresolved from W1402 F1 / W1450 F2)
**File:** `KrabEar/core/auto_glossary.py`, lines 275–303 (`build()`) + 309–314 (`invalidate()`)

No `threading.RLock` protects `self._cache` / `self._cache_built_at`. The IPC server runs
each connection in its own thread (`ipc_server.py` per-client reader threads). The race:

```
Thread A: build(force=True) — passes cache check, enters _build_from_history (~100–500 ms)
Thread B: invalidate() — sets _cache=[], _cache_built_at=0.0
Thread A: returns — overwrites _cache with stale result, undoing the invalidation
```

`invalidate()` is called from two IPC paths inside `RecordingCoreService` after stop-recording
and after history persist hooks (`recording_core_service.py` lines 1230–1234 and 1471).
The `test_concurrent_build` test in `test_auto_glossary.py` (line 566) uses 16 threads
calling `build(force=True)` simultaneously but does NOT test concurrent `build()` + `invalidate()`.

The fix is in the unmerged `fix-auto-glossary-rlock-W1417` branch (`09707e3a`), which adds
`threading.RLock(self._lock)` and wraps all mutation/read paths. That branch also includes
`test_concurrent_build_and_invalidate_safe` (10 threads × 50 interleaved ops).

**Fix:** Merge W1417 branch (`fix-auto-glossary-rlock-W1417`).

---

### F3 — MED: Non-atomic `_save_cache_to_disk()` — W1024 still unmerged (carry-over)

**Severity:** MED (unresolved from W1012 F1 / W1450 F3)
**File:** `KrabEar/core/auto_glossary.py`, lines 446–461 (`_save_cache_to_disk`)

`_save_cache_to_disk()` uses `path.write_text(...)` directly. On macOS, this truncates the
file before writing. A SIGKILL or power loss between truncation and final write leaves
`auto_glossary.json` as a zero-byte or partial JSON file. The next startup reads the
partial file, hits the `json.loads()` exception, resets `_cache = []`, and forces a full
rebuild on the next `build()` call. While not catastrophic, it silently degrades STT
prompt quality until the rebuild completes.

The fix (atomic `tempfile + fsync + os.replace`) is implemented in the unmerged
`fix-auto-glossary-atomic-privacy-W1024` branch. The same branch also adds a
`_is_privacy_mode_active()` helper and an `invalidate()`-level privacy guard.

**Fix:** Merge W1024 branch (`fix-auto-glossary-atomic-privacy-W1024`).

---

### F4 — LOW: PII tokens (IBAN, credit card numbers) pass all filters and enter Whisper prompt

**Severity:** LOW (privacy / STT quality)
**File:** `KrabEar/core/auto_glossary.py`, `_build_from_history()` (lines 365–383)

Confirmed with `TermExtractor` and all three filter functions:

```python
# Actual output from core/auto_glossary.py + core/term_extractor.py
_is_capitalized_or_multiword('IBAN1234')           # True (has digits + ≥2 uppercase)
_is_capitalized_or_multiword('visa4111111111111111')# True (has digits)
_is_capitalized_or_multiword('CH5604835012345678009')# True (has digits + uppercase start)

_looks_like_hallucination('IBAN1234')              # False
_starts_with_filler('IBAN1234')                    # False
```

`TermExtractor` extracts `IBAN1234`, `visa4111111111111111`, and `CH5604835012345678009`
from transcript text with `frequency=1`. All three pass `_is_stop_word`, `_is_capitalized_or_multiword`,
`_looks_like_hallucination`, and `_starts_with_filler` — they enter the `freq` counter and
are returned in `top_terms`. They are then injected verbatim into the Whisper
`initial_prompt` via `build_initial_prompt()` in `transcript_context.py`.

`TextAnonymizer` (`core/text_anonymizer.py`) has phone/IBAN/credit-card redaction rules but
is only called in the `TextPostProcessor` pipeline — never on text being fed to `extract_terms()`
in `_build_from_history()`. No phone number tokenized with `+79...` is extracted by TermExtractor
(the `+` causes the tokenizer to skip), but IBAN-prefixed tokens (`IBAN1234`), credit-card-style
tokens (`visa4111111111111111`), and full IBANs (`CH5604835012345678009`) are extracted.

This finding was first documented in W1450 F5 but remains unresolved.

**Fix (option A — cheapest):** Add a regex guard in `_build_from_history()` before `freq[key] += et.frequency`:
```python
import re
_PII_PATTERN = re.compile(r'^\+?\d{7,}$|^[A-Z]{2}\d{2}[A-Z0-9]{12,}$')  # phone/IBAN-style
if _PII_PATTERN.match(et.term):
    continue
```

**Fix (option B — comprehensive):** Apply `TextAnonymizer.anonymize()` to `raw_text` before
calling `extract_terms()` so that PII is replaced with placeholder tokens before extraction.
This has higher CPU cost (~1 ms per item) but is complete.

---

### F5 — LOW: `_ts_to_epoch()` deferred `calendar` + `datetime` imports on every call

**Severity:** LOW (code quality / style)
**File:** `KrabEar/core/auto_glossary.py`, lines 164–165 (`_ts_to_epoch`)

```python
def _ts_to_epoch(ts: str) -> float:
    import calendar    # ← inside function body
    import datetime    # ← inside function body
    ...
```

`_ts_to_epoch()` is called once per history item in `_build_from_history()` — up to 500 calls
per `build()` invocation (the `scan_limit = max(500, top_n * 20)` ceiling). Python caches
imported modules after the first `import` statement, so subsequent calls are fast (dict lookup
in `sys.modules`). However, this is a style anti-pattern: standard library modules should be
imported at module level to make dependencies explicit and allow static analysis tools
(mypy, flake8-import-order, pyright) to report correctly.

Neither `calendar` nor `datetime` appear in the top-level import block of `auto_glossary.py`
(which imports `json`, `logging`, `time`, `Counter`, `Path`, and the typing aliases).

**Fix:** Move `import calendar` and `import datetime` to the module-level import block (lines 9–18).

---

## Test coverage summary (post W1456)

| Area | Status |
|------|--------|
| `terms` as dict → reset to `[]` | YES (W1456, `test_load_dict_terms_returns_empty`) |
| Mixed list — int/None/empty-str dropped | YES (W1456, `test_load_mixed_valid_and_garbage_filtered`) |
| Dict entry with `{"term": ""}` admitted | **NO** — F1 of this audit |
| Concurrent `build()` + `invalidate()` TOCTOU | **NO** — W1417 not merged |
| Non-atomic disk write survives SIGKILL | **NO** — W1024 not merged |
| PII (IBAN, credit card) rejected from glossary | **NO** — W1450 F5 still open |
| `_ts_to_epoch` module-level imports | **NO** — F5 of this audit (style only) |

---

## Recommendations (priority order)

| Priority | Finding | Action |
|----------|---------|--------|
| 1 | F2 MED — TOCTOU race | Merge `fix-auto-glossary-rlock-W1417` |
| 2 | F3 MED — non-atomic write | Merge `fix-auto-glossary-atomic-privacy-W1024` |
| 3 | F1 LOW — empty-string in dict-term | Fix line 431–432: add truthiness check on `term_val` |
| 4 | F4 LOW — PII tokens in Whisper prompt | Add `_PII_PATTERN` regex guard in `_build_from_history()` |
| 5 | F5 LOW — deferred imports | Move `calendar`/`datetime` to module-level |
