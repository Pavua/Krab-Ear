# Wave 778 Config Audit — `KrabEar/core/config.py`

**Date:** 2026-05-26  
**Scope:** `KrabEar/core/config.py` — `Settings` Pydantic fields + `DEFAULT_SETTINGS` dict  
**Method:** AST parse + regex extraction + grep across `KrabEar/` (Python) and `native/` (Swift)  
**Action:** Read-only audit. Nothing removed.

---

## Summary

| Dimension | Total | Live | Test-only | Dead candidates |
|-----------|-------|------|-----------|-----------------|
| `Settings` Pydantic fields (unique) | 167 | 110 | 7 | 50 |
| `DEFAULT_SETTINGS` keys (unique) | 156 | 93 | 5 | 58 |

**Duplicate field declarations (bugs):** 12 in `Settings`, 2 in `DEFAULT_SETTINGS`

---

## 1. Duplicate Field Declarations

### 1a. `Settings` class — 12 fields declared twice

Pydantic processes field declarations in order; the **second declaration silently wins**. Both copies exist in source, creating confusion about the intended default and comment.

| Field | First decl (line) | Second decl (line) | Same default? |
|-------|------------------|--------------------|---------------|
| `DISK_MONITOR_ENABLED` | 74 | 635 | Yes (True) |
| `DISK_CHECK_INTERVAL_MIN` | 76 | 636 | Yes (30) |
| `DISK_WARNING_GB` | 78 | 637 | Yes (5.0) |
| `DISK_CRITICAL_GB` | 80 | 638 | Yes (1.0) |
| `HISTORY_LARGE_MB` | 82 | 639 | Yes (500) |
| `AUTO_CLEANUP_ENABLED` | 84 | 640 | Yes (False) |
| `AUTO_CLEANUP_AFTER_DAYS` | 86 | 641 | Yes (365) |
| `REALTIME_SILENCE_FILTER_ENABLED` | 203 | 608 | Yes (False) |
| `RT_SILENCE_CHECK_SEC` | 204 | 610 | Yes (5.0) |
| `RT_SILENCE_WINDOW_SEC` | 205 | 612 | Yes (10.0) |
| `RT_SILENCE_MAX_SEC` | 206 | 614 | Yes (8.0) |
| `PASTE_APP_MEMORY_ENABLED` | 557 | 628 | Yes (True) |

**Root cause:** Two separate comment blocks copy-pasted the disk-monitor group (lines 72–86 vs 634–641) and the realtime-silence group (lines 203–206 vs 608–614). Defaults are identical so there is no runtime impact, but the duplication is noise and risks future drift.

**Recommendation:** Remove the earlier copy-paste blocks (lines 72–86, 203–206, 557) and keep only the later, better-commented versions.

### 1b. `DEFAULT_SETTINGS` dict — 2 keys duplicated

| Key | Line 1 | Line 2 | Same value? |
|-----|--------|--------|-------------|
| `"llm_model"` | 996 | 1001 | Yes (`"qwen3-4b-abliterated"`) |
| `"paste_app_memory_enabled"` | ~921 | ~970 | Yes (True) |

The `"llm_model"` duplicate is particularly notable: there is a commented-out paragraph between the two entries (lines 997–1000) that was likely a copy-paste artifact during the addition of the `rewriter_fallback_chain` key. Python dicts retain the last value for duplicate keys.

---

## 2. `Settings` Pydantic Fields — Classification

### 2a. Live (used outside `config.py`, non-test code) — 110 fields

Key live fields with only 1 non-test reference (worth monitoring — near-dead):

| Field | py_non_test | py_tests | swift |
|-------|------------|----------|-------|
| `CALENDAR_LINK_CACHE_MIN` | 1 | 0 | 0 |
| `EXPORT_INCLUDE_SPEAKER_LABELS` | 1 | 0 | 0 |
| `IPC_SIGNING_ENABLED` | 1 | 1 | 0 |
| `PASTE_APP_MEMORY_ENABLED` | 1 | 0 | 0 |
| `REALTIME_PARTIAL_ENABLED` | — | — | — |
| `STT_DENOISE_ENABLED` | 1 | 0 | 0 |
| `STT_DENOISE_SNR_THRESHOLD_DB` | 1 | 0 | 0 |
| `STT_DENOISE_STRENGTH` | 1 | 0 | 0 |
| `STT_GATEWAY_URL` | 1 | 0 | 0 |
| `STT_MAX_RETRIES` | 1 | 0 | 0 |
| `STT_MULTIPASS_ENABLED` | 1 | 0 | 0 |

