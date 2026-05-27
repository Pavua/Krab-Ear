# Audit W1373 — CallSessionService (call_session_service.py)

**Date:** 2026-05-27
**Auditor:** W1373 sub-agent (Sonnet 4.6)
**Branch:** codex/krab-ear-v2 (commit 6c900317)
**Scope:** `KrabEar/backend/call_session_service.py` + collaborators:
  `call_session.py`, `call_session_store.py`, `call_auto_end.py`,
  `call_silence_probe.py`, `telnyx_adapter.py`, `twilio_adapter.py`

---

## Summary

6 findings: 1 HIGH, 3 MED, 2 LOW.

The state machine (`CallSessionStateMachine` + `CallSessionStore`) is structurally sound —
transitions are enforced at every write, terminals are immutable, and the NDJSON replay is
correct for normal operation. The IPC handlers are thin and well-tested. However, two
security fix branches (`fix-telnyx-security-W1196`, `fix-twilio-security-W1208`) are **not
merged** into `codex/krab-ear-v2`, leaving path-traversal and SSRF vulnerabilities live on
main. Three additional medium-severity design gaps exist.

---

## Findings

### F1 HIGH — W1195/W1203 security patches not merged into main branch

**Files:** `KrabEar/backend/telnyx_adapter.py`, `KrabEar/backend/twilio_adapter.py`

Two security fix commits exist on separate branches but are absent from `codex/krab-ear-v2`:

- `fix-telnyx-security-W1196` (commit `f346dfdb`): F1 Telnyx — `Retry-After` sleep is
  unbounded (`time.sleep(wait)` where `wait` is raw from header, no cap). F2 — `hangup()` and
  `get_call_status()` interpolate `call_control_id` directly into the URL path with no regex
  validation, enabling path-traversal (e.g. `../../other-resource`). F3 — `dial()` forwards
  `webhook_url` to Telnyx without SSRF validation; a malicious caller could pivot to
  RFC1918/localhost.

- `fix-twilio-security-W1208` (commit `1b5a02fe`): Same classes of issues on the Twilio side
  — unbounded `Retry-After`, no SID validation in `hangup()`/`get_call_status()`, no webhook
  SSRF check.

Both fixes are implemented and tested in their branches; the only remediation needed is a
merge/rebase into `codex/krab-ear-v2`.

**Evidence:**
```bash
git branch --contains f346dfdb   # → only fix-telnyx-security-W1196
git branch --contains 1b5a02fe   # → only fix-twilio-security-W1208
grep "_CTRL_RE\|_is_safe_webhook" KrabEar/backend/telnyx_adapter.py  # → no output
grep "retry_after.*min\|max.*retry" KrabEar/backend/telnyx_adapter.py  # → no output
```

---

### F2 MED — `_apply_delta` accumulates `cost_usd` with `+=` instead of `=`

**File:** `KrabEar/backend/call_session_store.py`, line 323

```python
# current code:
session.cost_usd += float(delta["cost_usd"])
```

`mark_completed` / `mark_failed` write the **total** `cost_usd` to the delta (not an
increment). On replay, `CallSession.create()` initialises `cost_usd = 0.0`, then `_apply_delta`
adds the full amount — correct for a single delta. However, the intent is overwrite (`=`), not
accumulation. Under two specific scenarios this becomes a bug:

1. NDJSON manual editing or a crash that duplicates the terminal delta row — the cost doubles on
   every extra delta.
2. Future code paths that write intermediate cost deltas (e.g. running cost ticker) would
   unexpectedly stack with the terminal value.

Under normal operation the state machine prevents double `mark_completed`, so this is latent,
not currently triggered. But the semantics are wrong and the code contradicts its own docstring
("applies delta to session in-place").

**Fix:** Change line 323 to `session.cost_usd = float(delta["cost_usd"])`.

---

### F3 MED — `CallSilenceProbe._response_received` is a singleton `threading.Event`

**File:** `KrabEar/backend/call_silence_probe.py`, lines 57, 145, 156

The `threading.Event` instance is created once per `CallSilenceProbe` object:

```python
self._response_received = threading.Event()
```

`confirm_silence_with_probe()` calls `self._response_received.clear()` then waits. If two
concurrent callers both invoke `confirm_silence_with_probe()` (possible if the IPC server
handles two rapid `call_check_auto_end` calls that each decide to probe), the second call's
`clear()` resets the event before the first call's `wait()` can observe a `set()` from
`signal_response_received()`. The first probe silently concludes "no response" even though the
user answered.

The existing concurrent test (`test_concurrent_probe`) only tests `detect_silence_window`
(stateless) — it does **not** exercise `confirm_silence_with_probe()` concurrently.

**Fix:** Replace the singleton Event with a per-call `threading.Event` local to each
`confirm_silence_with_probe()` invocation, or add a re-entrant guard that rejects a second
probe while one is in progress.

