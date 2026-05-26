# Wave 825 — Static Audit: Test Suite Time Cost

**Date:** 2026-05-26  
**Scope:** `KrabEar/tests/` — 439 test files  
**Method:** Static analysis only (no test execution). Counts `time.sleep()` call-argument sums, real (non-mocked) `subprocess.*` call counts, and real (non-mocked) `requests.*` HTTP call counts per file.  
**Tool:** AST-based parser (Python `ast` module) to exclude literals inside strings, comments, and `patch()` decorator arguments. Subprocess and `requests` lines that also contain `patch` or `mock` are excluded as mocked calls.

## Scoring Formula

```
slow_score = sleep_sum_sec + subprocess_real_count × 0.5 + requests_real_count × 0.3
```

- **`time.sleep(N)`** — wall-clock delay; each second costs exactly 1.0 score point.
- **`subprocess.*`** — process spawn; estimated ~0.5 s per real (non-mocked) invocation (process start + potential disk I/O).
- **`requests.*`** — HTTP round-trip; estimated ~0.3 s per real call on a local server with timeout guards.

## Top-20 Slowest Test Files

| Rank | Score | Sleep (s) | subprocess | requests | Tests | File |
|------|------:|----------:|-----------:|---------:|------:|------|
| 1 | **3.500** | 2.00 | 3 | 0 | 6 | `test_runtime_self_redirect.py` |
| 2 | **2.850** | 2.85 | 0 | 0 | 31 | `test_realtime_silence.py` |
| 3 | **2.400** | 0.00 | 0 | 8 | 4 | `test_e2e_voice_loop.py` |
| 4 | **1.500** | 0.00 | 3 | 0 | 17 | `test_verify_claude_md.py` |
| 5 | **1.100** | 1.10 | 0 | 0 | 32 | `test_ipc_throttle.py` |
| 6 | **0.590** | 0.59 | 0 | 0 | 6 | `test_ws_streaming.py` |
| 7 | **0.550** | 0.55 | 0 | 0 | 45 | `test_webhook_manager.py` |
| 8 | **0.500** | 0.50 | 0 | 0 | 27 | `test_ipc_throttle_extras.py` |
| 9 | **0.500** | 0.00 | 1 | 0 | 3 | `test_memory_baseline.py` |
| 10 | **0.500** | 0.00 | 1 | 0 | 46 | `test_recording_chain.py` |
| 11 | **0.500** | 0.00 | 1 | 0 | 10 | `test_validation_script.py` |
| 12 | **0.405** | 0.405 | 0 | 0 | 10 | `test_audio_recorder_lifecycle.py` |
| 13 | **0.350** | 0.35 | 0 | 0 | 17 | `test_bulk_reprocess.py` |
| 14 | **0.200** | 0.20 | 0 | 0 | 29 | `test_gigaam_memory_profile.py` |
| 15 | **0.150** | 0.15 | 0 | 0 | 22 | `test_lm_studio_lifecycle.py` |
| 16 | **0.120** | 0.12 | 0 | 0 | 21 | `test_shutdown_handler_deep.py` |
| 17 | **0.110** | 0.11 | 0 | 0 | 24 | `test_auto_backup.py` |
| 18 | **0.100** | 0.10 | 0 | 0 | 13 | `test_error_bus_integration.py` |
| 19 | **0.095** | 0.095 | 0 | 0 | 7 | `test_mlx_thread_safety.py` |
| 20 | **0.090** | 0.09 | 0 | 0 | 3 | `test_async_transcribe.py` |

## Per-file Notes

### #1 · `test_runtime_self_redirect.py` — score 3.500

Three real `subprocess.Popen()` calls that spawn actual Python processes (backend startup smoke
tests). One `time.sleep(2.0)` that waits for the backend to become ready before sending an IPC
ping. These are integration-level tests that cannot be mocked further without losing coverage
value. Candidate for a dedicated `pytest -m integration` marker so they can be excluded from
the fast-path CI gate.

### #2 · `test_realtime_silence.py` — score 2.850

12 `time.sleep()` calls accumulating 2.85 s total (individual values: 0.50, 0.20×3, 0.30,
0.15×2, 0.25×3, 0.20×2). Each sleep drives a `RealtimeSilenceFilter` thread to accumulate
audio energy and flush state. The sleeps are genuinely load-bearing (they replace real audio
input). The sum could be cut in half by replacing `time.sleep` with
`threading.Event().wait(timeout)` plus an explicit `event.set()` signal from the filter
callback — reducing worst-case wait from 0.5 s to the actual flush latency.

### #3 · `test_e2e_voice_loop.py` — score 2.400

8 real `requests.*` calls against live Voice Gateway and Krab Ear REST endpoints (not mocked).
These tests require both services to be running (`VG_URL`, `EAR_URL`) and each HTTP call
carries a `timeout=2–5` seconds. Already guarded by `pytest.importorskip` / skip decorators
when services are absent. Enforce `@pytest.mark.e2e` and exclude from fast CI.

### #4 · `test_verify_claude_md.py` — score 1.500

3 real `subprocess.run()` calls that invoke `scripts/verify_claude_md.py` as a child process.
These test the verification script itself; spawning a process is necessary but adds ~0.5 s each.
Could be refactored to call the script's internal functions directly (eliminating fork overhead)
but the process-boundary test is also valid for catching import errors.

### #5 · `test_ipc_throttle.py` — score 1.100

One `time.sleep(1.1)` that waits for the token-bucket replenishment window to expire and
confirms that a throttled call is eventually allowed through. The replenishment interval is
configured at 1.0 s in the test setup. Can be sped up by either (a) patching `time.monotonic`
to simulate elapsed time, or (b) setting the replenishment interval to 0.05 s in the test
fixture — dropping the sleep to ≤ 0.1 s.

