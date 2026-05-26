# Wave 919 — Audit: `backend/paste_app_memory.py`

**Date:** 2026-05-26
**Module:** `KrabEar/backend/paste_app_memory.py` (212 lines)
**Auditor:** wave919/conflict-triage sub-agent

---

## Summary

`PasteAppMemory` stores per-application paste-profile preferences (bundle\_id → `plain | markdown | html | telegram | email | notes`) in a JSON file. The module is small, focused, and generally well-written. **5 findings** identified: 2 medium, 3 low.

---

## Findings

### F-1 (MEDIUM) — Double-lock / TOCTOU in `get_profile_for`

**Location:** lines 84–99

```python
def get_profile_for(self, bundle_id: str) -> str | None:
    ...
    with self._lock:
        entry = self._data.get(bundle_id)   # read #1
    if entry is None:
        return None
    with self._lock:                         # separate acquisition
        if bundle_id in self._data:
            self._data[bundle_id]["last_used"] = _utcnow_iso()
            self._save()
    return entry["profile"]
```

Between the two lock acquisitions another thread may call `delete(bundle_id)` — the entry is gone from `self._data` but `entry` (captured under the first lock) still holds the old dict reference. The `entry["profile"]` return on the last line is safe (it reads a local reference), but the update block fires on a key that was just deleted and re-inserts stale data — silently undoing the delete.

**Fix:** merge both critical sections into a single `with self._lock:` block.

```python
def get_profile_for(self, bundle_id: str) -> str | None:
    if not self._enabled:
        return None
    if not bundle_id:
        return None
    with self._lock:
        entry = self._data.get(bundle_id)
        if entry is None:
            return None
        self._data[bundle_id]["last_used"] = _utcnow_iso()
        self._save()
        return entry["profile"]
```

---

### F-2 (MEDIUM) — No upper bound on bundle\_id length

**Location:** `record()` line 73

```python
if not bundle_id or not bundle_id.strip():
    return
```

A caller (compromised Swift client or fuzzer) can pass an arbitrarily long string (e.g. 1 MB). Each `_save()` then writes a JSON file whose key length inflates the file. There is no cap, no IPC-layer sanitization specific to this method.

`InputSanitizer` in `backend/input_sanitizer.py` exists in the project but `handle_record_paste_app_profile` does not call it.

macOS bundle IDs follow the `com.vendor.appname` reverse-DNS convention; the practical maximum is ~255 bytes. A simple guard suffices:

```python
_MAX_BUNDLE_ID_LEN = 512  # generous but bounded

def record(self, bundle_id: str, profile: str) -> None:
    if not self._enabled:
        return
    bundle_id = bundle_id.strip()
    if not bundle_id:
        return
    if len(bundle_id) > _MAX_BUNDLE_ID_LEN:
        raise ValueError(f"bundle_id превышает допустимую длину {_MAX_BUNDLE_ID_LEN}")
    ...
```

Note: `InputSanitizer.sanitize_string()` could be called in the IPC handler layer instead, keeping `record()` pure.

---

### F-3 (LOW) — `_save()` called on every `get_profile_for` (read amplification)

**Location:** lines 94–98

Every successful read updates `last_used` and triggers a full JSON rewrite. With frequent pastes (e.g. 10–20 calls/minute), this causes unnecessary disk I/O, fsync churn, and lock contention.

Options (in ascending effort):
1. **Dirty flag + flush on write ops:** only persist when an actual profile change or explicit `cleanup_stale()` occurs; let `last_used` be updated in-memory and flushed lazily.
2. **TTL guard:** skip re-write if `last_used` is within the last N seconds (`_TOUCH_INTERVAL_S = 60`).
3. **Accept as-is if dataset is small** — with ~10–50 app entries the file is tiny (<2 KB) and the cost is negligible. Low priority unless profiling shows contention.

---

### F-4 (LOW) — `cleanup_stale` not called automatically

`cleanup_stale()` is exposed only as an IPC handler (`cleanup_stale_app_profiles`). It is never wired to a periodic cron or startup hook. Records accumulate indefinitely until a client explicitly calls the method. For most installations this is a non-issue (the dataset is small), but the 180-day TTL promise is only honoured on-demand.

**Recommendation:** wire a once-daily `cleanup_stale()` call in `BackendService`'s startup or existing daily-cron dispatch (alongside `ObsidianSyncManager`, `AutoBackupManager`, etc.).

---

### F-5 (LOW) — `enabled` flag not respected by `list_profiles` and `delete`

**Location:** lines 101–116

When `enabled=False`, `record()` and `get_profile_for()` return early, but `list_profiles()` and `delete()` still read/mutate `self._data`. This is a minor contract inconsistency: a caller that sets `enabled=False` to "pause" tracking can still enumerate and delete entries. The existing tests (class `TestPasteAppMemoryDisabled`) do not cover `list_profiles` when data was pre-loaded from disk then disabled mid-flight.

Impact is low — `delete` is a voluntary management action, not a privacy risk. But the symmetry is worth noting if stricter privacy-off semantics are required.

---

## What is working well

- **Atomic write via `.tmp` + `rename`:** `_save()` uses `tmp.replace(self._path)` — POSIX atomic, no partial-write corruption.
- **Legacy format migration:** the flat `{bundle_id: "profile"}` v1 format is transparently upgraded on load.
- **Corrupted/empty file handling:** `_load()` wraps the parse in a broad `except Exception` with a warning log — the service continues without crashing.
- **Concurrency baseline:** a single `threading.Lock` covers all mutations. Tested by `TestPasteAppMemoryConcurrent` (20 threads).
- **Profile allowlist validation:** `VALID_PROFILES` frozenset prevents arbitrary values from being persisted.
- **Unicode support:** `ensure_ascii=False` in `json.dumps` keeps bundle IDs readable; verified by `TestPasteAppMemoryUnicode`.
- **`stale_days` injectable:** configurable via constructor — simplifies testing without monkeypatching globals.
- **Test coverage:** 12 test classes covering record, persistence, cleanup, unicode, concurrency, corruption, IPC handlers.

---

## Dedup / IPC integration cross-check

- 5 IPC handlers wired in `service.py` lines 998–1002; all match `handle_*` methods.
- `PASTE_APP_MEMORY_ENABLED` (`settings.PASTE_APP_MEMORY_ENABLED`) is passed at construction — runtime toggle works.
- No deduplication layer exists (records simply overwrite). This is intentional — the last-write wins.

---

## Priority actions

| # | Severity | Action |
|---|----------|--------|
| F-1 | MEDIUM | Merge double-lock into single `with self._lock` in `get_profile_for` |
| F-2 | MEDIUM | Add `_MAX_BUNDLE_ID_LEN = 512` guard in `record()` |
| F-3 | LOW | Add `_TOUCH_INTERVAL_S` guard to suppress redundant disk writes on reads |
| F-4 | LOW | Wire `cleanup_stale()` to daily cron in `BackendService` |
| F-5 | LOW | Decide if `enabled=False` should block `list_profiles` / `delete` |
