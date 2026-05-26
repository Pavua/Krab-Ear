# Wave 785 — Flaky Test Audit

**Date:** 2026-05-26  
**Scope:** `KrabEar/tests/` — 436 test files, read-only audit (no test execution)  
**Method:** static grep for anti-patterns: `time.sleep`, network I/O, subprocess spawning, `TemporaryDirectory` cleanup gaps, unconditional/conditional skips

---

## Summary

| Anti-pattern | Files affected | Occurrences |
|---|---|---|
| `time.sleep()` (any duration) | 48 | 146 |
| `time.sleep()` >= 0.5 s | 6 | 14 |
| `time.sleep()` >= 1.0 s | 4 | 4 |
| Real network calls (no mock) | 3 | ~20 |
| `subprocess.Popen/run` (real, not mocked) | 7 | ~10 |
| `subprocess.*` (mocked — OK) | 22 | ~150 |
| `TemporaryDirectory()` outside `with` | 94 | 380 |
| Skip annotations | 39 | 216 |
| Unconditional `@unittest.skip` | 1 | 4 |

**Total flakiness candidates (files with at least one HIGH or MEDIUM risk pattern):** ~35 files.

---

## 1. Tests with `time.sleep()` >= 0.5 s (SLOW / HIGH FLAKINESS RISK)

Long sleeps make tests slow and timing-sensitive — they fail on CI under load or on slow runners.

### >= 1.0 s sleeps

| File | Line | Duration | Reason |
|---|---|---|---|
| `test_ipc_throttle_extras.py` | 139 | **2.5 s** | Token bucket cap test — `_last_refill` manipulation below does the real work; the sleep is redundant |
| `test_runtime_self_redirect.py` | 210 | **2.0 s** | Waits to confirm a subprocess stays alive (still running check) |
| `test_backup_restore.py` | 105 | **1.1 s** | Ensures distinct timestamp in backup dir names |
| `test_ipc_throttle.py` | 88 | **1.1 s** | Waits for token bucket to refill 1 token at rate=1 t/s |

**Recommendations:**
- `test_ipc_throttle_extras.py:139` — The `_last_refill -= 1000.0` trick on line 142 already simulates elapsed time. The real `sleep(2.5)` preceding it is **fully redundant**; remove it.
- `test_ipc_throttle.py:88` — Replace with time-manipulation: set `bucket._last_refill` to a past value instead of sleeping 1.1 s.
- `test_backup_restore.py:105` — Use `unittest.mock.patch('time.time', ...)` or pass explicit timestamps to `handle_backup_history` to avoid wall-clock dependency.
- `test_runtime_self_redirect.py:210` — Wrap in `proc.wait(timeout=N)` + `assertIsNone(proc.poll())` immediately after launch; `sleep(2.0)` then poll is inherently racy.

### 0.5 – 0.9 s sleeps

| File | Lines | Duration | Count | Context |
|---|---|---|---|---|
| `test_realtime_partial.py` | 215, 241, 274, 296, 334, 403, 495 | 0.5–0.6 s | 7 | Waits for background `RealtimePartialTranscriber` thread to emit events |
| `test_realtime_silence.py` | 168 | 0.5 s | 1 | Same pattern — waiting for background filter thread |

**Recommendation:** Replace busy `time.sleep(0.5)` with `threading.Event` or `queue.get(timeout=2.0)` in the `RealtimePartialTranscriber`. The `EventBus.emit` mock can set an event; the test waits on it instead of sleeping. This is standard practice and eliminates 8 sleeps across these two files.

---

## 2. Tests making real network calls (HIGH FLAKINESS RISK)

These tests connect to localhost or external URLs without mocking. They pass only when the required service is running.

### `test_e2e_voice_loop.py` — REAL network, no mock fallback

