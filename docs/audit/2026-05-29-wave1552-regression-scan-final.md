# W1552 Final Regression Scan — Post-Batch-71+72

**Date:** 2026-05-29  
**Auditor:** W1552 sub-agent  
**Method:** AST-based `from X import Y` symbol scanner (`scripts/audit_cherry_pick_regressions.py`)  
**Scope:** 623 test files, 2075 from-imports checked  
**Baseline:** W1525 (26 regressions) + W1538 (26 total, 12 new MED added)  
**Trigger:** Batches 71+72 shipped 18+ regression fixes (W1442, W1530–W1549); verify production state.  
**Duplicate defs:** `audit_duplicate_defs.py --fail-on-found` → exit=0, 0 genuine shadowing bugs.

---

## Executive Summary

**20 of 26 baseline regressions fixed. 6 unique symbols remain missing.**

Scanner output at HEAD (`70463af9`):

| Metric | W1538 baseline | W1552 (now) | Delta |
|--------|:--------------:|:-----------:|:-----:|
| Total missing symbol entries (raw) | 26 | 8 | −18 |
| Unique (module, symbol) pairs missing | 26 | 6 | −20 |
| Genuine duplicate-def shadowing bugs | 0 | 0 | 0 |

---

## Fixed (20 of 26 — W1525 + W1538 NEW items resolved)

All fixed by Batch 71 (W1530–W1537) + Batch 72 (W1540–W1547):

| # | Symbol(s) | Module | Severity | Fix wave | PR |
|---|-----------|--------|----------|----------|----|
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
| 12 | `_text_similarity` | `backend.auto_deduplication` | HIGH | W1540 | #1419 |
| 13 | `_starts_with_filler` | `core.auto_glossary` | MED | W1541 | #1417 |
| 14 | `_FILLER_STARTERS` | `core.auto_glossary` | MED | W1541 | #1417 |
| 15 | `_ARCHIVE_LOCK_FILE` | `backend.archive_manager` | MED | W1542 | #1418 |
| 16 | `_PRUNE_CANCEL_EVENT_TTL` | `backend.job_tracker` | MED | W1542 | #1418 |
| 17 | `_MAX_TTL_HOURS` | `backend.sharing_manager` | MED | W1546 | #1416 |
| 18 | `_MAX_SHARE_ITEMS` | `backend.sharing_manager` | MED | W1546 | #1416 |
| 19 | `_HARD_MAX_ITEMS` | `core.topic_tracker` | MED | W1547 | #1420 |
| 20 | `_VOICE_COMMANDS_STRICT_MODE` | `core.voice_commands` | MED | W1547 | #1420 |

**Note:** `_MAX_TEXT_BYTES` in `backend.transcript_versioning` was partially addressed by W1547 but the
scanner still reports it missing — see Remaining section below for root cause analysis.

---

## Remaining (6 unique symbols — batch 73 targets)

### R1 — `core.audio_lang_id` :: `SUPPORTED_LANGUAGES` — HIGH

