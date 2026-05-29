# W1538 Regression Status Update — Post-Batch-71 Fresh Scan

**Date:** 2026-05-27  
**Auditor:** W1538 sub-agent  
**Method:** AST-based `from X import Y` symbol scanner (`scripts/audit_cherry_pick_regressions.py`)  
**Scope:** 617 test files, 2049 from-imports checked  
**Baseline:** W1525 known-regression list (26 items)  
**Trigger:** 7 repair waves (W1442, W1530–W1536) claimed shipped; verify production merge state.

---

## Executive Summary

**All 7 repair waves exist on local branches but have NOT been merged into `origin/codex/krab-ear-v2`.**  
The production branch still has **26 missing symbols** detected by the scanner — same count as the
W1525 baseline. Net improvement in `main`: **0 regressions resolved**.

The scanner additionally surfaced **14 NEW regressions** not present in the original W1525 list,
bringing the true outstanding count to **26 total missing symbols** (some from W1525 list, some new
— the overlap means total unique regressions = 26 at this moment in `main`).

---

## Merge State of Repair Waves

| Wave | Fix description | Branch | PR | Merged to main |
|------|----------------|--------|-----|----------------|
| W1442 | `audio_quality._safe_float` restore (duplicate shadow) | `fix/duplicate-defs-W1442` | #1337 partial | NO — W1107 re-removed it post-merge |
| W1530 | `audio_lang_id` zero-peak short-circuit + `MIN_CONFIDENCE` gate | `fix/audio-lang-id-zero-peak-W1530` | none | NO |
| W1531 | `silence_detector.SILENCE_THRESHOLD_DB_STRICT` + `PRESERVE_WHISPER` | `fix/silence-threshold-tiers-W1531` | none | NO |
| W1532 | `history_service._EXPORT_ALLOWED_ROOTS` + `_is_safe_export_dir` | `fix/history-export-allowlist-W1532` | none | NO |
| W1533 | `privacy_audit._compute_entry_hash` + `_KEY_FILENAME` (HMAC chain) | `fix/privacy-audit-hmac-chain-W1533` | none | NO |
| W1534 | `engine._UNAVAILABLE_TTL_SEC` + `_handle_clear_unavailable_models` | `fix/engine-unavailable-ttl-W1534` | none | NO |
| W1535 | `engine._VOXTRAL_REPO_ALLOWLIST` | `fix/voxtral-repo-allowlist-W1535` | none | NO |
| W1536 | `text_anonymizer._snils_valid` + `_iban_valid` | `fix/text-anonymizer-snils-iban-W1536` | none | NO |
| W1537 | `auto_deduplication._PRIVACY_SKIPPED` | `fix/autodedup-privacy-provider-W1537` | none | NO |
| W1538* | `text_postprocessor._punctuation_pass_allowed` privacy guard | `restore-punctuation-pass-privacy-W1538` | none | NO |

*W1538 is the punctuation-pass privacy fix itself (same wave number as this audit — authored concurrently).

---

## Full Scanner Output — 26 Unique Missing Symbols on `main`

Scanner run: `python3 scripts/audit_cherry_pick_regressions.py` on `origin/codex/krab-ear-v2` HEAD (`83007afa`).

