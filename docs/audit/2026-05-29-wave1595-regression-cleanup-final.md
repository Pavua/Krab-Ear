# W1595 Regression Cleanup FINAL — 26 of 26 Fixed (100% Complete)

**Date:** 2026-05-29  
**Auditor:** W1595 sub-agent  
**Method:** Three production audit scripts  
**Scope:** 628 test files, 2092 from-imports checked  
**Baseline:** W1538 (26 import regressions, all from W1497 cherry-pick train clobber)  
**Trigger:** W1591 alias landed (#1440) — confirm final production state.

---

## Executive Summary

**All 26 import regressions are fixed. Regression cleanup campaign is COMPLETE.**

| Scanner | Exit code | Finding |
|---------|:---------:|---------|
| `audit_cherry_pick_regressions.py --fail-on-found` | **0** | 0 missing symbols |
| `audit_duplicate_defs.py --fail-on-found` | **0** | 0 genuine shadowing bugs |
| `audit_orphan_imports.py` | **0** | 0 orphan imports in service.py |

**Production state: STABLE.**

---

## Scanner Output at HEAD (f4cfe6d1)

```
Checked 2092 from-imports across 628 test files.
Skipped 0 unresolvable modules.

No missing symbols found — all imports resolve.
cherry-pick exit=0
```

```
Found 232 Python files to check
[OK] No genuine duplicate definitions found (property pairs are expected).
dup-defs exit=0
```

```
scanned 156 class-call sites, 34 decorator sites — 0 missing import(s)
All checked names are imported or defined  [service.py]
orphan exit=0
```

---

## Final Regression Count: 26 → 0

| # | Module | Symbol | Severity | Wave fixed | PR |
|---|--------|--------|----------|------------|----|
| 1 | `core.silence_detector` | `SILENCE_THRESHOLD_DB_STRICT` | CRIT | W1531 | #1406 |
| 2 | `core.silence_detector` | `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER` | CRIT | W1531 | #1406 |
| 3 | `core.audio_quality` | `_safe_float` (×2 test files) | CRIT | W1442 | #1401 |
| 4 | `backend.history_service` | `_EXPORT_ALLOWED_ROOTS` | SEC HIGH | W1532 | #1405 |
| 5 | `backend.history_service` | `_is_safe_export_dir` | SEC HIGH | W1532 | #1405 |
| 6 | `core.engine` | `_VOXTRAL_REPO_ALLOWLIST` | SEC HIGH | W1535 | #1410 |
| 7 | `backend.privacy_audit` | `_compute_entry_hash` | HIGH | W1533 | #1407 |
| 8 | `backend.privacy_audit` | `_KEY_FILENAME` | HIGH | W1533 | #1407 |
| 9 | `core.text_anonymizer` | `_snils_valid` | HIGH | W1536 | #1408 |
| 10 | `core.text_anonymizer` | `_iban_valid` | HIGH | W1536 | #1408 |
| 11 | `core.audio_lang_id` | `SUPPORTED_LANGUAGES` | HIGH | W1561 | #1425 |
| 12 | `backend.auto_deduplication` | `_PRIVACY_SKIPPED` | HIGH | W1537+W1540 | #1411+#1419 |
| 13 | `backend.auto_deduplication` | `_text_similarity` | HIGH | W1537+W1540 | #1411+#1419 |
| 14 | `core.engine` | `_UNAVAILABLE_TTL_SEC` | HIGH | W1562 | #1424 |
| 15 | `core.auto_glossary` | `_starts_with_filler` | MED | W1541 | #1417 |
| 16 | `core.auto_glossary` | `_FILLER_STARTERS` | MED | W1541 | #1417 |
| 17 | `backend.archive_manager` | `_ARCHIVE_LOCK_FILE` | MED | W1542 | #1418 |
| 18 | `backend.job_tracker` | `_PRUNE_CANCEL_EVENT_TTL` | MED | W1542 | #1418 |
| 19 | `core.audio_denoiser` | `_NOISEREDUCE_PARAMS` | MED | W1550 | #1421 |
| 20 | `backend.speaker_manager` | `_MAX_EMBEDDING_FLOATS` | MED | W1550 | #1421 |
| 21 | `backend.sharing_manager` | `_MAX_TTL_HOURS` | MED | W1546 | #1416 |
| 22 | `backend.sharing_manager` | `_MAX_SHARE_ITEMS` | MED | W1546 | #1416 |
| 23 | `backend.transcript_versioning` | `_MAX_TEXT_BYTES` | MED | W1563 | #1428 |
| 24 | `core.topic_tracker` | `_HARD_MAX_ITEMS` | MED | W1547 | #1420 |
| 25 | `core.voice_commands` | `_VOICE_COMMANDS_STRICT_MODE` | MED | W1547 | #1420 |
| 26 | `core.pipeline.stt_parakeet` | `ParakeetAdapter` | LOW | W1591 | #1440 |

All 26 entries resolved. Final fix (W1591) added `ParakeetAdapter = ParakeetSTTAdapter` alias to
`stt_parakeet.py`, closing the last graceful-skip regression (6 previously-skipped Parakeet tests
now run and pass).

---

## CI Guard Status

Both audit scripts are wired in `.github/workflows/ci.yml`:

| Job | Script | Status after W1595 |
|-----|--------|--------------------|
| `duplicate-def-guard` | `audit_duplicate_defs.py --fail-on-found` | STRICT — always was |
| `cherry-pick-regression-guard` | `audit_cherry_pick_regressions.py --fail-on-found` | **STRICT** — `continue-on-error` removed in this wave |

**Action taken:** `continue-on-error: true` removed from the cherry-pick-regression-guard CI job.
The scanner now exits 0 on `--fail-on-found` (0 missing symbols), so the gate is safe to enforce
strictly. Any future cherry-pick train that clobbers a module-level symbol will cause an immediate
CI failure.

---

## Progress Trajectory

| Checkpoint | Import regressions | Batches completed |
|------------|:-----------------:|:-----------------:|
| W1525 initial scan | 26 | — |
| W1538 post-batch-71 attempt | 26 (0 merged at time) | 71 |
| W1552 post-batches-71-72 | 6 | 71-72 |
| W1590 post-batches-71-75 | 1 (LOW, graceful skip) | 71-75 |
| **W1595 (NOW)** | **0** | 71-76 |

**Total repair waves:** 14 (W1442, W1531–W1537, W1540–W1542, W1546–W1547, W1550, W1561–W1563, W1591)  
**Total PRs in repair campaign:** 15 (#1401, #1405–#1412, #1416–#1421, #1424–#1425, #1428, #1440)  
**Repair duration:** 2026-05-27 → 2026-05-29 (batches 71–76)

---

## Production State Summary: STABLE

- **0** CRIT regressions
- **0** SEC HIGH regressions
- **0** HIGH regressions
- **0** MED regressions
- **0** LOW regressions
- **0** genuine duplicate-definition shadowing bugs
- **0** orphan imports in `service.py`
- CI cherry-pick guard now strict (hard-fail on any future regression)

The import-regression cleanup campaign that began with the W1525 audit (26 regressions from the
W1497 cherry-pick train) is fully closed. All test files resolve their imports. The CI gate is
strict. No further remediation required.
