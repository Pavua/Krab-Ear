# Wave 1180 — Third-pass Audit: `backend/privacy_audit.py`

**Date:** 2026-05-26
**Scope:** `KrabEar/backend/privacy_audit.py` — third-pass re-audit after four targeted fixes:
  - **W957** (`fix/privacy-audit-clear-W957`, PR #886): `clear_privacy_audit_log` removed from IPC dispatch
  - **W958** (`fix/privacy-audit-singleton-W958`): singleton double-checked lock
  - **W974** (`fix/privacy-audit-hash-chain-W974`, PR #901): HMAC-SHA256 hash chain
  - **W1029** (`fix/privacy-audit-log-race-W1029`, PR #954): `_log_lock` for `log_event` race fix (W1027 F1 HIGH)

**Auditor:** Sub-agent W1180 (read-only; no code changes to privacy_audit.py)

---

## Merge State Verification

All four fix branches are **not merged** into `codex/krab-ear-v2` as of this audit.
The current production file (`6c900317`) is the original 155-line baseline with **none** of the four fixes applied.

| Wave | PR | Branch | State on `codex/krab-ear-v2` |
|------|----|--------|-------------------------------|
| W957 | #886 | `fix/privacy-audit-clear-W957` | **NOT MERGED** — OPEN |
| W958 | — | `fix/privacy-audit-singleton-W958` | **NOT MERGED** — OPEN |
| W974 | #901 | `fix/privacy-audit-hash-chain-W974` | **NOT MERGED** — OPEN |
| W1029 | #954 | `fix/privacy-audit-log-race-W1029` | **NOT MERGED** — OPEN |

**Production impact of unmerged state:**

- `clear_privacy_audit_log` is still in the IPC dispatch table (line 1222 of `service.py`). Any authenticated IPC caller can silently erase the entire privacy audit trail. This is the W952 CRITICAL issue.
- No tamper-detection (hash chain) in production log entries. Log tampering is undetectable.
- Singleton creation is not thread-safe under Python's GIL being released during I/O in `__init__` (`_ensure_parent` does `mkdir`). Risk is low in practice but not zero.
- `log_event` does not hold a threading lock over the `prev_hash` read + write cycle (moot until W974 merges, but will need W1029 immediately after W974).

---

## Fix Content Verification (branch-level)

### W957 — `clear_privacy_audit_log` removed from IPC dispatch

Content verified on `fix/privacy-audit-clear-W957`. The fix correctly:
- Removes `"clear_privacy_audit_log"` from the `handle_request` dispatch dict.
- Retains `_handle_clear_privacy_audit_log()` and `clear()` for internal/test use, with a W957 security comment.
- Adds a regression test asserting the method is absent from the dispatch table.

**Verdict: CORRECT. Pending merge.**

### W958 — Singleton double-checked lock

Content verified on `fix/privacy-audit-singleton-W958`. The fix correctly:
- Adds `_instance_lock: threading.Lock` as a class variable.
- Implements fast path (no lock when `_instance is not None`) + slow path (acquire lock + re-check).
- `reset_instance()` acquires the lock before clearing.

**Verdict: CORRECT. Pending merge.**

### W974 — HMAC-SHA256 hash chain

Content verified on `fix/privacy-audit-hash-chain-W974`. The fix:
- Adds `_compute_entry_hash()`, `_load_or_create_key()`, `verify_chain()`.
- HMAC math is correct. `verify_chain()` detects body edits, deletions, reorders, and direct hash substitutions.
- Key file written atomically via tmp-rename.
- **Does NOT include W958 `_instance_lock`** — `threading` is not imported on this branch. W974 must be rebased on top of W958 before merging.

**Verdict: CORRECT MATH, merge order dependency. Pending merge.**

### W1029 — `_log_lock` serialises `log_event` chain update

Content verified on `fix/privacy-audit-log-race-W1029`. The fix:
- Adds `self._log_lock: threading.Lock` per-instance in `__init__`.
- Wraps the entire `prev_hash` read → HMAC compute → disk write → `_last_hash` update in `with self._log_lock`.
- Also wraps `clear()` under `_log_lock` to prevent `_last_hash` being read after file deletion.
- **Does NOT include W958 `_instance_lock`** — singleton creation remains racy (same gap as W974).

**Verdict: CORRECT FIX FOR F1. Needs W958 singleton lock. Pending merge.**

---

## New Residual Findings (post-W957/W958/W974/W1029)

### F1 (HIGH) — `verify_chain()` IPC handler still absent from `service.py` (W1027 F4 still open)

**File:** `KrabEar/backend/service.py`, dispatch table ~line 1221

W1027 F4 reported this gap. W974 ships `verify_chain()` as a public method but still registers no `"verify_privacy_audit_chain"` IPC handler. Confirmed absent in the `codex/krab-ear-v2` baseline and on all four fix branches.

A compliance check, scheduled integrity scan, or Swift privacy audit panel cannot invoke chain verification remotely. The entire tamper-detection value of W974 is unreachable through the supported IPC interface.

**Fix:** Add to the dispatch table:
```python
"verify_privacy_audit_chain": self._handle_verify_privacy_audit_chain,
```
with handler returning `{"valid": bool, "first_broken_index": int|None, "checked": int, "reason": str|None}`.

---

### F2 (HIGH) — W1029 `_log_lock` is not in W974; branches need coordinated merge or one combined branch

**Files:** `fix/privacy-audit-log-race-W1029` and `fix/privacy-audit-hash-chain-W974` address overlapping `log_event` code. W974 branch does not have `_log_lock`. If W974 is merged before W1029 (or instead of W1029), the race condition (W1027 F1 HIGH) is re-introduced because the W974 `log_event` reads `_last_hash` and updates it outside any threading lock.

Additionally, W1029 was built on top of W974 (it imports `threading`, has `_log_lock`, and includes the full HMAC chain), but does **not** include the W958 singleton `_instance_lock`. The correct combined-state merge order is:

```
W957 (service.py) → W958 (singleton lock) → W974 (HMAC chain, rebased on W958) → W1029 (log_lock, rebased on W974+W958)
```

Or, alternatively, produce a single combined branch that contains all four fixes in their correct final state.

**Fix:** Create a combined consolidation branch rebasing W957 + W958 + W974 + W1029 into a single coherent commit, adding the missing `_instance_lock` to the W1029 variant. This eliminates the merge-order fragility.

---

### F3 (MEDIUM) — `verify_chain()` reads entire log into memory; no streaming path for large logs

**File:** `fix/privacy-audit-hash-chain-W974:privacy_audit.py`, `verify_chain()` (~line 310)

```python
entries_raw = [ln.rstrip("\n") for ln in fh if ln.strip()]
```

The entire log file is loaded into a list before any entries are processed. There is no `max_entries` bound or streaming path. A log with 100 000 entries (typical after a year of heavy use: ~200 events/day × 365 days) at ~300 bytes/entry occupies ~30 MB in memory during a single verify call. In the Swift host process context, this is not catastrophic, but the `_read_chain_tip()` helper (called at `__init__`) has the same pattern (line-by-line streaming, but through `flock` shared across the entire file read). Neither path has a size guard.

Also absent: a time complexity note. The current `verify_chain()` is O(N) in entry count, which is acceptable, but the missing IPC handler (F1) means it can only be called programmatically — so any caller that does so in a tight loop (e.g. a scheduled integrity cron) will do repeated full-file reads without caching.

**Fix:** Add a `max_entries` guard to `verify_chain()` that returns `{"valid": None, "reason": "too_large", "entry_count": N}` when the log exceeds a configurable threshold (e.g. 50 000 entries), and add streaming support for `_read_chain_tip()` to avoid holding the full file in memory at startup.

---

### F4 (MEDIUM) — Key rotation on disk does not reset in-memory `_secret_key`; key replacement is silently ignored

**File:** `privacy_audit.py` (all branches with HMAC chain)

`_load_or_create_key()` is called once during `__init__`. The resulting bytes are stored in `self._secret_key` for the lifetime of the singleton. If an operator replaces `privacy_audit.key` on disk (e.g. during a key-rotation ceremony or after a compromise), the running process continues using the old in-memory key. New `log_event()` entries will be written with the old key, but `verify_chain()` will load `_secret_key` from `self._secret_key` (also old), so verification still passes — masking the key mismatch. A fresh process would load the new key and fail to verify the old entries.

This is a silent key-rotation breakage: neither the running process nor a fresh process will see an error, but the two processes will disagree on chain validity.

**Fix:** Document that key rotation requires a coordinated restart. Add a `reload_key()` method that re-reads `privacy_audit.key` under `_log_lock`, and expose a `"reload_privacy_audit_key"` IPC handler for the case where key rotation is performed by ops scripts.

---

### F5 (LOW) — Chain rotation policy (W1027 F5) still open; log grows without bound in production

**File:** `privacy_audit.py` on `codex/krab-ear-v2` (155-line baseline)

W1027 F5 (INFO) flagged unbounded log growth. This remains unaddressed on all four fix branches. There is no `max_entries`, `max_age_days`, or rotation sentinel. The three call sites observed (`observability.py`, `translation_service.py` × 2, `service.py` × 2) emit events on every privacy-mode toggle, every blocked Sentry capture, and every forced-offline translation — potentially dozens of entries per hour in active use.

After a year of use (conservatively ~100 events/day), the log contains ~36 500 lines. At ~200–400 bytes/line (including HMAC fields after W974 merges), that is 7–15 MB — acceptable, but there is no upper bound, and `read_entries(limit=100)` still reads the full file before slicing (line 106: `lines = fh.readlines()`).

**Fix:** Add a `trim_to(max_entries: int)` method that rewrites the log keeping only the most recent N entries, emitting a rotation-sentinel as the first retained entry. Wire to a configurable threshold checked at `log_event()` (e.g. every 1000 writes), or expose via a maintenance IPC method.

---

## Summary

| Item | Severity | Status |
|------|----------|--------|
| W957 (clear() IPC) | CRITICAL | Applied on branch, **NOT merged to production** |
| W958 (singleton lock) | HIGH | Applied on branch, **NOT merged to production** |
| W974 (HMAC chain) | HIGH | Applied on branch, **NOT merged to production** |
| W1029 (log_event lock) | HIGH | Applied on branch, **NOT merged to production** |
| F1 | HIGH | `verify_chain()` has no IPC handler (W1027 F4 still open) |
| F2 | HIGH | Branches diverged: W974 lacks W1029's `_log_lock`; no combined branch |
| F3 | MEDIUM | `verify_chain()` + `_read_chain_tip()` load full log into memory |
| F4 | MEDIUM | Key rotation on disk silently ignored by running process |
| F5 | LOW | Chain rotation policy and log size bounds still absent (W1027 F5 still open) |

**Critical path:** The W952 CRITICAL issue (IPC-accessible `clear_privacy_audit_log`) remains live in production because W957 has not merged. Merge W957 → W958 → W974-rebased-on-W958 → W1029-rebased-on-W974+W958, then add the `verify_chain` IPC handler (F1) as a follow-up.
