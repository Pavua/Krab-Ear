# W1461 Post-fix Audit: `core/audio_quality.py`

**Date:** 2026-05-27
**Branch audited:** `codex/krab-ear-v2` @ `8b73fda9`
**Worktree:** `audit-audio-quality-post-fix-W1461`
**File:** `KrabEar/core/audio_quality.py`
**Prior audits:** W1015, W1100, W1133, W1384 (P1–P5)
**Fix waves reviewed:** W1442 (duplicate `_safe_float`), W1017/W1103 (NaN guard),
W1107 (silence threshold), W1320 (denoiser percentile analog), W1333 (silence constant)

---

## Merge State of All Prior Fix Waves

| Wave | Description | Commit | Status in `codex/krab-ear-v2` |
|------|-------------|--------|-------------------------------|
| W1017 | NaN/Inf JSON safety (`_safe_float` coerce) | `766ef8a1` | **MERGED** — commit present in main |
| W1103 | Re-apply W1017 NaN/Inf JSON safety | `dfcfe313` | **MERGED** — commit present in main |
| W1107 | Unify silence threshold via `SILENCE_THRESHOLD_AMP` | not in main | **NOT MERGED** |
| W1133 | Residual audit docs N1–N5 | `281ef2a3` | **MERGED** (docs only) |
| W1320 | Denoiser percentile strict-lt + zero-selection skip | not in main | **NOT MERGED** |
| W1333 | Shared silence constant `silence_constants.py` | `8ae75670` | **MERGED** (but not used in `audio_quality.py`) |
| W1384 | Fourth-pass audit docs P1–P5 | `77559a21` | **MERGED** (docs only) |
| W1441 | Meta-audit duplicate defs + CI guard | `7c6c838f` | **MERGED** — CI guard active |
| W1442 | Remove duplicate `_safe_float` (CRIT) | not in main | **NOT MERGED** |

Verification:
```bash
git merge-base --is-ancestor 766ef8a1 codex/krab-ear-v2  # W1017 → exit 0 (MERGED)
git merge-base --is-ancestor dfcfe313 codex/krab-ear-v2  # W1103 → exit 0 (MERGED)
python3 scripts/audit_duplicate_defs.py --fail-on-found   # exits 1 — W1442 NOT merged
grep -n "def _safe_float" KrabEar/core/audio_quality.py   # lines 20 and 40 — shadow present
```

---

## Key Facts About the W1442 / W1017 / W1103 Interaction

W1017 and W1103 are both confirmed **merged** into `codex/krab-ear-v2` (commits `766ef8a1`
and `dfcfe313` respectively). Both commits introduce the 2-argument form:

```python
# W1017/W1103 merged version (lines 20–27):
def _safe_float(v: float, default: float = 0.0) -> float:
    """Coerce v to a finite float, replacing NaN/Inf with default."""
    return v if (isinstance(v, (int, float)) and math.isfinite(v)) else default
```

However, **after** that merged definition, a second (pre-existing, legacy) definition
appears at line 40:

```python
# Legacy shadowing definition (lines 40–42):
def _safe_float(v: float) -> float:
    """Заменяет NaN/Inf нулём — защита от JSON-serialization failure."""
    return v if math.isfinite(v) else 0.0
```

Python's module-level name binding means the second definition silently replaces the
first at import time. The W1017/W1103 fix is **fully present in the source but entirely
dead** — the 1-argument shadow definition is what runs. This is the W1442 CRIT finding
confirmed by W1441.

---

## Runtime Impact of W1442 Not Being Merged

### 1. `AudioQualityAnalyzer.analyze()` always raises `TypeError` for non-empty audio

Line 159 in `analyze()`:
```python
silence_ratio=round(_safe_float(silence_ratio, 1.0), 4),
```
The shadow `_safe_float` accepts only 1 positional argument. This call raises:
```
TypeError: _safe_float() takes 1 positional argument but 2 were given
```

**Every call to `analyze()` on non-empty audio raises `TypeError`.** The IPC handler
`handle_analyze_audio_quality` (via `analyze_file`) wraps this in a try/except and
returns an error to Swift, but the quality check is completely non-functional.

Confirmed empirically:
```bash
PYTHONPATH=KrabEar python3 -m unittest KrabEar/tests/test_audio_quality_nan_W1017.py -v
# → FAILED (errors=6)
# TypeError: _safe_float() takes 1 positional argument but 2 were given
```

### 2. `to_dict()` silence_ratio NaN default is wrong (silent data corruption)