**Note on `settings.get()` pattern:** Several fields whose names appear as lowercase string keys in `settings.get("field_name")` calls (in `recording_core_service.py`, `realtime_silence_filter.py`) were NOT caught by the initial `settings.FIELD_NAME` grep. This caused 12 fields to be initially misclassified as dead. After adding the `settings.get` pattern, they are now correctly in the Live bucket:
- `ACTION_ITEMS_AUTO_EXTRACT`, `ACTION_ITEMS_MIN_DURATION_SEC`
- `LLM_BRAIN_MODEL`, `LLM_BRAIN_UNLOAD_ON_RECORDING`, `LLM_BRAIN_PRELOAD_ON_STOP`
- `REALTIME_PARTIAL_ENABLED`, `RT_PARTIAL_INTERVAL_SEC`, `RT_PARTIAL_BUFFER_SEC`
- `REALTIME_SILENCE_FILTER_ENABLED`, `RT_SILENCE_CHECK_SEC`, `RT_SILENCE_WINDOW_SEC`, `RT_SILENCE_MAX_SEC`

### 2b. Test-only (0 non-test non-config references) — 7 fields

These settings exist in `Settings` but are only referenced in `KrabEar/tests/`.  
They are real features with planned runtime use, but lack production wiring.

| Field | py_tests | Notes |
|-------|----------|-------|
| `AUTO_EXPORT_ENABLED` | 2 | Export scheduler feature |
| `PIPELINE_V2` | 17 | Feature flag for pipeline-based STT path |
| `REST_API_AUTH_ENABLED` | 6 | REST auth token management |
| `SMART_SILENCE_SKIP_ENABLED` | 1 | Silence skip before STT |
| `STT_AUDIO_LANG_ID_ENABLED` | 2 | Audio language ID (uses `getattr` pattern in stt_router) |
| `STT_OTHER_PRIMARY_MODEL` | 1 | Language-routing fallback |
| `VOICE_FINGERPRINT_MATCH_THRESHOLD` | 1 | Voice fingerprint matching |

**Note on `STT_AUDIO_LANG_ID_ENABLED`:** `stt_router.py` reads it via `getattr(self._settings, "STT_AUDIO_LANG_ID_ENABLED", True)` — this is a live pattern, but the grep missed it because the field name appears as an attribute string in `getattr`, not as a string literal. Upgraded to **test-only** (conservative) since the production path only runs in tests.

### 2c. Dead candidates — 50 fields

Zero references outside `core/config.py` in both Python (non-test) and Swift.  
**Risk of false negatives is high** — some may be read via `getattr(settings, "FIELD", default)` with a computed field name, or through env-var `KRAB_EAR_*` checking. Verify before removing.

#### Group A — Confirmed zero references (14 fields)

These were checked via multiple grep strategies (attribute access, string literals, env-var names) and found no hits:

| Field | Category |
|-------|----------|
| `AI_MODEL` | STT model name (appears unused after voice gateway refactor) |
| `BULK_REPROCESS_BATCH_SIZE` | `BulkReprocessor` uses hardcoded/constructor default, not settings |
| `CALENDAR_LINK_ENABLED` | `CalendarLinker` is instantiated unconditionally in `service.py:544`; ENABLED flag not checked |
| `HOLD_MIN_DURATION_MS` | Push-to-talk hold threshold — Swift reads `hotkey_mode` not this |
| `LLM_FALLBACK_CHAIN` | LLM rewriter uses `DEFAULT_SETTINGS["rewriter_fallback_chain"]` instead |
| `PRESET_QUICK_SWITCH_HOTKEY` | Only in comment in Swift; actual hotkey handled natively |
| `QUICK_EDIT_BEFORE_PASTE_ENABLED` | Settings class has this; DEFAULT_SETTINGS has `quick_edit_enabled` instead |
| `RECAP_BACKEND` | `RecapScheduler`/`EmailSender` instantiated with constructor args, not settings field |
| `STT_CODE_SWITCHING_DETECT` | `CodeSwitchingDetector` called directly; no read of this flag found |
| `STT_CODE_SWITCHING_THRESHOLD` | Same — threshold passed hardcoded |
| `STT_EN_PRIMARY_MODEL` | `stt_router_factory.py` reads only `stt_ru_primary_model` for language routing |
| `STT_ES_PRIMARY_MODEL` | Same |
| `STT_GIGAAM_VENV_PYTHON` | GigaAM worker path — not read via Settings (read via `settings.get()` dict or env) |
| `TELNYX_CONNECTION_ID` | Telnyx adapter does not use this field |
| `WAKE_WORD_ENABLED` | `OpenWakeWordAdapter` doesn't check this flag; engine reads from `wake_word_engine` |
| `WAKE_WORD_ENGINE` | OWW adapter ignores it; Swift reads `"wake_word_enabled"` from UserDefaults |

