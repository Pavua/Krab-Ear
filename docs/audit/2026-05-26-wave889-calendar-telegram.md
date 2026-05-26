# Wave 889 Audit: calendar_link.py + telegram_bridge.py

**Date:** 2026-05-26
**Files audited:**
- `KrabEar/backend/calendar_link.py` — CalendarLinker (osascript-based Calendar.app integration)
- `KrabEar/backend/telegram_bridge.py` — TelegramBridge (HTTP client to main Krab userbot)

**Scope:** osascript injection safety, HTTP retry/circuit breaker design, localhost-only enforcement.

---

## Summary

| # | Severity | File | Finding |
|---|----------|------|---------|
| 1 | INFO | calendar_link.py | No osascript injection — template is static |
| 2 | LOW | calendar_link.py | AppleScript epoch vs Unix epoch mismatch → timestamps off by ~31 years |
| 3 | LOW | calendar_link.py | `|||` in event titles/locations corrupts field parsing |
| 4 | MEDIUM | telegram_bridge.py | No localhost enforcement — `base_url` accepts any URL (SSRF vector) |
| 5 | LOW | telegram_bridge.py | No HTTP retry — single transient failure increments circuit breaker |
| 6 | INFO | telegram_bridge.py | Circuit breaker present and correct; half-open logic works |

Total: 6 findings (1 MEDIUM, 3 LOW, 2 INFO).

---

## calendar_link.py

### Finding 1 — INFO: No osascript injection risk

`_OSASCRIPT_TEMPLATE` is a **module-level constant**. No user-supplied data is interpolated into
it at any point. The script is passed verbatim to `osascript -e` via `subprocess.run()` with a
list argument (not a shell string), so no shell-injection is possible either.

Data flows exclusively *out* of Calendar.app into Python (via `proc.stdout`), never in. Verdict:
**no injection risk in the current implementation**.

### Finding 2 — LOW: AppleScript epoch ≠ Unix epoch (silent date bug)

```applescript
set evStart to ((start date of ev) as integer)
```

In AppleScript, casting a `date` object to `integer` returns **seconds since 2001-01-01 00:00:00
local time** (the Mac OS X reference date), not seconds since 1970-01-01 (Unix epoch).

The Python side passes this value directly to `datetime.fromtimestamp()`:

```python
# calendar_link.py line 52
return datetime.fromtimestamp(epoch_sec).isoformat(timespec="seconds")
```

`datetime.fromtimestamp()` assumes Unix epoch. The offset between the two epochs is
**978 307 200 seconds** (~31 years). A calendar event starting at 2026-01-01 will appear as
1994-11-26 in `start_iso`/`end_iso`.

This does not crash (the values are valid Unix timestamps in the past), but the stored ISO
strings are meaningless. Any downstream comparison using `start_iso` as a wall-clock time will
produce wrong results.

**Fix:** subtract the Mac reference epoch offset before calling `fromtimestamp()`, or use a
pure-Python AppleScript date string instead of `as integer` in the script.

```python
_MAC_EPOCH_OFFSET = 978_307_200  # seconds between 1970-01-01 and 2001-01-01

def _epoch_to_iso(epoch_sec: int) -> str:
    try:
        unix_ts = epoch_sec + _MAC_EPOCH_OFFSET
        return datetime.fromtimestamp(unix_ts).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""
```

### Finding 3 — LOW: `|||` separator in Calendar data corrupts field parsing

`_parse_osascript_output` splits each line on `"|||"`. If a calendar event title, location, or
calendar name contains the literal string `|||`, the split produces extra fields and the
`parts[0..4]` index mapping breaks silently. The parser skips lines with `len(parts) < 5` but
does **not** guard against `len(parts) > 5`, so title/location fields would be misread.

Exploitation requires a user to deliberately name a calendar event with `|||` — unlikely in
practice but worth noting. The fix is to use `split("|||", maxsplit=4)` so that at most 5 parts
are produced regardless of how many `|||` sequences appear in the data.

---

## telegram_bridge.py

### Finding 4 — MEDIUM: No localhost enforcement (SSRF risk)

`TelegramBridge.__init__` accepts an arbitrary `base_url` with no validation:

```python
def __init__(self, base_url: str = "http://localhost:8080", ...) -> None:
    self._base_url = base_url.rstrip("/")
```

In `service.py` line 478 the bridge is constructed with:

```python
self._telegram_bridge = TelegramBridge(base_url=settings.TELEGRAM_BRIDGE_URL)
```

`TELEGRAM_BRIDGE_URL` is a Pydantic-Settings field (`core/config.py` line 397) overridable via
the `KRAB_EAR_TELEGRAM_BRIDGE_URL` environment variable. An attacker who can set that env var,
or a future IPC method that allows settings writes without validation, could point the bridge at
an internal network host, turning `send_message` / `get_chats` into an SSRF gadget.

The IPC `set_settings` path does not currently expose `TELEGRAM_BRIDGE_URL` as a user-settable
key (it is configured at process startup), so the practical exploitability is low in the current
deployment. However, the class itself offers no defense in depth.

**Recommended fix:** add a URL validation step in `__init__` that asserts the host is a loopback
address:

```python
from urllib.parse import urlparse

def __init__(self, base_url: str = "http://localhost:8080", ...) -> None:
    parsed = urlparse(base_url)
    if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(
            f"TelegramBridge base_url must target localhost, got: {parsed.hostname!r}"
        )
    self._base_url = base_url.rstrip("/")
```

### Finding 5 — LOW: No HTTP retry — single failure increments circuit breaker

Both `send_message` and `get_chats` make a **single HTTP attempt** with no retry loop. A
transient connection hiccup (DNS blip, short OS TCP backlog) immediately calls `_record_failure`,
incrementing the circuit-breaker failure counter. With the default threshold of 3, three
back-to-back transient errors open the circuit for 60 seconds, blocking all bridge traffic
including legitimate messages.

This is intentional for a local IPC-style call where "transient" errors are unusual, but the
design means a burst of 3 timeout events (e.g., Krab restart taking >5 s) opens the circuit
unnecessarily. A single retry with a short delay on `ConnectionError` / `Timeout` would reduce
false positives without adding meaningful latency.

No retry is currently documented as a deliberate choice; the TODO comment at line 14 is
unrelated to retry policy.

### Finding 6 — INFO: Circuit breaker implementation is correct

The circuit breaker uses a `threading.Lock`-protected counter and monotonic timestamp.
Half-open logic (automatic reset after `circuit_reset_sec`) is present and correctly implemented:
`_is_open()` clears both `_open_at` and `_fail_count` upon expiry, allowing one probe request
through before re-opening if it fails. The `reset_circuit()` method provides a diagnostic escape
hatch. The implementation is thread-safe and does not have the common double-check locking bug.

---

## Recommendations (priority order)

1. **(MEDIUM)** Add localhost validation in `TelegramBridge.__init__` — 5-line change, eliminates
   SSRF class of issue regardless of future config path changes.
2. **(LOW)** Fix AppleScript epoch offset in `_epoch_to_iso` — one constant + arithmetic; prevents
   silently wrong calendar timestamps propagating into transcription records.
3. **(LOW)** Use `split("|||", maxsplit=4)` in `_parse_osascript_output` — 1-char change, makes
   parsing robust against edge-case Calendar data.
4. **(LOW)** Consider a single retry (with 0.5 s sleep) on `ConnectionError`/`Timeout` in
   `TelegramBridge` to reduce false-positive circuit-breaker trips during Krab restarts.
