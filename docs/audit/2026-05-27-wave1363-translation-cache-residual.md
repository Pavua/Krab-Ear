# Wave 1363 — Translation Cache Residual Audit

**Date:** 2026-05-27  
**Scope:** `KrabEar/backend/translation_cache.py` (121 lines) + interaction with
`translator.py`, `translation_service.py`; post-W1190/W1318/W1319 re-audit.  
**Status:** Read-only audit — 5 new findings

---

## Merge State of Prior Waves

| Wave | PR | Branch | State |
|------|----|--------|-------|
| W1190 | #1102 | `wire-translation-cache-W1190` | **OPEN — NOT merged** |
| W1318 | #1221 | `fix-translation-cache-key-network-mode-W1318` | **OPEN — NOT merged** |
| W1319 | #1224 | `fix-clear-cache-wipes-disk-W1319` | **OPEN — NOT merged** |
| W938  | #860  | `fix/translation-cache-fsync-W938` | **OPEN — NOT merged** |
| W925  | #849  | `docs/audit-translation-cache-W925` | **OPEN — NOT merged** |

**Current production state on `codex/krab-ear-v2`:**  
`TranslationCache` is instantiated nowhere in the production path. `translation_cache.py`
exists as a module but is never imported by `BackendService`, `TranslationService`, or
`Translator`. All five related fix-PRs are open and unmerged. The module is entirely
inert at runtime on `main`.

---

## Findings

### F1 HIGH — W1318 fixes cache API but W1190 caller never passes `network_mode`

**File:** `translator.py` (W1190 branch, lines 203–208, 247–253, 265–271)  
**Condition:** W1318 adds `network_mode: str = ""` parameter to `_make_key`, `get`, and
`put` in `translation_cache.py`. However, W1190's `_translate_impl` calls both
`self._translation_cache.get(...)` and `self._translation_cache.put(...)` without
passing the `network_mode` argument, which defaults to `""`.

As a result, even if both W1190 and W1318 are merged, every persistent cache entry will
use `network_mode=""` as the key component — a different hash from keys produced with
an actual mode (`"offline_default"`, `"offline_strict"`, `"online_opt_in"`). The privacy
bypass that W1318 claims to fix (W1313 F1 HIGH) remains fully in effect: an
`online_opt_in` result can never be distinguished from an `offline_strict` result in the
persistent cache, because both callers would arrive with `network_mode=""`.

The fix requires W1190 to be updated to pass `normalized_network_mode` to both
`cache.get()` and `cache.put()`.

**Introduced by:** Gap between W1318 (cache.py only) and W1190 (translator.py only) — no
PR bridges them.

---

### F2 HIGH — W1319 `_check_privacy_mode_changed` is dead code — never invoked

**File:** `translator.py` (W1319 branch, line 128)  
**Condition:** W1319 adds `clear_cache()` (clears both in-memory and disk) and
`_check_privacy_mode_changed(privacy_mode_enabled: bool)` to `Translator`. The privacy
transition method sets a `_last_privacy_mode` sentinel and calls `clear_cache()` when
the value changes. However, `_check_privacy_mode_changed` appears exactly once in the
W1319 branch — only at its definition site. Neither `translate()` nor `_translate_impl()`
calls it, and W1319 does not modify `translation_service.py`.

The consequence: the privacy-transition cache wipe that W1319 claims to deliver (W1313
F2 HIGH — "pre-privacy translations persist to disk after `privacy_mode_enabled=True`")
is still inoperative. `_check_privacy_mode_changed` must be called from `translate()`
(passing `privacy_mode_enabled` read from settings) for the wipe to trigger.

**How to reproduce:** After merging W1319, enable privacy mode while `_translation_cache`
is wired; previous translations remain readable from `translation_cache.json`.

---

### F3 MEDIUM — Persistence atomicity race (`clear()` + concurrent `put()`) still open

**File:** `translation_cache.py` (main branch, lines 85–91, 109–120)  
**Condition:** `clear()` releases `self._lock` before calling `self._persist()`.
`_persist()` then re-acquires `self._lock` to take a snapshot. In the window between
these two acquisitions, a concurrent `put()` (e.g. from a live-subtitles background
thread) can insert a new entry. `_persist()` will then snapshot and write that entry to
disk, defeating the privacy wipe semantics of `clear()`.

