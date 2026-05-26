# Wave 942 — CalendarLinker Residual Audit

**File:** `KrabEar/backend/calendar_link.py`
**Tests:** `KrabEar/tests/test_calendar_link.py`
**Prior fixes:** W898 (Mac epoch offset `_MAC_EPOCH_OFFSET`) · W899 (split maxsplit guard)
**Status of W898/W899:** Committed on feature branches but **NOT yet merged into `codex/krab-ear-v2`** (confirmed via `git merge-base`). The current base branch still lacks `_MAC_EPOCH_OFFSET`.

---

## Findings

### HIGH-1 — AppleScript newline injection in `_handle_create_calendar_event` (service.py:3163)

**Severity:** HIGH  
**Location:** `KrabEar/backend/service.py:3157–3183`

`_handle_create_calendar_event` builds an f-string AppleScript using user-supplied `title`, `notes`, `start_date`, and `calendar_name`. Only double-quote characters are escaped (`replace('"', '\\"')`). Newline characters are **not** stripped or escaped.

A `start_date` containing a newline terminates the AppleScript string literal early, allowing arbitrary AppleScript to be injected before the outer `end tell`. This is exploitable through any IPC client that can call `create_calendar_event` with a crafted `start_date`:

```
start_date = "2024-04-25\nend tell\ntell application \"Terminal\" to do script \"id\""
```

Generates valid AppleScript that terminates the Calendar block and opens a Terminal.

**Fix:** Strip `\r\n` from all four user-supplied parameters before building the script, or use `osascript -l AppleScript` with `-e` per statement and keep data out of the script body by passing through AppleScript `do shell script` or property files. Minimum safe patch:

```python
title = re.sub(r'[\r\n]', '', title)
notes = re.sub(r'[\r\n]', '', notes)
start_date = re.sub(r'[\r\n]', '', start_date)
calendar_name = re.sub(r'[\r\n]', '', calendar_name) if calendar_name else None
```

Note: `_query_calendar` (the read path) does NOT inject user data into AppleScript — the template is static. This finding is isolated to the write path in service.py.

---

### MEDIUM-1 — `find_active_event` is instantiated but never called (dead integration)

**Severity:** MEDIUM  
**Location:** `KrabEar/backend/service.py:508` · `calendar_link.py:103`

`CalendarLinker` is constructed at service startup (`service.py:508`) and stored as `self._calendar_linker`, but `find_active_event` is **never called** anywhere in the codebase outside of tests. `update_history_item_calendar` and `search_by_calendar_event` in `state_store.py` are also never called from service.py. The opt-in feature flag `CALENDAR_LINK_ENABLED` (config.py:631) is checked by no code path.

This means the calendar auto-linking described in the docstring and IPC reference is completely non-functional at runtime despite the `CalendarLinker` being initialized. Users cannot benefit from it, and the initialization cost is wasted on every backend start.

**Fix:** Either wire `find_active_event` into the transcription completion path (checking `CALENDAR_LINK_ENABLED` and `privacy_mode_enabled`), or remove `_calendar_linker` from `BackendService.__init__` until the integration is complete.

---

### MEDIUM-2 — No privacy mode guard when calendar integration is wired

**Severity:** MEDIUM  
**Location:** `KrabEar/backend/calendar_link.py:103` · `KrabEar/core/config.py:987`

`CalendarLinker.find_active_event` has no check for `privacy_mode_enabled`. Privacy mode is respected by `TranslationService` (translation_service.py:96,201) and `observability.py` (observability.py:122). Calendar event titles are metadata about the user's schedule and should be suppressed when privacy mode is active.

When the dead integration (MEDIUM-1) is eventually wired, calling `find_active_event` and writing the result to `state_store` while `privacy_mode_enabled=True` would link personally identifiable schedule data to transcriptions in violation of the privacy contract.

**Fix:** Add a `privacy_mode` parameter to `find_active_event` (or check it in the call site) and return `None` immediately when `privacy_mode_enabled` is true.

---

### MEDIUM-3 — Incomplete TCC/permission error detection (AE error codes missed)

**Severity:** MEDIUM  
**Location:** `KrabEar/backend/calendar_link.py:144`

```python
if "Not authorized" in stderr or "not allowed" in stderr.lower():
```

This catches the two most common English-language macOS TCC error strings. However, it misses:

1. **`-1743` (AEPrivilegeError)**: `execution error: Calendar got an error: (-1743)` — returned on macOS 12+ when the user has previously denied Calendar access. The integer error code is not matched.
2. **Non-English macOS**: On a Russian-locale macOS, the error may read `Приложение не имеет разрешения на отправку событий Apple Events в Calendar.` — neither "Not authorized" nor "not allowed" matches.
3. **"permission" keyword**: `"This application does not have permission to access Calendar"` is not caught.

