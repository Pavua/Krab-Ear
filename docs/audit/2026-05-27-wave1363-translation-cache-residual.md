# W1363 — Translation Cache Residual Audit

**Date:** 2026-05-27  
**Auditor:** W1363 sub-agent  
**Branch:** `audit-translation-cache-residual-W1363` off `codex/krab-ear-v2`  
**File:** `KrabEar/backend/translation_cache.py`

---

## Wave Merge State

| Wave | Description | Status in `codex/krab-ear-v2` |
|------|-------------|-------------------------------|
| W1190 | Wire `TranslationCache` into `BackendService` | **NOT MERGED** — `TranslationCache` is defined but never instantiated anywhere in production code. Zero imports outside tests. |
| W1318 | Cache key includes `network_mode` | **NOT MERGED** — `_make_key(text, source, target, engine)` does not include `network_mode`. |
| W1319 | `clear_cache` wipes disk too | **IMPLEMENTED** — `clear()` calls `self._persist()` after clearing in-memory dict, which writes an empty `{}` to disk. Correct. |
| W1161 | Translator `_cache` lock + privacy | **PARTIALLY ADDRESSED** — `Translator._translate_impl` in-memory `OrderedDict` cache (capacity 500) correctly includes `normalized_network_mode` in its tuple key `(mode, style, network_mode, text)`. However the `_cache_get` / `_cache_set` methods at lines 708-721 in `translator.py` have **no lock** protecting the `OrderedDict` operations — only a per-translate-call sequence, not thread-safe under concurrent IPC calls. |

---

## New Findings (5)

### F1 — CRIT: `TranslationCache` is a dead module — never instantiated (W1190 not merged)

**File:** `KrabEar/backend/translation_cache.py`, `KrabEar/backend/service.py`  
**Severity:** CRITICAL (data loss class — disk cache always empty, W1319 clear fix is unreachable)

`TranslationCache` has zero instantiation sites in production code. A grep across all non-test Python files confirms no `from backend.translation_cache import` or `TranslationCache(` call exists in `service.py`, `translator.py`, `translation_service.py`, or anywhere else in `KrabEar/backend/`. The module exists only on disk. Every `put()` and `get()` call advertised in tests exercises an isolated temp-dir instance; at runtime the on-disk `translation_cache.json` is never written or read.

**Impact:** All waves that built on top of W1190 (W1318 key fix, W1319 clear fix) are neutralised — the class exists but no production path ever creates it. Every per-session translation result is discarded on process restart; confidential translations are never persisted but also never cleared on `clear_translation_cache` IPC (handler would be a no-op even if wired).

**Fix:** Instantiate `TranslationCache(data_dir)` in `BackendService.__init__` or `TranslationService.__init__`; inject into `TranslationService`; expose `clear_translation_cache` and `get_translation_cache_stats` IPC handlers.

---

### F2 — HIGH: `_make_key` excludes `network_mode` — cross-mode cache poisoning (W1318 not merged)

**File:** `KrabEar/backend/translation_cache.py`, lines 24-27  
**Severity:** HIGH

```python
def _make_key(text: str, source: str, target: str, engine: str) -> str:
    raw = f"{text}\x00{source}\x00{target}\x00{engine}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

`network_mode` is not a parameter. If `TranslationCache` were wired (F1 fix), a translation cached under `offline_default` (using a local Helsinki-NLP model) would be returned on a subsequent call with `network_mode=online_opt_in` that might have invoked a higher-quality remote engine, and vice versa. The `Translator` in-memory cache correctly includes `network_mode` in its tuple key (`translator.py:183-188`), but the persistent disk cache does not.

**Fix:** Add `network_mode: str` parameter to `_make_key` and include it in the hash preimage:
```python
def _make_key(text: str, source: str, target: str, engine: str, network_mode: str = "") -> str:
    raw = f"{text}\x00{source}\x00{target}\x00{engine}\x00{network_mode}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```
`put()` and `get()` signatures must accept `network_mode` and forward it.

---

### F3 — HIGH: `_persist()` re-acquires `self._lock` while called from `put()`/`clear()` after lock release — TOCTOU window

**File:** `KrabEar/backend/translation_cache.py`, lines 60-71, 85-91, 109-120  
**Severity:** HIGH

