# Wave 1048 — SpeechPaceAnalyzer Audit

**Date:** 2026-05-26
**Auditor:** W1048 (sub-agent)
**Scope:** `KrabEar/core/speech_pace.py` — `SpeechPaceAnalyzer` (WPM, CPM, pace category)

---

## Summary

`SpeechPaceAnalyzer` is a clean, dependency-free module with solid edge-case handling and good test coverage. Five findings follow, ranging from a correctness gap to documentation nits.

---

## Findings

### F-1 [MEDIUM] Pace thresholds are language-agnostic; RU/ES differ significantly from EN

**File:** `KrabEar/core/speech_pace.py`, lines 24–28

Current thresholds (`slow < 100`, `normal 100–160`, `fast 160–200`, `very_fast > 200`) are tuned to English speech norms. Russian and Spanish speech rates differ meaningfully:

| Language | Typical conversational WPM |
|----------|---------------------------|
| English  | 130–160                   |
| Russian  | 100–120 (morphology-heavy) |
| Spanish  | 150–180 (syllable-timed)  |

A 140 WPM Russian recording is classified `normal` by the current logic, but a native speaker would perceive it as `fast`. The module docstring claims it is "adapted for Russian, Spanish and English" but the thresholds are identical for all three locales — the adaptation is only in the tokeniser regex, not in the pace boundaries.

**Recommendation:** Either (a) accept locale as an optional parameter and apply per-language thresholds, or (b) update the docstring to say thresholds reflect English norms and treat locale-specific classification as a known limitation.

---

### F-2 [MEDIUM] IPC handler `analyze_speech_pace` is not registered in `handle_request`

**File:** `KrabEar/backend/service.py`, lines 47, 389

`SpeechPaceAnalyzer` is imported and instantiated (`self._speech_pace_analyzer`), but a search for `"analyze_speech_pace"` and `_handle_.*pace` in `service.py` returns zero results. There is no JSON-RPC dispatch entry for this analyzer.

The test file simulates the handler inline (`SpeechPaceIPCHandlerTestCase._call_handler`) rather than going through `BackendService.handle_request`, confirming the gap: the method is available in tests but unreachable from the Swift agent or any external IPC caller.

**Recommendation:** Register `"analyze_speech_pace"` in the handler lookup table and add a corresponding `_handle_analyze_speech_pace` method following the existing delegation pattern.

---

### F-3 [LOW] `_empty_report` returns `pace_category="slow"` for zero-word input — semantically incorrect

**File:** `KrabEar/core/speech_pace.py`, line 207 (the `_empty_report` static method)

When `duration_sec > 0` but text is empty (whitespace-only, punctuation-only, or truly empty), the returned `PaceReport` has `words_per_minute=0.0` and `pace_category="slow"`. A caller cannot distinguish "silent recording" from "genuinely slow speech." The IPC consumer would display "slow" in a UI even though no words were detected.

**Recommendation:** Add a dedicated sentinel category, e.g. `"none"`, for reports where `word_count == 0`, and document this in `_pace_category`. Alternatively, keep `"slow"` but document the limitation explicitly.

---

### F-4 [LOW] `char_count` counts word characters only, not total transcript characters — naming mismatch

**File:** `KrabEar/core/speech_pace.py`, line 111

```python
char_count = sum(len(w) for w in words)   # only alphabetic chars in words
```

`char_count` excludes punctuation, numerals, spaces, and any characters not matched by `_RE_WORD`. For a transcript like `"Привет, мир!"` the `char_count` is 10 (the two word tokens), not 13 (all non-space characters). The `PaceReport` docstring says "число символов (без пробелов)" which is ambiguous.

`chars_per_minute` is derived from this count, making it a "word-character rate" rather than a true CPM. This may be intentional for STT analysis (counting spoken characters), but it differs from conventional CPM definitions and from what a Swift UI consumer might expect.

**Recommendation:** Rename the field `word_chars_per_minute` / `word_char_count`, or update the docstring to explicitly state that punctuation and numerals are excluded.

---

### F-5 [LOW] No integration with `WordTimingAnalyzer` — WPM duplicates timestamp-based data

**File:** `KrabEar/core/word_timing.py`, `KrabEar/core/speech_pace.py`

`WordTimingAnalyzer` already computes hesitations, pauses, and consistency of pace from Whisper word-level timestamps. `SpeechPaceAnalyzer` independently computes WPM from text + a single `duration_sec` scalar. When Whisper returns word-level timestamps (the common case), the two modules produce parallel but uncombined signals.

Specifically: `WordTimingAnalyzer` knows actual inter-word gap durations and could provide a more accurate "active speech WPM" (excluding long pauses), while `SpeechPaceAnalyzer` gives "recording WPM" (including silences). There is no coupling or joint report.

**Recommendation:** Add an optional `segments: List[dict]` parameter to `SpeechPaceAnalyzer.analyze()` or expose a separate `analyze_with_timing()` method that delegates to `WordTimingAnalyzer` internally to emit both gross WPM and net speech-active WPM. Not urgent, but would increase analytical value.

---

## Test Coverage Assessment

Coverage is thorough for the existing API surface:

- All four pace categories tested (`SpeechPaceCategoryTestCase`)
- Boundary WPM values (99 / 100 / 160 / 200 / 201) tested
- Edge cases: empty text, whitespace-only, punctuation-only, digits-only, zero/negative duration
- Multilingual tokenisation: RU (with Ё), ES (diacritics), mixed RU+EN
- Hyphenated words treated as single token
- `compare_pace` aggregation, distribution, empty list
- Concurrency safety under 20 parallel threads
- `as_dict()` serialisation

**Gap:** No test exercises the IPC handler through `BackendService.handle_request` (because the handler is not registered — see F-2). All IPC tests are simulated in-module.

---

## Wire Status

| Symbol | Status |
|--------|--------|
| Imported in `service.py` | Yes (line 47) |
| Instantiated in `__init__` | Yes (line 389) |
| Registered in handler table | **No** |
| Callable via IPC | **No** |

---

## Recommended Action Order

1. **F-2** (register IPC handler) — blocking; the module is dead from the Swift side.
2. **F-1** (locale-aware thresholds or honest docs) — medium; affects analytical accuracy for RU/ES.
3. **F-3** (sentinel category for empty reports) — low; cosmetic but affects UI correctness.
4. **F-4** (naming clarity for char_count) — low; documentation fix.
5. **F-5** (WordTimingAnalyzer integration) — low; enhancement.
