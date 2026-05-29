# W1658 Body-Revert Regression Scan — 2026-05-30

Scope: test files whose fix waves fall in the W900–W1500 cherry-pick-train era.
Method: per-file `python3 -m unittest` (memory-safe; one process per file).
Branch: `docs/body-revert-scan-W1658` off `origin/codex/krab-ear-v2`.

---

## Results Table

| Test file | Status | First failing test | Likely cause |
|-----------|--------|-------------------|--------------|
| `test_transcript_context` | FAILED (2F, 1E) | `test_initial_prompt_capped_at_560_chars_cyrillic` | **BODY-REVERT** — 560-char cap logic removed (len=7649 not ≤ 560) |
| `test_auto_glossary` | OK | — | clean |
| `test_text_anonymizer` | OK | — | clean |
| `test_audio_quality` | OK | — | clean |
| `test_audio_denoiser` | OK | — | clean |
| `test_punctuation_fixer` | OK | — | clean |
| `test_voice_commands` | OK | — | clean |
| `test_silence_detector` | OK | — | clean |
| `test_translator` | OK | — | clean |
| `test_settings_service` | OK | — | clean |
| `test_history_service` | OK | — | clean |
| `test_archive_manager` | OK | — | clean |
| `test_recording_chain` | OK | — | clean |
| `test_sharing_manager` | FAILED (2F) | `test_link_no_expiration_24h` | **BODY-REVERT** — `expires_at` present; W98 mandated no-TTL (package must be permanent) |
| `test_topic_tracker` | FAILED (1F) | `test_get_topic_timeline_empty_in_privacy_mode` | **BODY-REVERT** — handler returns `segments` key, not `timeline` key |
| `test_emotion_detector` | OK | — | clean |
| `test_datetime_normalizer` | FAILED (21F) | `test_es_date_primero_de_enero` | **BODY-REVERT** — separator reverted from `.` to `-`; `01.01` expected, got `01-01` |
| `test_number_normalizer` | OK | — | clean |
| `test_audio_lang_id` | OK | — | clean |
| `test_semantic_search` | OK | — | clean |
| `test_audio_quality_nan_W1017` | OK | — | clean |
| `test_audio_quality_silence_threshold_W1107` | OK | — | clean |
| `test_auto_glossary_filler_W1541` | OK | — | clean |
| `test_auto_glossary_invalidate_W1292` | FAILED (1F, 5E) | `TypeError: HistoryService.__init__() got unexpected kwarg 'auto_glossary_builder'` | **BODY-REVERT** — `HistoryService.__init__` lost `auto_glossary_builder` parameter (W1292 addition) |
| `test_auto_glossary_ipc_W1104` | FAILED (6F) | `unknown_method: get_auto_glossary` | **BODY-REVERT** — `get_auto_glossary` / `refresh_auto_glossary` IPC handlers missing from dispatch table |
| `test_auto_glossary_max_text_bytes_W1547` | OK | — | clean |
| `test_auto_glossary_privacy_W1570` | OK | — | clean |
| `test_auto_glossary_w1294` | FAILED (4F) | `test_privacy_mode_returns_empty_list` | **BODY-REVERT** — `AutoGlossary.build()` ignores `privacy_mode_enabled`; returns terms instead of `[]` |
| `test_audio_lang_id_allowlist_W1121` | OK | — | clean |
| `test_audio_lang_id_cache_evict_W1271` | FAILED (2F) | `test_service_registers_lang_id_hook` | **BODY-REVERT** — `register_after_save_hook(_on_settings_saved_lang_id)` absent from service.py `__init__` |
| `test_audio_lang_id_cache_limit` | OK | — | clean |
| `test_audio_lang_id_double_clear_W1465` | FAILED (1F, 2E) | `test_detect_with_mlx_has_finally_with_clear_cache` | **BODY-REVERT** — `_detect_with_mlx` lost `finally: mx.clear_cache()` block (W1367 addition) |
| `test_audio_lang_id_lock_clear_W1466` | OK | — | clean |
| `test_audio_lang_id_mx_clear_cache_W1416` | FAILED (4E) | `AttributeError: module 'core.audio_lang_id' has no attribute '_HAS_MLX'` | **BODY-REVERT** — `_HAS_MLX` module-level flag removed from `audio_lang_id.py` |
| `test_audio_lang_id_threadsafe_W1116` | FAILED (2F, 1E) | `test_rlock_class_attribute_exists` | **BODY-REVERT** — `AudioLanguageID._model_cache_lock` class attr missing |
| `test_translator_cache_W1149` | OK | — | clean |
| `test_translator_cache_lock_W1161` | FAILED (1F) | `test_cache_lock_is_not_reentrant_by_accident` | **BODY-REVERT** — `_cache_lock` reverted to `RLock` instead of plain `Lock` |
| `test_translator_clear_cache_W1319` | OK | — | clean |
| `test_translator_dup_clear_cache_W1498` | OK | — | clean |
| `test_translator_settings_getter_W1492` | OK | — | clean |
| `test_translator_glossary_W935` | OK | — | clean |
| `test_translator_glossary_boundary_W1430` | OK | — | clean |
| `test_translator_no_regression_w1517_findings` | OK | — | clean |
| `test_archive_manager_flock_W1262` | OK | — | clean |
| `test_recording_chain_ghost_cascade_W1260` | FAILED (2F) | `test_delete_history_item_cascades_to_chains` | **BODY-REVERT** — deleting/archiving an item no longer removes it from its recording chains |
| `test_semantic_search_anonymize_W1152` | OK | — | clean |
| `test_semantic_search_delete_W1151` | FAILED (2F) | `test_delete_invokes_remove_item` | **BODY-REVERT** — `delete_history_item` no longer calls `semantic_searcher.remove_item` |
| `test_semantic_search_delete_wiring_W1163` | FAILED (13E) | `TypeError: HistoryService.__init__() got unexpected kwarg 'semantic_searcher'` | **BODY-REVERT** — `HistoryService.__init__` lost `semantic_searcher` parameter (W1151/W1163 addition) |
| `test_semantic_search_remove` | OK | — | clean |
| `test_semantic_search_remove_alias_W1172` | FAILED (1F, 15E) | same `TypeError` as above | same root cause as W1163 |
| `test_sharing_manager_constant_time_W1246` | FAILED (1F, 4E) | `module 'backend.sharing_manager' has no attribute 'hmac'` | **BODY-REVERT** — `hmac` import removed / `hmac.compare_digest` usage removed from `sharing_manager.py` |
| `test_sharing_manager_ttl` | OK | — | clean |
| `test_sharing_manager_ttl_items_W1244` | FAILED (1F) | `test_ttl_hours_default_applied_when_missing` | **BODY-REVERT** — default TTL logic wrong: applies 24h instead of 1h default |
| `test_topic_tracker_dos_W1281` | OK | — | clean |
| `test_topic_tracker_tfidf_speedup_W1286` | FAILED (2F) | `test_set_conversion_present_in_source_ast` | **BODY-REVERT** — `_compute_tfidf` lost `set(...)` optimisation (O(n) membership check reverted to list) |
| `test_settings_service_hooks_W1308` | OK | — | clean |
| `test_datetime_normalizer_W1089` | FAILED (3F) | `test_pervogo_yanvarya_still_converted` | same root cause as `test_datetime_normalizer` — separator `.` reverted to `-` |
| `test_text_anonymizer_eu_phones_W1127` | FAILED (18F) | `test_uk_mobile_basic` | **BODY-REVERT** — EU/UK phone regex reverted; `+44...` matched as ПАСПОРТ not ТЕЛЕФОН |
| `test_text_anonymizer_inn_yul_W1128` | OK | — | clean |
| `test_voice_commands_w1256_ambiguous` | OK | — | clean |
| `test_voice_commands_w1257` | FAILED (3F) | `test_capitalize_next_at_end_logs_warning` | **BODY-REVERT** — `capitalize_next` at end-of-text no longer logs a warning |
| `test_history_service_edges` | OK | — | clean |
| `test_history_service_extended` | OK | — | clean |
| `test_history_service_semantic_W1431` | FAILED (6E) | `AttributeError: 'HistoryService' object has no attribute '_semantic_searcher'` | same root cause — `HistoryService.__init__` lost semantic_searcher parameter |

