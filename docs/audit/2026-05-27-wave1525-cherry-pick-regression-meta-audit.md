# W1525 — Meta-Audit: Cherry-Pick Regression Scan

**Date:** 2026-05-27  
**Branch:** fix-event-replay-mode-constant-W1316  
**Method:** Static import analysis — scan all 617 test files for symbols imported
from production modules that no longer exist at module scope.

## Root Cause Context

W1497 cherry-pick train (113 PRs) used `--theirs` conflict resolution when rebasing,
which silently reverted the "ours" side of any conflicting hunk.  
W1514–W1519 caught 4 specific file regressions post-hoc (translator, settings_service,
audio_quality, punctuation_fixer).  This audit systematically scans ALL modules.

## Methodology

Script: `scripts/audit_cherry_pick_regressions.py`

For each of 617 test files:
1. Parse all `from X import Y` statements via `ast`
2. Resolve `X` to its source file in `KrabEar/backend/` or `KrabEar/core/`
3. Extract top-level names from that source file (functions, classes, module-level
   assignments/annotations)
4. Flag every `Y` that is missing

False-positive filters applied:
- Dunder names (`__*`) excluded
- `werkzeug.*` (optional guard-import in test) excluded
- `backend.service._escape_as_str` excluded — currently a `@staticmethod` inside
  `BackendService` class; the test's `from backend.service import _escape_as_str`
  will `ImportError` at runtime but this was a pre-existing test design issue, not a
  cherry-pick regression (the function was never module-level).
- `core.pipeline.stt_parakeet.ParakeetAdapter` excluded — the class was renamed to
  `ParakeetSTTAdapter` in a legitimate refactor; the test alias `PARAKEET_AVAILABLE`
  gate skips the test body.

**Net confirmed regressions: 26 unique missing symbols across 18 modules.**

---

## Top 10 Regressions by Severity

### 1. CRIT — `core.audio_quality._safe_float` (W1017/W1442 reverted)

| Field | Value |
|-------|-------|
| Missing symbol | `_safe_float` |
| Module | `KrabEar/core/audio_quality.py` |
| Detected in | `KrabEar/tests/test_audio_quality_nan_W1017.py` |
| Introduced by | W1017 (NaN guard fix) + W1442 |
| Reverted by | W1107 cherry-pick `--theirs` (per W1519 audit) |
| Impact | NaN/inf values from noisy audio pass unchecked into quality report fields → crashes or silent wrong results downstream |
| Fix wave | W1522 (URGENT tag — already created, check merge status) |

**Status:** W1522 commit `9a0857f2` restores the fix. Verify it is on `codex/krab-ear-v2`.

---

### 2. CRIT — `core.silence_detector.SILENCE_THRESHOLD_DB_STRICT` + `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER` (W1018 reverted)

| Field | Value |
|-------|-------|
| Missing symbols | `SILENCE_THRESHOLD_DB_STRICT`, `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER` |
| Module | `KrabEar/core/silence_detector.py` |
| Detected in | `KrabEar/tests/test_silence_detector.py` |
| Introduced by | W1018 |
| Impact | Two named threshold constants replaced by a single shared `SILENCE_THRESHOLD_DB = -40.0`. The "preserve-Whisper" path (STT routing) now uses the wrong threshold: -40 dB instead of -55 dB, causing Whisper to receive audio classified as silence → empty transcripts in quiet-room recordings |
| Priority | CRIT — affects every STT call |

---

### 3. HIGH — `backend.history_service._EXPORT_ALLOWED_ROOTS` + `_is_safe_export_dir` (W1432 reverted)

| Field | Value |
|-------|-------|
| Missing symbols | `_EXPORT_ALLOWED_ROOTS`, `_is_safe_export_dir` |
| Module | `KrabEar/backend/history_service.py` |
| Detected in | `KrabEar/tests/test_export_obsidian_allowlist_W1432.py` |
| Introduced by | W1432 (path traversal allowlist — HIGH security fix) |
| Reverted by | W1497 cherry-pick |
| Impact | SECURITY: export handlers (`export_history_srt`, Obsidian export) accept arbitrary `output_dir` without path traversal check — attacker-controlled IPC param can write files outside data dir |

---

### 4. HIGH — `backend.privacy_audit._KEY_FILENAME` + `_compute_entry_hash` (W974 reverted)

| Field | Value |
|-------|-------|
| Missing symbols | `_KEY_FILENAME`, `_compute_entry_hash` |
| Module | `KrabEar/backend/privacy_audit.py` |
| Detected in | `KrabEar/tests/test_privacy_audit_hash_chain.py` |
| Introduced by | W974 (HMAC-SHA256 hash chain for tamper detection) |
| Impact | Privacy audit log has no tamper-detection chain — compliance-sensitive feature silently regressed; log entries can be forged |

