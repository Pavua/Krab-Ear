# Wave 1027 — Post-fix Audit: `backend/privacy_audit.py` (W957 / W958 / W974)

**Date:** 2026-05-26
**Scope:** `KrabEar/backend/privacy_audit.py` — re-audit after three targeted fixes:
  - **W957** (`fix/privacy-audit-clear-W957`): `clear_privacy_audit_log` removed from IPC dispatch
  - **W958** (`fix/privacy-audit-singleton-W958`): singleton double-checked lock added
  - **W974** (`fix/privacy-audit-hash-chain-W974`): HMAC-SHA256 hash chain for tamper detection

**Auditor:** Sub-agent W1027 (read-only; no code changes)

---

## Fix Verification

### W957 — `clear_privacy_audit_log` removed from IPC dispatch (CRITICAL)

**Verdict: APPLIED CORRECTLY on `fix/privacy-audit-clear-W957`.**

- `"clear_privacy_audit_log"` is absent from the `handle_request` dispatch table.
- A W957 security comment blocks re-introduction without mandatory signing + flag.
- `_handle_clear_privacy_audit_log()` is retained for tests/migration scripts; the `clear()` method carries a W957 docstring warning.
- Regression test `test_clear_not_in_ipc_dispatch` uses regex over `handle_request` source to enforce the invariant.

**Caveat:** This fix branch has not been merged to `codex/krab-ear-v2` at audit time. Production code still exposes `clear_privacy_audit_log`.

### W958 — Singleton double-checked lock (HIGH)

**Verdict: APPLIED CORRECTLY on `fix/privacy-audit-singleton-W958`.**

- `_instance_lock: threading.Lock` added as a class variable.
- `get_instance()`: fast path (no lock, `_instance is not None`) + slow path (lock + re-check before construct).
- `reset_instance()` acquires the lock before clearing `_instance`.
- 50-thread barrier concurrency test (`TestPrivacyAuditSingletonConcurrency`) asserts all callers receive the same object identity.

**Caveat:** The `fix/privacy-audit-hash-chain-W974` branch was built on the original code and does **not** include the W958 lock. When W974 is merged, W958 must be applied first or rebased on top.

### W974 — HMAC-SHA256 hash chain (HIGH)

**Verdict: HMAC MATH IS CORRECT; implementation has a chain-consistency race (see F1 below).**

Tamper-detection manual derivation:

```
HMAC-SHA256(key, "null|" + canonical_json(entry1_body)) = H1
HMAC-SHA256(key, H1   + "|" + canonical_json(entry2_body)) = H2
```

- Body edits, line deletions, reorders, and direct `entry_hash` substitutions all cause `verify_chain()` to return `valid=False` — confirmed by 18 new tests (`TestVerifyChainDetectsTampering`).
- Legacy entries (no `entry_hash`) are correctly treated as chain restart points with `prev_hash=None`.
- The HMAC secret (`self._secret_key`) is never emitted to any log — only the key file **path** appears in `logger.exception` messages.
- Key file permissions: atomic write via `tmp_path.rename(key_path)` after `os.chmod(tmp_path, 0o600)`.

---

## Residual Findings

### F1 (HIGH) — `_last_hash` race: concurrent writers produce broken chain links

**File:** `privacy_audit.py`, `log_event()` (W974 version, lines ~195–228)

`_last_hash` is read and written **outside** the `fcntl.flock` critical section:

```python
prev_hash = self._last_hash          # (A) read — no lock
entry_hash = _compute_entry_hash(...)
with self._log_path.open("a") as fh:
    fcntl.flock(fh.fileno(), LOCK_EX)
    fh.write(line)                   # disk write is serialised
    fcntl.flock(fh.fileno(), LOCK_UN)
self._last_hash = entry_hash         # (B) write — no lock
```

Two threads calling `log_event()` concurrently both reach `(A)` before either reaches `(B)`. Both compute entries with the same `prev_hash`. `flock` serialises disk writes, so one entry lands first in the file. The second entry's stored `prev_hash` matches the pre-write tip — not the actual preceding entry — so `verify_chain()` reports a break at that position, producing a false-positive integrity alarm.