`to_dict()` calls `_safe_float(self.silence_ratio)` (1-arg — works). But the intended
default for silence_ratio is `1.0` (conservative: unknown = fully silent), not `0.0`
(which means "no silence"). If any code path constructs an `AudioQualityReport` with
`silence_ratio=nan` and then calls `to_dict()`:

```python
# With shadow def:
_safe_float(float('nan'))  # → 0.0 (wrong: implies no silence)
# With W1442 fix:
_safe_float(float('nan'), default=1.0)  # → 1.0 (correct conservative default)
```

Swift receives `{"silence_ratio": 0.0}` instead of `{"silence_ratio": 1.0}`, potentially
triggering a "good" quality score for an audio frame whose silence ratio is unknown.

### 3. CI guard (W1441) catches W1442 correctly

`scripts/audit_duplicate_defs.py --fail-on-found` exits 1 and reports:
```
FILE: KrabEar/core/audio_quality.py  (1 genuine duplicate(s))
  scope=<module>  name=_safe_float
  first def at line 20, shadow at line 40
```
The guard is functional. However, since W1442 is not merged, `codex/krab-ear-v2` currently
fails the CI guard on every run. The `duplicate-defs-guard` job in `.github/workflows/ci.yml`
would fail for any branch rebased on current main.

---

## W1311 Denoiser Interaction

W1311 (AudioDenoiser third-pass audit) is a docs-only commit. The `<=` vs `<` fix for
`quiet_mask` in `audio_denoiser.py` was called out as W1320 in that audit but W1320 was
never merged (`audio_denoiser.py` still has `quiet_mask = rms_per_frame <= threshold` at
line 101). This is unrelated to `audio_quality.py` directly — the denoiser and quality
analyzer do not share code. The W1133 N1 finding (quality reports raw audio, not denoised)
remains open and unchanged.

There is no new interaction between the denoiser and audio_quality introduced since W1384.

---

## New Findings (cap 5)

### Q1 — CRIT: W1442 NOT merged — `analyze()` always raises `TypeError` for any real audio

**Status:** Confirmed above. This is not a new finding in the abstract sense — W1441 and
W1442 define it — but W1461 confirms it is NOT fixed in production.

**Location:** `KrabEar/core/audio_quality.py`, line 159 and line 40.

**Impact:** The IPC method `analyze_audio_quality` returns `{"ok": false, "error": ...}` for
every call. No pre-flight quality check has functioned since the W1017/W1103 fix was merged
(which added the 2-arg call site) while the legacy 1-arg shadow remains in place. Duration
of regression: since the first merge of W1017 or W1103 (commit `766ef8a1`).

**Fix:** Remove lines 40–42 (the shadow 1-arg `_safe_float`). The W1017/W1103 definition
at lines 20–27 is correct and complete.

---

### Q2 — HIGH: W1017/W1103 are MERGED but their fix is DEAD because W1442 is not merged

**Location:** `KrabEar/core/audio_quality.py`, lines 20–42.

This is an architectural finding: the merge history shows W1017 and W1103 committed
(correctly) but a pre-existing definition that was never removed silently negates both
fixes. The W1441 CI guard now catches this class of bug, but the existing broken state
in production passed the CI guard because the guard was only added in W1441 (after the
shadow was introduced).

The correct merge order for full resolution is:
1. W1442 first (remove shadow) — restores W1017/W1103 2-arg behavior
2. W1107 second (unify silence threshold) — requires working `_safe_float` first

If W1442 is merged without W1107, `_SILENCE_RMS_THRESHOLD = 0.001` remains (not the
shared constant from W1333) — that is acceptable as a separate fix wave.

---

### Q3 — HIGH: No regression test guards the 2-arg `_safe_float` call site in `analyze()`

**Location:** `KrabEar/tests/test_audio_quality_nan_W1017.py`, `test_normal_audio_still_works`

`test_audio_quality_nan_W1017.py` includes `test_normal_audio_still_works` which calls
`analyzer.analyze(clean_audio, sample_rate=16000)` and would catch the TypeError. However,
this test is in `test_audio_quality_nan_W1017.py`, not in the primary `test_audio_quality.py`.

The primary test file `test_audio_quality.py` has tests like `test_analyze_report_fields`
and `test_clipping_detection` which also call `analyzer.analyze()` with non-empty audio —
these ALL fail with `TypeError` currently. Six tests across both files fail.

