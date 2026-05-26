# Audit: CodeSwitchingDetector — Wave 1074

**Date:** 2026-05-26
**File:** `KrabEar/core/code_switching_detector.py` (181 lines)
**Wire:** `KrabEar/core/transcript_context.py` → `build_initial_prompt()`
**Tests:** `KrabEar/tests/test_code_switching.py` (23 tests, all pass)

---

## Summary

The `CodeSwitchingDetector` is a heuristic module that identifies mid-sentence
language switching between Cyrillic (RU) and Latin-script (EN/ES) tokens, then
injects a Whisper `initial_prompt` hint to improve STT accuracy on mixed text.
It is wired correctly and functional. Five findings were identified, two of which
are meaningful accuracy gaps.

---

## Findings

### F1 — All-caps abbreviations misclassified as Latin (MEDIUM)

**Scope:** `_classify_word()`, `_TECH_TOKEN_RE`

All-caps tokens (API, PR, OK, SDK, UI, UX, IDE, etc.) do not match the tech-token
regex (`_TECH_TOKEN_RE`) because the camelCase branch requires **both** uppercase
and lowercase letters. An all-caps word has only uppercase, so it falls through to
`_LATIN_RE` and is counted as a foreign Latin word.

Concrete impact:

| Input text | `is_mixed` | `switch_ratio` |
|---|---|---|
| `"нажми OK чтобы подтвердить"` | True | 0.25 |
| `"обновляй API чаще"` | True | 0.33 |
| `"открой IDE и создай PR"` | True | 0.40 |
| `"смотри в UI всё нормально"` | True | 0.20 |

These are normal Russian sentences used daily by developers. Triggering the
code-switching hint in Whisper on such inputs adds unnecessary noise to the
`initial_prompt`. The fix is to add an all-caps allowlist pattern to
`_TECH_TOKEN_RE`:

```python
# All-caps abbreviations (2-6 chars): API, PR, OK, SDK, UI, UX, IDE, etc.
[A-Z]{2,6} |
```

---

### F2 — Spanish not distinguished from English in output schema (LOW)

**Scope:** `analyze()` return value, CLAUDE.md documentation

CLAUDE.md states the module "detect mid-sentence language switches
(RU↔ES↔EN)". In reality:

- The module operates on **script**, not language: Cyrillic vs Latin.
- `secondary_lang` is hardcoded to `"en"` when Latin is detected, even if the
  Latin text is Spanish (e.g., `amigo`, `mucho`).
- Spanish accented characters (á, é, ñ, etc.) are non-Cyrillic and are thus
  treated the same as English.

This is not a runtime bug — the Whisper hint is identical for both cases and
the STT improvement goal is still served. However, the `secondary_lang` field
misleads callers that inspect it (e.g., for analytics). CLAUDE.md description
should be corrected to "RU↔Latin (EN/ES conflated)".

---

### F3 — `_detector_cache` not concurrency-safe for multiple thresholds (LOW)

**Scope:** `_get_detector()` in `transcript_context.py`

```python
_detector_cache: "CodeSwitchingDetector | None" = None  # module-level global

def _get_detector(threshold: float = 0.1) -> "CodeSwitchingDetector":
    global _detector_cache
    if _detector_cache is None or _detector_cache._threshold != threshold:
        _detector_cache = CodeSwitchingDetector(switch_threshold=threshold)
    return _detector_cache
```

Two concurrent callers with different `threshold` values will race: one caller
may receive a detector configured for the other's threshold. The CPython GIL
prevents memory corruption, but correctness is not guaranteed.

In practice `build_initial_prompt` is always called with `threshold=0.1` (the
default from `DEFAULT_SETTINGS`), so this is latent. The fix is a simple
`threading.Lock()` guard or converting the cache to a `functools.lru_cache`.

---

### F4 — Detection limited to last history item only (LOW)

**Scope:** `transcript_context.py:172-182`

```python
if code_switching_detect and history_items:
    last_item = history_items[0]  # newest item only
    ...
    cs_result = det.analyze(last_text)
```

The full `combined` context string (built from up to 10 recent items within
30 min) is not analyzed. Only the single most-recent item is checked. If the
most recent transcription happens to be pure Russian while earlier items in the
session were mixed, no hint is injected.

This conservative choice avoids false positives from historical items, but it
may under-detect sustained code-switching patterns spanning multiple recordings.
Acceptable as-is; worth noting for future tuning.

---

### F5 — No test coverage for all-caps abbreviation classification (LOW)

**Scope:** `KrabEar/tests/test_code_switching.py`

The 23 existing tests cover: pure RU, pure EN, mixed 30%, below-threshold 5%,
camelCase exclusion, snake_case exclusion, URL exclusion, `build_initial_prompt`
hook (hint present/absent/disabled), and Wave 112 edge cases.

None of the tests exercise all-caps abbreviations (API, OK, PR, UI) — the
category identified in F1 as producing false positives. The `test_tech_tokens_excluded`
test only checks camelCase (`MacBook`) and URL (`https://apple.com`), which
correctly return `None`.

A gap test for F1:

```python
def test_all_caps_abbreviations_excluded(self) -> None:
    """API, OK, PR should not inflate Latin count in RU text."""
    result = self.det.analyze("нажми OK чтобы подтвердить")
    # Currently FAILS: is_mixed=True, ratio=0.25
    self.assertFalse(result["is_mixed"])
```

---

## Correct behaviour confirmed

| Aspect | Status |
|---|---|
| Wire status | Wired via `transcript_context.build_initial_prompt()` |
| Config exposure | `stt_code_switching_detect` + `stt_code_switching_threshold` in `DEFAULT_SETTINGS` |
| camelCase exclusion | Correct (`MacBook`, `iPhone`, `GitHub` → None) |
| snake_case exclusion | Correct (`get_user_name`, `my_variable` → None) |
| URL exclusion | Correct (`https://...`, `domain.tld` → None) |
| Hex hash exclusion | Correct (7+ hex chars → None) |
| Threshold boundary | `>=` inclusive at 10%: 1/10 words → `is_mixed=True` |
| `switch_ratio` rounding | 4 decimal places via `round(x, 4)` |
| Newline/tab handling | `text.split()` handles all whitespace correctly |
| Performance | ~0.24 ms/call on 120-word text — negligible |
| Output schema | `{is_mixed: bool, primary_lang: str, secondary_lang: str|None, switch_ratio: float}` |

---

## Recommended actions

| Priority | Action |
|---|---|
| MEDIUM | Add `[A-Z]{2,6}` branch to `_TECH_TOKEN_RE` to exclude all-caps abbreviations |
| LOW | Add failing test for `test_all_caps_abbreviations_excluded` (documents F1) |
| LOW | Correct CLAUDE.md: "RU↔ES↔EN" → "RU↔Latin (EN/ES not distinguished)" |
| INFO | `_detector_cache` race is latent only; no action needed unless threshold changes at runtime |
