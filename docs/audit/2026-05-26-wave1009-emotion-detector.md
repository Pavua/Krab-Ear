# Wave 1009 — EmotionDetector Audit

**Date:** 2026-05-26  
**File audited:** `KrabEar/core/emotion_detector.py` (227 lines)  
**Auditor:** sub-agent W1009

---

## Summary

`EmotionDetector` is a pure heuristic, zero-dependency classifier that combines keyword lookup,
punctuation counting, and caps-ratio analysis. It is lightweight and thread-safe, but has five
concrete issues worth addressing.

---

## Findings

### F1 — HIGH: Negation particles "не"/"нет"/"no"/"never" cause systemic false negatives

**Severity:** HIGH  
**File:** `core/emotion_detector.py` lines 16-31

`"не"`, `"нет"` (RU) and `"no"`, `"never"` (EN) appear in the `_NEGATIVE_WORDS` dictionaries.
These tokens are among the most frequent Russian negation particles and common English determiners,
not emotion words. A plain neutral sentence like `"не знаю точно"` or `"нет информации"` is
classified as `negative` (confidence 0.47). Similarly, `"no problem, I can do that"` → `negative`.

Empirical check (live run against the detector):
```
'не знаю точно'               → negative (0.47), indicators=['не']
'нет информации'              → negative (0.47), indicators=['нет']
'Пожалуйста, не забудьте'    → negative (0.47), indicators=['не']
'no problem, I can do that'  → negative (0.47), indicators=['no']
'never mind, it is fine'     → negative (0.47), indicators=['never']
```

**Recommendation:** Remove `"не"`, `"нет"` from RU negative list; remove `"no"`, `"never"` from
EN negative list. These particles require bigram context to be meaningful emotion signals; absent
that, they produce more noise than signal. Replace with unambiguous emotion words
(e.g., `"провал"`, `"кошмар"`, `"hate"`, `"disaster"`).

---

### F2 — MEDIUM: No privacy-mode guard on `detect_emotion` IPC and `get_sentiment_trends`

**Severity:** MEDIUM  
**Files:** `backend/text_processing_service.py` line 255, `backend/service.py` line 2877

When `privacy_mode_enabled=True` the `detect_emotion` IPC method and `get_sentiment_trends`
analysis both pass full transcript text through `EmotionDetector.detect()`. Compared to
`TranslationService` (which hard-blocks remote calls in privacy mode), and `observability.py`
(which skips Sentry init), the emotion path has no guard at all.

While emotion detection is purely local (no network calls), the concern is that sentiment trend
data accumulates a secondary record of user speech patterns derived from transcript content. If
the user enables privacy mode expecting minimal secondary processing, the sentiment trend pipeline
should be skipped or return an empty report.

**Recommendation:** In `_handle_get_sentiment_trends` and `handle_detect_emotion`, check the
`privacy_mode_enabled` setting (via `self._get_runtime_setting`) and either return a stub
`EmotionResult(primary_emotion="neutral", confidence=0.0)` or raise a `RuntimeError` with a
clear message.

---

### F3 — MEDIUM: "да" (yes) classified as positive — Russian affirmative particle treated as emotion word

**Severity:** MEDIUM  
**File:** `core/emotion_detector.py` line 37

`"да"` (RU) and `"sí"` (ES) are affirmative particles, not emotion words. A transcript like
`"да, давайте обсудим это на следующей встрече"` (neutral business sentence) is classified as
`positive` because `"да"` appears in `_POSITIVE_WORDS`. Same issue applies to `"yes"` in
English.

**Recommendation:** Remove `"да"`, `"sí"`, `"yes"` from their respective positive word lists.
Affirmatives carry sentiment only in specific contexts; standalone they are too frequent in
neutral speech to be reliable emotion signals.

---

### F4 — LOW: Spanish inverted opening marks (¡ ¿) are silently ignored

**Severity:** LOW  
**File:** `core/emotion_detector.py` lines 115-116

`text.count("!")` counts only ASCII `!`. Inverted opening exclamation `¡` is a separate codepoint
(U+00A1) and is not counted. For Spanish input like `"¡Qué horror!"` the `¡` contributes nothing
to the `exclamation_count`. The existing test suite
(`TestEmotionDetectorSpanishMarkers.test_inverted_exclamation_triggers_excited`) documents this as
accepted behaviour in a comment, but does not validate that double-exclamation sentences (e.g.,
`"¡Terrible!"`) are correctly classified.

This is a minor accuracy gap: most Spanish exclamation sentences include a closing `!`, so they
are correctly detected. However, sentences like `"¡No!"` without a closing mark are missed.

**Recommendation:** Extend `exclamation_count` to include `text.count("¡")` and `question_count`
to include `text.count("¿")`. Add a test for `"¡No!"` (no closing mark).

---