---

### 5. HIGH — `core.engine._VOXTRAL_REPO_ALLOWLIST` (W1223 reverted)

| Field | Value |
|-------|-------|
| Missing symbol | `_VOXTRAL_REPO_ALLOWLIST` |
| Module | `KrabEar/core/engine.py` |
| Detected in | `KrabEar/tests/test_voxtral_security_W1223.py` |
| Introduced by | W1223 (Voxtral adapter security — model repo allowlist) |
| Impact | SECURITY: Voxtral STT adapter accepts arbitrary model repo string from settings → potential model-loading SSRF / arbitrary path traversal. Allowlist removed. |

---

### 6. HIGH — `core.engine._UNAVAILABLE_TTL_SEC` (W1141/W1304 reverted)

| Field | Value |
|-------|-------|
| Missing symbol | `_UNAVAILABLE_TTL_SEC` |
| Module | `KrabEar/core/engine.py` |
| Detected in | `KrabEar/tests/test_unavailable_models_ttl_W1141.py` |
| Introduced by | W1304 (W1141 was wrong, W1304 was the ACTUAL implementation) |
| Impact | `_unavailable_models` set never expires — once a model is marked unavailable (e.g. on cold start timeout) it stays permanently banned until process restart. Causes permanent STT degradation after any transient failure. |

---

### 7. HIGH — `backend.auto_deduplication._PRIVACY_SKIPPED` + `_text_similarity` (W1248/W1245 reverted)

| Field | Value |
|-------|-------|
| Missing symbols | `_PRIVACY_SKIPPED`, `_text_similarity` |
| Module | `KrabEar/backend/auto_deduplication.py` |
| Detected in | `test_auto_dedup_privacy_W1248.py`, `test_auto_deduplication_W1245.py` |
| Introduced by | W1248 (privacy_mode guard), W1245 (dedup logic) |
| Impact | Auto-deduplication runs on privacy-mode recordings (leaks content comparison); `_text_similarity` as module-level function may have been a refactor from method that broke internal coupling |

---

### 8. HIGH — `core.text_anonymizer._snils_valid` + `_iban_valid` (W1022 reverted)

| Field | Value |
|-------|-------|
| Missing symbols | `_snils_valid`, `_iban_valid` |
| Module | `KrabEar/core/text_anonymizer.py` |
| Detected in | `KrabEar/tests/test_text_anonymizer.py` |
| Introduced by | W1022 (SNILS/IBAN checksum validators) |
| Impact | SNILS (Russian pension fund ID) and IBAN checksum validation absent — PII redaction accepts invalid numbers, causing both false positives and false negatives in redaction |

---

### 9. MED — `backend.sharing_manager._MAX_SHARE_ITEMS` + `_MAX_TTL_HOURS` (W1244 reverted)

| Field | Value |
|-------|-------|
| Missing symbols | `_MAX_SHARE_ITEMS`, `_MAX_TTL_HOURS` |
| Module | `KrabEar/backend/sharing_manager.py` |
| Detected in | `KrabEar/tests/test_sharing_manager_ttl_items_W1244.py` |
| Introduced by | W1244 (TTL finite cap + item_ids cap + empty warning) |
| Impact | No upper bound on share package size or TTL → DoS via unbounded share creation; share links never expire |

---

### 10. MED — `core.audio_denoiser._NOISEREDUCE_PARAMS` (W1322 reverted)

| Field | Value |
|-------|-------|
| Missing symbol | `_NOISEREDUCE_PARAMS` |
| Module | `KrabEar/core/audio_denoiser.py` |
| Detected in | `KrabEar/tests/test_noisereduce_strength_floor_W1322.py` |
| Introduced by | W1322 (noisereduce strength-to-prop_decrease mapping) |
| Impact | Denoiser strength knob (`off/light/moderate/strong`) ignored — all calls pass identical parameters regardless of user setting |

---

## Full Finding Table (26 regressions, excluding 2 false positives)