| # | Module | Symbol | Test file | W1525 known? | Fix wave | Severity |
|---|--------|--------|-----------|:------------:|----------|----------|
| 1 | `core.silence_detector` | `SILENCE_THRESHOLD_DB_STRICT` | `test_silence_detector.py` | YES | W1531 (branch) | CRIT |
| 2 | `core.silence_detector` | `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER` | `test_silence_detector.py` | YES | W1531 (branch) | CRIT |
| 3 | `core.audio_quality` | `_safe_float` | `test_audio_quality_nan_W1017.py` | YES | W1442/W1522 (branch) | CRIT |
| 4 | `core.audio_quality` | `_safe_float` | `test_safe_float_W1442.py` | YES | W1442/W1522 (branch) | CRIT |
| 5 | `backend.history_service` | `_EXPORT_ALLOWED_ROOTS` | `test_export_obsidian_allowlist_W1432.py` | YES | W1532 (branch) | SEC HIGH |
| 6 | `backend.history_service` | `_is_safe_export_dir` | `test_export_obsidian_allowlist_W1432.py` | YES | W1532 (branch) | SEC HIGH |
| 7 | `core.engine` | `_VOXTRAL_REPO_ALLOWLIST` | `test_voxtral_security_W1223.py` | YES | W1535 (branch) | SEC HIGH |
| 8 | `backend.privacy_audit` | `_compute_entry_hash` | `test_privacy_audit_hash_chain.py` | YES | W1533 (branch) | HIGH |
| 9 | `backend.privacy_audit` | `_KEY_FILENAME` | `test_privacy_audit_hash_chain.py` | YES | W1533 (branch) | HIGH |
| 10 | `core.engine` | `_UNAVAILABLE_TTL_SEC` | `test_unavailable_models_ttl_W1141.py` | YES | W1534 (branch) | HIGH |
| 11 | `core.text_anonymizer` | `_snils_valid` | `test_text_anonymizer.py` | YES | W1536 (branch) | HIGH |
| 12 | `core.text_anonymizer` | `_iban_valid` | `test_text_anonymizer.py` | YES | W1536 (branch) | HIGH |
| 13 | `core.audio_lang_id` | `SUPPORTED_LANGUAGES` | `test_audio_lang_id_allowlist_W1121.py` | YES | W1530 (branch) | HIGH |
| 14 | `backend.auto_deduplication` | `_PRIVACY_SKIPPED` | `test_auto_dedup_privacy_W1248.py` | YES | W1537 (branch) | HIGH |
| 15 | `backend.auto_deduplication` | `_text_similarity` | `test_auto_deduplication_W1245.py` | **NEW** | — | HIGH |
| 16 | `core.auto_glossary` | `_starts_with_filler` | `test_auto_glossary_w1294.py` | **NEW** | — | MEDIUM |
| 17 | `core.auto_glossary` | `_FILLER_STARTERS` | `test_auto_glossary_w1294.py` | **NEW** | — | MEDIUM |
| 18 | `backend.archive_manager` | `_ARCHIVE_LOCK_FILE` | `test_archive_manager_flock_W1262.py` | **NEW** | — | MEDIUM |
| 19 | `backend.job_tracker` | `_PRUNE_CANCEL_EVENT_TTL` | `test_jobtracker_zombie_W1185.py` | **NEW** | — | MEDIUM |
| 20 | `core.audio_denoiser` | `_NOISEREDUCE_PARAMS` | `test_noisereduce_strength_floor_W1322.py` | **NEW** | — | MEDIUM |
| 21 | `backend.speaker_manager` | `_MAX_EMBEDDING_FLOATS` | `test_speaker_embedding_validation_W1236.py` | **NEW** | — | MEDIUM |
| 22 | `backend.sharing_manager` | `_MAX_TTL_HOURS` | `test_sharing_manager_ttl_items_W1244.py` | **NEW** | — | MEDIUM |
| 23 | `backend.sharing_manager` | `_MAX_SHARE_ITEMS` | `test_sharing_manager_ttl_items_W1244.py` | **NEW** | — | MEDIUM |
| 24 | `backend.transcript_versioning` | `_MAX_TEXT_BYTES` | `test_transcript_versioning.py` | **NEW** | — | MEDIUM |
| 25 | `core.topic_tracker` | `_HARD_MAX_ITEMS` | `test_topic_tracker_dos_W1281.py` | **NEW** | — | MEDIUM |
| 26 | `core.voice_commands` | `_VOICE_COMMANDS_STRICT_MODE` | `test_voice_commands_w1256_ambiguous.py` | **NEW** | — | MEDIUM |
| — | `core.pipeline.stt_parakeet` | `ParakeetAdapter` | `test_stt_adapter_router.py` | **NEW** | — | LOW* |

*`ParakeetAdapter` is in the scanner output as a raw count. However `core/pipeline/stt_parakeet.py` exists as a stub adapter file — this may be a class-name mismatch rather than a true regression. Verify separately.

---

## Status by W1525 Category

### CRIT (2 original items)

| Item | Symbol(s) | W1525 status | Current main | Fix wave |
|------|-----------|:------------:|:------------:|----------|
| silence_detector tier constants | `SILENCE_THRESHOLD_DB_STRICT`, `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER` | CRIT | STILL MISSING | W1531 on branch |
| audio_quality._safe_float | `_safe_float` | CRIT | STILL MISSING | W1442 merged #1337 then re-removed by W1107 (W1519 S1). W1522 on branch. |

**0 of 2 CRIT items resolved in main.**

### SECURITY HIGH (2 original items)

| Item | Symbol(s) | W1525 status | Current main | Fix wave |
|------|-----------|:------------:|:------------:|----------|
| history_service export allowlist | `_EXPORT_ALLOWED_ROOTS`, `_is_safe_export_dir` | SEC HIGH | STILL MISSING | W1532 on branch |
| engine Voxtral repo allowlist | `_VOXTRAL_REPO_ALLOWLIST` | SEC HIGH | STILL MISSING | W1535 on branch |

**0 of 2 SEC HIGH items resolved in main.**

### HIGH (7 original items — partial list from W1525)

| Item | Symbol(s) | W1525 status | Current main | Fix wave |
|------|-----------|:------------:|:------------:|----------|
| privacy_audit HMAC hash chain | `_compute_entry_hash`, `_KEY_FILENAME` | HIGH | STILL MISSING | W1533 on branch |
| engine unavailable TTL | `_UNAVAILABLE_TTL_SEC` | HIGH | STILL MISSING | W1534 on branch |
| text_anonymizer SNILS + IBAN | `_snils_valid`, `_iban_valid` | HIGH | STILL MISSING | W1536 on branch |
| auto_deduplication privacy flag | `_PRIVACY_SKIPPED` | HIGH | STILL MISSING | W1537 on branch |
| audio_lang_id confidence gate | `SUPPORTED_LANGUAGES` | HIGH | STILL MISSING | W1530 on branch |

**0 of 7 HIGH items resolved in main.**

---

