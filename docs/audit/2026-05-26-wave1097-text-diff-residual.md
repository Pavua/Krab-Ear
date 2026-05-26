# Wave 1097 — TextDiff Residual Audit

**Date:** 2026-05-26  
**File:** `KrabEar/core/text_diff.py`  
**Branch:** `audit-text-diff-W1097`  
**Scope:** Residual issues not covered by W1000 audit  
**Method:** Static analysis + runtime probing via Python REPL

---

## Context

`TextDiffAnalyzer.compute_diff()` produces word-level diffs between two text strings.
Wired in `engine.py` line 998 (LLM rewrite path) and exposed via `get_last_llm_diff` IPC handler
(`service.py:2575`). `transcript_versioning.py` uses `difflib.unified_diff` independently —
it does **not** call `TextDiffAnalyzer`.

W1000 audit confirmed: basic correctness (equal/replace/delete/insert opcodes), empty-string
handling, Unicode acceptance. 34 tests pass.

---

## Findings (5 NEW, not covered by W1000)

---

### F1 — MEDIUM: `position` field uses split coordinate spaces (undefined behavior for consumers)

**File:** `core/text_diff.py` lines 68, 74, 78, 83, 87  
**Severity:** MEDIUM — silent data bug, no crash, wrong positions delivered over IPC

`DiffChange.position` is documented as "позиция (индекс слова) в соответствующей строке"
but the implementation uses **two different index spaces** without distinguishing them:

- `"unchanged"` and `"removed"` → index into `orig_words` (`i1 + k`)
- `"added"` → index into `new_words` (`j1 + k`)

Observed at runtime:

```
compute_diff('cat sat mat', 'NEW cat sat here mat')
→ added  "NEW"  pos=0   # new_words index 0
→ unchanged "cat" pos=0  # orig_words index 0  ← COLLISION
→ unchanged "sat" pos=1
→ added  "here" pos=3   # new_words index 3
→ unchanged "mat" pos=2  # orig_words index 2
```

Two different changes at position=0 (different coordinate spaces). A consumer rendering
a side-by-side diff panel or highlight overlay cannot map positions without knowing the type.

The IPC response (`service.py:2590`) serializes this as-is:
```json
{"type": "added", "text": "NEW", "position": 0}
{"type": "unchanged", "text": "cat", "position": 0}
```

**Fix:** Add a `source` field (`"orig"` / `"new"`) to `DiffChange`, OR split into
`orig_position` / `new_position` on the dataclass. Alternatively document the existing
behavior explicitly in the docstring so consumers can trust it.

---

### F2 — LOW: Punctuation attached to words causes phantom word-level diffs for RU/ES text

**File:** `core/text_diff.py` line 51  
**Severity:** LOW — inflated diff stats for the primary (RU/ES) use case

`str.split()` is punctuation-unaware. A common LLM rewrite operation is adding/removing
punctuation without changing actual words, e.g.:

```
original:  "Добрый день, как дела?"
rewritten: "Добрый день как дела"
```

This produces `words_removed=2, words_added=0` because `"день,"` ≠ `"день"` and
`"дела?"` ≠ `"дела"` at the token level. The **similarity_ratio** (computed on raw chars)
stays high, but **words_removed/added** counters are misleading — a "0 word change" rewrite
appears as "removed 2 words".

This is the primary use case: LLM rewrites often add/remove punctuation. The summary string
"LLM removed 2 words, 92% similar" misrepresents a punctuation-only change.

**Fix (optional):** Strip punctuation tokens before word-level comparison, or add a
`punctuation_changes: int` counter. A simple approach:
```python
import re
def _clean_word(w: str) -> str:
    return re.sub(r"^[\W_]+|[\W_]+$", "", w, flags=re.UNICODE)
```
Mark as a known limitation in the docstring if not fixing.

---

### F3 — LOW: `_build_summary` prefix "LLM" is domain-specific and leaks into the generic utility

**File:** `core/text_diff.py` lines 108–116  
**Severity:** LOW — coupling / reuse concern

The `_build_summary` method hardcodes the string "LLM" as a prefix:
```python
return f"LLM {change_str}, {pct}% similar"
```

`TextDiffAnalyzer` is imported by `engine.py` for LLM-rewrite diffs, but the class name
(`TextDiffAnalyzer`) and module (`core/text_diff.py`) are generic. If this utility is reused
for diarization output comparison, transcript versioning, or AB-testing different STT models,
the "LLM" prefix is semantically wrong.

