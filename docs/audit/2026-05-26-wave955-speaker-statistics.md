# Wave 955 Audit: `speaker_statistics.py`

**Date**: 2026-05-26  
**File**: `KrabEar/backend/speaker_statistics.py` (269 lines)  
**Tests**: `KrabEar/tests/test_speaker_statistics.py` (575 lines, 9 test classes)  
**Status**: AMBER — 5 findings, no critical bugs, 1 dead-wiring gap  

---

## Summary

`SpeakerStatisticsAnalyzer` computes per-speaker word count, duration, confidence, and language
distribution from diarized history items. The implementation is clean and numerically robust, but
has one significant architectural gap: the IPC handler method `handle_get_speaker_statistics` is
fully implemented yet **never wired into `BackendService.handle_request`**. The method is dead from
the IPC perspective.

---

## Finding 1 — CRITICAL: IPC handler is unwired (dead)

**Severity**: HIGH  
**File**: `KrabEar/backend/service.py` lines 28, 376

`BackendService.__init__` instantiates `self._speaker_statistics = SpeakerStatisticsAnalyzer()` but
never registers `"get_speaker_statistics"` in the handler lookup table. The Wave 65 batch 4 dead-
handler cleanup deleted the legacy `_handle_get_speaker_statistics` from `service.py` (confirmed by
`test_ipc_dispatch_invariants.py` line 380 regression guard) but did not connect the extracted
service method as a replacement.

Result: `get_speaker_statistics` IPC calls receive an "unknown method" error at runtime. The
`SpeakerStatisticsAnalyzer` object is instantiated and occupies memory but is never called.

**Fix**: add one line to the handler table in `service.py`:
```python
"get_speaker_statistics": lambda params: self._speaker_statistics.handle_get_speaker_statistics(
    params, store=self.store, speaker_manager=self._speaker_manager
),
```

---

## Finding 2 — Performance: full history scan on every query, no cache

**Severity**: MEDIUM  
**File**: `KrabEar/backend/speaker_statistics.py` lines 207–227

`handle_get_speaker_statistics` calls `store._load_active_items_unlocked()` on every IPC request.
For 10 K history items this loads the entire NDJSON store into memory, iterates all items, and
accumulates per-speaker aggregates — O(N × turns_per_item) with no caching layer.

`SpeakerManager.handle_set_speaker_alias` (when an alias is renamed) does not invalidate any cache
because none exists, so at least the alias concern is moot. But repeated calls from the Analytics
Dashboard or a polling UI would deserialize the full history file each time.

Comparable analytics modules (`SentimentTrendAnalyzer`, `QualityTrendAnalyzer`) have the same
pattern. The project-wide IPC throttle (`IPCThrottle`) can rate-limit abusive callers, but latency
on a large store will be noticeable (estimated 200–500 ms for 10 K items with multi-turn diarization).

**Recommendation**: add a short-lived TTL cache (5–10 s) keyed on `store` version counter, similar
to `SettingsService`'s 5 s TTL, or accept the current on-demand pattern and document the latency.

---

## Finding 3 — Word count heuristic diverges from `speech_pace.py`

**Severity**: LOW  
**File**: `KrabEar/backend/speaker_statistics.py` line 17

The module uses:
```python
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)
```

`core/speech_pace.py` uses a more precise pattern that explicitly covers Cyrillic, Latin with
accents, and Spanish diacritics:
```python
_RE_WORD = re.compile(
    r"[А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+(?:[-'][А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+)*"
)
```

The `\w` class in Python `re.UNICODE` does match Cyrillic letters correctly (it covers `[0-9A-Za-z_]`
plus any Unicode letter), so Russian text is counted correctly. However `\w` also matches underscore
`_` and digits as standalone tokens, which the `speech_pace` pattern excludes. This means numeric
tokens like `"42"` and mixed tokens like `"SPEAKER_00"` are counted as words in speaker_statistics
but would be excluded in speech_pace. The discrepancy affects WPM figures when diarization turn text
contains numbers or speaker IDs embedded in transcript text.

**Recommendation**: align with `speech_pace._RE_WORD` or document the difference. No correctness
bug for normal speech text.

---

## Finding 4 — Confidence aggregation: simple mean, no per-turn weight

**Severity**: LOW  
**File**: `KrabEar/backend/speaker_statistics.py` lines 124–128, 155–157

Confidence is taken at the **item (recording) level**, not per turn:
```python
if item_confidence is not None:
    entry["confidences"].append(float(item_confidence))
```