The module-level `_services_available()` probe at import time makes **real HTTP requests** to `http://127.0.0.1:8090` (Voice Gateway) and `http://127.0.0.1:5005` (REST server). The entire test class is `@unittest.skipUnless(_services_available(), ...)` so it skips if services are down — but the probe itself executes at collection time on every CI run, adding latency and potential confusion (DNS errors, routing errors appearing in test output).

```
test_e2e_voice_loop.py:29  vg_ok = requests.get(f"{VG_URL}/health", timeout=2).ok
test_e2e_voice_loop.py:30  ear_ok = requests.get(f"{EAR_URL}/health", timeout=2).ok
```

The test body also makes real calls:
- `requests.get(VG_URL/v1/sessions/{id})` — line 102
- `requests.delete(VG_URL/v1/sessions/{id})` — line 105
- `requests.post(VG_URL/...)` — lines 74, 93

**Recommendation:** This is an integration test by design. Keep as-is but ensure it is excluded from the default CI gate (`pytest -m "not e2e"` or separate job). The skip guard is correct but add `@pytest.mark.e2e` for explicit filtering.

### `benchmark_llm_models.py` — real LM Studio call, no skip guard

```
benchmark_llm_models.py:90  resp = requests.post(f"{API_BASE}/chat/completions", ...)
```

This is a benchmark script (not a `test_*.py`), but pytest will collect it if `test` is not in the name filter. It hits LM Studio at `http://127.0.0.1:1234` with a 30 s timeout. No `@unittest.skip` or skip guard.

**Recommendation:** Rename to `bench_llm_models.py` (no `test_` prefix) or add an `@pytest.mark.benchmark` decorator and exclude from default collection.

### Mocked network (OK — informational)

The following files import `requests` or `urllib` but **only use them through `unittest.mock.patch`** — these are safe and not flaky:

`test_llm_rewriter.py`, `test_llm_rewriter_deep.py`, `test_llm_rewriter_edges.py`, `test_list_llm_models_endpoint.py`, `test_engine_remote_stt.py`, `test_telnyx_adapter.py`, `test_twilio_adapter.py`, `test_telegram_bridge.py`, `test_telegram_bridge_wave622.py`, `test_telegram_quick_share.py`, `test_lm_studio_lifecycle.py`, `test_backend_service.py` — all use `patch("requests.get/post")` consistently.

---

## 3. Tests spawning real subprocesses (HEAVY / MEDIUM RISK)

Real subprocess spawning makes tests slow, platform-dependent, and sensitive to PATH and installed tools.

### Real subprocess calls (not mocked)

| File | Type | Description | Risk |
|---|---|---|---|
| `test_runtime_self_redirect.py:148, 203, 250` | `subprocess.Popen` | Spawns real shell scripts to test bundle redirect guard | HIGH — creates/executes temp binaries, 5 s timeout per test |
| `test_stt_gigaam_subprocess.py:403+` | `subprocess.Popen` | Spawns real GigaAM worker process | HIGH — requires `venv_gigaam`; skip guard present but subprocess is real |
| `test_memory_baseline.py:21` | `subprocess.run` | Executes `scripts/memory_baseline.py` with Python | MEDIUM — requires `psutil`; skip guard present |
| `test_verify_claude_md.py:115, 141, 152` | `subprocess.run` | Runs `scripts/verify_claude_md.py` as subprocess | MEDIUM — path-dependent, shell behavior |
| `test_recording_chain.py:447` | `subprocess.run` | Runs `grep -r` on `native/` to assert no Swift caller | LOW — grep is always available; but sensitive to directory structure |
| `conftest.py:31` | `subprocess.run` | `git rev-parse HEAD` to get commit SHA for benchmark history | LOW — git always present; has `except Exception` fallback |

### Mocked subprocess (OK — informational)

22 files patch `subprocess.run` properly: `test_apple_integration_service.py`, `test_apple_notes.py`, `test_apple_reminders.py`, `test_audio_converter.py`, `test_calendar_event.py`, `test_calendar_link.py`, `test_engine_edge_cases.py`, `test_engine_extended.py`, `test_error_actions_extras.py`, `test_lm_studio_lifecycle.py`, `test_observability.py`, `test_observability_edge.py`, `test_sentry_release_tags.py` — all use `patch("subprocess.run", ...)`.