---

## Confirmed Body-Revert Regressions (Priority Order)

### P1 — CRITICAL (constructor signature reverted, many cascading test errors)

1. **`HistoryService.__init__` lost `semantic_searcher` kwarg** (W1151/W1163/W1172/W1431)
   - Files: `backend/history_service.py`
   - Evidence: `TypeError: HistoryService.__init__() got unexpected keyword argument 'semantic_searcher'`
   - 6 test files fail (test_semantic_search_delete_wiring_W1163, test_semantic_search_remove_alias_W1172, test_history_service_semantic_W1431, test_semantic_search_delete_W1151)
   - Fix: restore `semantic_searcher=None` parameter + call `self._semantic_searcher.remove_item(item_id)` in `delete_history_item`

2. **`HistoryService.__init__` lost `auto_glossary_builder` kwarg** (W1292)
   - Files: `backend/history_service.py`
   - Evidence: `TypeError: HistoryService.__init__() got unexpected keyword argument 'auto_glossary_builder'`
   - Fix: restore `auto_glossary_builder=None` + invalidation call after `add_history_item`

### P1 — CRITICAL (IPC handler missing from dispatch table)

3. **`get_auto_glossary` / `refresh_auto_glossary` missing from dispatch table** (W1104)
   - Files: `backend/ipc_dispatch.py` or `backend/service.py`
   - Evidence: `unknown_method: get_auto_glossary`
   - Fix: re-register both handlers in dispatch table