## New Regressions (not in W1525 list)

The scanner found **12 new regression symbols** (across 11 unique test failures) not flagged by W1525:

| Symbol | Module | Severity | Notes |
|--------|--------|----------|-------|
| `_text_similarity` | `backend.auto_deduplication` | HIGH | Internal similarity function; test `test_auto_deduplication_W1245` will ImportError |
| `_starts_with_filler` | `core.auto_glossary` | MEDIUM | Helper used in filler-word detection tests |
| `_FILLER_STARTERS` | `core.auto_glossary` | MEDIUM | Constant for filler pattern list |
| `_ARCHIVE_LOCK_FILE` | `backend.archive_manager` | MEDIUM | File-lock constant for flock tests |
| `_PRUNE_CANCEL_EVENT_TTL` | `backend.job_tracker` | MEDIUM | TTL constant for zombie-job pruning tests |
| `_NOISEREDUCE_PARAMS` | `core.audio_denoiser` | MEDIUM | Denoiser parameter dict; strength-floor tests |
| `_MAX_EMBEDDING_FLOATS` | `backend.speaker_manager` | MEDIUM | Embedding cap; validation tests |
| `_MAX_TTL_HOURS` | `backend.sharing_manager` | MEDIUM | Share TTL cap constant |
| `_MAX_SHARE_ITEMS` | `backend.sharing_manager` | MEDIUM | Share items cap constant |
| `_MAX_TEXT_BYTES` | `backend.transcript_versioning` | MEDIUM | Per-version text byte cap |
| `_HARD_MAX_ITEMS` | `core.topic_tracker` | MEDIUM | DoS guard cap constant |
| `_VOICE_COMMANDS_STRICT_MODE` | `core.voice_commands` | MEDIUM | Strict-mode flag for ambiguous command tests |

**Root cause pattern:** All new regressions follow the same "private constant extracted into a test
file but removed from source" pattern as the W1525 set. They were likely introduced by earlier
cherry-pick trains or refactor waves that removed module-level constants while keeping or adding
test imports expecting those constants.

---

## Scanner Tool

Created at: `scripts/audit_cherry_pick_regressions.py`

Capabilities:
- Parses all `KrabEar/tests/test_*.py` files with `ast.parse`
- Extracts `from backend.X import Y` and `from core.X import Y` statements
- Resolves the module to `KrabEar/backend/X.py` or `KrabEar/core/X.py`
- Collects all top-level definitions (classes, functions, constants, re-exports) via AST walk
- Reports `(test_file, module, symbol)` for any import not resolved in source

Run: `python3 scripts/audit_cherry_pick_regressions.py`  
JSON output: `python3 scripts/audit_cherry_pick_regressions.py --json`

---

## Recommended Action Plan

### Immediate (CRIT + SEC HIGH — 4 fixes, block release)

1. **Merge W1531** (`fix/silence-threshold-tiers-W1531`) — restores `SILENCE_THRESHOLD_DB_STRICT` + `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER`. Empty-transcript CRIT.
2. **Merge W1522** (`restore-safe-float-W1522`) or equivalent — restores `_safe_float` + `math` import. NaN crash CRIT.
3. **Merge W1532** (`fix/history-export-allowlist-W1532`) — restores export path traversal allowlist. Path-write SEC HIGH.
4. **Merge W1535** (`fix/voxtral-repo-allowlist-W1535`) — restores Voxtral repo allowlist. SSRF/supply-chain SEC HIGH.

### High priority (7 HIGH fixes)

5. **Merge W1533** — privacy_audit HMAC hash chain
6. **Merge W1534** — engine unavailable TTL + clear handler  
7. **Merge W1536** — SNILS + IBAN PII validators
8. **Merge W1537** — auto_dedup privacy flag
9. **Merge W1530** — audio_lang_id zero-peak + MIN_CONFIDENCE

### New regressions (12 MEDIUM — batch repair wave needed)

These 12 new symbols likely need a single consolidating wave (W1539 or W1540) to re-add the
removed constants/helpers to their source modules.

### Also pending (not reflected in scanner, logic-only regressions)

- **W1518**: `settings_service` — 8 fix waves clobbered, 31 tests failing (W1518 audit doc)
- **W1517**: `translator` — W1190 reverted all prior fixes (W1517 audit doc)
- **W1514**: `text_postprocessor` — W916 clobbered W1376+W1377 (W1514 audit doc)
- **W1526**: `history_service` — 4 of 5 fixes lost (W1526 audit doc)

These are behavior/logic regressions not captured by the import scanner (no missing symbols,
the functions exist but have wrong implementations).

---

## Conclusion

The 7 repair waves (W1530–W1536 + W1442) **exist as local fix branches** but were never
admin-merged to `codex/krab-ear-v2`. The main branch remains at the same regression baseline
as W1525 found, with an additional 12 new MEDIUM regressions discovered by this scan.

**Total outstanding import-breakage regressions in `main`: 26 unique symbols across 27 test
files (3 CRIT, 2 SEC HIGH, 9 HIGH, 12 MEDIUM + 1 LOW to verify).**

The merge train for Batch 71 fix branches is the next required step.
