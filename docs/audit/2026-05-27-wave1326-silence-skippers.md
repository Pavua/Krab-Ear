# W1326 Audit — SmartSilenceSkipper Wire Status (re-audit post-W1102 claim)

**Date:** 2026-05-27  
**Branch audited:** `codex/krab-ear-v2` tip (commit `62df2ec9`)  
**Worktree:** `/tmp/audit-silence-skippers-W1326`  
**Auditor:** W1326 (sub-agent, read-only)

---

## Wire Status: STILL UNWIRED — W1102 NOT MERGED

`git grep -rn "SmartSilenceSkipper|smart_silence_skip|SMART_SILENCE_SKIP_ENABLED" KrabEar/core/engine.py`
→ **0 matches**.

The W1102 commit (`6b1feb2d`) exists only on the `wire-smart-silence-skipper-W1102` branch. It was
**never merged** into `codex/krab-ear-v2`. PR #1024 is OPEN with `mergeable: CONFLICTING` as of
2026-05-27.

History of phantom claims:
- W1096 — initial audit, F1 = "SmartSilenceSkipper not wired into engine.py" (CRITICAL)
- W1102 — created PR #1024 claiming to fix W1096 F1; commit merged only into own branch
- W1252 (audio-chunker audit) — NF1 confirmed W1102 still not on main as of that audit
- W1312 (audio-chunker third audit) — referenced same phantom status
- **W1326 (this audit)** — CONFIRMED STILL UNFIXED. Three audits and one PR later, the setting
  `SMART_SILENCE_SKIP_ENABLED=False` in config.py controls a code path that does not exist in
  the production pipeline.

**Evidence:**
- `KrabEar/core/config.py:209` — `SMART_SILENCE_SKIP_ENABLED: bool = False` (declared, default off)
- `KrabEar/core/config.py:835` — `"smart_silence_skip_enabled": False` (in DEFAULT_SETTINGS)
- `KrabEar/core/smart_silence_skipper.py` — module exists (213 lines, algorithm implemented)
- `KrabEar/core/engine.py` — zero imports or calls to SmartSilenceSkipper

---

## Findings (5 total)

### F1 — CRITICAL · SmartSilenceSkipper STILL not wired into engine.py (W1096 F1 unresolved)

**Status:** OPEN since W1096. W1102 PR #1024 open/conflicting; not merged.

**Details:**
`engine.py` pipeline (as of tip `62df2ec9`) runs these audio preprocessing steps before STT:
- 2.4 — `zero_silence_ranges` (RealtimeSilenceFilter, zeroes samples, preserves timestamps)
- 2.5 — `_maybe_denoise` (AudioDenoiser, adaptive spectral gating)
- 2.6 — `GainNormalizer.auto_gain` (RMS gain normalisation, added by a later wave)
- 3   — VAD prefilter (`_apply_vad_prefilter`)

`SmartSilenceSkipper.process()` is never called. The `SMART_SILENCE_SKIP_ENABLED` flag is
read-settable via IPC (`set_settings`) but flipping it to `True` has no effect — no code in
the pipeline checks it.

**Additional complication:** W1102 intended to insert SmartSilenceSkipper at "step 2.6",
but the current main already uses step 2.6 for GainNormalizer. Any future merge of PR #1024
would require renumbering the step label (step 2.7 is now the correct insertion point) and
resolving the conflict with the GainNormalizer commit.

**Fix:** Rebase PR #1024 onto current `codex/krab-ear-v2`, resolve conflict (rename to step 2.7),
re-run tests. Or close PR #1024 and create a fresh one.

---

### F2 — MED · PR #1024 is CONFLICTING and has been stale for 1+ day

**File:** `wire-smart-silence-skipper-W1102` branch  
**PR:** #1024, state=OPEN, mergeable=CONFLICTING

The W1102 branch diverged from `codex/krab-ear-v2` before the GainNormalizer step was added.
`gh pr view 1024` returns `"mergeable":"CONFLICTING"`. The PR cannot be merged without rebase.
Because W1326 is now tracking the same UNWIRED status, any future fixer must rebase W1102
(or create a new branch from current tip) before the wire can land.

**Fix:** Rebase `wire-smart-silence-skipper-W1102` onto `codex/krab-ear-v2` tip, renumber the
step comment from 2.6 to 2.7 (after GainNormalizer), and re-verify the 6 existing engine
wiring tests in `test_smart_silence_skipper_engine_wiring_W1102.py` still pass.

---

### F3 — LOW · Redundant double `_to_mono` conversion in `SmartSilenceSkipper.process()`

**File:** `KrabEar/core/smart_silence_skipper.py:101, 123`

