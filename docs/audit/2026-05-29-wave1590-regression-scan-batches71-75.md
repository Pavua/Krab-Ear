# W1590 Regression Repair Final Tally — Post-Batches 71-75

**Date:** 2026-05-29  
**Auditor:** W1590 sub-agent  
**Method:** AST-based `from X import Y` symbol scanner (`scripts/audit_cherry_pick_regressions.py`)  
**Scope:** 627 test files, 2085 from-imports checked  
**Baseline chain:** W1525 (26) → W1538 (26, 0 merged) → W1552 (6 remaining after batches 71-72) → W1590 (now)  
**Trigger:** Batches 71-75 shipped 28+ regression fixes; verify final production state.

---

## Executive Summary

**25 of 26 import regressions fixed. 1 LOW-severity alias remaining (graceful skipUnless).**

| Metric | W1525 baseline | W1552 (batches 71-72) | W1590 (batches 71-75) |
|--------|:--------------:|:--------------------:|:--------------------:|
| Total missing symbol entries (raw) | 26 | 6 | 1 |
| Genuine duplicate-def shadowing bugs | 0 | 0 | 0 |
| Orphan imports in service.py | 0 | 0 | 0 |

**Import scanner exit:** `--fail-on-found` exits 1 on the 1 remaining LOW item; CI has `continue-on-error: true` (intentional — see CI Guard Health section).

---

## Scanner Output (HEAD: 7bdbfdfb)

```
Checked 2085 from-imports across 627 test files.
Skipped 0 unresolvable modules.

MISSING SYMBOLS (1):
  KrabEar/tests/test_stt_adapter_router.py
    from core.pipeline.stt_parakeet import ParakeetAdapter
    (source: KrabEar/core/pipeline/stt_parakeet.py)
```

---

## All Fixes Confirmed in Production

### Fixed in Batches 71-72 (W1530–W1549) — 20 regressions

These were documented in the W1552 report. All confirmed shipped:

| # | Symbol | Module | Severity | Wave | PR |
|---|--------|--------|----------|------|----|
| 1 | `SILENCE_THRESHOLD_DB_STRICT` | `core.silence_detector` | CRIT | W1531 | #1406 |
| 2 | `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER` | `core.silence_detector` | CRIT | W1531 | #1406 |
| 3 | `_safe_float` | `core.audio_quality` | CRIT | W1442 | #1401 |
| 4 | `_EXPORT_ALLOWED_ROOTS` | `backend.history_service` | SEC HIGH | W1532 | #1405 |
| 5 | `_is_safe_export_dir` | `backend.history_service` | SEC HIGH | W1532 | #1405 |
| 6 | `_VOXTRAL_REPO_ALLOWLIST` | `core.engine` | SEC HIGH | W1535 | #1410 |
| 7 | `_compute_entry_hash` | `backend.privacy_audit` | HIGH | W1533 | #1407 |
| 8 | `_KEY_FILENAME` | `backend.privacy_audit` | HIGH | W1533 | #1407 |
| 9 | `_snils_valid` | `core.text_anonymizer` | HIGH | W1536 | #1408 |
| 10 | `_iban_valid` | `core.text_anonymizer` | HIGH | W1536 | #1408 |
| 11 | `_PRIVACY_SKIPPED` | `backend.auto_deduplication` | HIGH | W1537+W1540 | #1411+#1419 |
| 12 | `_text_similarity` | `backend.auto_deduplication` | HIGH | W1537+W1540 | #1411+#1419 |
| 13 | `_starts_with_filler` | `core.auto_glossary` | MED | W1541 | #1417 |
| 14 | `_FILLER_STARTERS` | `core.auto_glossary` | MED | W1541 | #1417 |
| 15 | `_ARCHIVE_LOCK_FILE` | `backend.archive_manager` | MED | W1542 | #1418 |
| 16 | `_PRUNE_CANCEL_EVENT_TTL` | `backend.job_tracker` | MED | W1542 | #1418 |
| 17 | `_MAX_TTL_HOURS` | `backend.sharing_manager` | MED | W1546 | #1416 |
| 18 | `_MAX_SHARE_ITEMS` | `backend.sharing_manager` | MED | W1546 | #1416 |
| 19 | `_HARD_MAX_ITEMS` | `core.topic_tracker` | MED | W1547 | #1420 |
| 20 | `_VOICE_COMMANDS_STRICT_MODE` | `core.voice_commands` | MED | W1547 | #1420 |

