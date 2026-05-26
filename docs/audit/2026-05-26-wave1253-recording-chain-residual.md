# Wave 1253 — RecordingChainManager Residual Audit

**Date:** 2026-05-26  
**File audited:** `KrabEar/backend/recording_chain.py`  
**Auditor:** W1253 (sub-agent, read-only)  
**Prior waves checked:** W883, W1039 (audits), W900 (TOCTOU fix), W1046 (limit guard + privacy cascade)

---

## W900 / W1046 Merge State

| Wave | PR | Branch | Status |
|------|----|--------|--------|
| W900 | #818 | `feature/fix-recording-chain-toctou-W900` | **MERGED** into `codex/krab-ear-v2` |
| W1046 | #970 | `fix-recording-chain-W1046` | **OPEN — NOT merged** |
| W1039 (audit doc) | #959 | `audit-recording-chain-residual-W1039` | **OPEN — NOT merged** |

W900 TOCTOU fix (RC-1 from W883) is live in production. W1046 fixes (negative-limit guard + `delete_all_chains()` / privacy cascade) are NOT in `codex/krab-ear-v2`.

---

## Findings

### RC-1 [CRITICAL] — W1046 fixes are NOT in `codex/krab-ear-v2`

**File:** `KrabEar/backend/recording_chain.py` + `KrabEar/backend/service.py`

PR #970 (`fix-recording-chain-W1046`) has been open since 2026-05-26 and never merged. Two confirmed bugs from W1039 that were fixed in that PR remain present in the codebase:

- **F1** (W1039): `list_chains(limit=-1)` returns `N-1` items via Python negative slice (`chains[:-1]`). No guard exists in the current code at lines 180–195.
- **F5** (W1039): `delete_all_chains()` method does not exist in the current `recording_chain.py`. The `_handle_clear_privacy_audit_log` handler in `service.py` does NOT call `self._chains.delete_all_chains()` — recording chains survive a full privacy wipe.

**Reproduce F1:**
```python
mgr = RecordingChainManager(store=store)
for i in range(5): mgr.start_chain(f"c{i}")
assert len(mgr.list_chains(limit=-1)) == 4  # returns 4, not 0 or error
```

**Action required:** Merge PR #970 before the next release.

---

### RC-2 [MEDIUM] — No `delete_chain` IPC method; unbounded chain accumulation

**File:** `KrabEar/backend/recording_chain.py`, `KrabEar/backend/service.py`

There is no `delete_chain` IPC method. Once created, a chain can only be `end_chain`-ed (setting `ended_at`), but it is never removed from `recording_chains.json`. There is also no upper bound on the number of chains that can be created. The dispatch table in `service.py` (lines 1040–1046) wires 7 chain methods but none for deletion of individual chains.

Over time, `recording_chains.json` grows without bound. The `list_chains()` call returns at most `limit` entries (default 20) but the underlying dict grows without limit. A malicious or buggy caller can create tens of thousands of chains, causing:
- `recording_chains.json` to grow to hundreds of MB
- Each `_save()` call to serialise the full dict on every write
- `list_chains()` to deserialise the full dict before slicing

**Recommended fix:** Add `delete_chain(chain_id)` method + IPC handler, and optionally a `MAX_CHAINS = 500` cap in `start_chain()`.

---

### RC-3 [MEDIUM] — Ghost item_ids accumulate silently after history deletion/archiving

**File:** `KrabEar/backend/recording_chain.py`, interaction with `HistoryService.handle_delete_history_item`, `HistoryService.handle_cleanup_old_history`, `ArchiveManager.archive_items`

When a history item is deleted (`delete_history_item` IPC), cleaned up (`cleanup_old_history`), or archived (`archive_items`), the `item_ids` list inside any chain that referenced that item is NOT updated. The chain retains the stale item_id forever.