A dedicated test explicitly asserting `_safe_float` accepts 2 arguments (the signature
contract) would prevent future shadows from masking the fix:
```python
def test_safe_float_accepts_two_positional_args(self):
    """Regression guard: _safe_float must accept a default= argument (W1017 contract)."""
    import inspect
    sig = inspect.signature(_safe_float)
    self.assertEqual(len(sig.parameters), 2)
    self.assertIn("default", sig.parameters)
```

**Severity:** HIGH — the absence of this guard allowed W1442 to go undetected through
multiple merge cycles.

---

### Q4 — MEDIUM: `test_custom_default` in `test_audio_quality_nan_W1017.py` fails silently in CI (TypeError, not AssertionError)

**Location:** `KrabEar/tests/test_audio_quality_nan_W1017.py`, line 47.

```python
def test_custom_default(self):
    self.assertEqual(_safe_float(float("nan"), default=42.0), 42.0)
```

With the shadow definition active, this raises `TypeError: _safe_float() got an unexpected
keyword argument 'default'` rather than an `AssertionError`. In CI, both result in
test failure, but the error message obscures whether the _value_ is wrong or the
_signature_ is wrong. The test correctly fails, but the diagnostic message is confusing.

Additionally, there is no test that calls `_safe_float` with a positional second argument
(not keyword) to catch the `v, default=0.0` form vs `v` form separately:
```python
_safe_float(float("nan"), 42.0)  # positional 2-arg
```

---

### Q5 — LOW: W1441 CI guard runs on ubuntu-latest but `audio_quality.py` line 40 pre-dates the guard — the guard is retroactively failing current `codex/krab-ear-v2`

**Location:** `.github/workflows/ci.yml`, `duplicate-defs-guard` job.

The W1441 commit added the guard to CI. Since W1442 was not merged before or at the same
time, `codex/krab-ear-v2` now has a CI guard that would fail on any new PR targeting this
branch. This creates a merge-order constraint: W1442 must be merged before any other PR
that runs CI against `codex/krab-ear-v2`, or CI will fail due to the pre-existing
`audio_quality.py` shadow.

Recommendation: merge W1442 as the **first** item in the next merge train to unblock CI
for all downstream PRs.

---

## Summary Table

| ID | Severity | Description | New since W1384? |
|----|----------|-------------|-----------------|
| Q1 | CRIT | W1442 NOT merged — `analyze()` always raises TypeError; analyze_audio_quality IPC 100% broken | Confirmed post-W1441 |
| Q2 | HIGH | W1017+W1103 fixes are dead-code due to shadow; fix is merged but unreachable | Extends W1441 finding with runtime evidence |
| Q3 | HIGH | No signature-contract test for 2-arg `_safe_float`; allows shadow to go undetected | New |
| Q4 | MEDIUM | `test_custom_default` TypeError obscures root cause vs value failure in CI | New |
| Q5 | LOW | W1441 CI guard retroactively fails `codex/krab-ear-v2`; W1442 must merge first | New |

---

## Prior Open Findings Status (from W1384 P1–P5 and W1133 N1–N5)

| ID | Description | Status |
|----|-------------|--------|
| P1 | `quiet_mask` all-frames collapse for low-level clean signals (SNR=0 "poor") | **OPEN** |
| P2 | `silence_ratio` warning (>0.8) contradicts score threshold (>0.9) | **OPEN** |
| P3 | W1107 merge changes silence semantics for 0.001–0.01 RMS without test coverage | **OPEN** (W1107 not merged) |
| P4 | `sf.read` exception leaks full path to IPC response | **OPEN** |
| P5 | No test for low-level clean signal misclassification | **OPEN** |
| N1 | Quality reports raw audio, not denoised | **OPEN** |
| N2 | `float64` cast doubles RAM for long audio | **OPEN** |
| N3 | Python loops block IPC thread ~400–800 ms for 1-hour audio | **OPEN** |
| N4 | `_error_bus` path confirmed dead | **OPEN** |
| N5 | `np.clip` does not sanitize NaN before `_score()` receives it | **OPEN** |

---

## Priority Action Items

1. **Merge W1442** (remove line 40–42 shadow `_safe_float`) — CRIT, unblocks CI and restores all analyze_audio_quality IPC calls.
2. **Add Q3 test** (`test_safe_float_signature_contract`) to `test_audio_quality_nan_W1017.py` — prevents future regression.
3. **Merge W1107** — unifies silence threshold (F3/R1 MEDIUM), depends on Q3 being added first.
4. **Fix P1** (`quiet_mask` all-frames guard) — HIGH, clean low-level audio misclassified as "poor".
5. **Fix P2** (silence warning/score inconsistency) — MEDIUM.
