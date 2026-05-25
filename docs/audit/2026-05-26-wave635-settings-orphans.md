# Wave 635 — settings.json Orphan Audit

**Date:** 2026-05-26  
**Method:** `grep -rl` file-count across `KrabEar/` + `native/`, all `.py` and `.swift`.  
**Thresholds:** Orphan ≤1 file, Suspicious 2–4 files, Active ≥5 files.

---

## Orphans (≤1 file) — 48 keys

High-risk: defined in `DEFAULT_SETTINGS` but referenced only in the definition itself.

```
auto_cleanup_after_days       bulk_reprocess_batch_size
calendar_link_cache_min       calendar_link_enabled
conversation_brain            conversation_engine
disk_check_interval_min       disk_critical_gb
disk_monitor_enabled          disk_warning_gb
export_include_speaker_labels inline_translation_target
mlx_crash_recovery_enabled    mlx_transcribe_timeout_sec
paste_app_memory_enabled      preset_quick_switch_hotkey
recap_backend                 recap_email_enabled
rest_api_auth_enabled         semantic_search_auto_index
semantic_search_enabled       semantic_search_model
smart_silence_skip_enabled    stt_audio_lang_id_enabled
stt_audio_lang_id_preview_sec stt_code_switching_detect
stt_code_switching_threshold  stt_denoise_enabled
stt_denoise_snr_threshold_db  stt_denoise_strength
stt_dialogue_hint_threshold   stt_en_primary_model
stt_es_primary_model          stt_max_retries
stt_min_confidence_threshold  stt_multipass_enabled
stt_other_primary_model       stt_ru_finetune_model
stt_speaker_aware_prompt_enabled
stt_streaming_chunk_sec       stt_streaming_min_audio_sec
stt_streaming_overlap_sec     stt_use_ru_finetune
stt_vad_prefilter_enabled     stt_vad_silence_trim_threshold_sec
voice_fingerprint_match_threshold
wake_word_enabled             wake_word_engine
```

**Recommended action:** Verify each has a consumer (may be runtime-read via string key not matched by grep pattern). Priority top candidates: `mlx_crash_recovery_enabled`, `stt_denoise_*`, `stt_streaming_*`, `semantic_search_*` — all belong to well-developed subsystems and likely have consumers.

---

## Suspicious (2–4 files) — 28 keys

Present in feature modules but may lack Swift UI wiring.

Notable: `stt_routing`, `rewriter_fallback_chain`, `stt_language_routing_enabled`, `voxtral_*`, `stt_streaming_enabled`, `rt_partial_*`, `rt_silence_*`, `smtp_*`, `auto_dedup_*`, `bookmarks_hotkey_enabled`, `datetime_normalization_enabled`, `number_normalization_enabled`.

---

## Active (≥5 files) — 42 keys

Core settings with broad references. Top 10 by file count:

| Key | Files |
|-----|-------|
| `mode` | 200 |
| `quality_profile` | 94 |
| `translation_mode` | 86 |
| `cleanup_profile` | 60 |
| `network_mode` | 56 |
| `translation_style` | 48 |
| `translation_glossary` | 29 |
| `stt_hotwords` | 26 |
| `auto_paste` | 22 |
| `llm_rewrite_enabled` | 21 |

---

## Summary

| Category | Count |
|----------|-------|
| Orphan (≤1) | 48 |
| Suspicious (2–4) | 28 |
| Active (≥5) | 42 |
| **Total** | **118** |

**Note:** grep file-count undercounts dynamic reads like `settings.get(key)` with variable keys. Before removing any orphan, confirm via `_get_runtime_setting` / `cached_settings.get` call-sites.
