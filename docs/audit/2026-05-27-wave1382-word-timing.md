# Audit: WordTimingAnalyzer — W1382

**Date:** 2026-05-27
**Wave:** W1382
**Auditor:** sub-agent W1382
**Scope:** `KrabEar/core/word_timing.py` — W995 merge state, numerical stability,
out-of-order segments, confidence calibrator interaction, wire status, test
coverage, output schema, performance on 30+ min audio.

---

## W995 Merge State

Commit `45ff62a7` (`fix(wave995): word_timing finite guard + segment fallback debug log`)
is present in `codex/krab-ear-v2`. The fix:

1. Added `math.isfinite(start) and math.isfinite(end)` guard inside the
   **word-level** branch of `_extract_words()` to reject `inf`/`-inf`/`NaN`
   word timestamps.
2. Added `logger.debug("word_timing: words field absent, falling back to
   segment-level (coarse)")` when a segment has no `words` field.
3. Two regression tests added: `test_inf_timestamp_filtered`,
   `test_segment_fallback_logs_debug`.

Status: **MERGED**.

---

## Findings

### F1 — HIGH: `_extract_words` segment-level fallback missing `math.isfinite` guard (W995 incomplete)

**File:** `KrabEar/core/word_timing.py`, lines 51–59

W995 added the `math.isfinite()` guard only to the **word-level** path (inside
`if words:`). The **segment-level fallback** path (the `else` branch) still only
checks `end > start`:

```python
else:
    start = seg.get("start")
    end   = seg.get("end")
    if start is not None and end is not None:
        start = float(start)
        end   = float(end)
        if end > start:           # <-- no math.isfinite() guard
            result.append(...)
```