**Fix:** wrap the `(A)` read through `(B)` write with an instance-level `threading.Lock` (separate from the `flock`, which guards cross-process disk access). The singleton lock from W958 does not help here; it protects only `get_instance()`.

### F2 (MEDIUM) — W974 drops W958 singleton lock: branches need coordinated merge

**File:** `fix/privacy-audit-hash-chain-W974:privacy_audit.py` — `threading` is not imported; `_instance_lock` does not exist; `get_instance()` reverts to the original racy check-then-set.

The three fix branches address orthogonal problems in the same file. If merged in any order other than "W958 then W974 on top of W958", the singleton race re-emerges in the combined result.

**Required merge order:** W957 (service.py dispatch) → W958 (singleton lock) → W974 (hash chain, rebased to include W958 changes).

### F3 (LOW) — Key file has default umask permissions during a brief window

**File:** `privacy_audit.py`, `_load_or_create_key()` (lines ~60–71)

```python
tmp_path.write_bytes(new_key)                       # created with umask (e.g. 0o644)
os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)    # restricted to 0o600 after
tmp_path.rename(key_path)                           # then atomic rename
```

Between `write_bytes` and `chmod`, the temp file exists with the process umask applied (typically `0o644`). Any concurrent process that guesses the temp path (`privacy_audit.key.tmp`) during this window can read the HMAC key. The window is sub-millisecond and the path is predictable only on the local machine, but non-atomic creation is against key-handling best practice.

**Fix:** replace `write_bytes` + `chmod` with `os.open(path, O_WRONLY|O_CREAT|O_EXCL, 0o600)` to atomically create with correct permissions from the start.

### F4 (LOW) — `verify_chain()` has no IPC handler; compliance tooling cannot invoke it remotely

**File:** `KrabEar/backend/service.py`

W974 adds `verify_chain()` as a public method but does not register a `"verify_privacy_audit_chain"` IPC handler. The Swift privacy audit viewer and any external compliance scripts cannot trigger or schedule integrity checks without direct Python module access.

**Fix:** Add `"verify_privacy_audit_chain": self._handle_verify_privacy_audit_chain` to the dispatch table, returning `{"valid": bool, "first_broken_index": int|None, "checked": int}`.

### F5 (INFO) — No retention or rotation policy; unbounded log growth

`privacy_audit.log` appends indefinitely. No configurable maximum size, line count, or time-based rotation exists. After a deliberate `clear()` + restart, the HMAC chain silently resets; a log-reader has no way to distinguish intentional rotation from targeted erasure.

**Fix:** add an optional `max_entries` / `max_age_days` setting; on rotation, emit a signed rotation-sentinel entry (with a reason field) before clearing, providing an auditable rotation record.

### F6 (INFO) — Partial-purge / GDPR selective deletion is not supported

`clear()` destroys the entire log. There is no API to delete entries by subject identity, date range, or category while keeping the rest of the chain intact. Selective entry removal is cryptographically incompatible with the current linear chain design.

**Fix (design):** switch to a Merkle-tree structure where each branch can be pruned and re-signed independently, or maintain a separate redaction ledger that records which entries were removed and why, accepted as a chain break with a signed reason.

---

## Summary

| ID | Severity | Status | One-line |
|----|----------|--------|---------|
| W957 | CRITICAL | Applied (not merged) | `clear_privacy_audit_log` removed from IPC |
| W958 | HIGH | Applied (not merged) | Singleton double-checked lock |
| W974 | HIGH | Applied (not merged) | HMAC-SHA256 chain — math correct |
| F1 | HIGH | **New** | `_last_hash` race: concurrent writers break chain |
| F2 | MEDIUM | **New** | W974 branch missing W958 singleton lock |
| F3 | LOW | **New** | Key file briefly world-readable during creation |
| F4 | LOW | **New** | `verify_chain()` not exposed via IPC |
| F5 | INFO | Pre-existing | No retention / rotation policy |
| F6 | INFO | Pre-existing | No partial-purge / GDPR selective deletion |

**All three fix branches must be merged to `codex/krab-ear-v2` in order (W957 → W958 → W974) to land the security improvements. F1 should be resolved when W974 lands.**
