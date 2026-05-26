# Wave 928 Audit: `settings_validator.py`

**Date:** 2026-05-26  
**Scope:** `KrabEar/backend/settings_validator.py` (306 lines) + `KrabEar/tests/test_settings_validator.py`  
**Method:** Static read + runtime probing via venv

---

## Summary

`SettingsValidator` is well-structured and handles its defined scope correctly. The critical finding is a significant and widening **coverage gap**: the validator explicitly covers only a small fraction of the settings dict fields that exist in `DEFAULT_SETTINGS`. This gap is by design for some fields, but creates real risks for enum fields where typos silently pass through.

---

## Findings

### F1 — Migration idempotency: SAFE with one caveat

Running `migrate(result, "1.0", "2.0")` a second time on already-migrated output is safe:
- `add_default` ops skip keys already present — correct.
- `rename` ops guard with `if old_key in result and new_key not in result` — correct.
- Result: double-migration produces identical output.

**Caveat:** `migrate()` does not write a `schema_version` field into the output dict. After migration, the caller receives a dict that is indistinguishable from an original v2.0 dict. If the caller ever persists the version for future migration decisions, they must track it themselves. Currently, `settings_service.py` never calls `migrate()` at all (only `validate()`), so this is latent.

---

### F2 — No version detection: migration is caller-driven, never auto-invoked (MEDIUM risk)

`SettingsValidator.migrate()` requires the caller to supply `from_version` and `to_version` explicitly. There is no logic in the codebase that reads `schema_version` from `settings.json` and auto-invokes migration.

Searching `settings_service.py`, `service.py`, and `main.py` finds zero callsites of `SettingsValidator.migrate()` in production paths — only in tests. `data_migrator.py` has a separate `get_schema_version()` / `migrate()` pair that operates on NDJSON history files, not settings.

**Effect:** Users upgrading from v1.0 who have `history_limit` in their settings.json will never get it renamed to `history_policy`. The `validate()` path will silently preserve `history_limit` as an unknown key (passes through) while the canonical field `history_policy` will be absent. `settings_service.py` applies its own inline normalization for `history_policy` but falls back to `DEFAULT_SETTINGS` — so the user's value is ignored, not migrated.

**Recommendation:** Either call `migrate()` on load in `SettingsService.cached_settings()` when version is detected as older, or document that migration is manual only.

---

### F3 — Enum allowlist covers 13/28 string-type settings (MEDIUM risk)

`_ENUM_FIELDS` validates 13 string settings. `DEFAULT_SETTINGS` contains at least 15 additional string settings with a restricted set of valid values that are not covered:

| Field | Valid values | Covered? |
|---|---|---|
| `stt_denoise_strength` | `off/light/moderate/strong` | No |
| `wake_word_engine` | `openwakeword/porcupine/disabled` | No |
| `conversation_engine` | `auto/moshi/seamless` | No |
| `conversation_brain` | `auto/qwen3-30b/qwen3-4b` | No |
| `stt_gigaam_mode` | `rnnt/ctc` | No |
| `stt_gigaam_device` | `mps/cpu/auto` | No |
| `stt_routing` | `auto_scored/legacy_order` | No |
| `recap_backend` | `smtp/mail_app` | No |
| `hotkey_mode` | `toggle/push_to_talk` | No |

**Effect (verified by runtime probe):** `validate({'wake_word_engine': 'typo_engine'})` passes without any warning — the validator returns the typo as-is. A user writing `'openwakeword'` wrong (e.g. `'open_wake_word'`) silently gets the wrong engine loaded at runtime, with no feedback.

---

### F4 — Type coercion for booleans: lenient (acceptable)

`_coerce_bool()` accepts `"true"/"false"`, `"on"/"off"`, `"yes"/"no"`, `1/0`. This is deliberate and documented. Behavior:
- `"true"` → `True`: coerced, no warning.
- `None` → falls back to field default.
- `"maybe"` → falls back to default with a warning.

The coercion covers all common serialization formats (JSON round-trips, env var strings). **No issue** here.

**Uncovered boolean fields:** `_BOOL_FIELDS` covers 20 of 63 boolean fields in `DEFAULT_SETTINGS`. The remaining 43 unregistered bool fields (e.g. `stt_denoise_enabled`, `mlx_crash_recovery_enabled`, `voxtral_enabled`) pass through `validate()` without type normalization. If any of these arrive as the string `"false"` from the IPC layer, they will be treated as truthy by Python. `settings_service.py` applies `bool()` inline for some of these, providing a second safety layer, but not for all 43.