### P2 — HIGH (logic reverted, produces wrong output)

4. **`DateTimeNormalizer` date separator reverted `.` → `-`** (W1089)
   - Files: `core/datetime_normalizer.py`
   - Evidence: `'01.01' not found in '01-01'` — 21 tests fail in main + 3 in W1089 variant
   - Fix: restore `.` (dot) as RU/ES date separator

5. **`TextAnonymizer` EU/UK phone regex reverted** (W1127)
   - Files: `core/text_anonymizer.py`
   - Evidence: `+44 7911...` matched as `[ПАСПОРТ]` not `[ТЕЛЕФОН]` — 18 tests fail
   - Fix: restore EU/UK phone number patterns (`+44`, `+49`, `+33` etc.)

6. **`AutoGlossary.build()` ignores `privacy_mode_enabled`** (W1294)
   - Files: `core/auto_glossary.py`
   - Evidence: returns `['TensorFlow']` instead of `[]` when `privacy_mode_enabled=True`
   - Fix: restore early-return guard on `privacy_mode_enabled`

7. **`SharingManager` lost `hmac.compare_digest` for token lookup** (W1246)
   - Files: `backend/sharing_manager.py`
   - Evidence: `module 'backend.sharing_manager' has no attribute 'hmac'`
   - Fix: restore `import hmac` + use `hmac.compare_digest` in `get_shared` / `revoke_share`

8. **`SharingManager` default TTL is 24h instead of 1h** (W1244)
   - Files: `backend/sharing_manager.py`
   - Evidence: `expires_at` delta ~601199s ≈ 7 days instead of 1h
   - Note: related to W98 (no-TTL test says `expires_at` should not exist at all — conflicting specs between W98 and W1244. W1244 says default=1h, W98 says no TTL. Investigate which is the authoritative spec before restoring.)

9. **`SharingManager` now adds `expires_at` field (should be no-TTL)** (W98)
   - Files: `backend/sharing_manager.py`
   - Evidence: `assertNotIn("expires_at", d)` fails
   - Possibly same root cause as #8 — TTL feature was added then a different version reverted

10. **`recording_chain` cascade-on-delete broken** (W1260)
    - Files: `backend/recording_chain.py` and/or `backend/history_service.py`
    - Evidence: deleting item `item-to-delete` still present in chain `item_ids`; archiving `item-alpha` still in chain
    - Fix: restore cascade-remove from chains on `delete_history_item` and `archive_items`

11. **`TopicTracker` handler returns wrong response shape** (W1281-era)
    - Files: `backend/topic_tracker.py` or handler in service
    - Evidence: `get_topic_timeline` returns `{'segments':...}` not `{'timeline':...}` key in privacy mode
    - Fix: restore `timeline` key in privacy-mode early-return dict

12. **`TopicTracker._compute_tfidf` lost set-optimisation** (W1286)
    - Files: `core/topic_tracker.py`
    - Evidence: AST check fails — `set(...)` comprehension over `all_windows_tokens` not present
    - Fix: restore `all_tokens_set = set(...)` and use it for O(1) membership test

