# Wave 1277 Re-audit: `core/topic_tracker.py` — TopicTracker Residual

**Date:** 2026-05-26
**File:** `KrabEar/core/topic_tracker.py` (314 lines)
**Auditor:** Sub-agent W1277
**Predecessor audits:** W992 (initial), W996 (fix PR #925)

---

## W996/W992 Merge State

| Branch | Status | Merged into `codex/krab-ear-v2`? |
|---|---|---|
| `docs/audit-topic-tracker-W992` | Open (docs only) | No |
| `fix/topic-tracker-privacy-W996` | **PR #925 OPEN** | **NO — NOT MERGED** |

Both W992 and W996 work is on separate branches only. `codex/krab-ear-v2` has neither the `_MAX_ITEMS` cap nor the privacy-mode guard from W996 PR #925. The current production file (`KrabEar/core/topic_tracker.py`) at HEAD of `codex/krab-ear-v2` is the pre-W996 version.

**Consequence:** All W992 findings F3 (memory/cap) and F5 (privacy mode) remain live in production.

---

## Finding 1 — CRITICAL: W996 PR #925 Not Merged (W992 F3 + F5 Still Live)

**Area:** Merge state  
**Risk:** HIGH (production)

PR #925 (`fix/topic-tracker-privacy-W996`) has been open since 2026-05-26 and is not merged into `codex/krab-ear-v2`. The fix adds:

1. `_MAX_ITEMS = 500` cap in `track_topics` with a `logger.warning` on truncation.
2. Privacy-mode guard at the top of `_handle_get_topic_timeline` returning `{"ok": True, "timeline": [], "reason": "privacy_mode_active"}`.

Without these, the production handler has `limit=0` semantics (pass ALL items to tracker when the caller sends `limit=0`) and performs transcript text analysis even in privacy mode.

**Measured impact of missing cap:** with 5000 diverse items and `limit=0`, `track_topics` takes **103 seconds**, blocking the single-threaded IPC dispatch loop for the full duration.

**Action:** Merge PR #925.

---

## Finding 2 — HIGH: IPC DoS via `limit=0` with No Internal Cap

**Area:** Handler parameter handling  
**Risk:** HIGH (production DoS)

In `_handle_get_topic_timeline` (service.py line 3455):

```python
limit = int(params.get("limit", 100) or 100)
...
if limit > 0:
    items = items[-limit:]
```

When a caller sends `{"limit": 0}`, the condition `limit > 0` is `False` and **all history items** are passed to `TopicTracker.track_topics`. The tracker has no internal bound. Since the IPC server is single-threaded (Unix socket, sequential dispatch), a single `get_topic_timeline` call with `limit=0` on a large history store blocks ALL subsequent IPC requests.

Measured timing:
- n=1000: 0.18 s
- n=5000 (diverse topics): **103.7 s** (full IPC block)

The `limit=0 → all items` semantic is arguably intentional (documented as "0 — all"), but without an internal cap or async execution, this creates a trivial DoS vector for any caller (even the Swift UI making a misconfig call).

**Action:** Either reject `limit=0` (return error), cap at `_MAX_ITEMS`, or document that `limit=0` is unsafe for large stores. W996 PR #925 adds the `_MAX_ITEMS=500` cap which mitigates this.

---

## Finding 3 — MEDIUM: W992 F5 Cross-Language False Shift — Still Unfixed

**Area:** Algorithm correctness  
**Risk:** MEDIUM (incorrect output)

W992 Finding 6 (cross-language false shifts) is confirmed unfixed in `codex/krab-ear-v2` and was NOT addressed by W996 (PR #925 only covered privacy-mode and cap).

Reproduction:
```python
tracker = TopicTracker()
ru_med = [{"text": "медицина лечение здоровье врач пациент болезнь диагноз"}] * 5
es_med = [{"text": "medicina tratamiento salud medico paciente enfermedad diagnostico"}] * 5
segs = tracker.track_topics(ru_med + es_med, window_size=3)
# → 2 segments: false shift at index 4 (RU→ES same topic)
```

And the converse (topic shift missed):
```python
ru_prog = [{"text": "программирование алгоритм функция код разработка"}] * 5
es_music = [{"text": "musica cancion concierto instrumento melodia ritmo"}] * 5
segs = tracker.track_topics(ru_prog + es_music, window_size=3)
# → 1 segment: genuine topic shift MISSED
```

Root cause: TF-IDF keyword overlap is computed over raw token strings. Translated equivalents (`медицина` ↔ `medicina`) share zero Jaccard overlap. The window at the language boundary has overlap 0.0 < 0.30 threshold → spurious segment break. Meanwhile, topically unrelated but linguistically separated items can end up in one segment if vocabulary happens not to overlap well across windows.

No fix was planned or landed for this.

---

## Finding 4 — MEDIUM: IDF `doc_freq` Uses List Linear Scan — O(n) per Word per Window

**Area:** Algorithm performance (constant-factor)  
**Risk:** MEDIUM (performance)

In `_compute_tfidf` (line 130):

```python
doc_freq = sum(1 for w_tokens in all_windows_tokens if word in w_tokens)
```

`w_tokens` is a `List[str]` (not a `set`). The `in` operator on a list is O(len(list)). For a window of `window_size=5` items with 20 tokens each, each `word in w_tokens` scan is O(100).

For a full `track_topics` run with `n` items and vocabulary size `V`:
- Per window: `V × n × O(window_len)` operations for IDF computation
- Total: `O(n² × V × window_len)`

Measured speedup from converting `window_tokens` to sets before the IDF scan:
- n=500, V=50: list approach ~3.1 s vs set approach ~0.6 s → **5× speedup**

The fix is a one-line change: pre-convert `all_windows_tokens` elements to sets before entering the `_compute_tfidf` loop, or convert inside `_compute_tfidf`:

```python
# Inside _compute_tfidf:
all_windows_sets = [set(wt) for wt in all_windows_tokens]
doc_freq = sum(1 for wt_set in all_windows_sets if word in wt_set)
```

This change does not affect correctness (document-frequency semantics are preserved; duplicates within a window still count as one document).

---

## Finding 5 — LOW: `enrich_recording` Bypasses Privacy Mode for Topic Fields

**Area:** Privacy mode coverage  
**Risk:** LOW (secondary bypass)

`MetadataEnricher.enrich_recording` (IPC method `enrich_recording`) calls `TopicTracker.get_current_topic` to populate the `topics` metadata field on a history item. `MetadataEnricher` has zero privacy-mode awareness (`privacy` does not appear in `backend/metadata_enricher.py`).

The `enrich_recording` handler is registered directly from `_metadata_enricher.handle_enrich_recording` without any privacy guard in `service.py`. Even after W996 PR #925 is merged (which guards the `get_topic_timeline` handler), calling `enrich_recording` in privacy mode will still tokenize and extract topic keywords from transcript text.

This is a narrower surface than `get_topic_timeline` (single-item call, no IPC parameter leak), but it is a consistency gap with the privacy-mode contract that other analytics handlers enforce.

---

## Test Coverage Gap (Post-W996)

Tests added by W996 (in PR #925, not yet in `codex/krab-ear-v2`):
- `TestTopicTrackerInternalCap` — verifies `_MAX_ITEMS` truncation and warning
- `TestHandlerPrivacyModeGuard` — verifies handler returns `timeline=[]` with `reason='privacy_mode_active'`

These tests exist only on `fix/topic-tracker-privacy-W996` branch.

Missing tests in `codex/krab-ear-v2` (`KrabEar/tests/test_topic_tracker.py`):
- No test for `limit=0` DoS scenario (unbounded input to `track_topics`)
- No test for cross-language false-shift behavior (W992 F6)
- No test for `enrich_recording` privacy-mode bypass via `MetadataEnricher`
- No test for IDF with `n=1` (single document: IDF = 1.0 for all words; TF-IDF reduces to TF)

The concurrent test (`TestTopicTrackerConcurrent`) is present and passes. Thread safety is confirmed: `TopicTracker` is stateless (no instance fields mutated after `__init__`).

---

## Summary Table

| # | Finding | Risk | Fixed by W996? | Status |
|---|---|---|---|---|
| 1 | W996 PR #925 not merged (privacy + cap both missing) | HIGH | N/A | Not merged |
| 2 | `limit=0` IPC DoS — 103 s block on 5000 items | HIGH | Partially (cap) | Open |
| 3 | Cross-language false shift (W992 F6, same-topic RU→ES) | MEDIUM | No | Open |
| 4 | IDF doc_freq list scan O(n) vs set O(1) — 5× slower | MEDIUM | No | Open |
| 5 | `enrich_recording` bypasses privacy mode for topics | LOW | No | Open |