`put()` releases `self._lock` at line 71, then calls `_persist()`. `_persist()` re-acquires `self._lock` to snapshot `self._cache` (line 113). Between the lock release in `put()` and the re-acquisition in `_persist()`, another thread can call `put()` again, evict entries, and then both threads call `_persist()` concurrently, racing on the `.tmp` write and `os.replace`. The atomic `os.replace` at line 118 prevents file corruption, but the **snapshot order** is non-deterministic: the second thread's `_persist()` may complete before the first thread's `_persist()` snapshot — meaning the final on-disk state corresponds to the older in-memory snapshot, silently losing the newer entry.

**Concrete scenario:**
1. Thread A: `put("hello", …)` — acquires lock, inserts, releases lock, calls `_persist()`.
2. Thread B: `put("world", …)` — acquires lock, inserts, releases lock, calls `_persist()`.
3. Thread B's `_persist()` snapshots `{hello, world}` and writes first.
4. Thread A's `_persist()` snapshots `{hello}` (stale snapshot) and overwrites → "world" lost from disk.

**Fix:** Move `_persist()` call inside the `with self._lock` block and remove the inner lock re-acquisition in `_persist()`, or use a write-coalescing approach (dirty flag + background flush thread).

---

### F4 — MED: No file lock (fcntl) on `translation_cache.json` — concurrent-process writes not safe

**File:** `KrabEar/backend/translation_cache.py`, lines 109-120  
**Severity:** MEDIUM

`_persist()` uses `os.replace(tmp_path, self._path)` for atomic single-process writes, but there is no `fcntl.flock` advisory lock protecting against concurrent writes from multiple processes (e.g., REST server on port 5005 + IPC backend both instantiating `TranslationCache` against the same `data_dir`). `StateStore` uses `fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)` as the project-standard pattern for cross-process safety. `translation_cache.py` imports neither `fcntl` nor any equivalent.

The risk is low today because W1190 is not merged (F1), but upon wiring it becomes a real hazard when the REST server and IPC server run simultaneously with the same `data_dir`.

**Fix:** Wrap the `open(tmp_path)` + `os.replace()` block in `fcntl.flock(LOCK_EX)` on a `.lock` sidecar file, matching the `StateStore` pattern in `backend/state_store.py:113-117`.

---

### F5 — LOW: No persistent-format versioning — future schema migration is undetectable

**File:** `KrabEar/backend/translation_cache.py`, lines 95-107 (`_load`)  
**Severity:** LOW

The on-disk format is a bare `dict[str, str]` (SHA-256 hex → translated text). There is no `{"version": 1, "entries": {...}}` envelope. If a future wave changes the key scheme (e.g., F2 adding `network_mode`), existing `translation_cache.json` files on user machines will load silently with the old key format, producing cache misses for every query (the old SHA-256 hashes will never match new hashes). Users will see a cold cache after upgrade with no warning.

The `_load` method at lines 95-107 accepts any `dict` without checking a version field, so a migrated key format would also load the old cache silently.

**Fix:** Wrap the persisted structure as `{"version": 1, "entries": {...}}`. On load, if `data.get("version") != CURRENT_VERSION`, log a warning and discard (`_cache = OrderedDict()`). This makes stale-cache drops explicit and prevents silent cross-version pollution.

---

## Test Coverage Assessment

| Scenario | Covered |
|----------|---------|
| Basic put/get/miss | Yes |
| LRU eviction | Yes |
| Persist/reload | Yes |
| Corrupt JSON graceful load | Yes |
| Atomic .tmp write | Yes |
| Concurrent put/get (same process) | Yes |
| network_mode isolation (disk cache) | **No** — W1318 not merged and no test guards against cross-mode poisoning |
| File lock / multi-process | **No** |
| Schema version mismatch | **No** |
| TranslationCache actually wired in BackendService | **No** — integration-level wiring test missing |

---

## Summary

| ID | Severity | Description |
|----|----------|-------------|
| F1 | CRIT | `TranslationCache` never instantiated — dead module (W1190 not merged) |
| F2 | HIGH | `_make_key` excludes `network_mode` — cross-mode cache poisoning (W1318 not merged) |
| F3 | HIGH | TOCTOU: `_persist()` race between lock release and re-acquisition — stale snapshot can overwrite newer data |
| F4 | MED | No `fcntl` cross-process file lock — multi-process writes unsafe |
| F5 | LOW | No format version field — silent staleness after key-scheme migration |

W1319 (`clear_cache` wipes disk) is correctly implemented.  
W1161 (`network_mode` in in-memory key) is addressed in `Translator._cache` but the disk-persistent `TranslationCache` has the same gap (F2).