```python
# Line 101:
mono = SilenceDetector._to_mono(audio)
# ...
# Line 123:
silence_regions = self._detector.detect_silence(mono, sample_rate, ...)
```

`detect_silence` calls `self._to_mono(audio)` internally as its first step (line 67 of
`silence_detector.py`). Since `mono` is already 1-D after line 101, the second call to
`_to_mono` inside `detect_silence` is a no-op — but it allocates and type-checks the array
again on every call. At 16 kHz, a 60-second recording is ~960 000 float32 samples; the
redundant copy is negligible in practice but is a code quality issue.

**Fix:** Either (a) pass the already-computed `mono` to `detect_silence` directly and document
that `detect_silence` accepts mono input, or (b) expose a `_detect_silence_mono` internal
method that skips the conversion. Option (b) avoids coupling to implementation internals.

This is NOT a correctness bug — the algorithm produces correct output.

---

### F4 — LOW · No engine-wiring tests on `codex/krab-ear-v2` for SmartSilenceSkipper

**File:** `KrabEar/tests/` — no `test_smart_silence_skipper_engine_wiring_W1102.py` on main

The 6 engine integration tests created by W1102 exist only on the `wire-smart-silence-skipper-W1102`
branch. They never landed on `codex/krab-ear-v2`. The existing `test_smart_silence_skipper.py`
(34 tests) covers the class in isolation but has zero coverage of:
- The `SMART_SILENCE_SKIP_ENABLED` config path
- The VAD mutex (`_smart_silence_active`) interaction
- The `is_preview=True` skip path
- Soft-fail behaviour when SmartSilenceSkipper raises an exception mid-pipeline

Tests in `test_engine_multipass.py` (lines 164, 318) and `test_engine_streaming.py` (line 73)
explicitly set `SMART_SILENCE_SKIP_ENABLED = False`, confirming these tests are designed to
bypass the (unwired) feature — they do not test the `True` path at all.

**Fix:** When PR #1024 is rebased and merged, the 6 engine wiring tests will land. Until then,
this is a testing gap for a latent code path.

---

### F5 — LOW · `SMART_SILENCE_SKIP_ENABLED=True` via IPC is silently ignored (dead config)

**Files:** `KrabEar/core/config.py:209`, `KrabEar/core/config.py:835`

A user or operator who sets `smart_silence_skip_enabled: true` via the `set_settings` IPC call
will receive a successful response and see the setting persisted to `settings.json`, but the
behaviour of the STT pipeline will be **identical to the disabled state** — no silence is
removed before Whisper runs. There is no warning, log line, or error to indicate the setting
is inoperative.

This is a silent no-op, which is the worst failure mode from a user-observable standpoint:
it looks like it works but does nothing.

**Fix:** Either (a) wire the feature (merge PR #1024 after rebase), or (b) log a warning at
startup when `SMART_SILENCE_SKIP_ENABLED=True` but the engine does not have the wiring, or
(c) remove the config key until the feature is ready. Option (a) is preferred.

---

## Algorithm Correctness Summary (no critical bugs found)

The `SmartSilenceSkipper` algorithm in isolation is correct:
- Short-audio guard (`n_samples <= 2 * edge_samples`) prevents index errors on tiny arrays.
- Padding shrink logic (`skip_start = min(r_start + pad_samples, r_end)`) correctly skips
  regions where padding fully consumes the silence — `skip_end <= skip_start` guard at line 147.
- Tuple replacement in a list (`merged[-1] = (...)`) is valid Python — not a bug.
- Multichannel support: `audio[s:e]` slices the original (multichannel) array using indices
  computed from the mono projection — indices align correctly.
- The `-40 dB` default threshold matches `SILENCE_THRESHOLD_AMP` in `silence_detector.py`
  (the SSOT constant added by W1132's fix).

No timestamp-shift interaction with VAD can currently occur (feature is unwired), but the risk
remains latent: when wired, both SmartSilenceSkipper and `_apply_vad_prefilter` remove audio
samples and shift timestamps. The W1102 mutex guard (`_smart_silence_active`) is the designed
mitigation, but it only lands when the PR is merged.

---

## Previous Audit Chain

| Wave | PR | Status |
|------|----|--------|
| W1096 | #938 (docs) | OPEN — initial finding, F1=CRITICAL unwired |
| W1102 | #1024 (wire) | OPEN / CONFLICTING — wire attempt, not merged |
| W1252 | (audio-chunker audit) | NF1 confirmed phantom |
| W1312 | (audio-chunker third audit) | referenced same status |
| **W1326** | this doc | CONFIRMED STILL UNFIXED |
