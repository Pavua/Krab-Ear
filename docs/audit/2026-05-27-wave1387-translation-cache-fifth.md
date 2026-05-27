# Wave 1387 — Translation Cache Fifth Re-Audit

**Date:** 2026-05-27
**Scope:** `KrabEar/backend/translation_cache.py` (121 lines, main branch) +
`fix-translation-cache-key-toctou-W1371` branch (170 lines); post-W925/W938/W1313/W1363
re-audit.
**Status:** Read-only audit — 5 new findings

---

## Merge State of Prior Waves

| Wave | Branch | Commit | State |
|------|--------|--------|-------|
| W925 | `docs/audit-translation-cache-W925` | baaec098 | **NOT merged to codex/krab-ear-v2** |
| W938 | `fix/translation-cache-fsync-W938` | cf110bdd | **NOT merged to codex/krab-ear-v2** |
| W1313 | `audit/translator-post-W1313` | 2b458d84 | **NOT merged to codex/krab-ear-v2** |
| W1318 | `fix-translation-cache-key-network-mode-W1318` | dee14de2 | **NOT merged to codex/krab-ear-v2** |
| W1319 | `fix-clear-cache-wipes-disk-W1319` | a5183ded | **NOT merged to codex/krab-ear-v2** |
| W1363 | `audit-translation-cache-residual-W1363` | bdb19ab9 | **NOT merged to codex/krab-ear-v2** |
| W1371 | `fix-translation-cache-key-toctou-W1371` | a935b616 | **NOT merged to codex/krab-ear-v2** |

**Current production state on `codex/krab-ear-v2`:**
`TranslationCache` is still the original 121-line version. It is not imported by
`BackendService`, `TranslationService`, or `Translator` on main — the module is entirely
inert at runtime. All seven related fix/audit PRs remain open and unmerged. All prior
findings from W925, W938, W1313, W1318, W1319, W1363, and W1371 are still active in the
production codebase.

The W1371 branch (`fix-translation-cache-key-toctou-W1371`) proposes two fixes that
address W1363 F2 (network_mode in key) and F3 (put TOCTOU). This audit examines NEW
residual issues in both the current main-branch code and the W1371 proposed fix.

---

## Findings

### F1 HIGH — W1371 `_persist_locked()` drops `fsync` that W938 explicitly adds

**File:** `fix-translation-cache-key-toctou-W1371` branch,
`KrabEar/backend/translation_cache.py`, `_persist_locked()` (lines 143–158)

