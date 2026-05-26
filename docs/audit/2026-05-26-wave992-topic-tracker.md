# Wave 992 Audit: `core/topic_tracker.py` — TopicTracker

**Date:** 2026-05-26  
**File:** `KrabEar/core/topic_tracker.py` (315 lines)  
**Auditor:** Sub-agent W992

---

## Summary

`TopicTracker` is a stateless, purely in-memory utility class that detects topic shifts across a list of transcription history items. It uses a keyword-based TF-IDF sliding-window algorithm with no external dependencies. The class is wired to one production IPC handler (`get_topic_timeline`) and is also used internally by `MetadataEnricher`.

---

## Finding 1 — Topic Detection Algorithm: Keyword TF-IDF, O(n²) latency profile

**Algorithm:** pure keyword-based with TF-IDF-like weighting. No embeddings, no LLM.

Steps:
1. Each item is tokenized (regex `[А-Яа-яA-Za-zÁÉÍÓÚáéíóúÑñÜü]{3,}`, stop-word filtered for RU/ES/EN combined).
2. A sliding window of `window_size` items is merged into one bag-of-words.
3. TF-IDF weights are computed per window; the IDF denominator scans **all** windows → O(n × vocab) per window → O(n²) total.
4. Adjacent windows whose keyword overlap (Jaccard) falls below `SHIFT_THRESHOLD` (0.30) trigger a segment boundary.

**Latency profile:** for the default `limit=100` items, the O(n²) TF-IDF is negligible at runtime (pure Python, small n). For very large corpora (thousands of items) the quadratic behaviour would become noticeable, but the handler hard-caps at `limit=100`.

**Risk (LOW):** algorithm is accurate enough for RU/ES topic shifts given the combined stop-word list. No external service call, so latency is deterministic.

---

## Finding 2 — State Persistence: None (stateless, no restart survival)

`TopicTracker` holds **zero instance state**. All three public methods (`track_topics`, `get_topic_timeline`, `get_current_topic`) are pure functions over their input lists. Topic history is not persisted anywhere; after a restart the backend re-computes it on demand from the existing NDJSON history store.

**Risk (LOW):** no data loss risk because the tracker is a view over persisted history items. The handler re-loads items from `StateStore` on every call, so it is always consistent with the live store.

---

## Finding 3 — Memory Bound: Unbounded class, capped only at handler level

The `TopicTracker` class itself has **no internal memory bound** — it accepts arbitrarily large item lists. The `_handle_get_topic_timeline` handler enforces `limit` (default 100, 0 = all) before passing items to the tracker. The `get_current_topic` path in `MetadataEnricher` passes a single-item list, so memory is trivially bounded there.

**Risk (MEDIUM):** callers that bypass the handler and call `TopicTracker` directly with an unbounded list (e.g., a future service extraction) could allocate large intermediate lists. Recommend adding a `max_items` guard inside `track_topics` or documenting the expectation clearly.

---

## Finding 4 — Concurrency: Thread-safe (stateless, no shared mutable state)

The class has no instance variables mutated after construction. All state is local to each method call. The concurrent test (`TestTopicTrackerConcurrent`) confirms 20 parallel calls on the same instance produce no errors and correct coverage. The `_handle_get_topic_timeline` handler takes a `StateStore` lock only to load items, then releases it before calling `TopicTracker` — correct pattern.

**Risk (NONE):** fully thread-safe as implemented.

---

## Finding 5 — Privacy Mode: No guard — topic analysis runs regardless

Neither `TopicTracker` nor `_handle_get_topic_timeline` checks for `privacy_mode`. When privacy mode is active, history items are still loaded from the store and their text is tokenized for topic extraction. This is a data-minimization gap: in privacy mode the backend should arguably return an empty or stub response rather than analyzing transcript content.

**Risk (MEDIUM):** transcript text is processed (tokenized, keyword-extracted) even when the user has enabled privacy mode. Compare with other analytics handlers that typically skip analysis and return `{"status": "privacy_mode"}` when `privacy_mode=True`.

**Recommendation:** add a privacy-mode check at the top of `_handle_get_topic_timeline`, consistent with other analytics handlers.

---

## Finding 6 — Cross-Language: Partial support via shared stop-word list