On `get_chain()`, the code falls back to `{"id": iid}` for items not found in the store (lines 154–167). This is silent — callers receive partial results with no indication that listed items no longer exist. In `merge_chain_text()` the ghost items are silently skipped (no text in the fallback stub). In `get_chain()`'s aggregate stats, ghost items contribute zero duration/words, causing `total_duration_sec` and `total_word_count` to silently shrink as history is pruned.

None of the three deletion paths (`HistoryService`, `ArchiveManager`, `StateStore.maybe_compact`) call any chain cleanup.

**Recommended fix:** Either (a) add a `notify_item_deleted(item_id)` hook to `RecordingChainManager` and call it from all deletion paths, or (b) document the ghost-item behaviour in the chain response schema with an `"active": false` flag.

---

### RC-4 [LOW] — No chain name length cap; IPC `limit` param accepts non-integer strings without error

**File:** `KrabEar/backend/recording_chain.py` lines 79–83, 241–243

Two input-validation gaps:

1. **Unbounded name length:** `start_chain(name)` strips whitespace and rejects empty strings but has no maximum-length guard. An IPC client can create a chain with a name that is many megabytes long, causing the `recording_chains.json` file to balloon and `json.dump` latency to spike.

2. **`handle_list_chains` type error:** `int(params.get("limit", 20))` (line 242) will raise `ValueError` if `params["limit"]` is a non-numeric string such as `"all"`. The IPC server converts this to a generic error response rather than a clear validation message. The W1046 guard (`max(0, min(limit, 1000))`) would also catch the negative-int case, but the `int()` conversion itself needs a try/except.

**Recommended fix:** Add `if len(name) > 500: raise ValueError(...)` in `start_chain()`; wrap the `int()` in `handle_list_chains` with a try/except and default to 20 on failure.

---

### RC-5 [LOW] — `_save()` silently swallows persist failures; in-memory state can diverge from disk

**File:** `KrabEar/backend/recording_chain.py` lines 65–73

`_save()` wraps the entire write in `try/except Exception` and calls only `logger.exception(...)` on failure — it returns `None` regardless. All callers (`start_chain`, `add_to_chain`, `end_chain`, `unlink_recording_from_chain`) invoke `_save()` but have no way to know whether the persist succeeded.

If the data directory is read-only, the disk is full, or `json.dump` raises for any reason, the in-memory state is updated and the IPC response returns `{"ok": True}`, but the change is not written to disk. On the next process restart the update is silently lost.

This pattern is consistent with `StateStore`'s approach for resilience, but unlike `StateStore`, `RecordingChainManager` has no secondary recovery path (no append log, no compaction). A single failed `_save()` permanently loses the update.

**Recommended fix:** Raise a warning-level Sentry breadcrumb (non-fatal) when `_save()` fails, so production data loss is observable. The error is already logged via `logger.exception`, but it does not surface to the caller or Sentry.

---

## Test Coverage Assessment

| Scenario | Covered? |
|----------|----------|
| W900 TOCTOU (snapshot inside lock) | Yes — concurrent tests in `RecordingChainUnlinkTestCase` |
| Negative limit (W1046 F1) | No — not testable until W1046 merges |
| `delete_all_chains` cascade (W1046 F5) | No — method absent |
| Individual `delete_chain` | No — method absent |
| Ghost item_id after `delete_history_item` | No — no cross-service integration test |
| `_save()` exception path | No |
| Name length guard | No |

---

## Summary Table

| ID | Severity | Description | Status |
|----|----------|-------------|--------|
| RC-1 | CRITICAL | W1046 fixes (negative-limit + privacy cascade) not merged into main | Unmerged PR #970 |
| RC-2 | MEDIUM | No `delete_chain` IPC; unbounded chain accumulation | Open |
| RC-3 | MEDIUM | Ghost item_ids after history deletion/archive/compaction | Open |
| RC-4 | LOW | No name length cap; `handle_list_chains` unguarded `int()` cast | Open |
| RC-5 | LOW | `_save()` swallows persist failures silently | Open |

5 new findings (W900 confirmed merged, W1046 confirmed NOT merged).
