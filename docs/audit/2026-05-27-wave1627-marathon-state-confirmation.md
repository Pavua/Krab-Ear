# Wave 1627 — Marathon State Confirmation (Post-Batches 78–80)

**Date:** 2026-05-29  
**Branch:** codex/krab-ear-v2  
**HEAD:** ec2a92fd  
**Scope:** Regression scan after batch 78+79+80 fixes (#1446–#1456)

---

## 1. Audit Scanner Results

| Scanner | Exit Code | Finding |
|---------|-----------|---------|
| `audit_cherry_pick_regressions.py --fail-on-found` | **0** | Checked 2 118 from-imports across 631 test files — 0 missing symbols |
| `audit_duplicate_defs.py --fail-on-found` | **0** | 13 entries scanned, all 13 are `@property` getter/setter pairs (false positives) — 0 genuine shadowing bugs |
| `audit_orphan_imports.py` | **0** | 156 class-call sites, 34 decorator sites — 0 missing imports in service.py |

All three scanners exit 0. Marathon-state is **clean**.

### Duplicate-defs detail (false positives only)

All 13 flagged items are legitimate `@property` getter/setter pairs:

- `keyword_cloud.py`: `_stop_words`
- `llm_rewriter.py`: `_timeout`
- `paste_app_memory.py`: `enabled`
- `service.py`: `_clipboard_history`, `_last_stt_engine`, `_list_audio_inputs`, `_preview_error_count`, `_preview_error_last_reset_ts`, `_preview_updated_at`, `_rt_partial`, `_rt_session_id`, `_transcription_counter`, `recorder`

---

## 2. Commits Since W1595 (#1444)

10 PRs merged after the W1595 "regression cleanup FINAL — 26 of 26 fixed" baseline:

| Commit | PR | Description |
|--------|----|-------------|
| ec2a92fd | #1456 | fix(wave1618): wrap engine.py mx.clear_cache calls in mlx_lock (W1612 F1+F3, W63 thread-safety) |
| 16d436c7 | #1455 | docs(wave1615): first-pass audit backend/startup_diagnostics.py — 5 findings (1H/2M/2L) |
| ad35bc08 | #1454 | fix(wave1616): repair test_error_codes failures + add audio.max_duration_reached registry entry (W1614 F1+F2 HIGH) |
| 44e700b2 | #1451 | docs(wave1605): first-pass audit contracts/registry.py — 5 findings (1H/2M/2L) |
| 97393d71 | #1450 | fix(wave1603): Sentry re-init when privacy_mode toggles OFF (W1599 F2 MED) |
| dbb07aa0 | #1449 | docs(wave1606): first-pass audit core/parsing_utils.py — 5 findings |
| a2aa706d | #1448 | fix(wave1607): repair flake8 lint debt accumulated from --admin merges (CI red) |
| 2dcefbb1 | #1447 | fix(wave1602): _transcribe_paths_core respects auto_dedup_enabled (W1588 F2 MED) |
| 250d1f5c | #1446 | docs(wave1604): first-pass audit core/mlx_subprocess.py — 7 findings |
| c816902c | #1445 | fix(wave1542b): restore unarchive_items restore_history_item_raw path (W1047 reverted by W1497 cherry-pick train) |

### New regressions introduced by batches 78–80

**None.** All 10 commits since W1595 are either:
- Audit/docs only (waves 1604/1605/1606/1615) — no runtime changes
- Targeted fixes closing known audit findings — no side-effect regressions detected by scanners

---

## 3. CI Status

Latest CI run on HEAD `ec2a92fd` is **pending/queued** (triggered at 2026-05-29T08:02:25Z, two workflows: `CI` and `krab-ear-ci`). Run just started — no conclusion yet.

Previous completed runs for prior commits were `cancelled` (superseded by newer pushes in rapid succession), not `failure`. The last substantive completed conclusion before this batch was the W1607 flake8 fix (#1448), which repaired a lint-red CI.

**CI status: indeterminate (pending)** — no red signal; prior cancellations were supersession artifacts from fast push cadence.

---

## 4. Overall Verdict

**STABLE**

- All 3 audit scanners exit 0
- 0 cherry-pick regressions
- 0 genuine duplicate definitions  
- 0 orphan imports in service.py
- 10 post-W1595 commits introduce no new regressions
- Batch 78–80 fixes (#1446–#1456) address outstanding audit findings without breaking invariants
- CI pending (just triggered), no failure signal

Marathon state is clean and ready for next batch.
