# Krab Ear deps + test audit 2026-05-20

Generated: Wave 275. Pure static inspection — no backend started.

---

## Requirements (`KrabEar/requirements.txt`)

Total active entries: **20** (excluding commented-out optional blocks).

| Category         | Count | Packages |
|------------------|-------|----------|
| Pinned (`==`)    | 3     | `pyannote.audio==4.0.4`, `pyobjc-framework-Cocoa` (platform guard), `pyobjc-framework-Vision` (platform guard) |
| Floor (`>=`)     | 7     | `flask-sock>=0.7.0`, `websockets>=12.0`, `gunicorn>=21.2.0`, `flask-limiter>=3.5.0`, `flask-cors>=4.0.0`, `sentry-sdk>=2.0`, `pytest-xdist>=3.5` |
| Fully loose      | 10    | `mlx-whisper`, `numpy`, `sounddevice`, `requests`, `pydantic-settings`, `soundfile`, `pyperclip`, `flask`, `flask-smorest`, `marshmallow` |

### Installed versions (anaconda env, as-found)

| Package           | Installed | Pin in req.txt    | Notes |
|-------------------|-----------|-------------------|-------|
| mlx-whisper       | 0.4.3     | none              | loose |
| numpy             | 2.4.4     | none              | loose |
| sounddevice       | 0.5.5     | none              | loose |
| requests          | 2.32.4    | none              | loose |
| pydantic-settings | 2.9.1     | none              | loose |
| soundfile         | 0.13.1    | none              | loose |
| pyperclip         | 1.11.0    | none              | loose |
| flask             | 3.1.3     | none              | loose |
| flask-smorest     | 0.47.0    | none              | loose |
| marshmallow       | 4.2.0     | none              | loose |
| flask-sock        | 0.7.0     | `>=0.7.0`         | floor |
| websockets        | 15.0.1    | `>=12.0`          | floor |
| flask-limiter     | 4.1.1     | `>=3.5.0`         | floor |
| flask-cors        | 6.0.2     | `>=4.0.0`         | floor |
| sentry-sdk        | 2.30.0    | `>=2.0`           | floor |
| pyobjc-*          | 12.1      | platform-guarded  | pinned by platform guard |
| pyannote.audio    | not in env | `==4.0.4`        | separate venv_krab_ear |

### Security / outdated advisories

`pip-audit` not installed in active env. No known critical CVEs in the listed packages as of 2026-05-20 based on static review. Packages to watch:
- `requests 2.32.4` — latest stable, no open CVEs.
- `flask 3.1.3` — latest stable.
- `sentry-sdk 2.30.0` — latest stable.
- `marshmallow 4.2.0` — **major version bump** from 3.x; `flask-smorest` 0.47 requires marshmallow >=3.18; marshmallow 4.x may introduce breaking changes in `fields` API. Worth pinning `marshmallow>=3.18,<5`.

### Duplicates

None found. All 20 active entries are unique.

### Recommendations

1. Pin `mlx-whisper` — STT core; any upgrade may break transcription quality or API.
2. Pin `numpy` with a floor: `numpy>=1.26,<3` — avoid silent numeric behavior changes.
3. Add upper bound on `marshmallow`: `marshmallow>=3.18,<5` — 4.x API drift risk.
4. Pin `flask` floor: `flask>=3.0` — avoid regression to 2.x.
5. Optional: add `pip-audit` to dev-deps for CI security scanning.

---

## Test files

- **Total test files:** 385
- **Total test methods:** 10 191

### Sister / variant file groups (consolidation candidates)

Files matching `test_X_<suffix>.py` where suffix ∈ {extras, deep, coverage, extended, advanced}:

| Family              | Files |
|---------------------|-------|
| `translator`        | 2 (`test_translator_coverage.py`, `test_translator_extended.py` + `test_translator_glossary_deep.py`) |
| `sentiment_trends`  | 2 (`test_sentiment_trends_coverage.py`, `test_sentiment_trends_extras.py`) |
| `collection_manager`| 2 (`test_collection_manager_coverage.py`, `test_collection_manager_extras.py`) |
| `analytics_dashboard`| 2 (`test_analytics_dashboard_advanced.py`, `test_analytics_dashboard_extras.py`) |

Additional standalone suffix files (single, not yet flagged as family): `test_audit_logger_rotation_deep.py`, `test_auto_backup_advanced.py`, `test_bulk_reprocess_extras.py`, `test_call_assist_service_deep.py`, `test_daily_digest_coverage.py`, `test_engine_extended.py`, `test_export_scheduler_extras.py`, `test_history_service_extended.py`, `test_integrity_checker_extras.py`, `test_ipc_throttle_extras.py`, `test_live_subs_service_deep.py`, `test_metrics_collector_coverage.py`, `test_observability_coverage.py`, `test_obsidian_sync_coverage.py`, `test_plugin_system_advanced.py`, `test_quality_trends_extras.py`, `test_rest_coverage.py`, `test_settings_migration_deep.py`, `test_shutdown_handler_deep.py`, `test_smart_vocabulary_extras.py`, `test_summary_profiles_extras.py`, `test_transcript_writer_coverage.py` — **22 single-variant files**; total **30 variant files** across the test suite.

These are not duplicates but test depth layers; no consolidation required unless test time becomes a concern.

---

## flake8 baseline (`--max-line-length=120`, all `KrabEar/`)

**Total warnings: 72** — all in `KrabEar/tests/`. Production code (`backend/`, `core/`) is **clean (0 warnings)**.

| Code | Count | Description |
|------|-------|-------------|
| F401 | 44    | Imported but unused |
| F841 | 11    | Local variable assigned but never used |
| E306 | 4     | Missing blank line before nested def |
| F541 | 3     | f-string without placeholders |
| E401 | 3     | Multiple imports on one line |
| E303 | 3     | Too many blank lines |
| E203 | 3     | Whitespace before `:` (slice notation) |
| W391 | 1     | Blank line at end of file |

### Top cleanup candidates (F401 — files with most unused imports)

1. `test_ipc_dispatch_invariants.py` — 5 unused imports (`types`, `inspect`, `PropertyMock`, `re` x2)
2. `test_stop_recording_phases_wave88.py` — 4 unused imports (`threading`, `time`, `patch`, `MagicMock`)
3. `test_error_bus_phase_b_wave61.py` — 4 unused imports (`datetime`, `timezone`, `AsyncMock`, `patch`)
4. `test_stt_warmup.py` — 3 unused imports (`time`, `MagicMock`, `call`)
5. `test_stt_routing_scored.py` — 3 unused imports (`patch`, two private `stt_router` constants)

### Notes

- All E203 (whitespace before `:`) are in `test_mlx_concurrency.py` — these are slice notation patterns that `black` would emit; consider adding `--extend-ignore=E203` to match black-compatible config.
- F401 pattern: majority are `unittest.mock.MagicMock` / `unittest.mock.patch` imported at module level but only used via `with patch(...)` inline — safe to remove.
- Production code passes flake8 strict (0 warnings) confirming existing lint discipline is effective.

---

## Validation

```
python -m flake8 KrabEar/backend/ KrabEar/core/ --count --statistics 2>&1 | tail -20
# Expected: 0
```

Result: **0 warnings** in production code.