`transcript_versioning.py` already uses `difflib.unified_diff` directly (avoiding this module)
— possibly because of this coupling.

**Fix:** Accept an optional `prefix: str = "LLM"` parameter in `_build_summary` and pass
it through from `compute_diff`. Current callers remain unaffected (default="LLM").

---

### F4 — LOW: `changes` list grows unbounded for large inputs; no size cap

**File:** `core/text_diff.py` lines 60–89  
**Severity:** LOW — memory / IPC payload concern

The `changes` list contains one `DiffChange` per word. A 50k-word transcription produces
~26 000 `DiffChange` objects. These are serialized in full by the IPC handler at
`service.py:2589–2592` and sent over the Unix socket.

Observed at runtime: 50k-word diff → `len(result.changes) = 26 000`, IPC payload would
be ~1–2 MB of JSON. The IPC server has a default max-message-bytes limit (see
`backend/ipc_constants.py`); a very large diff could exceed it silently (message truncation).

LLM rewrites in practice operate on transcript chunks (<1k words), so the real-world risk
is low. However, if a large file import triggers `compute_diff` (e.g., via bulk-reprocess),
this could manifest.

**Fix:** Add an optional `max_changes: int = 10_000` cap, truncating the list and setting a
`truncated: bool` flag on `TextDiffResult`. Alternatively, document the size bound assumption
in the class docstring.

---

### F5 — LOW: `DiffChange.type` is an unvalidated string, not an enum

**File:** `core/text_diff.py` lines 17–19  
**Severity:** LOW — type safety / future-proofing

```python
@dataclass
class DiffChange:
    type: str  # "added" | "removed" | "unchanged"
```

The three valid values are documented in a comment, but there is no `Literal` type annotation
or `Enum` to enforce them. Any code that receives a `DiffChange` (e.g., a Swift IPC consumer
parsing the JSON) relies on the comment contract.

The IPC serialization at `service.py:2590` emits `c.type` directly with no validation.
A future code change that misspells one of the opcodes (e.g., `"delete"` instead of
`"removed"`) would produce a broken schema without any error at the Python layer.

**Fix:** Use `typing.Literal`:
```python
from typing import Literal
type: Literal["added", "removed", "unchanged"]
```
Or define a `DiffChangeType` `StrEnum` (Python 3.11+). This also improves IDE autocomplete
and static analysis coverage.

---

## Test Coverage Gaps

The existing 34 tests cover: identical, empty, word replace/insert/delete, Unicode (RU/CJK),
whitespace normalization, concurrency, summary format, and partial edits.

**Uncovered scenarios:**
- No test for `position` field correctness on an insert-at-start case (F1)
- No test for punctuation-attached words (e.g., `"дела?"` vs `"дела"`) triggering
  unexpected word diffs (F2)
- No test for large inputs (>1000 words) — performance regression guard (F4)
- No test that `summary` does NOT start with "LLM" when texts are identical (F3 cross-check)

---

## Wire Status

- `compute_diff` is wired and active in `engine.py:998` (LLM rewrite path, production)
- `get_last_llm_diff` IPC handler at `service.py:2575` exposes the full `changes` list
- `transcript_versioning.py` uses `difflib.unified_diff` independently — NOT wired to `TextDiffAnalyzer`
- No calls in `backend/transcript_versioning.py`, `backend/history_service.py`, or Swift

---

## Performance

Tested at runtime:
- 10k words (mixed orig/rewritten): **0.011s** — no concern
- 50k words: **0.051s** — acceptable, but serialization of 26k `DiffChange` objects may
  cause IPC payload size issues (F4)
- 10k identical words: **0.021s** — `SequenceMatcher` O(n²) worst case not triggered because
  the autojunk heuristic activates for >200 matching elements

---

## Summary Table

| ID | Severity | Title |
|----|----------|-------|
| F1 | MEDIUM | `position` uses split coord spaces (orig vs new) |
| F2 | LOW | Punctuation-attached tokens inflate word diff counts (RU/ES) |
| F3 | LOW | Hardcoded "LLM" prefix limits reusability |
| F4 | LOW | Unbounded `changes` list; large-input IPC payload risk |
| F5 | LOW | `DiffChange.type` is unvalidated `str`, not `Literal`/`Enum` |