| # | Severity | Module | Symbol | Wave Introduced | Test File |
|---|----------|--------|--------|-----------------|-----------|
| 1 | CRIT | `core.audio_quality` | `_safe_float` | W1017/W1442 | test_audio_quality_nan_W1017.py |
| 2 | CRIT | `core.silence_detector` | `SILENCE_THRESHOLD_DB_STRICT` | W1018 | test_silence_detector.py |
| 3 | CRIT | `core.silence_detector` | `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER` | W1018 | test_silence_detector.py |
| 4 | HIGH | `backend.history_service` | `_EXPORT_ALLOWED_ROOTS` | W1432 | test_export_obsidian_allowlist_W1432.py |
| 5 | HIGH | `backend.history_service` | `_is_safe_export_dir` | W1432 | test_export_obsidian_allowlist_W1432.py |
| 6 | HIGH | `backend.privacy_audit` | `_KEY_FILENAME` | W974 | test_privacy_audit_hash_chain.py |
| 7 | HIGH | `backend.privacy_audit` | `_compute_entry_hash` | W974 | test_privacy_audit_hash_chain.py |
| 8 | HIGH | `core.engine` | `_VOXTRAL_REPO_ALLOWLIST` | W1223 | test_voxtral_security_W1223.py |
| 9 | HIGH | `core.engine` | `_UNAVAILABLE_TTL_SEC` | W1304 | test_unavailable_models_ttl_W1141.py |
| 10 | HIGH | `backend.auto_deduplication` | `_PRIVACY_SKIPPED` | W1248 | test_auto_dedup_privacy_W1248.py |
| 11 | HIGH | `backend.auto_deduplication` | `_text_similarity` | W1245 | test_auto_deduplication_W1245.py |
| 12 | HIGH | `core.text_anonymizer` | `_snils_valid` | W1022 | test_text_anonymizer.py |
| 13 | HIGH | `core.text_anonymizer` | `_iban_valid` | W1022 | test_text_anonymizer.py |
| 14 | MED | `backend.sharing_manager` | `_MAX_SHARE_ITEMS` | W1244 | test_sharing_manager_ttl_items_W1244.py |
| 15 | MED | `backend.sharing_manager` | `_MAX_TTL_HOURS` | W1244 | test_sharing_manager_ttl_items_W1244.py |
| 16 | MED | `core.audio_denoiser` | `_NOISEREDUCE_PARAMS` | W1322 | test_noisereduce_strength_floor_W1322.py |
| 17 | MED | `backend.job_tracker` | `_PRUNE_CANCEL_EVENT_TTL` | W1185 | test_jobtracker_zombie_W1185.py |
| 18 | MED | `backend.archive_manager` | `_ARCHIVE_LOCK_FILE` | W1262 | test_archive_manager_flock_W1262.py |
| 19 | MED | `backend.speaker_manager` | `_MAX_EMBEDDING_FLOATS` | W1236 | test_speaker_embedding_validation_W1236.py |
| 20 | MED | `backend.transcript_versioning` | `_MAX_TEXT_BYTES` | W1045 | test_transcript_versioning.py |
| 21 | MED | `core.audio_lang_id` | `SUPPORTED_LANGUAGES` | W1121 | test_audio_lang_id_allowlist_W1121.py |
| 22 | MED | `core.auto_glossary` | `_FILLER_STARTERS` | W1294 | test_auto_glossary_w1294.py |
| 23 | MED | `core.auto_glossary` | `_starts_with_filler` | W1294 | test_auto_glossary_w1294.py |
| 24 | MED | `core.topic_tracker` | `_HARD_MAX_ITEMS` | W1281 | test_topic_tracker_dos_W1281.py |
| 25 | MED | `core.voice_commands` | `_VOICE_COMMANDS_STRICT_MODE` | W1256 | test_voice_commands_w1256_ambiguous.py |
| 26 | LOW | `backend.service` | `_escape_as_str` (see note) | W944/W1033 | test_applescript_injection.py |

**Note on #26:** `_escape_as_str` is currently a `@staticmethod` inside `BackendService` class.
The test does `from backend.service import _escape_as_str` which will `ImportError`.
W944 added it as a static method but the test was written assuming module-level.
This may require promoting it to module scope or adjusting the test import.

---

## False Positives Excluded

| Symbol | Reason |
|--------|--------|
| `werkzeug.utils.secure_filename` | Optional guard-import with inline stub — not a regression |
| `core.pipeline.stt_parakeet.ParakeetAdapter` | Class renamed to `ParakeetSTTAdapter` in legitimate refactor; test is skip-gated |

---

## Recommended Fix Priority

1. **Immediate (CRIT):** `silence_detector` two-threshold constants (W1018) — every STT call affected
2. **Immediate (CRIT):** `audio_quality._safe_float` — verify W1522 is merged to `codex/krab-ear-v2`  
3. **Security (HIGH):** `history_service` path allowlist (W1432) + `engine` Voxtral allowlist (W1223)
4. **Compliance (HIGH):** `privacy_audit` hash chain (W974)
5. **Correctness (HIGH):** `engine._UNAVAILABLE_TTL_SEC` (W1304), `text_anonymizer` checksum validators (W1022)
6. **DoS protection (MED):** `sharing_manager` caps (W1244), `topic_tracker` cap (W1281)
7. **Feature correctness (MED):** remaining 10 MED items

---

## How to Regenerate

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
python3 scripts/audit_cherry_pick_regressions.py
```

Exit code 1 if any regressions remain; 0 when clean.
