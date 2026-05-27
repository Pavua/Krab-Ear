# Wave 922 Audit: `backend/recap_scheduler.py`

**Date:** 2026-05-26  
**Auditor:** Sub-agent W922  
**Files reviewed:** `KrabEar/backend/recap_scheduler.py` (377 lines), `KrabEar/backend/email_sender.py`, `KrabEar/backend/daily_digest.py`, `KrabEar/tests/test_recap_scheduler.py`

---

## Summary

`RecapScheduler` is a daemon-thread cron-like scheduler that fires a daily digest email at a configured hour. The core state-persistence and thread-lifecycle design is sound, but the module has a meaningful TOCTOU race between `_should_send` and the actual send+state-update (two callers can both pass the check and both fire the email in the same minute window), settings baked at construction are never re-read from the live settings store (sister to W918), and production observability is entirely absent — no Sentry breadcrumbs or error captures. The test suite is comprehensive (20 tests) but does not cover the TOCTOU race under concurrent `send_recap` calls or the settings-staleness path.

---

## HIGH Findings

### H1 — TOCTOU race: `_should_send` + `send_recap` not atomic (double-fire possible)

**Lines:** `recap_scheduler.py:227–237` (`_should_send`), `recap_scheduler.py:276–281` (state write in `send_recap`)

`_should_send` reads `last_sent_date` from disk and returns `True`; then `send_recap` sends the email and *only then* acquires `self._lock` to write `last_sent_date`. If two threads both call `_should_send` simultaneously before either has updated state — for example via a manual IPC `send_recap` call arriving at the same time as the scheduler tick — both see `last_sent_date != today`, both send, and both increment `send_count`. The lock in `send_recap` (line 276) protects the state write but not the check-then-send sequence, so the guard is incomplete.

**Concrete path:**
1. Scheduler tick fires `send_recap()` at 20:00:00.
2. User triggers IPC `send_recap()` at 20:00:01 before tick completes.
3. Both pass `_should_send`, both call `email_sender.send`, user receives two identical digests.

**Fix:** wrap `_should_send` + the send call in the same `self._lock`, or re-check `last_sent_date` inside the lock before sending.

---

### H2 — Settings staleness: `recap_time_hour`, `enabled`, `recap_email_to` baked at `__init__`

**Lines:** `recap_scheduler.py:192–196` (constructor), `recap_scheduler.py:229, 231, 233` (used in `_should_send`)

`recap_time_hour`, `enabled`, and `recap_email_to` are stored as plain instance attributes at construction time and never refreshed. If the user changes the digest hour or toggles the feature via IPC `set_settings`, the running scheduler thread continues using stale values until the backend restarts. This is the same class of bug found in W918 for other services. The thread loop at line 303–311 calls `self._should_send(now)` on every tick, which reads `self.recap_time_hour` (line 233) — a value frozen since startup.

**Fix:** read `recap_time_hour`, `enabled`, and `recap_email_to` from the live settings store (via `_get_runtime_setting`) on each tick, following the pattern established in `service.py:593–601`.

---

## MEDIUM Findings

### M1 — Naive datetime throughout: DST causes double-fire or missed send

**Lines:** `recap_scheduler.py:195` (`self._clock_fn = clock_fn or datetime.now`), `recap_scheduler.py:233` (`now.hour != self.recap_time_hour`)

`datetime.now()` returns a naive local datetime. The docstring says "UTC local time" which is contradictory. When DST rolls back at 02:00 (clocks repeat 01:00 twice), the scheduler can fire twice in the same calendar day if `recap_time_hour=1`. When DST springs forward and skips an hour, a digest at that hour is missed entirely. Using `datetime.now(tz=ZoneInfo("Europe/..."))` or `datetime.now(timezone.utc)` with an explicit hour-in-UTC policy would resolve the ambiguity.

---

### M2 — Email failure has no retry: digest silently dropped for the day

**Lines:** `recap_scheduler.py:264–273` (email send try/except), `recap_scheduler.py:303–311` (scheduler loop)

When `email_sender.send` raises (SMTP timeout, DNS failure, Keychain unavailable), `send_recap` returns `{"sent": False, ...}` and does **not** update `last_sent_date`. This means the next tick (60 seconds later) will call `_should_send` again — which looks correct at first glance. However, `_should_send` checks `now.hour != self.recap_time_hour` (line 233). Because ticks happen every 60 seconds, any SMTP failure that takes more than ~59 minutes to resolve causes the scheduler to fall out of the send window (`now.hour` advances past `recap_time_hour`) and the digest is silently lost for the day. There is no exponential backoff, no retry budget, and no dead-letter notification.

---

### M3 — `stop()` without thread-alive check after join: phantom second thread

**Lines:** `recap_scheduler.py:328–333`

```python
def stop(self) -> None:
    self._stop_event.set()
    if self._thread is not None:
        self._thread.join(timeout=5)
        self._thread = None   # ← unconditional, even if thread still alive
```

If `email_sender.send` is blocking in a long SMTP handshake, `join(timeout=5)` returns with the thread still alive. `_thread` is then set to `None`. A subsequent `start()` call (e.g., on settings-reload) passes the `is_alive()` guard (line 318) because `_thread is None`, spawning a second scheduler thread. Both threads now run concurrently, doubling email send attempts.

**Fix:** after `join`, check `self._thread.is_alive()` and log a warning if still running before setting `_thread = None`.

---

## LOW Findings

### L1 — No Sentry breadcrumbs or error captures

**Lines:** `recap_scheduler.py:283–288` (success log), `recap_scheduler.py:308–309` (exception log in loop)

All scheduler events use only `logger.info` / `logger.exception`. The production observability pattern established in `backend/observability.py` (Sentry breadcrumbs + `capture_exception`) is not applied here. A digest failure in production is invisible in the Sentry dashboard and won't appear in crash breadcrumb trails. Minimum: `add_breadcrumb(category="recap", message="send_recap", data={"ok": result["sent"], "date": date_str})` after each send attempt, and `capture_exception` on the outer `except` in `_run` (line 308).

---

## Test Coverage Observations

- **20 tests** across 9 test classes; passes cleanly.
- `TestSMTPFailure` (tests 4a, 4b) and `TestWave138RecapScheduler.test_handles_email_failure_*` correctly verify that state is not updated on send failure.
- `test_concurrent_trigger_idempotent` (line 487) tests 5 concurrent `send_recap` calls — but it only asserts no crash and correct return shape, **not** that `email_sender.send` was called exactly once. The race (H1) goes undetected.
- No test for settings staleness (H2): no test changes `recap_time_hour` post-construction and verifies the thread picks it up.
- No test for DST / timezone-aware datetime (M1).
- No test for the `stop()` → `start()` double-thread scenario (M3).

---

## Solid (What's Already Good)

- **Stop event pattern**: uses `threading.Event.wait(timeout=...)` instead of `time.sleep`, making the thread responsive to `stop()` within one interval — correct pattern.
- **Daemon thread**: `daemon=True` ensures the thread does not block clean process exit.
- **State persistence**: `recap_state.json` prevents double-send across backend restarts; `_save_state` is called only after confirmed send success.
- **Exception guard in `_run`**: the `try/except Exception` at line 304–309 prevents a crash in `_should_send` or `send_recap` from killing the scheduler thread permanently.
- **`clock_fn` injection**: makes the module fully testable without real time dependencies — good design.
- **`start()` idempotency**: correctly checks `is_alive()` before spawning a new thread (line 318).