**Status:** W1530 (#1404) restored `_MIN_PEAK_AMPLITUDE` and `_MIN_CONFIDENCE` guards, but
`test_audio_lang_id_allowlist_W1121.py` additionally imports `SUPPORTED_LANGUAGES` (a `frozenset`
of allowed ISO 639-1 codes: `{"ru", "uk", "en", "es", ...}`). This symbol was never added by W1530.

**Root cause:** The W1121 allowlist feature originally added `SUPPORTED_LANGUAGES` to `audio_lang_id.py`,
but a subsequent cherry-pick used `--theirs` and dropped both the constant and the guards. W1530 only
restored the guards, not the allowlist constant.

**Fix:** Add `SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"ru", "uk", "en", "es", ...})` to
`KrabEar/core/audio_lang_id.py` as a module-level constant, exposed alongside the existing guards.

**Severity:** HIGH — test `test_audio_lang_id_allowlist_W1121.py` will ImportError at collection time,
causing 4 test cases to be silently skipped.

---

### R2 — `core.engine` :: `_UNAVAILABLE_TTL_SEC` — HIGH

**Status:** W1534 (#1409) restored `_handle_clear_unavailable_models` and confirmed `_UNAVAILABLE_MODEL_TTL_SEC`
(= 300 s) exists in `engine.py` at line 229. However `test_unavailable_models_ttl_W1141.py` line 109
imports `_UNAVAILABLE_TTL_SEC` (no `_MODEL_` infix). This is a name mismatch: the constant was renamed
in an earlier refactor but the backward-compat alias was never added.

**Root cause:** Original W1141 test expected `_UNAVAILABLE_TTL_SEC`; engine.py was later refactored to
`_UNAVAILABLE_MODEL_TTL_SEC` without adding an alias.

**Fix:** Add alias `_UNAVAILABLE_TTL_SEC = _UNAVAILABLE_MODEL_TTL_SEC` to `KrabEar/core/engine.py`
(module level, after the existing constant definition), or rename the test import. Alias approach
preserves backward compat without touching the test.

**Severity:** HIGH — 3 TTL test cases in `test_unavailable_models_ttl_W1141.py` will ImportError.

---

### R3 — `core.audio_denoiser` :: `_NOISEREDUCE_PARAMS` — MEDIUM

**Status:** `audio_denoiser.py` has the equivalent dict as `_STRENGTH_PARAMS` (line 34) but the test
`test_noisereduce_strength_floor_W1322.py` imports `_NOISEREDUCE_PARAMS`. Name mismatch — the constant
was renamed in a refactor but the alias was dropped.

**Root cause:** W1322 originally used `_NOISEREDUCE_PARAMS`; a later refactor renamed it `_STRENGTH_PARAMS`
without preserving the old name as an alias.

**Current `_STRENGTH_PARAMS` values** (do NOT match test expectations):
- `"strong": prop_decrease=0.95` — test expects `0.75`
- `"moderate": prop_decrease=0.75` — test expects `0.85`
- `"light": prop_decrease=0.50` — test expects `0.50`

**Fix:** Add `_NOISEREDUCE_PARAMS = _STRENGTH_PARAMS` alias AND restore correct `prop_decrease` values
per W1322 spec: `strong=0.75`, `moderate=0.85`, `light=0.50`.

**Severity:** MEDIUM — 4 test cases in `test_noisereduce_strength_floor_W1322.py` will fail.

---

### R4 — `backend.speaker_manager` :: `_MAX_EMBEDDING_FLOATS` — MEDIUM

**Status:** `speaker_manager.py` has no `_MAX_EMBEDDING_FLOATS` constant at module level.
`test_speaker_embedding_validation_W1236.py` imports it to verify embedding size validation.

**Fix:** Add `_MAX_EMBEDDING_FLOATS: int = <value>` to `KrabEar/backend/speaker_manager.py`.
Check W1236 commit history for the original value (likely 512 or 1024 for typical speaker embeddings).
The test uses it as a boundary: `dim=_MAX_EMBEDDING_FLOATS + 1` should raise `ValueError`.

**Severity:** MEDIUM — 3 test cases will ImportError.

---

### R5 — `backend.transcript_versioning` :: `_MAX_TEXT_BYTES` — MEDIUM

**Status:** W1547 (#1420) commit message claims it restores `_MAX_TEXT_BYTES`, but the constant is
still absent from `KrabEar/backend/transcript_versioning.py`. The W1547 commit may have added it
elsewhere or the symbol was dropped during rebase.

**Root cause:** Likely W1547 had a rebase conflict that silently dropped this constant.

**Fix:** Add `_MAX_TEXT_BYTES: int = <value>` (likely 1_000_000 or 500_000) to
`KrabEar/backend/transcript_versioning.py`. Check `test_transcript_versioning.py` for the
expected value (used in byte-limit boundary tests).

**Severity:** MEDIUM — test_transcript_versioning.py will ImportError.

---

### R6 — `core.pipeline.stt_parakeet` :: `ParakeetAdapter` — LOW (skipUnless)

**Status:** `stt_parakeet.py` exports `ParakeetSTTAdapter` (line 49), but the test imports
`ParakeetAdapter`. The test wraps the import in `try/except ImportError`, so at runtime
the test class is decorated with `@unittest.skipUnless(PARAKEET_AVAILABLE, ...)` and silently
skips. No ImportError crashes test collection.

**Impact:** 6 Parakeet adapter tests are always skipped. Low urgency — fix by adding
`ParakeetAdapter = ParakeetSTTAdapter` alias in `stt_parakeet.py`.

**Severity:** LOW — tests skip gracefully, no test-collection failures.

---

## Non-scanner Regressions (logic-only, not import-breakage)

These were documented in W1514/W1517/W1518/W1526 audit docs and are NOT captured by the import
scanner (functions exist, implementations are wrong):

| Area | Audit doc | Status | Action |
|------|-----------|--------|--------|
| `settings_service` — 31 tests failing | W1518 | **Unknown** — no fix wave shipped | Batch 73 target |
| `text_postprocessor` — W916 clobbered W1376+W1377 | W1514 | **Unknown** — no fix wave shipped | Batch 73 target |
| `translator` — W1190 reverted all prior fixes | W1517 | **Unknown** — no fix wave shipped | Batch 73 target |
| `history_service` — 4 of 5 fix waves lost | W1526 | **Unknown** — no fix wave shipped | Batch 73 target |

---

## Duplicate Definitions

`audit_duplicate_defs.py --fail-on-found` → **exit=0**

- 0 genuine shadowing bugs
- 13 `@property` getter/setter pairs (expected false positives)

Clean.

---

## Recommendation

**Continue Batch 73. Do NOT declare regression cleanup complete yet.**

### Batch 73 targets (import-scanner regressions — 5 fixes needed):

| Wave | Symbol | Module | Fix type |
|------|--------|--------|----------|
| W1553 | `SUPPORTED_LANGUAGES` | `core.audio_lang_id` | Add frozenset constant |
| W1554 | `_UNAVAILABLE_TTL_SEC` | `core.engine` | Add backward-compat alias |
| W1555 | `_NOISEREDUCE_PARAMS` | `core.audio_denoiser` | Add alias + correct prop_decrease values |
| W1556 | `_MAX_EMBEDDING_FLOATS` | `backend.speaker_manager` | Add int constant |
| W1557 | `_MAX_TEXT_BYTES` | `backend.transcript_versioning` | Add int constant (W1547 incomplete) |
| W1558 | `ParakeetAdapter` | `core.pipeline.stt_parakeet` | Add alias (LOW, batch last) |

### Batch 73 targets (logic regressions — 4 investigations needed):

| Wave | Module | Audit doc | Action |
|------|--------|-----------|--------|
| W1559 | `settings_service` | W1518 | Investigate 31 failing tests, repair |
| W1560 | `text_postprocessor` | W1514 | Repair W916 clobber |
| W1561 | `translator` | W1517 | Restore W1190-reverted fixes |
| W1562 | `history_service` | W1526 | Restore 4 lost fix waves |

### Progress trajectory

- **W1525 baseline (original):** 26 import regressions
- **W1538 scan (post-batch-71 attempt):** still 26 (0 merged at that point)
- **W1552 scan (now):** 6 remaining (20 fixed = 77% cleared)
- **After batch 73:** target 0 import regressions (+ logic regression sweep)

---

## Scanner Tool Note

CI integration added in W1548 (#1415): both `audit_cherry_pick_regressions.py` and
`audit_duplicate_defs.py` now run on every push. Future cherry-pick trains cannot silently
reintroduce these regressions without CI catching them immediately.