**Condition:** W938 (PR #860, branch `fix/translation-cache-fsync-W938`) addresses W925 F2
by adding `fh.flush(); os.fsync(fh.fileno())` inside the `.tmp` write block before
`os.replace`. W1371 refactors `_persist()` into `_persist_locked()` and introduces a new
`_persist()` compatibility wrapper, but neither method calls `fh.flush()` or `os.fsync()`.

Since W938 and W1371 both modify `_persist()` in incompatible ways (they cannot be
rebased onto each other cleanly — both replace the same `_persist()` function), exactly
one of them can be merged. If W1371 is selected as the merge candidate (it addresses the
higher-severity W1363 F2+F3 findings), W925 F2 (fsync durability) silently regresses to
the pre-W938 state: on macOS APFS with write-behind page cache, a power loss between the
`json.dump` return and the kernel flush can leave `translation_cache.json` as a
partially-written (truncated or empty) file even though `os.replace` appeared to succeed.

The fix is a one-line addition inside `_persist_locked()`:
```python
with open(tmp_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False)
    fh.flush()
    os.fsync(fh.fileno())   # <- add this line
os.replace(tmp_path, self._path)
```

**Impact on current main branch:** W925 F2 is also unpatched on main; this finding is
specific to the *W1371 branch* as the merge candidate — it will regress an already-open
finding if W1371 is merged without incorporating W938's fsync.

---

### F2 MEDIUM — `_load()` performs no per-entry value type validation

**File:** `KrabEar/backend/translation_cache.py` (main branch), `_load()` lines 95–107;
same issue present in W1371 branch `_load()` lines 107–136.

**Condition:** After parsing `translation_cache.json`, `_load()` calls
`list(data.items())[-self._max_entries:]` and assigns the result directly to
`self._cache = OrderedDict(items)`. The only structural validation performed is
`isinstance(data, dict)` (main branch) or `isinstance(entries, dict)` + version check
(W1371 branch). No per-item check verifies that each value is a `str`.

A corrupted, externally modified, or future-version-written JSON could contain entries
with `None`, `int`, `list`, or `dict` values. These would be stored in `_cache` without
error. `get()` would then return the non-string value, violating the declared return type
`Optional[str]`. The caller in `translator.py` (when eventually wired) would receive a
non-string silently — likely causing an `AttributeError` or type-confusion downstream in
`Translator._translate_impl()`.

**Reproduce:**
```python
import json, tempfile, os
tmpdir = tempfile.mkdtemp()
p = os.path.join(tmpdir, "translation_cache.json")
with open(p, "w") as f:
    json.dump({"somehash": None, "otherhash": 42}, f)
cache = TranslationCache(data_dir=tmpdir)
result = cache.get("text", "en", "ru", "engine")
# If "text" happened to hash to "somehash", get() returns None — looks like a miss
# If it hashes to "otherhash", get() returns 42 — wrong type silently
```

**Fix:** Add a value type filter in `_load()`:
```python
items = [(k, v) for k, v in entries.items() if isinstance(k, str) and isinstance(v, str)]
items = items[-self._max_entries:]
```

---

### F3 MEDIUM — `_persist_locked()` holds `self._lock` across blocking file I/O — get() latency regression

**File:** `fix-translation-cache-key-toctou-W1371` branch,
`KrabEar/backend/translation_cache.py`, `put()` (lines 77–97), `clear()` (lines 107–116)

**Condition:** The W1371 fix resolves the put() TOCTOU race (W1363 F3) by keeping
`self._lock` held across both the OrderedDict mutation and the full `_persist_locked()`
call. While this eliminates the race, it introduces a new latency hazard: every `get()`
call blocks until the concurrent `put()` finishes writing to disk.

For a fully-loaded 5000-entry cache the `json.dump()` serialization alone takes
approximately 5–15 ms on macOS (M4 Max, APFS, ~1 MB payload). During this window all
`get()` calls from the live-subtitles background thread (`live_subs_service.py`) are
queued. The original implementation deliberately called `_persist()` *outside* the lock
(at the cost of the TOCTOU race) to avoid this blocking.

The original W925 design recommendation and the W938 approach both pass a pre-taken
`snapshot` dict to `_persist()` so disk I/O happens outside the lock. W1371 abandons
this approach.

**Impact:** Not a data-corruption issue, but a latency regression that degrades real-time
subtitle throughput under concurrent translation load. Not flagged in W925, W938, W1313,
or W1363.

**Fix recommendation:** separate the snapshot from the disk-write as in W938:
```python
with self._lock:
    ...  # mutate _cache
    snapshot = dict(self._cache)
# Release lock, then write
self._persist(snapshot=snapshot)
```

---

### F4 MEDIUM — Silent data loss on upgrade: W1371 v1 cache discarded with no backup

**File:** `fix-translation-cache-key-toctou-W1371` branch,
`KrabEar/backend/translation_cache.py`, `_load()` lines 120–136

**Condition:** W1371 bumps the on-disk format from v1 (plain `{key: value}` dict) to v2
(`{"version": 2, "entries": {...}}`). On first startup after deploying W1371, `_load()`
detects the v1 format and logs `INFO: "translation_cache.json версии unknown устарел
(текущая v2) — кэш сброшен для чистой инвалидации."` then discards all v1 entries
without creating a backup.

The *next* `put()` call overwrites the original v1 file with a v2 file containing only
the new entry. All previously cached translations are silently lost. For a production
system with months of accumulated cache entries (up to 5000 × average 200 chars = ~1 MB),
this is a one-time loss of potentially significant translation work.

There is no mechanism to:
1. Migrate v1 entries to v2 keys (a bulk re-hash with `network_mode=""` appended);
2. Rename the v1 file to `translation_cache.v1.json.bak` before discarding;
3. Warn the user at a higher log level (e.g. `WARNING` instead of `INFO`).

**Note:** The design choice to discard v1 entries is intentional (the commit message states
"v1 plain-dict files are silently discarded for clean invalidation of incompatible keys").
However, the INFO severity and lack of backup are unintentional gaps.

**Fix recommendation:** Elevate log level to `WARNING`. Optionally rename the v1 file:
```python
bak_path = self._path + ".v1.bak"
os.rename(self._path, bak_path)
logger.warning("Старый кэш сохранён в %s, новый начат пустым.", bak_path)
```

---

### F5 LOW — No regression test for `clear()` atomicity fix in W1371

**File:** `KrabEar/tests/test_translation_cache.py` (539 lines, main branch)

**Condition:** W1363 F3 identifies a race condition: `clear()` releases `self._lock`
before calling `_persist()`, allowing a concurrent `put()` to insert an entry between
the `_cache.clear()` and the disk write. W1371 addresses this by moving `_persist_locked()`
inside `clear()`'s `with self._lock:` block.

The W1371 commit message states the fix passes 51 existing tests, but neither the current
`test_translation_cache.py` on `codex/krab-ear-v2` nor the W1371 branch's updated test
file contains a test case that:

1. Starts a background `put()` thread looping continuously;
2. Calls `clear()` from the main thread;
3. Asserts that after `clear()` returns, the disk file (`translation_cache.json`) contains
   an empty entries dict — not an entry inserted by the concurrent `put()` *after*
   `clear()` committed to disk.

Without this test, a future refactor that moves `_persist_locked()` back outside the lock
(e.g. for the latency reason cited in F3 above) would silently reintroduce the W1363 F3
race without any test failure.

**Fix recommendation:** Add one integration test:
```python
def test_clear_atomically_empties_disk_under_concurrent_put(self):
    cache = TranslationCache(data_dir=self._tmpdir)
    stop = threading.Event()
    def putter():
        n = 0
        while not stop.is_set():
            cache.put(f"t{n}", "en", "ru", "e", f"v{n}")
            n += 1
    t = threading.Thread(target=putter, daemon=True)
    t.start()
    time.sleep(0.02)
    cache.clear()
    stop.set(); t.join(timeout=1)
    # After clear(), reloaded cache must be empty
    cache2 = TranslationCache(data_dir=self._tmpdir)
    self.assertEqual(cache2.get_stats()["entries"], 0,
                     "clear() must atomically empty the disk file")
```

---

## Open Prior-Wave Findings Still Live on Main

All prior findings remain open. Cumulative open backlog:

| Finding | Wave | Status |
|---------|------|--------|
| W925 F1 — Cache stampede (100 concurrent misses) | W925 | Open (no fix branch) |
| W925 F2 — No fsync before os.replace | W938 | Fix branch open, not merged |
| W925 F3 — Plaintext translations on disk (privacy) | W1319 | Fix branch open, not merged |
| W925 F5 — _persist() outside lock (TOCTOU) | W938/W1371 | Both fix branches open, not merged |
| W1313 F1 — network_mode not in cache key | W1318/W1371 | Both fix branches open, not merged |
| W1313 F2 — Privacy transition does not wipe disk | W1319 | Fix branch open, not merged |
| W1363 F1 — W1318 caller gap (W1190 never passes network_mode) | — | No fix branch |
| W1363 F2 — W1319 _check_privacy_mode_changed dead code | — | No fix branch |
| W1363 F3 — clear() + concurrent put() atomicity | W1371 | Fix branch open, not merged |
| W1363 F4 — No network_mode isolation tests | — | No fix branch |
| W1363 F5 — No per-value size cap | — | No fix branch |

---

## Summary

| # | Severity | Finding |
|---|----------|---------|
| F1 | HIGH | W1371 `_persist_locked()` omits `os.fsync()` — if W1371 merges without W938, power-loss durability regresses |
| F2 | MED | `_load()` no per-entry value type validation — non-string values from corrupt JSON silently returned by `get()` |
| F3 | MED | W1371 holds `self._lock` across full disk I/O in `put()` and `clear()` — all concurrent `get()` calls block for 5–15 ms |
| F4 | MED | W1371 v1 cache silently discarded on upgrade with no backup and only INFO log |
| F5 | LOW | No regression test for `clear()` atomicity fix — future refactor could silently reintroduce W1363 F3 race |

**Production risk today:** LOW — `TranslationCache` is not wired in production on
`codex/krab-ear-v2`. F1–F4 become active the moment W1371 (or W938/W1318/W1319) merges.
F3 is a new concern introduced *by* the W1371 fix and absent from all prior audits.
The critical merge-order constraint is: W1371 must incorporate W938's `fsync` before
merging, or W938 must be rebased to include W1371's format-version and `network_mode`
changes — otherwise one of the two medium-severity durability fixes will be permanently
lost.