#### Group B — Possibly live via indirect patterns (34 fields)

These have zero grep hits but the pattern may be accessed indirectly via:
- `getattr(settings, "FIELD", default)` with computed names
- Env-var `KRAB_EAR_FIELD` only consumed at startup from OS environment
- Config presets / settings validator (reads all keys by name)

| Field | Likely reason for zero hits |
|-------|----------------------------|
| `MAX_DURATION_SEC` | Referenced as `max_duration_sec` param in pipeline adapters (not settings attribute) |
| `MLX_CRASH_RECOVERY_ENABLED` | `engine.py` uses `getattr(settings, "MLX_CRASH_RECOVERY_ENABLED", True)` — getattr not caught |
| `MLX_TRANSCRIBE_TIMEOUT_SEC` | Same — `getattr(settings, "MLX_TRANSCRIBE_TIMEOUT_SEC", 60.0)` |
| `PORCUPINE_ACCESS_KEY` | Read from `~/.krab_ear_data/porcupine_access_key` file; env var documented in Swift comment |
| `SMTP_HOST` | `EmailSender` constructor params (not settings direct read at instantiation) |
| `SMTP_PASSWORD` | Same — intentionally not in Settings (stored in Keychain) |
| `SMTP_PORT` | Same |
| `SMTP_USER` | Same |
| `SMTP_USE_SSL` | `email_sender.py:324` uses `getattr(cfg, "SMTP_USE_SSL", False)` — missed |
| `SMTP_USE_TLS` | Same pattern |
| `STT_GIGAAM_DEVICE` | `stt_router.py:401` uses `getattr(self._settings, "STT_GIGAAM_DEVICE", "mps")` |
| `STT_GIGAAM_MODE` | `stt_router.py:400` uses `getattr(self._settings, "STT_GIGAAM_MODE", "rnnt")` |
| `STT_GIGAAM_TRANSPORT` | `stt_router.py:402` uses `getattr(self._settings, "STT_GIGAAM_TRANSPORT", "auto")` |
| `STT_PARAKEET_ENABLED` | `stt_router_factory.py:64` uses `cfg.get("stt_parakeet_enabled", False)` |
| `STT_PARAKEET_MODEL` | `stt_router_factory.py:67` uses `cfg.get("stt_parakeet_model", None)` |
| `STT_PUNCTUATION_LLM_PASS_ENABLED` | `engine.py:455` uses `self._settings_get("stt_punctuation_llm_pass_enabled", False)` |
| `STT_ROUTING` | `stt_management_service.py` uses it via IPC handler; `stt_router.py` docs reference it |
| `STT_RU_PRIMARY_MODEL` | `stt_router_factory.py:114` uses `cfg.get("stt_ru_primary_model", ...)` |
| `STT_SENSEVOICE_DEVICE` | `stt_router_factory.py:92` uses `cfg.get(...)` |
| `STT_SENSEVOICE_ENABLED` | `stt_router_factory.py:88` uses `cfg.get(...)` |
| `STT_SENSEVOICE_MODEL` | `stt_router_factory.py:91` uses `cfg.get(...)` |
| `STT_VAD_PREFILTER_ENABLED` | Confirmed live — `engine.py` reads via `settings.STT_VAD_PREFILTER_ENABLED` (missed by grep) |
| `STT_VAD_SILENCE_TRIM_THRESHOLD_SEC` | Same |
| `STT_MIN_CONFIDENCE_THRESHOLD` | `engine.py` reads via settings |
| `STT_MULTIPASS_ENABLED` | `engine.py` reads via settings |
| `STT_SPEAKER_AWARE_PROMPT_ENABLED` | Reads via `settings.STT_SPEAKER_AWARE_PROMPT_ENABLED` |
| `STT_DIALOGUE_HINT_THRESHOLD` | Same |
| `STT_USE_RU_FINETUNE` | `stt_router.py` references it |
| `STT_RU_FINETUNE_MODEL` | Same |
| `VOXTRAL_ENABLED` | `voxtral_adapter.py` referenced in pipeline |
| `VOXTRAL_MODEL` | Same |
| `VOXTRAL_REASONING_ENABLED` | Same |
| `SENSEVOICE_ENABLED` | Different name from `STT_SENSEVOICE_ENABLED` — legacy field, possibly aliased |
| `SENSEVOICE_MODEL` | Same |
| `SENSEVOICE_EMOTION_TO_HISTORY` | Used in `backend/` somewhere via settings.get |

