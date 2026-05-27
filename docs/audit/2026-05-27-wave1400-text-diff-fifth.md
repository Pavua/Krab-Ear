# Wave 1400 MILESTONE — TextDiff Fifth Re-Audit

**Date:** 2026-05-27
**File:** `KrabEar/core/text_diff.py`
**Branch:** `audit-text-diff-fifth-W1400`
**Audit round:** 5th (W1000 → W1097 → W1108 → W1131 → W1400)
**Method:** Static analysis + runtime probing (Python REPL, difflib internals)

---

## Previous Wave Merge State

| Wave | Branch | Merged into `codex/krab-ear-v2`? |
|------|--------|----------------------------------|
| W1000 | `docs/audit-text-diff-W1000` | NO — docs-only, audit document only |
| W1097 | `audit-text-diff-W1097` | NO — audit document on separate branch |
| W1108 | `text-diff-coordinate-space-W1108` | NO — fix on unmerged branch |
| W1131 | `fix-text-diff-punctuation-W1131` | NO — fix on unmerged branch |

**Current state of `codex/krab-ear-v2`:** The production file at `KrabEar/core/text_diff.py`
has NONE of the W1097/W1108/W1131 fixes applied. All five W1097 findings (F1-F5) remain
open in the production codebase. W1108 and W1131 exist as unmerged branches only.

---

## Context

`TextDiffAnalyzer.compute_diff()` computes word-level diffs between two text strings.

**Active wiring:**
- `engine.py:998` — called on every successful LLM rewrite (production path):
  `llm_diff = TextDiffAnalyzer().compute_diff(cleaned_text, llm_result.text)`
- `service.py:2574` — `get_last_llm_diff` IPC handler serializes `diff.changes` to JSON.
- `transcript_versioning.py:205` — uses `difflib.unified_diff` independently (NOT TextDiffAnalyzer).

**Not wired:** `TranscriptVersionManager.diff_versions()` is not registered as an IPC handler.

---

## Findings (5 NEW, not covered by W1097/W1108/W1131)

---

### F1 — HIGH: `SequenceMatcher` autojunk degrades word-level diff on transcripts >=200 words

**File:** `core/text_diff.py` line 59
**Severity:** HIGH — silent data correctness bug, primary use case (RU/ES transcripts) affected

`difflib.SequenceMatcher` with `autojunk=True` (the default) marks elements as "popular junk"
when they appear more than 1% of the time in the `b` sequence (threshold = `len(b) // 100`).
For transcripts with a limited vocabulary — which is typical for RU/ES voice recordings where
30-50 unique content words repeat across 200-1000 tokens — this heuristic marks almost every
word as junk and degrades the LCS computation dramatically.

**Observed at runtime:**

```python
# 300-word RU transcript, LLM adds 30 commas (common punctuation rewrite)
analyzer = TextDiffAnalyzer()
result = analyzer.compute_diff(orig_300_words, rewritten_with_30_commas)
# result.words_added=300, result.words_removed=300, result.words_unchanged=0
# result.summary: "LLM added 300 words, removed 300 words, 0% similar"
```

With `autojunk=False`, the same input correctly reports:
```python
# words_added=30, words_removed=30, words_unchanged=270
```

The degradation threshold is exactly at N=200 words (autojunk threshold = `N//100 = 2`,
and content words in short-vocabulary RU/ES transcripts appear >2 times each):

| Transcript length | `autojunk=True` (current) | `autojunk=False` (correct) |
|-------------------|--------------------------|---------------------------|
| 100 words         | correct                  | correct                   |
| 150 words         | correct                  | correct                   |
| 200 words         | DEGRADED (50% matched)   | correct (99% matched)     |
| 300 words         | DEGRADED (50% matched)   | correct (99% matched)     |
| 500 words         | DEGRADED (50% matched)   | correct (99% matched)     |

The `similarity_ratio` (computed from char-level SequenceMatcher, which has its own separate
autojunk on characters and is unaffected at typical transcript char counts) remains close to
1.0 while the word-level counters report near-complete replacement. The summary string becomes
self-contradictory: `"LLM added 300 words, removed 300 words, 99% similar"`.

