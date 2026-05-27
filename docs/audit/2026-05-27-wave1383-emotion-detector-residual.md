# W1383 — EmotionDetector residual audit (post W1020 + W1370)

**Date:** 2026-05-27  
**Auditor:** W1383 sub-agent  
**Branch audited:** `codex/krab-ear-v2` (HEAD `6c900317`)  
**File audited:** `KrabEar/core/emotion_detector.py`

---

## W1020 / W1370 merge state

| Fix | Commit | Merged into `codex/krab-ear-v2`? |
|-----|--------|----------------------------------|
| W1020 — drop negation particles from sentiment lists | `3000f9bc` | **NO** |
| W1370 — multi-word phrase support (`_NEGATIVE_PHRASES` / `_match_phrases`) | `51d6532d` | **NO** |

Both fixes exist only on their own branches (confirmed by reading `codex/krab-ear-v2` HEAD of
`KrabEar/core/emotion_detector.py`).  The live code still contains `"не"`, `"нет"`, `"no"`,
`"never"`, `"да"`, `"sí"`, `"yes"` as raw sentiment words, and has no `_NEGATIVE_PHRASES` dict.

---

## Findings (5 NEW — beyond W1020/W1370)

### F1 — DEAD CODE: multi-word `"не нравится"` entry in `_NEGATIVE_WORDS` is structurally unreachable — HIGH

**Location:** `KrabEar/core/emotion_detector.py` lines 20, 214–226

`"не нравится"` (with a space) is listed in `_NEGATIVE_WORDS["ru"]`. However, `_match_words`
iterates over tokens produced by `_tokenize`, which uses `_RE_WORD_TOKENS =
re.compile(r"[А-Яа-яёЁA-Za-zÀ-ÿ]+")` — this regex splits on whitespace and punctuation,
so the token `"не нравится"` (containing a space) is **never** generated.

Verified live:
```
tokens("не нравится этот подход") = ['не', 'нравится', 'этот', 'подход']
"не нравится" in tokens → False  (always)
```

Because the entry is dead, the W1370 bug persists even pre-W1370: `"нравится"` still lives in
`_POSITIVE_WORDS["ru"]`, so `detect("не нравится этот подход")` returns `positive`, not
`negative`.

**Fix:** Remove the dead `"не нравится"` entry from `_NEGATIVE_WORDS["ru"]` (it contributes
nothing and creates confusion). The real fix is W1370's `_NEGATIVE_PHRASES` + `_match_phrases`
approach.

---

### F2 — PERF: `_match_words` uses `list` membership check — O(n·m) per call — LOW

**Location:** `KrabEar/core/emotion_detector.py` line 219 (`if token in candidates`)

`candidates` is a Python `list`. Each `in` membership test is O(m) where m = word-list size
(22 words for RU). For a long transcript with 1 400 tokens this is 1 400 × 22 = 30 800
comparisons per `_match_words` call (called twice per `detect`).

Benchmark (10 000 calls × 300 tokens):
```
list lookup: 0.215 s
set  lookup: 0.045 s   (4.8× faster)
```

The current dictionaries are small (≤22 entries) so the absolute cost is ~1 ms/call on M4.
This is acceptable today but will hurt if word lists grow.

**Fix:** Convert `_NEGATIVE_WORDS` / `_POSITIVE_WORDS` values to `frozenset` at module level,
or convert `candidates` to `set` inside `_match_words`.

---

### F3 — FALSE NEGATIVE: technical words `"error"` / `"ошибка"` / `"fail"` skew SentimentTrendAnalyzer — MEDIUM

**Location:** `KrabEar/core/emotion_detector.py` lines 27–31; `KrabEar/backend/sentiment_trends.py` line 100

The EN negative list includes `"error"`, `"fail"`, `"failure"` and the RU list includes
`"ошибка"`. These are neutral technical log terms, not sentiment-bearing words.