When these strings appear, the code falls through to the `proc.returncode != 0 and not proc.stdout.strip()` check at line 147. If stdout is empty (it usually is on denial), the event is silently dropped with a WARNING-level log. The user sees no indication that Calendar permission is needed.

**Fix:** Add `or "(-1743)" in stderr or "permission" in stderr.lower()` to the guard, and log at `WARNING` (not `INFO`) on permission denial so operators notice TCC misconfiguration.

---

### LOW-1 — Event selection picks earliest-start, not most-overlapping

**Severity:** LOW  
**Location:** `KrabEar/backend/calendar_link.py:157`

```python
best = min(events, key=lambda e: e.get("_start_epoch", 0), default=None)
```

When multiple events are simultaneously active (e.g., a 2-hour all-hands starting at 09:00 and a 30-minute 1:1 starting at 09:15), the one with the **earliest start time** wins. This is not necessarily the most relevant event — the user is more likely in the shorter, more specific meeting started closest to the recording time.

The docstring claims "наиболее релевантное активное событие" (most relevant active event) but the implementation delivers "oldest active event."

**Fix:** Score events by overlap ratio or proximity of start time to `at_time` rather than raw start epoch:

```python
# Prefer the event that started most recently (closest start to at_time)
at_epoch = at_time.timestamp()
best = min(events, key=lambda e: at_epoch - e.get("_start_epoch", 0))
```

---

### LOW-2 — Thread-safety race on cache state (no lock)

**Severity:** LOW  
**Location:** `KrabEar/backend/calendar_link.py:112–122`

`find_active_event` performs a check-then-act on `_cache_window_key`, `_cache_at_time`, and `_cached_result` without a lock. Two threads calling simultaneously can both see a cache miss, both call `_query_calendar`, and both write back to `_cached_result`. Under CPython's GIL, individual attribute reads/writes are atomic, so there is no data corruption — but two `osascript` processes are spawned unnecessarily.

The more serious scenario: one thread reads `_cached_result` between another thread's `_cached_result = result` write and `_cache_at_time = now_mono` write. Under GIL semantics this is safe (reads are atomic) but brittle.

**Fix:** Add a `threading.Lock` to `CalendarLinker.__init__` and wrap the cache check+update block in it. Given the 10-second osascript timeout, the lock hold time is bounded.

---

### LOW-3 — W898 Mac epoch fix not yet merged into `codex/krab-ear-v2`

**Severity:** LOW (tracking)  
**Location:** Commits `02a78e06` (W898) and `a0d37d47` (W899) on feature branches

`_MAC_EPOCH_OFFSET = 978_307_200` and the corrected `_epoch_to_iso` are committed on `feature/fix-*` branches but have not been merged into `codex/krab-ear-v2`. The base branch therefore still contains the wrong-date bug described in W889/W898.

No new analysis needed here — this is a merge-order issue. The fixes exist; they need to land in the merge train.

---

## Coverage Assessment

`test_calendar_link.py` has good coverage of the happy path, cache expiry, permission denial, timeout, FileNotFoundError, and unicode titles (28 test methods across 13 test classes). Gaps:

- No test for newline injection in `_handle_create_calendar_event`
- No test that verifies `find_active_event` returns `None` when `privacy_mode_enabled=True`
- No test covering the `-1743` or non-English TCC error strings
- No test verifying `find_active_event` is wired into a transcription completion flow
- `TestConcurrentLink` tests correctness but not subprocess call count under concurrency

## Summary Table

| ID | Severity | Location | Issue |
|----|----------|----------|-------|
| HIGH-1 | HIGH | service.py:3157–3183 | Newline injection in `create_calendar_event` AppleScript |
| MEDIUM-1 | MEDIUM | service.py:508 + calendar_link.py:103 | `find_active_event` never called — dead integration |
| MEDIUM-2 | MEDIUM | calendar_link.py:103 | No privacy mode guard on calendar auto-linking |
| MEDIUM-3 | MEDIUM | calendar_link.py:144 | Incomplete TCC denial detection (AE -1743, non-EN locales) |
| LOW-1 | LOW | calendar_link.py:157 | Wrong "best" event heuristic (earliest start ≠ most relevant) |
| LOW-2 | LOW | calendar_link.py:112–122 | No lock on cache check-then-act |
| LOW-3 | LOW | commits 02a78e06, a0d37d47 | W898/W899 epoch fix not merged into base branch |
