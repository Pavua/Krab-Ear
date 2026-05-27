# Audit: core/auto_glossary.py — sixth-pass (W1450)

**Date:** 2026-05-27
**Auditor:** W1450 sub-agent (Sonnet 4.6)
**File:** `KrabEar/core/auto_glossary.py`
**Base branch:** `codex/krab-ear-v2` (HEAD `4eb8356f`)

---

## Merge state matrix — all 9 prior waves

| Wave | Description | Commit | On `codex/krab-ear-v2`? |
|------|-------------|--------|------------------------|
| W1012 | Initial audit doc (6 findings) | `1939234c` | YES (doc only) |
| W1024 | Atomic write + privacy_mode guard (W1012 F1+F4) | `33da9fbc` | **NO** — branch `fix-auto-glossary-atomic-privacy-W1024`, never PR'd |
| W1098 | Residual audit doc (4 findings post-W1024) | `5fe74671` | YES (doc only, #1013) |
| W1104 | IPC handler wiring | — | **NOT FOUND** on any branch near the code |
| W1288 | Residual re-audit doc (5 findings, W1024/W1104 NOT merged) | `5b233714` | YES (doc only, #1194) |
| W1292 | Wire `AutoGlossary.invalidate()` into history+recording persist | `1aeef4b3` | YES (#1201) |
| W1293 | transcript_context 560-char cap | `6fb82d5a` | YES (#1196) |
| W1294 | Settings_provider + filler bigrams | `30270d63` | YES (#1202) |
| W1402 | Fifth-pass audit doc (5 findings) | `cb624970` | YES (doc only, #1303) |
| W1417 | RLock + type validation (W1402 F1 MED + F2 LOW) | `09707e3a` | **NO** — branch `fix-auto-glossary-rlock-W1417`, never PR'd |
| W1418 | Remove bare "корректор" from hallucination patterns (W1402 F3) | `5aa39e98` | YES (#1316) |

**Summary of unmerged fixes:** W1024 (atomic write + privacy_mode), W1417 (RLock + type validation) remain unmerged.

---

## New findings (W1450)

### F1 — CRITICAL: `_load_cache_from_disk` assigns non-list to `self._cache` → `KeyError` crash on `build()`

**Severity:** HIGH (reproducible crash in production)
**File:** `KrabEar/core/auto_glossary.py`, `_load_cache_from_disk()` (line 411)

`_load_cache_from_disk()` does `self._cache = data.get("terms", [])` without validating
the type. If `auto_glossary.json` was written by external tooling or a future format that
stores `"terms"` as a `dict` (e.g. `{"TensorFlow": 3}`) or a plain string, `self._cache`
becomes a non-list. Then:

1. `_is_cache_valid()` returns `True` — `if not self._cache` is `False` for a non-empty dict.
2. `build()` executes `return list(self._cache[:top_n])` — **dict slicing raises `KeyError: slice(None, 30, None)`**.

Confirmed with Python 3.12:

```python
# Reproduce: write cache with dict instead of list
cache_file.write_text('{"terms": {"TensorFlow": 3}, "built_at": 9999999999.0}')
b = AutoGlossaryBuilder(store=store, data_dir=...)
b.build()  # → KeyError: slice(None, 30, None)
```

This is the W1402 F2 / W1417 F2 finding that was fixed in the unmerged `fix-auto-glossary-rlock-W1417`
branch but is still live in production. The fix adds `isinstance(raw_terms, list)` validation
before assigning to `self._cache`.

**Fix:** In `_load_cache_from_disk()`, validate `isinstance(raw_terms, list)` before assignment;
log a warning and fall back to `[]` for non-list types.

---

### F2 — MED: TOCTOU race — `build()` writes stale result after concurrent `invalidate()`

**Severity:** MED
**File:** `KrabEar/core/auto_glossary.py`, `build()` (lines 275–303)

No threading lock protects `_cache` / `_cache_built_at`. The IPC server runs each request
in its own thread. The following sequence is exploitable:

```
Thread A: build(force=True) — passes cache check, starts _build_from_history (~100–500 ms)
Thread B: invalidate() — sets _cache=[], _cache_built_at=0.0
Thread A: [build_from_history returns] — overwrites _cache with stale results
```

`invalidate()` is called from two IPC paths (`refresh_auto_glossary` and `history_persist` hooks).
The race window is wide because `_build_from_history` involves a `get_history_page()` store
call plus TermExtractor over potentially 500 history items.

The unmerged `fix-auto-glossary-rlock-W1417` adds `threading.RLock` and wraps `build()`,
`get_cached()`, `invalidate()`, and `_is_cache_valid()`. That PR resolves this.

The existing `test_concurrent_build` (line 566) only tests concurrent `build(force=True)` calls
without any `invalidate()` calls — the actual TOCTOU is not covered.

**Fix:** Add `threading.RLock` (`self._lock`) and wrap all mutation/read paths as done in W1417.

---

### F3 — MED: Non-atomic `_save_cache_to_disk()` — partial file on crash/SIGKILL

**Severity:** MED (data integrity on power loss)
**File:** `KrabEar/core/auto_glossary.py`, `_save_cache_to_disk()` (lines 423–438)

`_save_cache_to_disk()` uses `path.write_text(...)` directly. On macOS, `write_text` opens the
file, truncates it, and writes the payload. A SIGKILL or power loss between truncation and
the final write leaves `auto_glossary.json` as a zero-byte or partial JSON file.

The next startup reads this partial file in `_load_cache_from_disk()` and hits the `json.loads()`
exception path — `self._cache` is reset to `[]` and the built-at timestamp is zeroed. This forces
an immediate full rebuild on the next `build()` call. While not catastrophic, it silently degrades
STT quality until the rebuild completes.

The unmerged `fix-auto-glossary-atomic-privacy-W1024` fixes this with `tmp+fsync+os.replace`.

**Fix:** Replace `path.write_text(...)` with `tempfile.mkstemp(dir=dir) → fh.write → fh.flush → os.fsync → os.replace`.

---

### F4 — LOW: `settings_provider` path in `build()` has zero test coverage

**Severity:** LOW
**File:** `KrabEar/core/auto_glossary.py` lines 264–301; `KrabEar/tests/test_auto_glossary.py`

W1294 added `settings_provider` and the privacy-mode guard to `build()` (lines 264–301).
Neither the privacy-mode path nor the `settings_provider` exception-handling fallback has
any test coverage in `test_auto_glossary.py`. Searching for `settings_provider`, `privacy`,
and `privacy_mode_enabled` in the test file returns zero results.

The omission means:
- Privacy bypass via the `settings_provider` exception path is untested (fail-open behaviour).
- The double `settings_provider()` call inside `build()` (lines 266 + 293) — once for the
  early return, once for the disk-persist guard — has no regression protection.
- The `invalidate()` method does NOT honour `settings_provider`; it unconditionally calls
  `_save_cache_to_disk()`, meaning an empty-list file is written to disk even when privacy
  mode is active (harmless but inconsistent with the stated contract).

**Fix:** Add test class `TestPrivacyModeGuard` covering:
  - `build()` returns `[]` when `settings_provider` returns `privacy_mode_enabled=True`
  - `build()` does not write to disk when privacy mode is active
  - `settings_provider` raising an exception → fall-open (terms returned normally)

---

### F5 — LOW: PII terms (phone numbers, account-number tokens) pass all filters and enter STT prompt

**Severity:** LOW (privacy / quality)
**File:** `KrabEar/core/auto_glossary.py`, `_is_capitalized_or_multiword()` (lines 30–53)

`_is_capitalized_or_multiword()` accepts any token that contains a digit:

```python
if any(c.isdigit() for c in term):
    return True
```

If a transcript contains a spoken phone number that Whisper transcribes as a token
(e.g. `"79165551234"`, `"IBAN1234"`, or `"+79165551234"`), `TermExtractor` may
surface it as a unigram with frequency ≥ 1. That token passes all three guards
(`_is_stop_word`, `_is_capitalized_or_multiword`, `_looks_like_hallucination`) and
ends up in the glossary — and is then injected verbatim into the Whisper
`initial_prompt` via `build_initial_prompt()`.

Confirmed via Python 3.12:
```python
extractor.extract_terms("Позвони на IBAN1234 завтра")
# → ExtractedTerm(term='IBAN1234', frequency=1)
_is_capitalized_or_multiword('IBAN1234')   # True (has digits + ≥2 uppercase)
_is_capitalized_or_multiword('+79165551234')  # True (has digits)
```

The `TextAnonymizer` in `core/text_anonymizer.py` (phone, email, credit-card rules)
is never called on extracted terms — only on full transcript text in the
`TextPostProcessor` pipeline, which runs after transcription. The auto-glossary has
no anonymization step.

**Fix:** Add a regex guard in `_build_from_history()` (or in `_is_capitalized_or_multiword()`)
to reject tokens that match phone/IBAN/credit-card patterns before they enter the frequency counter.
Alternatively, apply `TextAnonymizer.anonymize()` to the source text before calling
`extract_terms()` in `_build_from_history()`.

---

## Test coverage summary

| Area | Covered? |
|------|---------|
| Empty / error history | YES |
| Date filtering | YES |
| Capitalized/multiword filter | YES |
| Filler-starter bigrams (`_starts_with_filler`) | NO test in test_auto_glossary.py |
| Hallucination filter | NO direct test (covered by integration path only) |
| Disk persistence (save/load/corrupt) | YES |
| Cache hit / expiry / force | YES |
| Concurrent `build()` | YES (16 threads, no `invalidate()`) |
| Concurrent `build()` + `invalidate()` TOCTOU | **NO** |
| `settings_provider` / privacy mode | **NO** |
| Non-list `terms` in disk cache → crash | **NO** |
| PII (phone/IBAN) in extracted terms | **NO** |

---

## Recommendations

Priority order:
1. **F1 HIGH** — Merge W1417 (`fix-auto-glossary-rlock-W1417`); contains both the type-validation
   fix (F1 of this audit) and the RLock fix (F2 of this audit).
2. **F3 MED** — Merge W1024 (`fix-auto-glossary-atomic-privacy-W1024`) for atomic disk write.
3. **F4 LOW** — Add `TestPrivacyModeGuard` tests covering `settings_provider` path.
4. **F5 LOW** — Add digit-sequence / phone-pattern guard before terms enter the frequency counter.
