# Audit: telegram_bridge.py residual — W1206

**Date:** 2026-05-26  
**Branch audited:** `codex/krab-ear-v2` (worktree `audit-telegram-bridge-residual-W1206`)  
**Files:** `KrabEar/backend/telegram_bridge.py`, `KrabEar/backend/apple_integration_service.py`,  
`KrabEar/backend/service.py`, `KrabEar/core/paste_formatter.py`,  
`KrabEar/tests/test_telegram_bridge.py`

---

## Fix merge state

| Wave | PR | Branch | Status in `codex/krab-ear-v2` |
|------|----|--------|-------------------------------|
| W898 | #819 | `feature/fix-telegram-ssrf-W898` | **MERGED** (commit `02a78e06`, 2026-05-26) |
| W945 | #866 | `fix/telegram-bridge-ssrf-W945` | **NOT MERGED** (PR OPEN) |
| W946 | #875 | `fix/telegram-privacy-W946` | **NOT MERGED** (PR OPEN) |

### W898 fix content (merged)

`TelegramBridge.__init__` now validates `base_url` hostname at construction via:

```python
parsed = urlparse(base_url)
if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
    raise ValueError(...)
```

This closes the SSRF vector for `KRAB_EAR_TELEGRAM_BRIDGE_URL` env-var override.

### W945 delta (not merged)

W945 adds the same check but with a `_ALLOWED_HOSTS` class-level frozenset and a
different error message (`"refusing non-localhost base_url"` instead of W898's
`"must point to localhost"`). W945 also adds 7 allowlist-specific test cases and
updates two pre-existing tests that were broken by the W898 fix.

### W946 delta (not merged)

W946 adds a `privacy_mode_enabled` guard to `handle_send_to_telegram` in both
`service.py` and `AppleIntegrationService`. It also adds an optional
`settings_get` callable to `AppleIntegrationService.__init__`.

---

## Findings (5 NEW residual issues)

### F1 HIGH — Test suite broken by W898 (2 failing tests)

**File:** `KrabEar/tests/test_telegram_bridge.py` lines 339–390

W898 was merged without updating tests. Two test cases that pass non-localhost
`base_url` values at construction time now raise `ValueError` instead of the
expected `requests.ConnectionError`:

```
FAILED TestTelegramBridgeDisabledViaSetting::test_disabled_via_setting
  – TelegramBridge(base_url="http://disabled-host:0") raises ValueError at __init__
    but test expects ConnectionError from send_message

FAILED TestTelegramBridgeHandlesInvalidUrlSetting::test_handles_invalid_url_setting
  – TelegramBridge(base_url="http://INVALID_HOST_@@@:99999") raises ValueError at __init__
    but test expects ConnectionError from send_message
```

