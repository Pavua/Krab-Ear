# Wave 990 — WordTimingAnalyzer Audit

**File:** `KrabEar/core/word_timing.py`  
**Date:** 2026-05-26  
**Auditor:** W990 (read-only)

---

## Summary

`WordTimingAnalyzer` is a lightweight, zero-dependency per-word timestamp analyzer for Whisper
output. It is fully wired in production via `AudioAnalyticsService` / `analyze_word_timing` IPC,
well-tested, and numerically safe. Five findings are documented below, all low severity.

---

## Findings

### F1 — Timestamp source: graceful fallback present, but segment-level granularity is coarse

**Severity:** Low  
**Location:** `_extract_words()` lines 30–60

When Whisper word-level timestamps are absent (`words` field missing or empty), the function falls
back to treating each segment as a single word unit. This means the hesitation threshold and
pause metrics measure inter-*sentence* gaps rather than inter-*word* gaps. For older or non-MLX
STT adapters (GigaAM, SenseVoice, Parakeet) that emit segments without `words`, all metrics
degrade silently — there is no warning log and callers receive plausible-looking but structurally
different output.

**Recommendation:** Emit a single `logger.debug()` when falling back, so callers can distinguish
word-level from segment-level analysis in diagnostics.

---

### F2 — Hesitation threshold not locale-calibrated (single global 0.5 s)

**Severity:** Low  
**Location:** `_HESITATION_THRESHOLD_SEC = 0.5` (line 21)

The 0.5 s hesitation threshold is a standard English-language value from psycholinguistics
literature. Russian speech has a somewhat shorter natural hesitation onset (~0.35–0.4 s due to
shorter average word length and different prosodic rhythm); Spanish falls between EN and RU.
No locale parameter is accepted by `analyze()` — the threshold is applied uniformly for all
languages Krab Ear serves (RU primary, ES secondary, EN tertiary).

**Recommendation:** Accept an optional `language: str = None` parameter; apply per-language
thresholds (`RU: 0.4 s`, `ES: 0.45 s`, `EN: 0.5 s`) with the current value as default.

---

### F3 — Last-pause exclusion logic is off-by-one for exactly two pauses

**Severity:** Low  
**Location:** `analyze()` lines 152–154

```python
mid_pauses = pauses_sec[:-1] if len(pauses_sec) > 1 else pauses_sec
```

When there are exactly two inter-word pauses, `mid_pauses = pauses_sec[:-1]` correctly
excludes the final pause. However, when there is exactly one pause — which is between two
words — `mid_pauses = pauses_sec` (unchanged), so that single pause is counted as a
hesitation even though it could be a sentence boundary. The intent appears to be "exclude
the final pause if there's more than one"; with one pause there is no reliable way to
distinguish mid-sentence from sentence-boundary — this is a known ambiguity but worth
documenting in the docstring.

**Recommendation:** Add a docstring note explaining the single-pause ambiguity; no code
change strictly required.

---

### F4 — Performance: O(N log N) due to sort; acceptable for all realistic inputs

**Severity:** Informational  
**Location:** `analyze()` line 138: `sorted(words, key=lambda w: w["start"])`

The sort is O(N log N) where N = number of words. For a 60-minute transcript at average
speaking rate ~130 WPM that is ~7800 words → ~7800 × log₂(7800) ≈ 100k comparisons, well
under 1 ms on M4 Max. The rest of the function is O(N). Performance is not a concern.

The sort is necessary because Whisper segments from diarized multi-speaker output may not
be ordered by word start time (speaker turn interleaving). Keeping it is correct.

---

### F5 — No NaN/Inf guard on timestamps from IPC callers

**Severity:** Low  
**Location:** `_extract_words()` lines 47–49

```python
start = float(w["start"])
end = float(w["end"])
if end > start:
    result.append(...)
```

`float("nan")` passes the `end > start` check (`nan > nan` is `False`, but `1.0 > nan` is
also `False`) — so NaN-stamped words are silently dropped. `float("inf")` where `end=inf`
and `start=0` would pass (`inf > 0`), yielding infinite word duration and polluting
`avg_word_duration_ms` with `inf`. This is unlikely from `mlx-whisper` output but possible
from hand-crafted IPC payloads or mock data in tests.

**Recommendation:** Add `math.isfinite(start) and math.isfinite(end)` guard in
`_extract_words()`:
```python
import math
if math.isfinite(start) and math.isfinite(end) and end > start:
    result.append(...)
```

---

## Checklist

| Check | Result |
|-------|--------|
| Timestamp source assumption | Graceful fallback to segment-level; no warning emitted (F1) |
| Hesitation threshold (ms) | 500 ms fixed, not locale-calibrated (F2) |
| Edge cases: 0-word, 1-word | Zero-report for empty; 1-word works correctly |
| Edge case: overlapping speakers | Sort by `start` handles diarized interleaving (F4) |
| Performance O(N) | O(N log N) due to sort; acceptable for 60-min transcripts |
| Locale-specific thresholds | Not implemented (F2) |
| Wire status | Fully wired: `BackendService` → `AudioAnalyticsService.handle_analyze_word_timing` → `IPC analyze_word_timing` |
| Test coverage | Comprehensive: 8 test classes, ~35 cases covering empty, single-word, hesitations, consistency, fallback, concurrency, IPC |
| Output schema stability | Stable 6-key `TimingReport` dataclass; `as_dict()` used by IPC response |
| NaN/Inf robustness | NaN silently dropped (safe); Inf passes guard → downstream pollution (F5) |
| Privacy (raw word timings) | IPC returns *aggregated* metrics only — no raw word text or individual timestamps exposed |

---

## Wire Path

```
IPC "analyze_word_timing"
  → BackendService._audio_analytics_svc.handle_analyze_word_timing()   [service.py:1184]
  → AudioAnalyticsService.handle_analyze_word_timing()                  [audio_analytics_service.py:114]
  → WordTimingAnalyzer.analyze(segments)                                [word_timing.py:117]
  → TimingReport.as_dict()                                              [word_timing.py:89]
```

`WordTimingAnalyzer` is instantiated once at `BackendService.__init__()` (line 390) and
reused across all calls — stateless, thread-safe.

---

## Privacy Assessment

The `analyze_word_timing` IPC method accepts raw Whisper segment data (which includes word
text) but returns only aggregate numeric metrics. No word text, individual timestamps, or
speaker identifiers are included in the response. Privacy impact: **none**.