13. **`VoiceCommandProcessor` lost end-of-text warning log** (W1257)
    - Files: `core/voice_commands.py`
    - Evidence: `assertLogs("KrabEar.VoiceCommands")` finds no log when `capitalize_next` at text end
    - Fix: restore `logger.warning(...)` call in capitalize-next boundary condition

### P2 — HIGH (AudioLanguageID body reverts)

14. **`AudioLanguageID._model_cache_lock` class attribute missing** (W1116)
    - Files: `core/audio_lang_id.py`
    - Evidence: `hasattr(AudioLanguageID, "_model_cache_lock")` returns False
    - Fix: restore `_model_cache_lock = threading.RLock()` class-level attribute

15. **`AudioLanguageID._HAS_MLX` module flag missing** (W1416)
    - Files: `core/audio_lang_id.py`
    - Evidence: `AttributeError: module 'core.audio_lang_id' has no attribute '_HAS_MLX'`
    - Fix: restore `try: import mlx.core as mx; _HAS_MLX = True; except ImportError: _HAS_MLX = False`

16. **`AudioLanguageID._detect_with_mlx` lost `finally: mx.clear_cache()` block** (W1465)
    - Files: `core/audio_lang_id.py`
    - Evidence: AST check finds no `finally` with `clear_cache()` in `_detect_with_mlx`
    - Fix: restore `finally: mx.clear_cache()` under `mlx_lock()` context

17. **`service.py __init__` lost `register_after_save_hook(_on_settings_saved_lang_id)` call** (W1271)
    - Files: `KrabEar/backend/service.py`
    - Evidence: source string search for `register_after_save_hook(_on_settings_saved_lang_id)` fails
    - Fix: restore hook registration in `BackendService.__init__`

### P3 — MEDIUM

18. **`Translator._cache_lock` reverted to `RLock` instead of plain `Lock`** (W1161)
    - Files: `backend/translator.py`
    - Evidence: `_cache_lock` is reentrant (`RLock`) but spec requires non-reentrant `Lock`
    - Fix: change `self._cache_lock = threading.RLock()` → `threading.Lock()`

19. **`transcript_context.build_initial_prompt` lost 560-char cap** (W913/W1293)
    - Files: `core/transcript_context.py`
    - Evidence: len=7649, expected ≤560; truncation logger not called (already tracked in W1656 — repair in progress)

---

## Env-Limitation Failures (Not Body-Reverts, Skip in CI)

| Test file | Note |
|-----------|------|
| `test_audio_lang_id_cache_evict_W1271.TestSettingsHookEvictsOnModelBalancedChange` | Fails partly because `register_after_save_hook` missing — see #17 above. The `test_service_hook_references_clear_model_cache` test does a source-grep. Fixable once #17 restored. |
| `test_topic_tracker` (torchcodec warning) | `OSError: Could not load libtorchcodec_core4.dylib` is a warning only (not the cause of failure). The actual failure is the response-shape body-revert (#11). |
| All `test_semantic_search_delete_wiring_W1163` / `_remove_alias_W1172` ERRORs | Cascade from #1 (missing `semantic_searcher` kwarg). Fix #1 first, these will pass. |

---

## Summary

- **Test files scanned**: 65
- **Files with failures**: 20
- **Confirmed body-revert regressions**: 19 distinct issues across 10 source files
- **Already tracked (W1656)**: `transcript_context` 560-char cap (#19) — repair in flight
- **New findings (this scan)**: 18 regressions not previously documented

### Most impacted source files:

| Source file | Regressions |
|-------------|-------------|
| `backend/history_service.py` | 2 (semantic_searcher + auto_glossary_builder kwarg) |
| `core/audio_lang_id.py` | 3 (_model_cache_lock, _HAS_MLX, finally-clear_cache) |
| `core/datetime_normalizer.py` | 1 (separator . vs -) |
| `core/text_anonymizer.py` | 1 (EU phone patterns) |
| `core/auto_glossary.py` | 1 (privacy_mode guard) |
| `backend/sharing_manager.py` | 3 (hmac, TTL default, no-TTL) |
| `backend/recording_chain.py` / `history_service.py` | 1 (cascade on delete) |
| `core/topic_tracker.py` | 2 (timeline key, tfidf set-opt) |
| `core/voice_commands.py` | 1 (capitalize_next warning log) |
| `backend/translator.py` | 1 (RLock vs Lock) |
| `backend/service.py` | 2 (IPC dispatch + settings hook) |