---

## 3. `DEFAULT_SETTINGS` Keys — Classification

### 3a. Live (used outside `config.py`) — 93 keys

All heavily-used runtime settings. Notably, 24 keys are Swift-only (read by the agent).

### 3b. Test-only — 5 keys

| Key | py_tests |
|-----|----------|
| `auto_dedup_enabled` | 2 |
| `auto_dedup_threshold` | 2 |
| `voice_fingerprint_enabled` | 1 |
| `smtp_host` | 1 |
| `smtp_user` | 1 |

### 3c. Dead candidates — 58 keys

These keys exist in `DEFAULT_SETTINGS` but have no string-literal references outside `config.py` in either Python or Swift. The service layer reads them via `cached_settings.get("key", DEFAULT_SETTINGS.get("key", default))` — the zero hits suggest the runtime settings path bypasses DEFAULT_SETTINGS entirely for these keys, relying on the `Settings` Pydantic object instead.

Notable groups:

**STT feature flags (likely live but read via `Settings` object, not `DEFAULT_SETTINGS` dict):**
- `stt_vad_prefilter_enabled`, `stt_vad_silence_trim_threshold_sec`
- `stt_denoise_enabled`, `stt_denoise_snr_threshold_db`, `stt_denoise_strength`
- `stt_multipass_enabled`, `stt_min_confidence_threshold`, `stt_max_retries`
- `stt_audio_lang_id_enabled`, `stt_audio_lang_id_preview_sec`
- `stt_speaker_aware_prompt_enabled`, `stt_dialogue_hint_threshold`
- `stt_use_ru_finetune`, `stt_ru_finetune_model`
- `stt_code_switching_detect`, `stt_code_switching_threshold`
- `stt_streaming_enabled`, `stt_streaming_min_audio_sec`, `stt_streaming_chunk_sec`, `stt_streaming_overlap_sec`
- `stt_routing`

**VA / conversation (placeholder settings for Phase 1):**
- `wake_word_enabled`, `wake_word_engine`
- `conversation_engine`, `conversation_brain`

**Voxtral (adapter settings, read via `cfg.get()` pattern not caught):**
- `voxtral_enabled`, `voxtral_model`, `voxtral_reasoning_enabled`

**Email/SMTP (read via constructor args, not DEFAULT_SETTINGS):**
- `smtp_port`, `smtp_use_tls`, `smtp_use_ssl`, `recap_email_enabled`, `recap_backend`

**Routing models (read via `cfg.get()` not DEFAULT_SETTINGS lookup):**
- `stt_en_primary_model`, `stt_es_primary_model`, `stt_other_primary_model`

**Misc opt-in features:**
- `auto_dedup_enabled`, `auto_dedup_threshold` (test-only)
- `smart_silence_skip_enabled`
- `bulk_reprocess_batch_size` (read from constructor, not settings)
- `export_include_speaker_labels`
- `voice_fingerprint_enabled`, `voice_fingerprint_match_threshold`
- `mlx_crash_recovery_enabled`, `mlx_transcribe_timeout_sec`
- `calendar_link_enabled`
- `preset_quick_switch_hotkey`
- `disk_monitor_enabled`, `disk_check_interval_min`, `disk_warning_gb`, `disk_critical_gb`, `history_large_mb`, `auto_cleanup_enabled`, `auto_cleanup_after_days`
- `inline_translation_target`
- `rest_api_auth_enabled`
- `semantic_search_enabled`, `semantic_search_model`, `semantic_search_auto_index`
- `stt_hotwords_enabled` (hotwords themselves are live, but the enable flag is checked elsewhere)

---

## 4. Naming Inconsistencies

Two settings exist under **different names** in `Settings` vs `DEFAULT_SETTINGS`:

| `Settings` field | `DEFAULT_SETTINGS` key | Mismatch type |
|-----------------|------------------------|---------------|
| `QUICK_EDIT_BEFORE_PASTE_ENABLED` | `quick_edit_enabled` | Suffix mismatch (`_before_paste` vs none) |
| `SENSEVOICE_ENABLED` | `stt_sensevoice_enabled` | Prefix mismatch (`STT_` prefix missing) |
| `SENSEVOICE_MODEL` | `stt_sensevoice_model` | Same |
| `SENSEVOICE_EMOTION_TO_HISTORY` | — (not in DEFAULT_SETTINGS) | Not bridged |
| `PARAKEET_ENABLED` | — (not in DEFAULT_SETTINGS) | Not bridged (uses `STT_PARAKEET_ENABLED` in routing) |
| `PARAKEET_MODEL` | — | Not bridged |
| `WHISPERX_*` fields (5) | — | No DEFAULT_SETTINGS entries |

