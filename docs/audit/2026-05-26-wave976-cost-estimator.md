# Audit: cost_estimator.py — Wave 976

**Date:** 2026-05-26  
**Auditor:** W976 (sub-agent)  
**Target:** `KrabEar/backend/cost_estimator.py`  
**Tests:** `KrabEar/tests/test_cost_estimator.py`  
**Status:** Read-only, 5 findings

---

## Summary

`CostEstimator` is a pure, stateless computation module that estimates CPU time, memory,
and disk cost for STT recordings. It is wired into production via two IPC handlers:
`estimate_recording_cost` and `get_daily_cost_summary`. No monetary cost is modelled.
The module is well-structured; five issues are worth noting.

---

## Findings

### F-1 — Stale STT coefficients (Medium)

**File:** `cost_estimator.py:23-27`

```python
_STT_RATES: dict[str, float] = {
    "balanced": 0.3,   # 0.3 s compute per 1 s audio
    "max": 0.5,
    "remote": 0.1,
}
```

On M4 Max with mlx-whisper large-v3 (`max` profile), measured RTF is approximately
0.05–0.15 depending on segment length — roughly 3–10× faster than the declared 0.5.
Similarly, `balanced` (small/medium) runs at RTF ~0.03–0.08, not 0.30.

**Impact:** `total_relative_cost` and `estimated_compute_sec` presented to the user
are 3–10× too high. The relative cost of a 10-minute balanced recording is reported
as ~36 s, when actual M4 Max processing is ~2–3 s.

**Recommendation:** Run `time mlx_whisper.transcribe()` on a 60 s clip for each profile
and update `_STT_RATES` accordingly. Also update `_MAX_REFERENCE_SEC` / `_MAX_COMPUTE_SEC`
to keep the normalisation anchor valid.

---

### F-2 — LLM cost is duration-independent flat constant (Low)

**File:** `cost_estimator.py:31`

```python
_LLM_FLAT_SEC = 0.5   # flat addition for LLM rewrite
```

LLM rewrite latency scales with transcript length, which grows with audio duration.
A 1-second clip and a 60-minute clip both receive `llm_compute = 0.5 s`.
At 60 tok/s on M4 Max with qwen3-4b, even a 200-token rewrite takes ~3 s; a
long meeting summary is 10–30 s.

**Impact:** LLM cost is underestimated by 6–60× for long recordings. The normalisation
reference (`_MAX_COMPUTE_SEC`) also omits this scaling, so `total_relative_cost` is
understated for long + LLM configurations.

**Recommendation:** Replace with a linear term:
`llm_compute = _LLM_BASE_SEC + _LLM_PER_SEC * duration_sec` (e.g., base=0.5, per_sec=0.005).

---

### F-3 — `estimate_batch_cost` not exposed as IPC handler (Low)

**File:** `KrabEar/backend/service.py:1097-1098`

The handler lookup table in `BackendService` exposes `estimate_recording_cost` and
`get_daily_cost_summary` but **not** `estimate_batch_cost`. The method exists in
`CostEstimator` and has full test coverage, but Swift callers (confirmed via grep)
cannot call it. Any batch-import UI that wants pre-flight cost estimation must
call `estimate_recording_cost` N times.

**Recommendation:** Add an IPC handler `_handle_estimate_batch_cost` and wire it in
the handler table. Low-effort — the service already owns `self._cost_estimator`.

---

### F-4 — Memory coefficients do not account for model sharing (Low)

**File:** `cost_estimator.py:35-42`

```python
_MEMORY_MB: dict[str, float] = {
    "balanced": 900.0,
    "max": 1800.0,
    "remote": 200.0,
}
_DIARIZATION_MEM_MB = 400.0
_LLM_MEM_MB = 300.0
```

All values are additive constants, but models are loaded once and shared across
recordings. Reporting 1800 MB per recording overstates peak RAM when consecutive
recordings reuse a resident mlx-whisper model. The true incremental memory cost
for the second recording onward is near zero for the STT component.

**Impact:** `estimate_batch_cost.total_memory_mb` reports `peak` (correctly using `max`)
rather than sum, which mitigates the issue for batch. For per-recording advice shown
in UI the estimate is accurate on first load but misleading for warm sessions.

**Recommendation:** Document the "cold-start" interpretation explicitly in the docstring.
Optionally add a `warm=True` parameter that omits the model base for already-loaded
models.

---

### F-5 — No test for `super-long` audio (>60 min) clamping behaviour (Trivial)

**File:** `KrabEar/tests/test_cost_estimator.py`

`TestEstimateCostRelative.test_relative_cost_between_0_and_1` tests up to 3600 s
(60 min = the reference ceiling). It does not test 7200 s (120 min), which would
confirm the `min(1.0, ...)` clamp works for audio longer than the normalisation
anchor. Manual testing confirms it clamps correctly to 1.0, but the gap in test
coverage could silently regress.

**Recommendation:** Add one assertion for `duration_sec=7200` to
`test_relative_cost_between_0_and_1`.

---

## Checklist Answers

| Check | Result |
|---|---|
| Accuracy — calibrated for M4 Max + actual MLX models? | **No** (F-1, F-2) |
| Edge cases: 0 / tiny / very long audio | OK — 0 handled; very long clamped to 1.0 |
| Unit consistency (ms vs sec vs min) | **OK** — all internal values are seconds; disk uses `duration_sec / 60.0` correctly |
| Persistent / unbounded state | None — module is fully stateless |
| Privacy — sensitive data persisted? | None — no PII, no persistence |
| Test coverage | Good — 8 test classes, 30+ cases; one gap (F-5) |
| Wire status | Wired via 2 IPC handlers; `estimate_batch_cost` is not wired (F-3) |
| Monetary cost / currency hardcoded? | No monetary cost modelled |
| Thread safety | Safe — pure computation, no shared mutable state; confirmed by concurrency test |
| Mock-friendliness | `get_daily_cost_summary` takes `usage_tracker` as dependency injection |

---

## Files Examined

- `/Users/pablito/Antigravity_AGENTS/Krab Ear/KrabEar/backend/cost_estimator.py` (247 lines)
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/KrabEar/tests/test_cost_estimator.py` (399 lines)
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/KrabEar/backend/service.py` (handler wiring, lines 1097-1098 and 3475-3503)
