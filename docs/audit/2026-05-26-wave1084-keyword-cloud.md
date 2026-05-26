# Audit: KeywordCloudGenerator (W1084)

**Date:** 2026-05-26  
**File:** `KrabEar/backend/keyword_cloud.py`  
**Auditor:** W1084 sub-agent

---

## Summary

`KeywordCloudGenerator` is a solid, well-tested module. IPC wiring is correct, output schema
is stable, and 50 unit tests pass. Five findings are catalogued below, ranging from a
functional correctness bug (F1) to design improvements (F2–F5). No critical or HIGH-severity
issues were found.

---

## Findings

### F1 — `max_words=0` (or negative) returns 1 word instead of 0 (MEDIUM)

**File:** `keyword_cloud.py:148`

```python
top_n = counter.most_common(max(1, max_words))
```

The `max(1, max_words)` guard was intended to prevent a `Counter.most_common(0)` edge case,
but it makes `max_words=0` and negative values silently return the single most-frequent word
instead of an empty list. The IPC handler in `analytics_service.py:122` casts the parameter
with `int(params.get("max_words", 100))` without clamping, so a caller passing `max_words=0`
gets unexpected output.

**Fix:** clamp at the handler boundary (`max(0, int(...))`) and return early when
`max_words == 0`:

```python
max_words = max(0, int(params.get("max_words", 100)))
# and in generate_cloud:
if max_words == 0:
    return []
top_n = counter.most_common(max_words)
```

---

### F2 — Linear font-size scaling produces poor visual distribution (LOW)

**File:** `keyword_cloud.py:281–283`

```python
def _scale_font(self, weight: float) -> int:
    span = self._font_size_max - self._font_size_min
    return int(round(self._font_size_min + weight * span))
```

With linear scaling a word that appears 1 time in a corpus where the top word appears 1 000
times gets `weight = 0.001 → font_size = 12 px` — visually indistinguishable from other
rare words. Measured comparison (min=12, max=72):

| count | linear font | log font |
|------:|------------:|---------:|
| 1     | 12 px       | 18 px    |
| 10    | 13 px       | 33 px    |
| 100   | 18 px       | 52 px    |
| 500   | 42 px       | 66 px    |
| 1 000 | 72 px       | 72 px    |

Log-scaling (`math.log(1 + count) / math.log(1 + max_count)`) distributes visual prominence
more evenly. This is a design choice rather than a correctness bug, but it affects UI quality
when history is large.

---

### F3 — No `privacy_mode` guard on `handle_get_keyword_cloud` (LOW)

**File:** `KrabEar/backend/analytics_service.py:121–143`

The handler loads all active history items and extracts word frequencies regardless of
`privacy_mode_enabled`. Other analytics paths (translation, Sentry) check this setting before
processing transcript text. Word clouds derived from transcript text can indirectly expose
sensitive content (e.g. names, places) when privacy mode is active.

The fix follows the existing pattern in `translation_service.py:96`:

```python
if self._settings_service.get_settings().get("privacy_mode_enabled"):
    return {"words": [], "privacy_mode": True}
```

---

### F4 — Russian compound-word fragments leak through tokenization (LOW)

**File:** `keyword_cloud.py:268`

```python
return re.findall(r"[^\W\d_]+", text.lower(), re.UNICODE)
```

The regex splits on hyphens and apostrophes. Common Russian indefinite pronouns
(`что-то → ['что', 'то']`, `как-нибудь → ['как', 'нибудь']`) are split into their parts.
`'то'` and `'что'` are in the stop-word list, but `'нибудь'` (4 chars, passes `_MIN_WORD_LENGTH`)
and `'либо'` are not — they will appear in the word cloud as seemingly meaningful keywords.

Similarly, English contractions (`don't → ['don', 't']`) split into `'don'` (3 chars,
passes filter) which may inflate noise in EN-heavy transcripts. `'t'` is filtered by
min-length, but fragments like `'ve'`, `'re'`, `'ll'` pass.

**Fix options:**
1. Add `нибудь`, `либо`, `ка`, `ve`, `re`, `ll`, `don` to the stop-word lists.
2. Pre-process text to remove hyphens inside words before tokenization.

---

### F5 — `generate_cloud_svg` has no IPC handler (INFO)

The `KeywordCloudGenerator.generate_cloud_svg()` method is implemented and tested but not
exposed via any IPC method. Only `get_keyword_cloud` (returning JSON word data) is wired.
If the Swift GUI ever needs a server-generated SVG (e.g. for export or share), the handler
is missing. This is not a bug but a gap to note for future work.

**Confirmed wire status:**
- `get_keyword_cloud` → `analytics_service.handle_get_keyword_cloud` → `generate_cloud` ✓
- `get_keyword_cloud_svg` → not registered, not reachable via IPC ✗

---

## Coverage & Health

- **Test file:** `KrabEar/tests/test_keyword_cloud.py` — 50 tests, all passing.
- **IPC dispatch test:** `test_analytics_service.py::TestGetKeywordCloud` (4 cases) and
  dispatch invariant at line 480 — all passing.
- **Edge cases covered:** empty list, all-stop-words, single repeated word, max_words,
  language filter, object vs dict items, Unicode (Cyrillic + accented Spanish), SVG
  determinism with seed.
- **Performance:** 5 000 items × 200 words processed in ~0.41 s — acceptable for
  on-demand IPC call; no caching needed at current scale.
- **Output schema:** `{"words": [{"word": str, "count": int, "weight": float, "font_size": int}]}`
  — stable, documented, matches `CloudWord.to_dict()`.
- **XSS safety:** `_tokenize` only matches letter characters; `_escape_xml` provides a
  second layer. No injection risk in SVG output.

---

## Finding Summary

| ID | Severity | Description |
|----|----------|-------------|
| F1 | MEDIUM   | `max_words=0` returns 1 word instead of empty list |
| F2 | LOW      | Linear font-size scaling → poor visual distribution at scale |
| F3 | LOW      | No `privacy_mode_enabled` guard on keyword cloud IPC handler |
| F4 | LOW      | Hyphen-split fragments (`нибудь`, `don`, `ve`) leak into output |
| F5 | INFO     | `generate_cloud_svg` not exposed as IPC handler |