W938 (PR #860) addresses this with an explicit `snapshot=` parameter that moves the
snapshot inside the original lock scope. W938 is open and not merged; the race remains
live on main.

This finding is a refinement of W925 F5 with a concrete privacy impact: `clear()` called
from a future `Translator.clear_cache()` cannot guarantee an empty disk file if
`live_subs_service` is concurrently producing translations.

---

### F4 MEDIUM — No test coverage for `network_mode` key isolation in persistent cache

**File:** `KrabEar/tests/test_translation_cache.py` (539 lines, main branch)  
**Condition:** The existing test suite (34 cases covering `_make_key`, LRU eviction,
persistence, concurrency, unicode) contains zero tests that:
1. Call `get()` / `put()` with different `network_mode` values and assert that results
   are isolated across modes.
2. Confirm that a `put()` with `network_mode="online_opt_in"` does not produce a hit for
   a `get()` with `network_mode="offline_strict"`.

`test_key_uniqueness_all_params_matter` (line 408) only varies `text`, `source`,
`target`, and `engine` — not `network_mode`, because the main-branch `_make_key` does
not accept that parameter.

When W1318 merges, these tests still pass (the new parameter defaults to `""`), but the
critical isolation behaviour added by W1318 has no regression test. A future refactor of
`_make_key` could silently remove the isolation.

**Fix:** Add 2–3 test cases asserting `get(network_mode="offline_strict")` misses after
`put(network_mode="online_opt_in")` for the same text/source/target/engine tuple.

---

### F5 LOW — No per-value size bound; 5 000-entry limit doesn't bound disk file size

**File:** `translation_cache.py` (main branch, line 20: `_MAX_ENTRIES = 5000`)  
**Condition:** LRU eviction discards entries when `len(self._cache) > _MAX_ENTRIES`.
This bounds entry count but not total file size: a cache containing 5 000 entries of
average 2 000-character translations (plausible for long paragraph translations) produces
a ~10 MB JSON file. A pathological case — e.g., a single very long document translation
of 50 000 characters — counts as one entry and contributes 50 KB to the file, while the
eviction logic discards one 50-character "hello → hola" entry.

No file-size check or value-length cap is performed in `put()`. The `.tmp` → `os.replace`
pattern is already atomic, but writing a 20+ MB JSON file on every `put()` (current
behaviour) stalls the GIL-holding thread for tens of milliseconds on first-write under
macOS APFS metadata operations.

**Recommended fix:** Add a per-value size cap (e.g., `len(result) <= 10_000` bytes)
before inserting into the cache; log and skip oversized entries.

---

## Open Prior-Wave Findings Still Live on Main

| Finding | Wave | PR | Status |
|---------|------|----|--------|
| W925 F2 — No fsync before os.replace | W938 | #860 | Open |
| W925 F3 — Plaintext translations on disk, no privacy purge | W1319 | #1224 | Open |
| W925 F5 — Persist called outside lock (TOCTOU race) | W938 | #860 | Open |
| W1313 F1 — network_mode missing from persistent cache key | W1318 | #1221 | Open |
| W1313 F2 — Privacy transition does not wipe disk cache | W1319 | #1224 | Open (+ F2 above) |
| W1145 F1 — `_cache` (OrderedDict) has no lock, race with live_subs | unmerged | #1054 | Open |

---

## Summary

| # | Severity | Finding |
|---|----------|---------|
| F1 | HIGH | W1318 fixes cache.py API but W1190 caller never passes `network_mode` → privacy bypass persists after both merge |
| F2 | HIGH | W1319 `_check_privacy_mode_changed` is dead code — never called from `translate()` → privacy wipe inoperative |
| F3 | MED | `clear()` + concurrent `put()` atomicity race — can write non-empty file after privacy wipe |
| F4 | MED | No test for `network_mode` key isolation in persistent cache |
| F5 | LOW | No per-value size bound; large translations can inflate disk file without LRU pressure |

**Production risk today:** LOW — `TranslationCache` is not wired in `codex/krab-ear-v2`
production. All findings become active the moment W1190 merges. F1 and F2 are
coordination failures between the three open PRs: each PR fixes one layer but leaves the
adjacent layer unpatched, so the end-to-end privacy contract is still broken even with
all three merged in sequence.