### F5 — LOW: Confidence floor is inconsistent — neutral path uses 0.5, winner path uses raw score

**Severity:** LOW  
**File:** `core/emotion_detector.py` lines 176-186

When `best_score < 0.1` the method returns `neutral` with `confidence=0.5`. However when a winner
is found (score ≥ 0.1), `confidence = min(best_score, 1.0)` which can be as low as 0.35 (from
`min(0.35 + 0.12 * 1, 0.9)`). This means a weak `negative` hit (`confidence=0.35`) appears less
certain than an empty `neutral` result (`confidence=0.5`), which is counter-intuitive for callers
(e.g., `SentimentTrendAnalyzer` which uses the numeric score in regression analysis).

**Recommendation:** Normalise the confidence output: either lower the neutral floor to 0.3, or
add an explicit minimum confidence floor (e.g., `max(best_score, 0.3)`) for non-neutral results
so that any detected signal always reports higher confidence than the "no signal" case.

---

## Wire Status

- **`detect_emotion` IPC:** wired in `BackendService.handle_request` → `TextProcessingService.handle_detect_emotion` (line 1095 in `service.py`). Active.
- **`get_sentiment_trends` IPC:** wired at line 1055, delegates to `SentimentTrendAnalyzer` which owns its own `EmotionDetector` instance (shared with the one from `BackendService.__init__` via `detector=self._emotion_detector` at line 416).
- **Swift callers:** zero — no Swift code calls `detect_emotion` or reads `primary_emotion` fields. The detector output surfaces only through `get_sentiment_trends` JSON responses and the analytics dashboard.

---

## Test Coverage

| Suite | File | Tests |
|-------|------|-------|
| Unit — basic | `test_emotion_detector.py::TestEmotionDetectorBasic` | 14 methods |
| Spanish markers | `test_emotion_detector.py::TestEmotionDetectorSpanishMarkers` | 5 methods |
| Locale fallback | `test_emotion_detector.py::TestEmotionDetectorLocaleFallback` | 4 methods |
| Monotonicity | `test_emotion_detector.py::TestEmotionDetectorMonotonicity` | 3 methods |
| Tokenizer | `test_emotion_detector.py::TestEmotionDetectorTokenize` | 4 methods |
| IPC round-trip | `test_emotion_detector.py::TestEmotionDetectorIPC` | 4 methods |
| Emoji handling | `test_emotion_detector.py::TestEmotionDetectorEmoji` | 5 methods |
| Concurrent | `test_emotion_detector.py::TestEmotionDetectorConcurrent` | 1 method |
| Regex precompile | `test_regex_precompile_perf.py::TestEmotionDetectorRegex` | 1 method |
| Perf benchmark | `test_performance_unit_benchmarks.py::BenchEmotionDetector` | 1 method (80 ms budget) |

**Gap:** No test covers the negation-particle false-positive (F1) or the confidence floor
inconsistency (F5). Tests for `"не знаю"`, `"no problem"`, and the neutral-vs-weak-negative
confidence ordering are missing.

---

## Performance

Measured 100 `detect()` calls on varied 30-150 char texts:

- **Actual:** ~1.0 ms total (0.01 ms/call)
- **CI budget:** 80 ms total (`test_performance_unit_benchmarks.py`)
- **Status:** 80× under budget. No performance concern.

`_RE_WORD_TOKENS` is module-level compiled (line 54). `_match_words` uses a `set` for O(1)
dedup. All operations are O(n) in text length with tiny constant factors.

---

## Output Schema

`EmotionResult` dataclass fields:

| Field | Type | Description |
|-------|------|-------------|
| `primary_emotion` | `str` | One of: `neutral`, `positive`, `negative`, `excited`, `frustrated`, `questioning` |
| `confidence` | `float` | 0.0–1.0 (rounded to 3dp). Neutral-empty always 0.0; neutral-low-score 0.5 |
| `indicators` | `list[str]` | Matched words + punctuation tags (e.g., `"exclamation_marks:2"`, `"caps_ratio:0.85"`) |
| `exclamation_count` | `int` | Count of ASCII `!` |
| `question_count` | `int` | Count of ASCII `?` |
| `caps_ratio` | `float` | Fraction of uppercase letters among all letters (0.0–1.0, rounded to 3dp) |

IPC response from `detect_emotion` serialises all six fields directly (no transformations).

---

## Locale Coverage

| Language | Keyword coverage | Notes |
|----------|-----------------|-------|
| Russian (`ru`) | 23 negative + 23 positive | F1 applies: `не`/`нет` are false-positive triggers |
| Spanish (`es`) | 18 negative + 15 positive | F3 applies: `sí` included; ¡¿ not counted (F4) |
| English (`en`) | 16 negative + 15 positive | F1+F3 apply: `no`/`never`/`yes` included |
| Any other locale | Silently falls back to EN | Documented in code; tested |