---

### F4 MED — Phone numbers written to NDJSON regardless of `privacy_mode_enabled`

**Files:** `KrabEar/backend/call_session_service.py` (handle_call_session_create),
           `KrabEar/backend/call_session_store.py` (create → _append)

`CallSessionService` has no reference to `settings` or `privacy_mode_enabled`. When privacy
mode is active, `phone_number` (PII) and `goal_text` are unconditionally persisted in
`call_sessions.ndjson`. Other export/persist paths in `service.py` gate on
`settings.get("privacy_mode_enabled")` (lines 3656, 3703, 3748), but call-session creation
does not. A user who enables privacy mode expecting their call data to be withheld from disk
will have their outbound phone numbers logged.

**Fix:** Add a `settings` or `get_settings_fn` collaborator to `CallSessionService` and check
`privacy_mode_enabled` in `handle_call_session_create`. Return an error or strip PII fields
(redact phone to last 4 digits) when privacy mode is on.

---

### F5 LOW — No E.164 phone number format validation in `handle_call_session_create`

**File:** `KrabEar/backend/call_session_service.py`, lines 46–51

`handle_call_session_create` validates that `phone` is non-empty but does not check E.164
format. The Telnyx and Twilio adapters do validate E.164 at dial time (via `_is_valid_phone`),
so the call itself will fail cleanly, but a malformed number is stored in NDJSON and listed
in `call_session_list` results, leading to user confusion and wasted storage.

**Fix:** Import and reuse `_is_valid_phone` (or a shared `core.utils` equivalent) in
`handle_call_session_create` and raise `ValueError` on non-E.164 input.

---

### F6 LOW — `CallSessionStore.delete()` has no IPC handler; method is unreachable

**Files:** `KrabEar/backend/call_session_store.py` (delete method),
           `KrabEar/backend/call_session_service.py` (no handle_call_session_delete),
           `KrabEar/backend/service.py` (handler table lines 1197–1202)

`CallSessionStore.delete()` implements soft-delete via tombstone and is tested in
`test_call_session_store.py`. However, no `handle_call_session_delete` method exists in
`CallSessionService` and no `"call_session_delete"` IPC key is registered in
`BackendService.handle_request`. Operators and Swift callers have no IPC path to remove
orphaned or erroneous sessions. Over time the NDJSON can accumulate stale entries with no
cleanup mechanism.

**Fix:** Add `handle_call_session_delete(params)` to `CallSessionService` and register it as
`"call_session_delete"` in `service.py`.

---

## State Machine Assessment

The `CallSessionStateMachine` is correct. The `_VALID_TRANSITIONS` table enforces the
documented `idle → dialing → connected → talking → ending → completed/failed` graph. Terminal
states (`COMPLETED`, `FAILED`) have empty transition sets. `CallSessionStore` constructs a
fresh `CallSessionStateMachine` from the replayed session status before every
`update_status()` / `mark_completed()` / `mark_failed()` call, so concurrent IPC calls cannot
create an inconsistent transition — `fcntl.LOCK_EX` serialises all writes. The replay-on-read
pattern is safe for the current IPC threading model.

## Concurrency Assessment

`CallSessionStore` serialises every read-replay-write cycle under `fcntl.LOCK_EX`, preventing
torn writes and lost updates. `CallSessionService` itself is stateless (no instance fields
mutated at request time), so concurrent IPC calls are safe at the service layer. The `auto_end`
collaborator (`CallAutoEnd`) is also stateless (pure evaluation). The only concurrency bug is
F3 (singleton probe Event), which is confined to `CallSilenceProbe`.

## Provider Adapter Assessment

Both `TelnyxAdapter` and `TwilioAdapter` follow the `CallProvider` protocol. Stub mode (empty
credentials) is correctly detected and all methods short-circuit. Retry configuration (urllib3
`HTTPAdapter` + `Retry`) is present on both. The security issues listed in F1 are documented
above and are already fixed in the security branches awaiting merge.

## Test Coverage Assessment

| Area | Coverage | Gap |
|---|---|---|
| `CallSession` data model / state machine | High (281 lines, 10 classes in test_call_session.py) | None |
| `CallSessionStore` CRUD + NDJSON | High (643 lines, 10 test classes) | None |
| `CallSessionService` 6 handlers | Adequate (302 lines, 6 test classes, happy + sad paths) | None |
| `CallAutoEnd` rules | Present (test_call_auto_end.py) | None |
| `CallSilenceProbe.confirm_silence_with_probe` concurrency | Missing | concurrent probe race (F3) |
| Telnyx/Twilio path-traversal + SSRF | Missing on main branch | F1 (fixes on separate branches) |
| `call_session_delete` IPC | Missing | no handler exists (F6) |