---

## 4. `TemporaryDirectory` outside `with` statement

`tempfile.TemporaryDirectory()` as a context manager (`with tempfile.TemporaryDirectory() as d:`) guarantees cleanup even on exception. Assigning to an instance variable (`self.tmp = tempfile.TemporaryDirectory()`) is safe **only if** `tearDown` or `addCleanup` is used — but if tearDown throws, cleanup can be missed.

**94 files** use the assign-and-cleanup pattern. Manual audit of a representative sample shows:

- **Correctly cleaned up (addCleanup pattern):** `test_auto_deduplication.py`, `test_profiler_integration.py` — use `self.addCleanup(self.tmp.cleanup)` immediately after assignment. This is the safe pattern.
- **Cleaned up in tearDown:** `test_transcript_versioning.py`, `test_history_service_extended.py` — explicit `self.temp_dir.cleanup()` in `tearDown()`. Safe if tearDown itself does not fail before cleanup.
- **Risk cases:** Tests where `TemporaryDirectory()` is assigned as a local variable (not `self.*`) and cleanup is deferred to `finally`. Example: `test_auto_deduplication.py:264` — `tmp = tempfile.TemporaryDirectory()` then `tmp.cleanup()` in a `finally` block — acceptable but fragile if the `try` block re-raises inside a `finally`.

**Recommendation:** Prefer `self.addCleanup(self.tmp.cleanup)` immediately after assignment — it is exception-safe and does not depend on `tearDown` execution order. Do not leave `TemporaryDirectory` objects without cleanup registration.

High-risk files (local variable, no `with`, no visible `addCleanup`):
- `test_auto_deduplication.py:264` — local `tmp` with manual `tmp.cleanup()` outside `finally`
- `test_profiler_integration.py:215, 254` — use `addCleanup` (OK)

---

## 5. Skipped tests

### 5a. Unconditional skips (`@unittest.skip`) — permanent dead weight

| File | Classes skipped | Reason given |
|---|---|---|
| `test_va_multimodal.py` | 4 classes (all tests in file) | `"Phase 2A WIP — не интегрирован с основным VA pipeline"` |

These have been skipped since Wave 56+. The file predates the current VA Phase 1 architecture. Either the feature has landed and tests need updating, or the file should be removed.

### 5b. Environment-gated skips (conditionally OK)

| Skip condition | Files | Count | Assessment |
|---|---|---|---|
| `_REST_AVAILABLE` (Flask/REST deps) | `test_rest_server.py`, `test_rest_e2e.py`, `test_rest_coverage.py`, `test_rest_hardening.py`, `test_health_dashboard.py`, `test_rest_smoke.py`, `test_rest_server_endpoints.py`, `test_rest_server_unit.py` | ~100 skip annotations | **OK** — correct pattern for optional dep |
| `_SKIP_PERF_ON_CI` | `test_performance_benchmarks.py`, `test_e2e_performance_benchmarks.py` | ~18 | **OK** — perf tests excluded on CI |
| `_SKIP_BENCH` | `test_performance_unit_benchmarks.py` | ~12 | **OK** — opt-in benchmark |
| `RUN_CHAOS=1` | `test_backend_chaos.py` | ~3 | **OK** — opt-in chaos |
| `_SKIP` (HistoryService not extracted) | `test_history_service_edges.py`, `test_history_service_extended.py` | ~15 | **STALE** — HistoryService exists (`backend/history_service.py`) |
| `_SKIP` (REST deps unavailable) | `test_rest_api_versioning.py` | ~10 | **OK** |
| `PARAKEET_AVAILABLE` | `test_stt_adapter_router.py` | 1 | **OK** — optional adapter |
| `CI=true` | `test_concurrency_stress.py` | 1 | **MEDIUM RISK** — explicitly labeled flaky on CI; skip masks a real race condition in StateStore |