---

### F5 — Unknown keys: silently passed through, no warning (design choice, minor risk)

`validate()` passes unknown keys through unchanged (`fixed = dict(settings)`, then only named fields are touched). Tests confirm this is intentional (`test_unknown_keys_not_in_warnings`).

**Forward-compatibility benefit:** new settings added in code can be written by the app before the validator is updated.

**Typo risk:** `{'qulity_profile': 'max'}` passes silently — the user's intent is lost. Given the IPC layer is Swift-generated and not user-typed, practical typo risk is low. Still, a `DEBUG`-level warning for keys not in any known set would help during development.

---

### F6 — Range validation covers 13/46 numeric fields (LOW risk)

`_RANGE_FIELDS` validates 13 of 46 numeric settings. The 33 uncovered include operationally important ones:

| Field | Default | Risk of out-of-range |
|---|---|---|
| `mlx_transcribe_timeout_sec` | 120.0 | Setting to 0 would disable the watchdog |
| `stt_min_confidence_threshold` | 0.65 | Should be in [0.0, 1.0] |
| `auto_dedup_threshold` | 0.9 | Should be in [0.0, 1.0] |
| `smtp_port` | 587 | Should be in [1, 65535] |
| `recap_time_hour` | 20 | Should be in [0, 23] |
| `rt_partial_interval_sec` | 3.0 | Setting < 0 would crash |

`settings_service.py` applies inline `_coerce_bounded()` for the 13 fields that `_RANGE_FIELDS` also covers — there is no duplication gap, but no additional coverage either. The remaining 33 pass through unchecked.

---

### F7 — Partial migration failure leaves settings in intermediate state (LOW risk, by construction)

`_apply_migration_ops()` iterates ops sequentially on a working copy. Each op is atomic (dict insert/pop). If `migrate()` raises `ValueError` (unknown step in chain), it raises before `result` is returned — the caller's input is never mutated.

However, if an individual op silently fails (e.g. `rename` where `old_key` is absent — guards exist for this), the migration continues. There is no rollback and no "partially migrated" flag. If migration is called on a partially-migrated dict, subsequent `add_default` ops will be no-ops (correct), so the result is safe in practice.

---

## Test Coverage

| Area | Tests | Assessment |
|---|---|---|
| Enum validation + auto-fix | 7 | Good |
| Range clamping + type coercion | 10 | Good |
| Bool coercion (true/false/1/0) | 8 | Good |
| Special fields (glossary, templates, gateway URL) | 7 | Good |
| Migration 1.0 → 2.0 | 7 | Good |
| Unknown key passthrough | 2 | Present |
| Thread safety | 2 | Present |
| Uncovered enum fields (F3) | 0 | Missing |
| Bool fields not in _BOOL_FIELDS (F4) | 0 | Missing |
| Range fields not in _RANGE_FIELDS (F6) | 0 | Missing |
| Auto-migration on load (F2) | 0 | Missing (no production path exists) |

Total test count in file: ~55 test methods across 9 test classes.

---

## Recommendations (priority order)

1. **(F2, HIGH)** Wire `migrate()` into `SettingsService.cached_settings()`: detect absence of `schema_version` field or presence of known v1.0 sentinel keys (`history_limit`), auto-migrate, and persist the result. OR document explicitly that migration is manual-only.

2. **(F3, MEDIUM)** Add `wake_word_engine`, `stt_denoise_strength`, `conversation_engine`, `hotkey_mode` to `_ENUM_FIELDS`. These have finite, known value sets used in production code paths.

3. **(F4, LOW)** Add the 20 most critical bool fields (STT enablement flags, privacy flags) to `_BOOL_FIELDS`. Lower priority because `settings_service.py` provides a partial second pass.

4. **(F6, LOW)** Add `mlx_transcribe_timeout_sec` (min: 5), `stt_min_confidence_threshold` [0.0, 1.0], `recap_time_hour` [0, 23] to `_RANGE_FIELDS`.

5. **(F5, LOW)** Add DEBUG-level logging for unknown keys during validate() — not a warning to avoid spam in prod, but useful during development.