### Fixed in Batch 73 (W1550–W1559) — 2 regressions

| # | Symbol | Module | Severity | Wave | PR |
|---|--------|--------|----------|------|----|
| 21 | `_NOISEREDUCE_PARAMS` | `core.audio_denoiser` | MED | W1550 | #1421 |
| 22 | `_MAX_EMBEDDING_FLOATS` | `backend.speaker_manager` | MED | W1550 | #1421 |

**W1550 details:** Both symbols were confirmed absent after the W1538 cherry-pick train. W1550 restored
`_NOISEREDUCE_PARAMS` (dict with `prop_decrease` values per strength tier) and `_MAX_EMBEDDING_FLOATS = 4096`
as module-level constants, plus wired them into their respective guard paths.

### Fixed in Batch 74 (W1560–W1569) — 3 regressions

| # | Symbol | Module | Severity | Wave | PR |
|---|--------|--------|----------|------|----|
| 23 | `SUPPORTED_LANGUAGES` | `core.audio_lang_id` | HIGH | W1561 | #1425 |
| 24 | `_UNAVAILABLE_TTL_SEC` | `core.engine` | HIGH | W1562 | #1424 |
| 25 | `_MAX_TEXT_BYTES` | `backend.transcript_versioning` | MED | W1563 | #1428 |

**W1561 details:** Restored `SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"ru", "es", "en", "de", "fr", "it", "pt"})`
to `audio_lang_id.py` line 45; wired into allowlist check at line 361. W1121 feature had been clobbered
by the cherry-pick train.

**W1562 details:** Added backward-compat alias `_UNAVAILABLE_TTL_SEC = _UNAVAILABLE_MODEL_TTL_SEC` to
`engine.py` line 231. The constant was renamed in a prior refactor without preserving the old name.

**W1563 details:** Restored `_MAX_TEXT_BYTES = 256 * 1024` to `transcript_versioning.py` line 29.
W1547 had claimed to restore this but the cherry-pick dropped it silently; W1563 is the definitive fix.

### Batch 75 (W1571–W1580) — 0 import regressions fixed