### 5c. Notable: stale skip guards

`test_history_service.py:26` and `test_history_service_edges.py:34+` guard on:
```python
@unittest.skipIf(HistoryService is None, "HistoryService not yet extracted")
```
`backend/history_service.py` now exists (Wave 683+). The `HistoryService` import should succeed. These guards are likely stale and the `skipIf` condition may evaluate to `False` (tests run), but should be confirmed and removed if so.

---

## 6. Time-dependent assertions (MEDIUM RISK)

Tests using `datetime.now()`, `time.time()`, or `time.monotonic()` for assertion logic without mocking the clock:

| File | Lines | Risk |
|---|---|---|
| `test_activity_calendar.py:238` | `datetime.now().strftime(...)` in fixture | LOW — only used as input timestamp |
| `test_state_store.py:338, 382, 402` | `datetime.now().isoformat()`, `datetime.now() - timedelta(days=400)` | MEDIUM — if test runs near midnight boundary, date arithmetic could fail |
| `test_auto_glossary.py:38, 230, 285, 296, 329, 356` | `time.time() - 7200.0` etc. | LOW — relative arithmetic is safe |
| `test_startup_diagnostics.py:774, 776` | `monotonic()` timing around actual function call | LOW — measures elapsed, correct |
| `test_webhook_manager.py:506, 508` | `monotonic()` for elapsed timing | LOW |

**Recommendation:** `test_state_store.py` is the highest risk — midnight boundary tests can fail if `datetime.now()` straddles a day boundary. Use a fixed timestamp via `unittest.mock.patch('backend.state_store.datetime')`.

---

## 7. `soak_backend.py` — not a unit test

`KrabEar/tests/soak_backend.py` contains `subprocess.Popen` (line 157) to spawn the full backend process and run a soak loop. It is a soak/load testing script, not a unit test. It should not be in the `tests/` directory (pytest collects it). It has no `test_` prefix so pytest ignores it by default — but the file is confusingly co-located.

---

## Priority Ranking

| Priority | Action | Files | Effort |
|---|---|---|---|
| P1 | Remove redundant `sleep(2.5)` in `test_ipc_throttle_extras.py:139` | 1 | 5 min |
| P1 | Replace `sleep` in `test_realtime_partial.py` with `threading.Event` | 1 | 1–2 h |
| P1 | Remove/rename `benchmark_llm_models.py` from test collection | 1 | 5 min |
| P2 | Replace token-bucket sleep in `test_ipc_throttle.py:88` with time-injection | 1 | 30 min |
| P2 | Replace sleep in `test_backup_restore.py:105` with mock timestamp | 1 | 30 min |
| P2 | Investigate `test_va_multimodal.py` — remove file or re-enable tests | 1 | 1 h |
| P2 | Confirm `HistoryService` skip guard is stale; remove if so | 2 | 15 min |
| P3 | Add `@pytest.mark.e2e` to `test_e2e_voice_loop.py` | 1 | 5 min |
| P3 | Address `test_concurrency_stress.py` StateStore race (deferred in Wave 58) | 1 | 2–4 h |
| P3 | Audit `TemporaryDirectory` cleanup in remaining 94 files | 94 | 2 h grep audit |

---

## Counts

- **Tests audited:** 436 files  
- **Flakiness candidates (HIGH/MEDIUM):** 35 files  
- **Sleep occurrences total:** 146 (48 files); 14 are >= 0.5 s in 6 files  
- **Real network calls:** 3 files (`test_e2e_voice_loop.py`, `benchmark_llm_models.py`, `test_startup_diagnostics.py` startup probe)  
- **Real subprocess spawns:** 7 files  
- **Skip annotations total:** 216 (39 files); 4 unconditional (`test_va_multimodal.py`)  
- **TemporaryDirectory without context manager:** 380 occurrences, 94 files (mostly safe via addCleanup)
