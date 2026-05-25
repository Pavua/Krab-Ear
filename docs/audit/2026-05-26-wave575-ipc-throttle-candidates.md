# Wave 575 — IPC Throttle Coverage Audit

**Date:** 2026-05-26  
**Scope:** `KrabEar/backend/ipc_throttle.py` + `service.py` handler table  
**Goal:** Identify expensive unthrottled IPC methods and propose rate limits.

---

## Current State

`IPCThrottle` uses a token-bucket per method with three pre-defined categories:

| Category | Limit | Burst (bucket capacity) | Applies to |
|----------|-------|------------------------|------------|
| `heavy`  | 5/min  | 5 tokens  | 17 methods (STT import, LLM summarize, exports, waveform…) |
| `medium` | 30/min | 30 tokens | 24 methods (search, stats, health, translate…) |
| `light`  | 120/min | 120 tokens | all others not in EXCLUDED_METHODS |

Methods in `EXCLUDED_METHODS` bypass throttle entirely (recording lifecycle, settings sliders, live-subs ingest).

---

## Gap: Expensive Methods Without Explicit Category

The five methods below fall through to `light` (120/min) despite being CPU/I/O-intensive.  
None appear in `HEAVY_METHODS`, `MEDIUM_METHODS`, or `EXCLUDED_METHODS`.

### Proposal Table

| Method | Proposed category | rps equiv. | Burst | Rationale | Error response |
|--------|-------------------|------------|-------|-----------|----------------|
| `semantic_search` | **heavy** | 0.08/s | 5 | Loads `multilingual-e5-base` sentence-transformer on first call; each query encodes the query vector + cosine-scans the entire embedding index (O(n) over history). Concurrent bursts cause memory pressure and can collide with MLX inference. | `{"ok": false, "error": "rate_limit", "retry_after_sec": <wait>}` |
| `semantic_search_reindex` | **heavy** | 0.08/s | 5 | Re-encodes every history item in batches; ~1 s/100 items on M4 Max. Should be rarer than regular search; already serialized via `_index_lock` but burst-firing it holds the lock for seconds at a time. | Same as above |
| `test_microphone` | **heavy** | 0.08/s | 3 | Blocks the calling thread for `duration_sec` (up to 5 s) via `sounddevice.rec()` + `sd.wait()`. Multiple concurrent callers each open an exclusive audio device handle. Burst = 3 to allow one retry after device conflict. | `{"ok": false, "error": "rate_limit", "retry_after_sec": <wait>}` |
| `get_keyword_cloud` | **medium** | 0.5/s | 10 | Full store scan (`_load_active_items_unlocked`) + tokenisation + frequency sort over the entire history. O(n·words) on history size. Currently light (120/min); 30/min is safe for UI refresh. | `{"ok": false, "error": "rate_limit", "retry_after_sec": <wait>}` |
| `get_sentiment_trends` | **medium** | 0.5/s | 10 | Full store scan + `EmotionDetector` per-item heuristics + linear-regression over daily buckets. O(n) with non-trivial constant; same profile as `word_frequency_analysis` (already medium). | `{"ok": false, "error": "rate_limit", "retry_after_sec": <wait>}` |

---

## Suggested Code Change (non-normative)

Add to `HEAVY_METHODS` in `ipc_throttle.py`:
```python
"semantic_search",          # sentence-transformer inference + O(n) cosine scan
"semantic_search_reindex",  # batch re-encode entire history
"test_microphone",          # blocking audio device capture (up to 5 s)
```

Add to `MEDIUM_METHODS` in `ipc_throttle.py`:
```python
"get_keyword_cloud",        # full store scan + tokenisation
"get_sentiment_trends",     # full store scan + per-item emotion heuristics
```

---

## Out of Scope

`generate_stats_report`, `compare_periods`, `get_recording_insights`, and `get_activity_calendar`  
are also full-store-scan operations but are analytics-panel calls with no known burst pattern.  
Recommend monitoring throttle stats (`get_throttle_stats`) for 1–2 weeks before classifying.

---

*Audit by Wave 575 agent. Implementation deferred to follow-up PR.*