Batch 75 shipped logic fixes (not import-breakage fixes):
- W1570 (#1429): `auto_glossary` privacy guard on invalidate + stale cache clear (HIGH logic bug)
- W1571 (#1434): `_text_similarity` wired into `check_duplicate` (was dead code, W1567 F1)
- W1572 (#1433): `AutoDeduplicator` wired into `RecordingCoreService` (W1567 F2)
- W1574 (#1432): `silence_detector` dead code cleanup + SSOT (W1566 F1+F2+F3)

No new import regressions introduced by batch 75.

---

## Remaining Regression (1)

### R6 — `core.pipeline.stt_parakeet` :: `ParakeetAdapter` — LOW

**Severity:** LOW  
**Impact:** 6 Parakeet adapter tests are always silently skipped (no test-collection failures, no CI
failures). The test file wraps the import in `try/except ImportError` and uses
`@unittest.skipUnless(PARAKEET_AVAILABLE, ...)` — so `PARAKEET_AVAILABLE = False` and all test cases
skip gracefully.

**Root cause:** `stt_parakeet.py` exports `ParakeetSTTAdapter` (class defined at line 49) but the test
imports `ParakeetAdapter` (the shorter alias name from an older API contract). The alias was never added.

**Production impact:** None. All Parakeet functionality works via `ParakeetSTTAdapter`. Only test
coverage of the 6 skipped cases is lost.

**Fix (single line):** Add `ParakeetAdapter = ParakeetSTTAdapter` at the end of
`KrabEar/core/pipeline/stt_parakeet.py`.

**Recommended wave:** W1591 or first available batch 77 slot (LOW priority).

---

## Duplicate Definitions Audit

`audit_duplicate_defs.py --fail-on-found` → **exit=0**

- 13 entries found — all are `@property` getter/setter pairs (expected false positives)
- 0 genuine shadowing bugs

Clean.

---

## Orphan Imports Audit

`audit_orphan_imports.py` (service.py scope):
- 156 class-call sites checked
- 34 decorator sites checked
- **0 missing imports**

Clean.

---

## CI Guard Health

Both audit scripts are wired into `.github/workflows/ci.yml`:

| Job | Script | Status |
|-----|--------|--------|
| `duplicate-def-guard` | `audit_duplicate_defs.py --fail-on-found` | **STRICT** — hard fail |
| `cherry-pick-regression-guard` | `audit_cherry_pick_regressions.py --fail-on-found` | `continue-on-error: true` |

**Action needed for batch 77:** The cherry-pick regression guard was set to `continue-on-error: true`
in W1548 (#1415) because 18 pre-existing regressions existed at wiring time. Now that only 1 LOW
regression remains (which causes graceful test skips, not failures), the `continue-on-error` flag
should be **removed** to make the gate strict. This is safe once R6 (`ParakeetAdapter` alias) is fixed
in W1591, making the scanner return 0 missing symbols and exit=0 on `--fail-on-found`.

Suggested CI tightening plan:
1. W1591: Add `ParakeetAdapter = ParakeetSTTAdapter` alias → scanner exits 0
2. Batch 77: Remove `continue-on-error: true` from cherry-pick-regression-guard job

---

## Progress Trajectory

| Checkpoint | Import regressions | Batches completed |
|------------|:-----------------:|:-----------------:|
| W1525 initial scan | 26 | — |
| W1538 post-batch-71 attempt | 26 (0 merged at time) | 71 |
| W1552 post-batches-71-72 | 6 | 71-72 |
| W1590 post-batches-71-75 (NOW) | **1** | 71-75 |
| After W1591 (projected) | **0** | 76+ |

**Total fixed across repair campaign:** 25 of 26 (96% clearance rate)  
**Total remaining:** 1 LOW (alias-only, 6 tests skip gracefully)

---

## Recommendation

**Declare import-regression cleanup FUNCTIONALLY COMPLETE.**

The 1 remaining entry is a graceful-skip alias (LOW) with zero production impact. All CRIT, SEC HIGH,
HIGH, and MED severity import regressions are resolved.

### Immediate next step (batch 77, W1591)

Add `ParakeetAdapter = ParakeetSTTAdapter` to `KrabEar/core/pipeline/stt_parakeet.py`.  
Then remove `continue-on-error: true` from the CI cherry-pick-regression-guard job to enforce
strict gating going forward. This prevents any future cherry-pick train from silently re-introducing
regressions.

### Logic regression status (not tracked by import scanner)

Addressed in batches 70-75:
- `settings_service` — W1518 audit closed by W1564 (#1426): confirmed all fixes already landed, "31-failure report" was a ghost audit against wrong branch state.
- `text_postprocessor` — W1523 (#1393) restored W1376 colon + W1377 dotted abbreviation (W916 clobber fixed).
- `translator` — W1520 (#1399) restored W1428+W1429+W1430+W1455+W1498+W1500 (W1497 cherry-pick revert fixed).
- `history_service` — W1521 (#1396) restored 8 waves clobbered by W941.
- `auto_glossary` — W1570 (#1429) wired privacy guard on invalidate + stale cache clear.
- `auto_deduplication` — W1571 (#1434) + W1572 (#1433) wired dead-code logic into production paths.
- `silence_detector` — W1574 (#1432) cleaned dead code + unified SSOT.

All 4 logic-regression areas from W1552 are now resolved.
