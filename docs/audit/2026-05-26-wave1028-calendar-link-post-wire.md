# Wave 1028 — Calendar Link Post-W947 Audit

**Date:** 2026-05-26  
**Branch:** `audit/wave1028-calendar-link-post-wire`  
**Scope:** Re-audit of `KrabEar/backend/calendar_link.py` after W947 claimed to wire 3 IPC handlers  
(`link_to_calendar_event`, `get_calendar_link`, `search_by_calendar_event`)

---

## Summary

W947's 3 IPC handler claims are **false** — those exact method names were never wired and were
subsequently deleted as dead code in Wave 65 batch 3. The only wired calendar IPC handler is
`create_calendar_event` (Phase D.4, `_handle_create_calendar_event`). The underlying
`CalendarLinker` class and the `StateStore` calendar-link methods are solid, but 5 residual
issues remain.

---

## Finding 1 — CRITICAL: W947 IPC wiring claim is incorrect

**File:** `KrabEar/backend/service.py` (dispatch table, line ~1191)

The three methods W947 claimed to wire —
`link_to_calendar_event`, `get_calendar_link`, `search_by_calendar_event` — are **absent** from
the dispatch table. A grep across the entire codebase (`KrabEar/`, `native/`) returns zero hits
on these method names in production code. `test_ipc_dispatch_invariants.py` lines 352–363
explicitly records that `_handle_get_calendar_link` and `_handle_search_by_calendar_event` were
**deleted in Wave 65 batch 3** as dead code with no callers.

The only wired calendar IPC handler is:
```
"create_calendar_event": self._handle_create_calendar_event  # line 1191
```

W947's test file (`test_calendar_event.py`, 40 tests passing) exercises `_handle_create_calendar_event`
and `StateStore` calendar methods — these are real and correct — but the claim of wiring 3 IPC
methods named `link_to_calendar_event / get_calendar_link / search_by_calendar_event` is inaccurate.

**Impact:** No regression in production (the missing handlers were intentionally removed), but the
W947 audit report is misleading about what was accomplished.

---

## Finding 2 — HIGH: `CalendarLinker` not thread-safe — cache writes are unprotected

**File:** `KrabEar/backend/calendar_link.py`, lines 94–122

`CalendarLinker` stores cache state in three plain instance attributes (`_cached_result`,
`_cache_at_time`, `_cache_window_key`) with no lock protecting read-modify-write. The shared
`self._calendar_linker` instance in `BackendService` is accessed from the IPC server thread
(via `create_calendar_event`) and, if auto-link were ever re-wired, from the transcription thread
simultaneously.

The test `TestConcurrentLink` (line 352 of `test_calendar_link.py`) confirms this is exercised
concurrently: 10 threads call `find_active_event` simultaneously. Although CPython GIL limits
true parallel execution, a check-then-act sequence on three separate attributes can produce a
torn cache state (stale `_cache_window_key` + fresh `_cache_at_time`) causing an spurious
osascript spawn or returning a result from the wrong time window.

**Fix:** Add a `threading.Lock()` in `__init__` and wrap the cache check + update block in
`find_active_event` with `with self._lock:`.

---

## Finding 3 — HIGH: TCC denial detection misses `error -1743` (AppleEvent denied)

**File:** `KrabEar/backend/calendar_link.py`, line 144

```python
if "Not authorized" in stderr or "not allowed" in stderr.lower():
```

macOS returns AppleEvent error `-1743` when Calendar.app automation is denied by TCC on first
launch. The osascript stderr in this case contains strings like:
- `"Not allowed to send Apple events to Calendar."` — caught
- `"(-1743)"` alone (no "Not authorized" text) — **not caught**
- `"Application isn't running."` (Calendar closed + TCC not yet granted, rc=1, empty stdout) — falls
  through to the generic `rc != 0 and not stdout` path → logs a warning instead of a TCC-specific
  info message

The second path (`rc=1, empty stdout`) is handled (returns `None`) but logs `CalendarLinker: osascript rc=1` 
rather than a TCC-specific message, making post-install diagnostics harder to read.

**Fix:** Extend the denial check to include `"-1743"` in stderr and the "isn't running" pattern:
```python
if ("Not authorized" in stderr
        or "not allowed" in stderr.lower()
        or "-1743" in stderr
        or "isn't running" in stderr):
    logger.info("CalendarLinker: нет разрешения TCC Calendar (err: %s)", stderr[:80])
    return None
```

---

## Finding 4 — MEDIUM: `CALENDAR_LINK_ENABLED` flag is never checked by any handler

**Files:** `KrabEar/core/config.py` (line 631), `KrabEar/backend/service.py`

The config exposes `CALENDAR_LINK_ENABLED: bool = False` (default off) and
`DEFAULT_SETTINGS["calendar_link_enabled"] = False` (line 967–968). The docstring of
`calendar_link.py` states "Opt-in: включается через настройку CALENDAR_LINK_ENABLED=True."

However, `_handle_create_calendar_event` never reads this flag. It always executes the osascript
regardless of whether the user opted in. A user who disabled calendar integration via settings
would still get a Calendar.app TCC prompt if they accidentally call `create_calendar_event` via
IPC.

`CalendarLinker.find_active_event` likewise has no opt-in gate — it only early-returns on
non-Darwin. Since `find_active_event` is never called in the current codebase (see Finding 1),
this is dormant but creates a mismatch between documented intent and implementation.

**Fix:** Add `if not self._get_runtime_setting("calendar_link_enabled", False): return {"ok": False, "error": "calendar integration disabled"}` at the top of `_handle_create_calendar_event`.

---

## Finding 5 — LOW: `_handle_create_calendar_event` does not sanitize backslash before quote injection

**File:** `KrabEar/backend/service.py`, lines ~3155–3165

The handler escapes double-quotes in `title`, `notes`, `calendar_name` via `.replace('"', '\\"')`.
This prevents direct quote injection but does not handle a backslash immediately before a quote:

Input: `title = 'Stand\\"`  
Escaped: `'Stand\\"'`  
In AppleScript context: the `\\` is a literal backslash that cancels the escape, leaving an
unescaped `"` that breaks the script string boundary.

This is the classic "backslash-quote" injection pattern. The fix is to escape backslashes first:
```python
title_esc = title.replace("\\", "\\\\").replace('"', '\\"')
```

The existing test `test_create_event_escapes_quotes` only tests `"` → `\\"` and would miss this
path. The test file (`test_calendar_event.py` line 69) asserts `'\\"hello\\"'` appears but does
not try `'\\\"hello'` input.

---

## Verification: What W947 actually delivered

| Claim | Actual state |
|---|---|
| `link_to_calendar_event` wired | NOT wired; was deleted Wave 65 batch 3 |
| `get_calendar_link` wired | NOT wired; was deleted Wave 65 batch 3 |
| `search_by_calendar_event` wired | NOT wired (as IPC); exists only in StateStore |
| `create_calendar_event` wired | YES — correct, line 1191 |
| 24 tests claimed | 40 tests exist (31 in `test_calendar_link.py` + 9 in `test_calendar_event.py`), all pass |
| StateStore calendar methods present | YES — `update_history_item_calendar`, `get_history_item_calendar`, `search_by_calendar_event` all correct |
| Privacy mode guard | ABSENT (Finding 4) |
| Thread-safe cache | ABSENT (Finding 2) |

## Test count

```
KrabEar/tests/test_calendar_link.py   31 passed
KrabEar/tests/test_calendar_event.py   9 passed
Total: 40 passed
```