This means every turn of speaker X within a single recording contributes the same item-level
confidence value once per turn, not once per recording. A recording with 20 turns from SPEAKER_00
contributes 20 samples of the same confidence value, while a recording with 1 turn from SPEAKER_01
contributes 1 sample. The effective average is then **weighted by turn count per recording**, not by
recording count or speaking time.

The model is not documented. For a recording where SPEAKER_00 dominates (many turns), this
overweights that recording's confidence in the average. A recording where confidence is low but the
speaker has many short turns will pull the average down more than a single long turn.

**Recommendation**: either sample confidence once per item (not per turn), or document the weighting
model in the docstring. A turn-duration-weighted average would be more semantically correct.

---

## Finding 5 — No privacy mode gate; no time-window filtering

**Severity**: LOW  
**File**: `KrabEar/backend/speaker_statistics.py` lines 207–227

Two gaps:

**Privacy mode**: `handle_get_speaker_statistics` loads and processes all history items without
checking whether the backend is in privacy mode. Other analytics IPC handlers (e.g., sentiment
trends, keyword cloud) also lack this gate, so this is a project-wide pattern. If privacy mode
purges history on enable but not retroactively, the risk is low — the store would be empty.
However if privacy mode is advisory (not destructive), speaker stats expose who spoke and when.

**Time-window filtering**: the `params` dict is accepted but entirely ignored — no `date_from`,
`date_to`, or `limit` filtering is available. Stats are always lifetime aggregates. Other analytics
modules (`PeriodComparisonService`, `SentimentTrendAnalyzer`) support date-range scoping. The UI
cannot request "last 7 days speaker balance" without client-side filtering.

**Recommendation**: accept optional `date_from`/`date_to` ISO-8601 params and filter items by `ts`
before aggregation (same pattern as `sentiment_trends.py`).

---

## Edge case coverage — no bugs found

| Scenario | Handling |
|----------|----------|
| Speaker with 0-duration turns | `max(0.0, end - start)` clamps to 0; turn not added to `turn_durations`; no div-by-zero in `avg_turn` (guarded by `if turn_durations`) |
| Zero speaking time with words | `avg_wpm = 0.0` (guarded by `if total_time > 0`); no div-by-zero |
| No confidence data | `avg_confidence = None` (correctly typed as `float | None`) |
| Single speaker balance | `_compute_balance` returns `0.0` for `n <= 1` |
| Empty history | Returns `{"speakers": {}, "total_speakers": 0, "most_active_speaker": None, "speaker_balance": 1.0}` |
| `store._lock()` raises | Caught, logged as WARNING, returns empty result |
| Cyrillic speaker IDs | `str(turn.get("speaker", ""))` handles any Unicode correctly |
| NaN propagation | No `float("nan")` path identified; `item_confidence` numeric coercion has try/except guard |

---

## Test coverage

575 lines / 9 classes / ~30 test methods in `test_speaker_statistics.py`.

Coverage is solid for the `analyze_speakers` public API:
- Empty input, disabled diarization, single/multi-speaker basics
- Balance entropy (equal and unequal cases)
- Turn durations (longest, average)
- Alias resolution via SpeakerManager (present, absent, unknown speaker)
- Multi-item aggregation and language counting
- IPC handler with fake store (happy path, store exception, speaker_manager pass-through)
- Field name stability and Unicode speaker IDs

**Gaps not covered**:
- The time-window filtering feature (does not exist yet)
- Privacy mode gate behaviour
- Turn count > 1 in same item — confidence weighting is tested indirectly (multi-item case) but the
  multi-turn-same-item overweighting (Finding 4) is not explicitly tested
- The `_WORD_RE` digit-counting divergence from `speech_pace`

---

## Output format stability

All keys (`speakers`, `total_speakers`, `most_active_speaker`, `speaker_balance`) and per-speaker
keys (`alias`, `total_speaking_time_sec`, `total_words`, `avg_words_per_minute`, `appearances`,
`avg_confidence`, `languages`, `longest_turn_sec`, `avg_turn_sec`) are stable. `avg_confidence` can
be `None` — callers must guard for `null` in Swift.

---

## Recommendations (priority order)

1. **Wire `get_speaker_statistics` into `service.py` handler table** — the handler is fully
   implemented and tested; one line needed.
2. **Add optional `date_from`/`date_to` params** to enable time-scoped queries.
3. **Fix confidence aggregation** — sample once per item, not once per turn, to avoid
   turn-count-weighted distortion.
4. **Document or align word regex** — use `speech_pace._RE_WORD` or add a note explaining why
   `\w'` is preferred.
5. **Consider TTL cache** if the Analytics Dashboard polls this endpoint frequently on large stores.
