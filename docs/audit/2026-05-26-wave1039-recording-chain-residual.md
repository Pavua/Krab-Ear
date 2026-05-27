# Wave 1039 — RecordingChainManager Residual Audit

**Date:** 2026-05-26  
**Scope:** `KrabEar/backend/recording_chain.py` post-W900 snapshot  
**Tests baseline:** 46 passed (all green, `KrabEar/tests/test_recording_chain.py`)  
**Not re-reported:** W900 findings (assumed fixed before this snapshot)

---

## Summary

6 residual findings, none critical. All are LOW/MED severity gaps — correctness,
completeness, and privacy hygiene issues that W900 did not address.

---

## Finding 1 — `list_chains` negative `limit` leaks data (MED)

**File:** `recording_chain.py:187`  
**Code:** `for c in chains[:limit]:`

`handle_list_chains` passes `int(params.get("limit", 20))` directly to `list_chains()` with no
lower bound check. A caller that sends `limit: -1` receives Python slice `chains[:-1]` — all
chains except the last, instead of an empty or error result. `limit: -2` returns all but the last
two, and so on.

```python
# chains[:(-1)] == all_but_last  -- data leakage via negative slice
```

**Fix:** Clamp in `handle_list_chains` before calling `list_chains`:

```python
limit = max(1, min(int(params.get("limit", 20)), 200))
```

**Test gap:** No test for `limit < 0` or `limit > reasonable_cap`.

---

## Finding 2 — `get_chain` reads chain dict fields outside the lock (LOW, race)

**File:** `recording_chain.py:144–173`

`get_chain()` acquires `self._lock`, copies `item_ids`, and releases the lock at line ~149.
Lines 171–173 then read `chain["name"]`, `chain["created_at"]`, and `chain.get("ended_at")`
from the same mutable dict object **without holding the lock**.

`end_chain()` writes `chain["ended_at"]` under the lock concurrently. If `end_chain` runs
between the lock release in `get_chain` (line ~149) and the dict reads (line 171), the caller
sees a stale `ended_at: None` when the chain is already ended.

Since no `rename_chain` method exists today, `name` and `created_at` are write-once after
`start_chain`, so the practical impact is confined to `ended_at` staleness. Low severity but
the pattern is fragile; adding rename in future will promote this to MED.

**Fix:** Snapshot all needed fields under the lock:

```python
with self._lock:
    chain = self._data["chains"].get(chain_id)
    if chain is None:
        raise KeyError(...)
    item_ids = list(chain["item_ids"])
    name = chain["name"]
    created_at = chain["created_at"]
    ended_at = chain.get("ended_at")
```

---

## Finding 3 — Ghost links accumulate silently; no `cleanup_stale_links` API (LOW)

**File:** `recording_chain.py:153–168`

When a history item is deleted from the store, its ID remains in `chain["item_ids"]`
indefinitely. `get_chain()` handles this gracefully by returning a fallback stub
`{"id": iid}`, and `merge_chain_text()` skips items with empty text. However:

- Ghost links accumulate without bound over time.
- The caller has no IPC-level way to identify and remove stale refs in bulk.
- `unlink_recording_from_chain` requires the caller to already know which IDs are stale.

**Missing method:** `cleanup_stale_links(chain_id)` — remove item_ids whose corresponding
history items no longer exist in the store.

**Test gap:** No test verifying ghost-link count after repeated history deletes.

---

## Finding 4 — Chain name uniqueness not enforced, not documented (LOW)

**File:** `recording_chain.py:79–94`

`start_chain("Meeting")` called twice creates two independent chains with identical names.
This is silent and produces ambiguous results in `list_chains`. The behaviour is neither
validated (no uniqueness check) nor documented (docstring does not mention it).

Decision required: enforce uniqueness at the IPC level, or explicitly document that duplicate
names are allowed and let callers disambiguate by `chain_id`.

**Test gap:** No test verifies whether duplicate names are allowed or rejected.

---

## Finding 5 — `recording_chains.json` not purged by privacy/history wipe (MED)

**File:** `recording_chain.py:23` (`_CHAINS_FILE = "recording_chains.json"`)  
**Context:** `KrabEar/backend/service.py` — no reference to `recording_chains.json`

A user-initiated history wipe (e.g. "Очистить историю") deletes `history.ndjson` entries but
leaves `recording_chains.json` intact. After the wipe, chains still exist and still list the now-
deleted `item_ids` as ghost links (see Finding 3). The chain names themselves — which may
contain meeting titles, names of participants, or other PII — are **not cleared**.

No privacy-mode hook or `clear_history` handler in `service.py` touches this file.

**Risk:** User expects full data wipe; chain metadata (names, timestamps, item ID lists)
persists undetected.

**Fix:** Add `reset_chains()` to `RecordingChainManager` and call it from any full-history
purge handler in `BackendService`.

---

## Finding 6 — No IPC `delete_chain` method; ended chains cannot be removed (LOW)

**File:** `recording_chain.py` — no `delete_chain` / `handle_delete_chain`  
**Dispatch table:** `service.py:1040–1046` — 7 chain methods, no delete

A chain can be ended (`end_chain`) but never deleted from `recording_chains.json`.
Over time, ended chains accumulate in the file without bound. There is no IPC method
for a client to delete a chain by ID.

**Missing method:** `delete_chain(chain_id: str) -> None` + `handle_delete_chain`.

**Test gap:** No test for chain deletion lifecycle. `RecordingChainBackendServiceDispatchTestCase`
(Wave 156) only verifies `unlink_recording_from_chain` wiring.

---

## Wire Status

All 7 existing IPC methods (`start_chain`, `add_to_chain`, `end_chain`, `get_chain`,
`list_chains`, `merge_chain_text`, `unlink_recording_from_chain`) are correctly wired in
`service.py:1040–1046`. No Swift callers for any chain method exist in
`native/KrabEarAgent/` (confirmed by grep); the Swift UI integration is pending.

---

## Severity Table

| # | Finding | Severity | Fix size |
|---|---------|----------|----------|
| 1 | Negative `limit` leaks data in `list_chains` | MED | 1 line |
| 2 | `get_chain` reads dict outside lock (stale `ended_at`) | LOW | 5 lines |
| 3 | Ghost links accumulate; no `cleanup_stale_links` | LOW | new method |
| 4 | Chain name uniqueness undocumented/unenforced | LOW | decision + doc |
| 5 | `recording_chains.json` not purged on history wipe | MED | hook in service.py |
| 6 | No `delete_chain` IPC method; ended chains never removed | LOW | new method + handler |

**Action priority:** Fix 1 (trivial) + Fix 5 (privacy) in same PR. Findings 2, 3, 4, 6 are
implementation gaps best addressed when chain UI is implemented in Swift.