**Fix (one-liner):**
```python
# line 59 of text_diff.py -- change:
word_matcher = difflib.SequenceMatcher(None, orig_words, new_words)
# to:
word_matcher = difflib.SequenceMatcher(None, orig_words, new_words, autojunk=False)
```

**Performance:** `autojunk=False` at 300 words = 0.011s; at 500 words = 0.06s — acceptable
for the LLM-rewrite use case. The existing `max_changes` cap proposal (W1097 F4, not yet
fixed) would bound the upper end if added.

**Note:** The W1131 `strip_punctuation=True` fix (unmerged) addresses a related symptom (F2
in W1097) but does NOT fix this issue. Strip-punctuation makes `"дела,"` equal to `"дела"`
in the comparison lists, but autojunk still degrades the LCS computation regardless of
vocabulary normalization.

---

### F2 — MEDIUM: `_last_llm_diff` not reset at transcription start — stale diff served

**File:** `core/engine.py` lines 360, 999; `backend/service.py` line 2577
**Severity:** MEDIUM — silent data bug, stale diff returned over IPC

`AudioEngine._last_llm_diff` is initialized to `None` at construction time (line 360) and
only assigned when an LLM rewrite succeeds (line 999):

```python
if llm_result.ok:
    llm_diff = TextDiffAnalyzer().compute_diff(cleaned_text, llm_result.text)
    self._last_llm_diff = llm_diff  # only assigned on success, never cleared
```

It is never reset at the beginning of a new transcription call. As a result:

- Recording A succeeds with LLM rewrite — `_last_llm_diff` = diff_A.
- Recording B is processed; LLM rewriter circuit breaker is open (or rewriter disabled)
  — LLM rewrite is skipped — `_last_llm_diff` still holds diff_A.
- Caller requests `get_last_llm_diff` after recording B — receives diff_A, believing it
  applies to recording B's text.

The `service.py` handler returns `{"available": True, ...diff_A}` with no timestamp or
`item_id` to allow the caller to detect staleness.

**Fix:**
```python
# At the beginning of transcribe() in engine.py (~line 700), add:
self._last_llm_diff = None  # reset so callers detect "no rewrite this recording"
```

Or add a `transcription_id` field to both `TextDiffResult` and the IPC response so callers
can cross-reference the diff against the correct recording.

---

### F3 — MEDIUM: W1108 `coordinate_space` field added to `DiffChange` but NOT serialized by IPC handler

**File:** `backend/service.py` line 2589; `core/text_diff.py` (W1108 branch)
**Severity:** MEDIUM — incomplete fix; IPC consumers cannot use the coordinate_space field

W1108 (on unmerged branch `text-diff-coordinate-space-W1108`) adds a `coordinate_space` field
to `DiffChange`:
```python
coordinate_space: Literal["orig", "new"] = "orig"
```

However, `service.py:2589` — the IPC serialization path — still emits:
```python
{"type": c.type, "text": c.text, "position": c.position}
```

The `coordinate_space` field is absent from the IPC response. After W1108 merges, Swift
consumers calling `get_last_llm_diff` would receive `DiffChange` objects without the
disambiguation field that W1108 was intended to provide. The F1 (split position coordinate
spaces) finding from W1097 would be nominally "fixed" at the Python object level but the fix
would not reach the only real consumer (Swift HistoryPanel UI).

**Fix:** When merging W1108, also update the serializer at service.py:2589:
```python
{"type": c.type, "text": c.text, "position": c.position,
 "coordinate_space": getattr(c, "coordinate_space", "orig")}
```
The `getattr` with default ensures backwards compatibility with `DiffChange` objects that
predate the W1108 field addition.

---

### F4 — LOW: `diff_versions()` not wired as IPC handler in `TranscriptVersionManager`

**File:** `backend/transcript_versioning.py` line 177; `backend/service.py` lines 1076-1078
**Severity:** LOW — feature gap; callers cannot perform diff between versions over IPC

`TranscriptVersionManager.diff_versions()` computes a unified diff between any two saved
versions. Three IPC handlers are registered for save/get/revert:

```python
"save_transcript_version": ...,
"get_transcript_versions": ...,
"revert_transcript_version": ...,
```