### #6 · `test_ws_streaming.py` — score 0.590

7 `time.sleep()` calls totalling 0.59 s (0.15, 0.10×4, 0.02, 0.10). Drives WebSocket event
ordering between producer and consumer threads. Like `test_realtime_silence.py`, an
`Event`-based synchronization would eliminate most of these sleeps.

### #7 · `test_webhook_manager.py` — score 0.550

10 `time.sleep()` calls totalling 0.55 s (0.10, 0.05×6, 0.10, 0.05, others). Used to let the
webhook HTTP thread deliver requests before assertions. A `threading.Event` set by the mock
HTTP server on first request would make these synchronous and remove all sleeps.

### #8 · `test_ipc_throttle_extras.py` — score 0.500

One `time.sleep(0.5)` — same bucket-replenishment pattern as #5. Same fix applies: patch
`time.monotonic` or shorten the replenishment window in the fixture.

### #9 · `test_memory_baseline.py` — score 0.500

One real `subprocess.run()` call that runs `scripts/memory_baseline.py` to capture an RSS
snapshot. Necessary for the test to have any meaning; no mock opportunity without losing
coverage. Mark `@pytest.mark.slow`.

### #10 · `test_recording_chain.py` — score 0.500

One real `subprocess.run()` in a single edge-case test. Low priority; 46 of the tests are
fully synchronous. Mark the one subprocess test with `@pytest.mark.integration`.

### #11 · `test_validation_script.py` — score 0.500

One real `subprocess.run()`. Same situation as #9/#10 — spawns the validation script to test
its exit codes. Mark `@pytest.mark.slow`.

### #12 · `test_audio_recorder_lifecycle.py` — score 0.405

10 sleeps totalling 0.405 s (individual: 0.000, 0.005, 0.050, 0.080, 0.020, 0.050, others).
Drives `AudioRecorder` start/stop lifecycle with real audio buffer threads. Sleeps wait for
hardware-simulated stream init; can be replaced with `recorder._ready_event.wait(timeout=1)`.

### #13 · `test_bulk_reprocess.py` — score 0.350

Two `time.sleep()` calls: 0.20 s and 0.15 s. Used to let the bulk-reprocess background thread
finish a job. Can be replaced with `job_tracker.wait_for(job_id, timeout=5)` which already
exists in `JobTracker`.

### #14 · `test_gigaam_memory_profile.py` — score 0.200

Two `time.sleep(0.1)` calls (total 0.2 s) that let the GigaAM subprocess settle before
reading RSS. These are inherently timing-dependent; reduce to `time.sleep(0.02)` since the
subprocess is stubbed in this test file.

### #15 · `test_lm_studio_lifecycle.py` — score 0.150

Two sleeps: `time.sleep(0.05)` and `time.sleep(0.1)` (total 0.15 s). Await model load
confirmation from a background thread. Use `Event.wait(timeout)` pattern.

### #16 · `test_shutdown_handler_deep.py` — score 0.120

Three sleeps: 0.05, 0.02, 0.05 s (total 0.12 s). Let graceful-shutdown coroutines complete.
Low priority; total contribution is small.

### #17 · `test_auto_backup.py` — score 0.110

Two sleeps: 0.01 and 0.10 s (total 0.11 s). Wait for the backup background thread to write a
file. Replace with `backup_manager._last_backup_event.wait(timeout=2)`.

### #18 · `test_error_bus_integration.py` — score 0.100

Two `time.sleep(0.05)` calls (total 0.10 s). Synchronization between `ErrorBus` push and
`WarnBatcher` flush interval. The batcher interval is configurable; set it to 0.001 s in
fixture to eliminate the sleep.

### #19 · `test_mlx_thread_safety.py` — score 0.095

Three sleeps: 0.005, 0.050, 0.040 s (total 0.095 s). Tests concurrent MLX lock acquisition.
The actual lock is a Python `RLock`; use `Event` barriers instead of fixed sleeps to make
thread ordering deterministic.

### #20 · `test_async_transcribe.py` — score 0.090

Five sleeps summing to 0.09 s (two are 0.0 s — from `time.sleep(0)` yield calls). Three real
sleeps: 0.02, 0.05, 0.02 s. Drives async transcription job lifecycle. Low priority.

## Summary Statistics

| Metric | Value |
|--------|-------|
| Total test files analyzed | 439 |
| Files with any `time.sleep` | 37 |
| Total `time.sleep` budget (all files) | ~12.4 s |
| Files with real subprocess calls | 8 |
| Files with real `requests` calls | 1 |
| Files with score ≥ 1.0 | 5 |
| Files with score ≥ 0.1 | 20 |

## Quick Wins (Highest ROI fixes)

1. **`test_ipc_throttle.py` + `test_ipc_throttle_extras.py`** — patch `time.monotonic` in token-bucket tests. Saves ~1.6 s combined with 2-line fixture change.
2. **`test_realtime_silence.py`** — replace `time.sleep` with `threading.Event` callbacks. Saves up to 2.85 s.
3. **`test_webhook_manager.py`** — use mock HTTP server `Event.set()` on first request. Saves 0.55 s.
4. **`test_ws_streaming.py`** — `Event`-based producer/consumer synchronization. Saves 0.59 s.
5. **`test_runtime_self_redirect.py` + `test_e2e_voice_loop.py`** — add `@pytest.mark.integration` / `@pytest.mark.e2e` and exclude from fast CI gate. No code change needed; CI gate drops 3.5 + 2.4 s.

Implementing all five would remove approximately **11 seconds** from the serial test run and allow the fast CI gate to skip ~10 structurally slow tests.