Confirmed by running `pytest KrabEar/tests/test_telegram_bridge.py` in the
worktree: 2 failed, 30 passed. W945 (unmerged, PR #866) contains the corrected
versions of both tests.

**Fix:** Merge PR #866 (W945), which also adds 7 allowlist-specific test cases.
Alternatively, update only the two broken tests to `assertRaises(ValueError)`.

---

### F2 MEDIUM — privacy_mode guard missing from `handle_list_telegram_chats`

**File:** `KrabEar/backend/apple_integration_service.py` line 112

W946 (PR #875, not merged) adds a `privacy_mode_enabled` guard to
`handle_send_to_telegram` but the sibling method `handle_list_telegram_chats`
receives no guard. In privacy mode a caller can enumerate the user's Telegram
chat IDs and titles via the `list_telegram_chats` IPC method, leaking metadata
about the user's account setup even when transcript text is blocked.

Confirmed: both the current `codex/krab-ear-v2` code and the W946 branch code
for `handle_list_telegram_chats` contain no `privacy_mode` check.

**Fix:** Add the same guard to `handle_list_telegram_chats`:

```python
if self._settings_get("privacy_mode_enabled", False):
    return {"chats": [], "privacy_mode_active": True}
```

---

### F3 MEDIUM — No Telegram 4096-char message cap in `TelegramBridge`

**File:** `KrabEar/backend/telegram_bridge.py` `send_message()`

Telegram's Bot/MTProto API enforces a hard 4096-character limit per message.
`TelegramBridge.send_message()` places no cap on the `text` parameter. Long
transcripts pass through and arrive at the main Krab web-panel, which will
forward them to Telegram; the Telegram API will then reject with
`MESSAGE_TOO_LONG`. The error propagates back as a generic `krab_error` with no
clear user-facing message.

The downstream `_fmt_telegram` function in `core/paste_formatter.py` also has no
4096 cap (PR #974 / W1053 fixes `paste_formatter` but is not merged). Even after
W1053 merges, `paste_formatter` is not always used before `send_to_telegram`
(callers may pass raw text directly through IPC).

**Fix:** Add a cap in `_build_payload()` or at the top of `send_message()`:

```python
TELEGRAM_MAX_CHARS = 4096
if len(text) > TELEGRAM_MAX_CHARS:
    text = text[:TELEGRAM_MAX_CHARS - 1] + "…"
```

---

### F4 LOW — Circuit breaker half-open thundering herd

**File:** `KrabEar/backend/telegram_bridge.py` `_is_open()` / `_check_circuit()`

When the circuit breaker resets (after `circuit_reset_sec` has elapsed), `_is_open()`
clears `_open_at` and returns `False` while holding `self._lock`. However, every
subsequent caller that acquires `self._lock` in `_check_circuit()` also sees
`_open_at = None` and proceeds immediately. Under concurrent load, all threads
that were blocked during the open period will burst through simultaneously when
the CB resets, potentially reproducing the failure condition that triggered the CB.

True half-open semantics require a single probe thread. The current implementation
lets all threads through at reset, which is a thundering herd on the recovery probe.

**Severity:** LOW for Krab Ear's localhost bridge (low concurrency), but worth
noting for correctness.

**Fix:** Track a boolean `_probing: bool` flag; only the thread that transitions
from open to half-open may proceed, others raise `CircuitBreakerOpen` until the
probe succeeds or fails.

---

### F5 LOW — W945 `_ALLOWED_HOSTS` error message incompatible with pending test assertions

**File:** `KrabEar/backend/telegram_bridge.py` lines 59–64; `KrabEar/tests/test_telegram_bridge.py`

W898 (merged) uses the error message `"must point to localhost"`.  
W945 (pending, PR #866) checks for `"refusing non-localhost"` in its 7 new
`TestTelegramBridgeHostnameAllowlist` test assertions.

When W945 is merged on top of W898, the `_ALLOWED_HOSTS` class constant and the
new error message will replace W898's inline set and message. If the PRs are
merged out of order (W945 first, W898 second — impossible given W898 is already
merged), or if W945 is partially applied, the tests will fail because the W898
error text does not contain `"refusing non-localhost"`.

Currently the 7 new allowlist tests in W945 **cannot pass** against the live
`codex/krab-ear-v2` branch (W898 message mismatch). This is a minor but concrete
test incompatibility that will surface when W945 is merged without its own bridge
patch included (the bridge patch is the precondition for the test assertions).

**Fix:** Merging W945 as-is (PR #866) will resolve this: it replaces W898's inline
check with `_ALLOWED_HOSTS` + new message, and its tests match the new message.

---

## Summary table

| # | Severity | File | Description |
|---|----------|------|-------------|
| F1 | HIGH | `test_telegram_bridge.py` | 2 tests broken by W898 (expect ConnectionError, get ValueError) |
| F2 | MED | `apple_integration_service.py` | `list_telegram_chats` missing privacy_mode guard |
| F3 | MED | `telegram_bridge.py` | No Telegram 4096-char cap in `send_message()` |
| F4 | LOW | `telegram_bridge.py` | CB half-open thundering herd (all threads pass at reset) |
| F5 | LOW | `telegram_bridge.py` + tests | W945 error message mismatch with W898 (test incompatibility) |

---

## Pending PRs to merge

- **PR #866** (W945): Fixes F1 + F5; adds `_ALLOWED_HOSTS` + 7 allowlist tests.
- **PR #875** (W946): Fixes privacy_mode for `send_to_telegram` (partial F2 coverage).
- **PR #974** (W1053): Fixes `paste_formatter` 4096 cap (partial F3 mitigation).

F2 (list_telegram_chats privacy gap) and F3 (bridge-level 4096 cap) and F4 (CB
half-open) require new fixes beyond the pending PRs.