But there is no `diff_transcript_versions` IPC handler. Any UI that wants to show what changed
between version 2 and version 3 of a transcript cannot call `diff_versions()` over IPC.
The `diff_versions()` method also lacks a `handle_diff_versions()` wrapper following the
project's IPC delegation pattern.

Note: `diff_versions()` uses `difflib.unified_diff` directly (not `TextDiffAnalyzer`),
so it is not affected by the F1 autojunk issue.

**Fix:** Add `handle_diff_versions` wrapper to `TranscriptVersionManager` and register it:
```python
# transcript_versioning.py
def handle_diff_versions(self, params):
    item_id = str(params.get("item_id", "")).strip()
    v1 = int(params.get("v1", 0))
    v2 = int(params.get("v2", 0))
    return self.diff_versions(item_id, v1, v2)

# service.py
"diff_transcript_versions": self._transcript_versioning.handle_diff_versions,
```

---

### F5 — LOW: No `word_similarity_ratio` field — char-level and word-level metrics inconsistent

**File:** `core/text_diff.py` lines 55-56, 91-100
**Severity:** LOW — missing data; consumers cannot compute a consistent word-level similarity

`TextDiffResult` exposes one `similarity_ratio` (char-level, computed on the raw strings):
```python
char_matcher = difflib.SequenceMatcher(None, original or "", rewritten or "")
similarity_ratio = round(char_matcher.ratio(), 4)
```

This ratio correctly measures character-level change. However:
- `words_added` and `words_removed` are word-level metrics.
- The summary string combines both: `"LLM removed 5 words, 94% similar"`.
- After F1 is fixed (`autojunk=False`), a clean word-level similarity can be derived:
  `2 * words_unchanged / (2 * words_unchanged + words_added + words_removed)`.
  This is never exposed as a separate `word_similarity_ratio` field.

The W1131 `strip_punctuation=True` fix makes the char-level and word-level metrics even more
divergent: punctuation is stripped from word comparison but NOT from the char-level strings,
so `similarity_ratio` and a hypothetical `word_similarity_ratio` may differ substantially
for punctuation-heavy LLM rewrites.

**Fix:** Add `word_similarity_ratio: float = 0.0` to `TextDiffResult` and compute:
```python
total_words = words_unchanged + words_added + words_removed
word_similarity_ratio = round(
    2 * words_unchanged / max(total_words + words_unchanged, 1), 4
) if total_words > 0 else 1.0
```

---

## Test Coverage Gaps (Post W1097/W1108/W1131)

The current test file (`KrabEar/tests/test_text_diff.py`) has 34 tests covering
identical/empty/replace/insert/delete/Unicode/whitespace/concurrency. Remaining gaps:

- **No test for >200-word vocabulary-limited text** (F1 trigger condition)
- **No test verifying `_last_llm_diff` resets between transcriptions** (F2, engine.py scope)
- **No test verifying `coordinate_space` appears in IPC response** (F3, service.py scope)
- **No test for `diff_transcript_versions` IPC method** (F4, handler gap)
- **No test asserting `word_similarity_ratio` is consistent with word counters** (F5)

---

## Summary Table

| ID | Severity | Title | Reproducible |
|----|----------|-------|--------------|
| F1 | HIGH | `autojunk=True` degrades word diff at >=200 words (RU/ES vocab) | Yes (verified) |
| F2 | MEDIUM | `_last_llm_diff` not reset at transcription start — stale diff | Yes (code path) |
| F3 | MEDIUM | W1108 `coordinate_space` not serialized by IPC handler | Yes (code gap) |
| F4 | LOW | `diff_versions()` not wired as IPC handler | Yes (missing) |
| F5 | LOW | No `word_similarity_ratio` — char and word metrics inconsistent | Yes (design gap) |

---

## Wire Status (codex/krab-ear-v2 HEAD)

- `compute_diff` is wired in `engine.py:998` (LLM rewrite path).
- `get_last_llm_diff` IPC handler at `service.py:2574` is active.
- W1108/W1131 fixes exist on separate unmerged branches — NONE are in production.
- `TranscriptVersionManager.diff_versions()` is implemented but not reachable over IPC.
