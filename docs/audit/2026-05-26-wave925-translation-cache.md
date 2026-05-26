# Wave 925 — TranslationCache Audit

**Date:** 2026-05-26  
**File:** `KrabEar/backend/translation_cache.py` (121 lines)  
**Tests:** `KrabEar/tests/test_translation_cache.py` (540 lines, 9 test classes, ~35 cases)

---

## Summary

`TranslationCache` is a well-structured in-memory + on-disk LRU cache backed by SHA-256 keyed
`OrderedDict`. Serialization format is **JSON** (safe — no binary deserialization risk). The
implementation is largely correct with one **high** severity finding (cache stampede), two
**medium** findings (fsync missing, privacy / plaintext cache), and four **low / informational**
findings.

---

## Findings

### F1 — Cache stampede: no coalescing for concurrent misses (HIGH)

**Location:** `put()` / `get()` — no barrier between cache miss and translation completion.

When 100 concurrent callers ask for the same uncached key, all 100 call `get()`, all receive
`None`, and all independently invoke the underlying translator. Only the last `put()` wins; the
other 99 translations are discarded. For a slow translation engine (HF MarianMT can take 2–8 s)
this wastes CPU and can saturate the inference worker pool.

No in-flight coalescing mechanism exists (`asyncio.Event`, `threading.Event` per-key map,
or similar). The call site in `translation_service.py` does not add one.

**Recommendation:** add a per-key `threading.Event` "in-flight" map inside `TranslationCache`
(or at the `TranslationService` call site) so all waiters for the same key block until the first
caller finishes and stores the result.

---

### F2 — Atomic write missing `fsync` before `os.replace` (MEDIUM)

**Location:** `_persist()` lines 115–118.

```python
with open(tmp_path, "w", encoding="utf-8") as fh:
    json.dump(snapshot, fh, ensure_ascii=False)
os.replace(tmp_path, self._path)
```

`os.replace` provides atomic directory-entry swap, but if the machine loses power between
`json.dump` returning and `fsync` being called, the `.tmp` file may contain a partially-written
buffer. On macOS HFS+/APFS with write-behind caching the kernel may not flush before
`os.replace`. Result: after crash, `translation_cache.json` could be truncated / empty even
though the swap appeared to succeed.

**Recommendation:** call `fh.flush(); os.fsync(fh.fileno())` before closing the `tmp_path`
context manager, prior to `os.replace`.

---

### F3 — Full translated texts cached unencrypted on disk — privacy mode bypass (MEDIUM)

**Location:** `_persist()` writes `translation_cache.json` to `data_dir`; values are full
translated strings; keys are SHA-256 hashes of the source text (irreversible, but the *values*
are plaintext translations).

If the user enables privacy mode (purge history), `translation_cache.json` is **not** cleared.
A forensic actor with filesystem access can read every cached translation, partially reconstructing
what the user said. The cache key hashes the source text (SHA-256, not reversible), but the
corresponding translated *value* stored in the JSON is the full translated output in plaintext.

**Recommendation:**
1. Wire `clear()` into the privacy-purge IPC path alongside history deletion.
2. Document the cache's privacy implications in the module docstring.
3. Consider an opt-in `encryption_key` parameter for at-rest AES-GCM encryption of values
   (low complexity with `cryptography` package already in deps).

---

### F4 — No TTL: stale entries served forever (LOW / informational)

**Location:** module docstring explicitly states "Максимум 5000 записей; при превышении
удаляются самые старые." There is no timestamp stored per entry and no max-age check.

A translation cached today with engine `hf_marian` model v1 will be returned unchanged if the
model is updated to v2 tomorrow. The `engine` component of the key mitigates this for explicit
engine upgrades (a new engine name produces a new key), but silent model weight updates with the
same engine string will not bust the cache.

The test `test_no_ttl_entries_persist_indefinitely` explicitly documents this as intentional
behaviour.

**Recommendation:** for long-lived deployments, expose a `max_age_days` constructor parameter
and filter entries on `_load()` by comparing a stored `ts` field. Not urgent — translation
outputs are generally stable for a given engine string.

---

### F5 — `_persist()` called outside the lock; snapshot races with `clear()` (LOW)

**Location:** `put()` line 71, `clear()` line 91. Both call `self._persist()` after releasing
`self._lock`. Inside `_persist()` the lock is re-acquired on line 113 to take a snapshot.

Sequence that can produce a stale persist:

1. Thread A calls `clear()` → acquires lock, empties `_cache`, releases lock.
2. Thread B calls `put()` → acquires lock, inserts one entry, releases lock, enters `_persist()`.
3. Both threads now race inside `_persist()` to re-acquire the lock and snapshot.
4. If A's `os.replace` executes *after* B's, the file ends up empty despite B's put succeeding
   in memory — an entry is silently dropped from the on-disk store.

**Recommendation:** hold the lock across the entire `_persist()` path, or use a single
serialized writer thread (queue-based) to eliminate re-entrant lock and ordering races.

---

### F6 — Key construction: correct, no collision risk; JSON format is safe (INFORMATIONAL)

**Location:** `_make_key()` lines 26–27.

```python
raw = f"{text}\x00{source}\x00{target}\x00{engine}"
return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

The null-byte `\x00` separator prevents concatenation collision (e.g. `"ab"+"c"` vs `"a"+"bc"`).
SHA-256 collision probability is negligible for practical corpus sizes (2^-128 birthday bound at
5000 entries). The on-disk format is JSON — **no binary deserialization risk** from tampered
cache files.

**Status:** no action required.

---

### F7 — Test coverage: comprehensive, one gap (INFORMATIONAL)

Test suite covers: basic get/put, eviction LRU, persistence reload, corruption graceful-fail,
unicode/emoji, concurrency (20-thread race), empty/boundary, key uniqueness, clear behaviour.

**Gap:** no test for the stampede scenario (F1). A test that launches 100 threads with the same
key and asserts the translator is called exactly once would catch any future stampede fix.

**Status:** no immediate action — informational only.

---

## Issue Table

| # | Severity | Area | Finding |
|---|----------|------|---------|
| F1 | HIGH | Concurrency | Cache stampede — 100 misses → 100 translations, 99 wasted |
| F2 | MEDIUM | Durability | No `fsync` before `os.replace` — power loss = truncated file |
| F3 | MEDIUM | Privacy | Plaintext translations on disk; not cleared on privacy purge |
| F4 | LOW | Correctness | No TTL — stale entries after silent model weight updates |
| F5 | LOW | Concurrency | `_persist()` outside lock — ordering race on concurrent clear+put |
| F6 | INFO | Correctness | Key design sound; SHA-256 + null separator; JSON format (safe) |
| F7 | INFO | Testing | No stampede test; all other paths well covered |

---

## Files Audited

- `/KrabEar/backend/translation_cache.py` — 121 lines
- `/KrabEar/tests/test_translation_cache.py` — 540 lines