`float('inf') > 0.0` evaluates to `True`, so a segment with `{"start": 0.0,
"end": float('inf')}` passes the guard and produces an `inf` duration. With two
or more such segments in the extracted list, `statistics.stdev(durations_ms)` in
`_compute_consistency()` raises `ValueError: inf or nan encountered in data` on
Python 3.14 (the project's runtime). Confirmed with reproduction:

```
segments = [{"text": "x", "start": 0.0, "end": float("inf")}]
analyzer.analyze(segments)  # → ValueError: inf or nan encountered in data (Python 3.14)
```

One such segment produces `durations_ms = [inf]`; `len < 2` returns 1.0 safely.
Two segments (or one inf + one finite) hit `stdev` and raise. Real Whisper output
can produce `inf` end-times on GPU hang or malformed bitstream.

**Fix:** Add `math.isfinite(start) and math.isfinite(end)` to the segment fallback
path, matching the word-level guard.

---

### F2 — MED: Negative inter-word gaps from overlapping timestamps silently accepted

**File:** `KrabEar/core/word_timing.py`, lines 140–143

After sorting words by `start`, overlapping timestamps (e.g., WhisperX alignment
artifacts where `word[i+1].start < word[i].end`) produce a negative gap. The
`_MIN_INTER_WORD_PAUSE_SEC = 0.08` filter correctly rejects them (negative < 0.08),
so they do not appear in `pauses_sec` — no crash. However, no debug log is emitted
for this case, making it invisible in diagnostics.

More subtly: for nested timestamps (word A `0.0–2.0`, word B `0.5–1.5` both in
`words` field), after sorting by start, word A sorts first. Gap = `0.5 - 2.0 =
-1.5 s` — filtered. But word B's duration (`1.0 s`) is still counted in
`durations_ms`, silently inflating `avg_word_duration_ms`. This mirrors the
WhisperX behaviour where alignment produces overlapping spans on phoneme boundaries.

There is no test verifying the negative-gap path; a regression is plausible.

---

### F3 — MED: Chunk-relative timestamps produce phantom cross-segment pauses

**File:** `KrabEar/core/word_timing.py`, `_extract_words` / `analyze`

Some mlx-whisper versions emit per-chunk `words` with timestamps relative to the
chunk start (not the recording start). When two chunks have `words` with `start`
values restarting from 0.0, `_extract_words` returns words from both chunks
interleaved. After sorting by start, chunk-2 words (all `< 5.0 s`) sort before
chunk-1 words (all `> 5.0 s`) if chunk ordering is non-monotone.

Observed effect in testing:

```
# Chunk 1: words at 0.0–0.5, 0.6–1.1 (global seconds)
# Chunk 2: words at 0.0–0.5, 0.6–1.1 (relative to chunk start)
# After sort by start: [0.0, 0.0, 0.6, 0.6, ...]
# Gap between index 0 and 1: 0.0 - 0.5 = -0.5 → filtered
# total_pause_time_sec = 0.1 s (only the 0.6-0.5=0.1s gap survives)
```

For a 30-min recording split into 5-minute chunks (common for `audio_chunker.py`),
this produces near-zero `total_pause_time_sec` and `hesitation_count = 0`
regardless of actual pauses. There is no guard or warning in `_extract_words` when
chunk-relative timestamps are detected. The W990 audit did not flag this scenario.

---

### F4 — LOW: No interaction with ConfidenceCalibrator (wiring gap)

**File:** `KrabEar/backend/audio_analytics_service.py`, `handle_analyze_word_timing`

`WordTimingAnalyzer` and `ConfidenceCalibrator` are completely independent —
`analyze_word_timing` returns only timing metrics with no confidence fields.
WhisperX word-level data includes a `confidence` (score) field per word, but
`_extract_words` silently drops it. The `TimingReport` schema has no
`avg_word_confidence` or `speaking_rate_confidence_correlation` field.

This is a **design gap** rather than a bug: the two analyzers could usefully
cross-reference (e.g., flag hesitations where confidence is also low as likely
disfluencies vs. genuine pauses), but currently there is no bridge.

No caller in native Swift (`grep -r "analyze_word_timing" native/` returns empty)
uses the IPC method at all — the method is wired and tested but not yet called
from any UI component.

---

### F5 — LOW: Performance is excellent; no concern for 30+ min audio

**Measured:** 10 200 words (simulated 60-min audio at 170 WPM), Python 3.14:

```
Words: 10 200, elapsed: 4.3 ms
```

The algorithm is O(N log N) due to `sorted()` on the word list, and O(N) for all
subsequent passes. For 5 000 words (30-min audio): 2.6 ms. No concern at any
realistic recording length. The `statistics.stdev` call in `_compute_consistency`
is O(N) and not the bottleneck. **No action needed.**

---

## Test Coverage Summary

| Area | Coverage | Notes |
|---|---|---|
| Empty/invalid segments | Full | `TestWordTimingAnalyzerEmpty` |
| Basic arithmetic | Full | `TestWordTimingAnalyzerBasic` |
| Hesitation detection | Full | `TestWordTimingHesitations` |
| Consistency metric | Full | `TestWordTimingConsistency` |
| Fallback path (no words) | Full | `TestWordTimingFallback` |
| `_extract_words` helper | Full | `TestExtractWordsHelper` |
| Inf word timestamps (W995 F5) | Present | `test_inf_timestamp_filtered` |
| Fallback debug log (W995 F1) | Present | `test_segment_fallback_logs_debug` |
| Inf segment-level timestamps | **Missing** | F1 above — not covered |
| Negative gap (overlapping words) | **Missing** | F2 above |
| Chunk-relative timestamp detection | **Missing** | F3 above |
| Thread safety | Full | `TestConcurrentAnalyze` |
| IPC wiring | Present | `test_dispatch_complete.py` |
| Swift callers | None (expected) | No Swift UI uses this yet |

---

## Wire Status

- Instantiated in `BackendService.__init__` (line 389): `self._word_timing_analyzer = WordTimingAnalyzer()`
- Passed to `AudioAnalyticsService` constructor (line 452)
- Dispatched via `"analyze_word_timing"` key in handler table (line 1183)
- No Swift caller currently invokes `analyze_word_timing` — the IPC method is
  ready but unused in UI. This is consistent with `word_timing` being a
  diagnostic/analytics endpoint rather than a hot path.

---

## Recommendations

| Priority | Action |
|---|---|
| HIGH | F1: extend `math.isfinite` guard to segment-level fallback in `_extract_words` |
| MED | F2: add `logger.debug` warning when negative inter-word gap is encountered |
| MED | F3: document chunk-relative timestamp limitation in docstring; optionally add detection heuristic |
| LOW | F4: consider adding `avg_word_confidence` to `TimingReport` when WhisperX provides scores |