The regex `[А-Яа-яA-Za-zÁÉÍÓÚáéíóúÑñÜü]{3,}` covers Cyrillic, Latin, and Spanish diacritics in a single pass. The stop-word list (`_STOP_WORDS`) is a merged RU + ES + EN frozenset of ~200 entries. This means:

- The **same content word** in different languages (e.g., `Python`, technical terms) correctly unifies across language switches.
- **Translated equivalents** of domain words (e.g., RU `программирование` vs ES `programación`) are treated as distinct tokens — a shift from RU to ES on the same topic will likely appear as a topic shift even if the underlying subject is identical.

**Risk (LOW for cross-script switches; MEDIUM for same-topic language switches):** a user who switches mid-session from RU to ES while discussing the same subject will see a spurious topic-shift boundary. There is no cross-language semantic bridging (no embeddings or translation step).

---

## Finding 7 — Sensitivity Tuning: Class-level constant, not configurable per-call

`SHIFT_THRESHOLD = 0.30` is a class attribute. It is not exposed via IPC params, not read from settings, and not overridable at runtime. The `window_size` parameter is the only tuning knob passed through the IPC handler. There is no per-user or per-session sensitivity setting.

**Risk (LOW):** the 0.30 threshold works well for clearly distinct topics (tested). For domain-heavy corpora (e.g., long technical sessions) the threshold may be too aggressive, fragmenting continuous discussions into many segments. A simple `threshold` IPC param or settings key would allow tuning without code changes.

---

## Wire Status

| Caller | Method | Notes |
|---|---|---|
| `BackendService._handle_get_topic_timeline` | `get_topic_timeline` + `get_current_topic` | Registered in handler table at line 1088; limit=100 default |
| `MetadataEnricher.enrich` | `get_current_topic` | Single-item call; used to populate `topics` field on new history items |

One production IPC method: `get_topic_timeline`. No Swift-side direct caller found; it would be called from the History or Analytics UI panels.

---

## Test Coverage

File: `KrabEar/tests/test_topic_tracker.py` (592 lines, 8 test classes, ~35 test methods)

Coverage highlights:
- Basic cases: empty list, single item, same/different topics, `items_count` correctness, `to_dict` keys.
- `get_topic_timeline`: `is_shift` flag, required keys.
- `get_current_topic`: empty fallback, `last_n` capping.
- Segment coverage: contiguity, no gaps, `source_text` field support.
- Gradual drift: smoothing effect of larger window size.
- Unicode: Cyrillic, Spanish diacritics, emoji (filtered by regex), mixed-language input.
- Window size: 0, 1, equal-to-n, larger-than-n edge cases.
- **Concurrency**: 20 parallel threads on same instance — pass.
- Helper functions: `_tokenize`, `_keyword_overlap`, `_make_summary`, `_top_keywords`.

**Missing:** no test covers privacy-mode bypass. No test covers the `_handle_get_topic_timeline` handler directly with a mocked store (dispatch-level test in `test_dispatch_complete.py` notes a comment that the handler "may fail on HistoryItem vs dict mismatch" — line 611).

---

## Backward Compatibility

`TopicSegment.to_dict()` returns a fixed set of 5 keys (`start_index`, `end_index`, `topic_words`, `summary`, `items_count`). The IPC response adds `is_shift` at the timeline level. No schema version field. Adding new keys to the response would be backward-compatible for clients that ignore unknown fields; removing or renaming existing keys would be a breaking change. No migration risk currently.

---

## Verdict

| # | Area | Risk | Action |
|---|---|---|---|
| 1 | Algorithm (TF-IDF keyword) | LOW | Acceptable; O(n²) bounded by handler limit |
| 2 | State persistence | LOW | Stateless by design; consistent with store |
| 3 | Memory bound | MEDIUM | Add `max_items` guard inside `track_topics` |
| 4 | Concurrency | NONE | Stateless; confirmed by test |
| 5 | Privacy mode bypass | MEDIUM | Add privacy-mode guard in handler |
| 6 | Cross-language | MEDIUM | Same-topic RU→ES switches appear as false shifts |
| 7 | Threshold not configurable | LOW | Expose as IPC param or settings key |