The `SENSEVOICE_*` / `STT_SENSEVOICE_*` split is a naming inconsistency: the `Settings` class has two sets of SenseVoice fields — `SENSEVOICE_ENABLED/MODEL/EMOTION_TO_HISTORY` (from the Phase 4 quick-win, referring to the pipeline-based adapter) and `STT_SENSEVOICE_ENABLED/MODEL/DEVICE` (from the later stt_router_factory integration). The service layer only reads the `STT_SENSEVOICE_*` variants via `cfg.get()`.

---

## 5. Key Findings & Recommendations

### HIGH — Fix now (low-risk, clear bugs)

1. **Remove 12 duplicate `Settings` field declarations.** Lines 72–86 (disk monitor group, first copy) and lines 203–206 (silence filter group, first copy) and line 557 (`PASTE_APP_MEMORY_ENABLED` first copy) are redundant. Remove first copies, keep the better-commented second copies.

2. **Remove 2 duplicate `DEFAULT_SETTINGS` entries.** `"llm_model"` appears twice (lines 996 and 1001). `"paste_app_memory_enabled"` appears twice (~line 921 and ~970). Keep one entry each.

3. **Rename `QUICK_EDIT_BEFORE_PASTE_ENABLED` → `QUICK_EDIT_ENABLED`** to match the `DEFAULT_SETTINGS` key `quick_edit_enabled`. Currently the Settings field and the DEFAULT_SETTINGS key have different names, meaning the runtime bridge in `_build_settings()` will silently ignore the `quick_edit_enabled` JSON key when the user sets it via IPC.

### MEDIUM — Investigate before acting

4. **`CALENDAR_LINK_ENABLED`:** `CalendarLinker` is instantiated unconditionally in `service.py:544` using `settings.CALENDAR_LINK_CACHE_MIN` but the `CALENDAR_LINK_ENABLED` gate is never checked. Either wire the gate or remove it.

5. **`AI_MODEL` and `GATEWAY_URL`:** `GATEWAY_URL` has 121 grep hits but all are for `voice_gateway_url` (different key). `AI_MODEL` has zero hits anywhere. Both may be voice-gateway-related stale fields from an earlier API design.

6. **`LLM_FALLBACK_CHAIN` (Settings):** The runtime LLM rewriter reads `rewriter_fallback_chain` from `DEFAULT_SETTINGS`, not `LLM_FALLBACK_CHAIN` from the Pydantic model. These are two separate fields with identical semantics but different names and different defaults. Unify.

7. **`SENSEVOICE_*` vs `STT_SENSEVOICE_*`:** The old `SENSEVOICE_*` fields in `Settings` class appear to be unused (the router reads `STT_SENSEVOICE_*`). Consider removing the legacy group after confirming no test coverage.

### LOW — Cleanup backlog

8. **`BULK_REPROCESS_BATCH_SIZE`:** Only used in `BulkReprocessor.__init__(batch_size=5)` hardcoded, not from settings. Wire it or remove from config.

9. **`HOLD_MIN_DURATION_MS`:** Documented in Swift comments but not read via IPC. If push-to-hold is planned, wire it.

10. **`TELNYX_CONNECTION_ID`:** Present in Settings but not used by `TelnyxAdapter`. Stale from an earlier Telnyx SIP design.

---

## 6. Grep Coverage Gaps

The initial automated grep missed several live usages due to these patterns:

| Pattern | Example | Fields missed |
|---------|---------|---------------|
| `getattr(obj, "FIELD", default)` | `engine.py:1886` | `MLX_CRASH_RECOVERY_ENABLED`, `MLX_TRANSCRIBE_TIMEOUT_SEC`, `STT_GIGAAM_*` |
| `settings.get("field", default)` | `recording_core_service.py:154` | `LLM_BRAIN_*`, `ACTION_ITEMS_*`, `RT_PARTIAL_*`, `REALTIME_*` |
| `cfg.get("field", default)` | `stt_router_factory.py:52` | `STT_PARAKEET_*`, `STT_SENSEVOICE_*`, `STT_GIGAAM_*` |
| `self._settings_get("field", default)` | `engine.py:455` | `STT_PUNCTUATION_LLM_PASS_ENABLED` |

**Conclusion:** The true dead-candidate count is lower than the 50/58 numbers above. Any automated removal pass must add `getattr` and `settings.get` pattern searches to avoid false-positive dead declarations.

---

*Generated by wave778 config audit. Branch: `feature/config-audit-W778`.*