Live evidence:
```python
detect("error: connection refused", "en")  →  negative  conf=0.47
detect("error 404 not found", "en")        →  negative  conf=0.47
detect("syntax error in line 42", "en")    →  negative  conf=0.47
```

`SentimentTrendAnalyzer` scores each of these as `−0.7`, skewing the daily and overall
sentiment trends negatively whenever technical logs or diagnostic output are transcribed.
`MetadataEnricher` (line 117) also writes `emotion: "negative"` for all such items, polluting
the history metadata.

**Fix:** Remove `"error"`, `"fail"`, `"failure"`, `"ошибка"` from the negative word lists, or
add a technical-context guard (e.g. if ≥1 digit or URL-pattern in text, skip word-based scoring).

---

### F4 — SILENT TIE: equal pos + neg scores always resolves to `positive` — LOW

**Location:** `KrabEar/core/emotion_detector.py` lines 126–133, 173

When a text contains exactly one positive word and one negative word their scores are both
`0.35 + 0.12×1 = 0.47`. The `scores` dict is created with insertion order
`{"neutral", "positive", "negative", ...}`. Python `max()` returns the first maximum key in
iteration order, so `"positive"` always beats `"negative"` on a tie.

Live evidence:
```python
detect("хорошо плохо", "ru")  →  positive  (not neutral or negative)
```

A genuinely mixed-sentiment transcript (one good thing, one bad thing) is silently classified
as `positive`. There is no `"mixed"` emotion in the result set, and the tie is invisible to
callers.

**Fix:** Add a `"mixed"` score bucket, or detect ties and return `"neutral"` when
`abs(positive_score − negative_score) < 0.05`.

---

### F5 — INTERFACE GAP: `detect()` accepts `str` but `TextPostProcessor.process()` returns `PostProcessResult` — LOW

**Location:** `KrabEar/core/emotion_detector.py` line 105; `KrabEar/core/text_postprocessor.py`

`EmotionDetector.detect(text: str, ...)` calls `text.strip()` on line 105. If a caller passes
the result of `TextPostProcessor.process(raw)` directly (which returns a `PostProcessResult`
dataclass, not `str`), the call crashes at runtime with:

```
AttributeError: 'PostProcessResult' object has no attribute 'strip'
```

Reproduction (Python REPL):
```python
from core.text_postprocessor import TextPostProcessor
from core.emotion_detector import EmotionDetector
pp = TextPostProcessor()
result = pp.process("КАКОЙ ЗАМЕЧАТЕЛЬНЫЙ результат")
EmotionDetector().detect(result, "ru")   # ← crashes
```

No current production call path connects these two directly — `handle_detect_emotion` in
`text_processing_service.py` always calls `str(params.get("text", ""))` which is safe.
However, the type annotation `text: str` is unenforced and any future caller that naively
chains `process()` → `detect()` will hit this silently.

**Fix:** Add `text = text.text if hasattr(text, "text") else str(text)` guard in `detect()`,
or update the type annotation to `text: str | PostProcessResult` and handle both paths.

---

## Test coverage gaps

- No test for equal-score tie resolution (F4).
- No test for technical-word false negatives (`"error 404"` → negative) (F3).
- No regression test asserting `"не нравится"` in `_NEGATIVE_WORDS` is dead (F1).
- `TestEmotionDetectorNegationParticles` (from W1020 branch) is not present in
  `codex/krab-ear-v2` — the W1020 tests were only added alongside the fix commit.

## Priority matrix

| Finding | Severity | Effort |
|---------|----------|--------|
| F1 dead multi-word entry | HIGH | XS (1-line delete) |
| F2 list vs set O(n·m) | LOW | XS (frozenset at module level) |
| F3 technical word false negatives | MEDIUM | S (remove 4 words + test) |
| F4 silent tie resolution | LOW | S (tie-detect + neutral fallback) |
| F5 PostProcessResult crash risk | LOW | XS (guard or annotation) |
| W1020 NOT merged | HIGH (pre-existing) | already fixed on branch |
| W1370 NOT merged | HIGH (pre-existing) | already fixed on branch |
