# Wave 873 — Context Memory & Transcript Context Audit

**Files audited:**
- `KrabEar/core/context_memory.py` — `ContextMemory` sliding-window class
- `KrabEar/core/transcript_context.py` — `build_initial_prompt()` function

**Date:** 2026-05-26
**Auditor:** Wave 873

---

## 1. Window Correctness

### ContextMemory (`context_memory.py`)

**Implementation:** `deque(maxlen=window_size)` for `_texts` and `_word_lists`, plus a manual `Counter` (`_word_counter`) that is kept in sync via explicit subtraction on eviction.

**Finding W873-1 (LOW): Off-by-one in eviction check — functionally harmless**

```python
# Line 156
if len(self._word_lists) == self._window_size:
    evicted = self._word_lists[0]  # будет вытеснен при append
```

The eviction guard checks `len == window_size` before the `deque.append()`. This is correct because `deque(maxlen=N)` does NOT auto-evict until the append call itself. However, the comment "будет вытеснен при append" could mislead future editors into thinking the access is post-eviction. The logic is correct but the manual counter bookkeeping duplicates what `deque` does internally, creating unnecessary coupling. If someone ever changes `maxlen` without updating the guard, the counter desync would be silent.

**Recommendation:** Add an assertion or unit test that verifies `len(self._word_lists) == len(self._texts)` after every operation (invariant test).

**Finding W873-2 (INFO): `_texts` deque is maintained but never read internally**

`self._texts` stores raw transcript strings but `get_context_words()`, `get_recent_topics()`, and `to_dict()` all operate on `_word_lists` and `_word_counter`. The `_texts` deque exists solely for the `size()` method. This is a minor memory waste: window_size=50 transcripts × average 200 chars = ~10 KB overhead, negligible in practice but worth noting.

### `build_initial_prompt()` (`transcript_context.py`)

**Finding W873-3 (MEDIUM): `max_words=0` silently no-ops instead of enforcing empty**

```python
# Lines 144-147
words = combined.split()
if len(words) > max_words:
    words = words[-max_words:]
combined = " ".join(words)
```

When `max_words=0`, `len(words) > 0` is `True` for any non-empty text, so `words = words[-0:]` evaluates to the **full list** (`words[-0:]` in Python equals `words[0:]`). The `max_words=0` case therefore does NOT truncate; the full combined text is emitted under `Previous transcript:`. The existing test at line 268 explicitly acknowledges this with a comment ("just checks we don't crash") and does not assert the prompt is empty.

**Recommendation:** Guard with `if max_words > 0` before the truncation block, or document that `max_words=0` means "no limit" rather than "empty transcript". Current behavior is confusing — the parameter name implies a hard cap.

---

## 2. Prompt Budget (224-token constraint)

Whisper's `initial_prompt` is hard-capped at **224 tokens** by the model tokenizer. Beyond that, tokens are silently dropped (no error). The codebase does **not** use tiktoken or any token counter.

**Finding W873-4 (MEDIUM): Word-count proxy underestimates token cost for Cyrillic/emoji**

`build_initial_prompt` truncates to `max_words=250` words. For English, 250 words ≈ 330 tokens (acceptable headroom). For Russian, CJK, or Arabic text, the ratio is roughly 1 word = 2–4 tokens due to subword tokenization. A 250-word Cyrillic history context could easily exceed 224 tokens and be silently truncated by Whisper.

Additionally the engine prepends `context_suffix` before its own `TRANSCRIBE_PROMPT + domain_desc` string (line 741 in `engine.py`):

```python
dynamic_prompt = f"{context_suffix} {dynamic_prompt}"
```

`TRANSCRIBE_PROMPT` alone is an unknown-length user-configurable string. Total prompt budget available for history context is therefore **unpredictable** — it is whatever remains after `TRANSCRIBE_PROMPT + domain_desc + Ключевые слова` are accounted for.

**Finding W873-5 (LOW): `_MAX_COMBINED_TERMS = 250` and `max_words: int = 250` are unrelated constants with the same magic number**

The module-level constant `_MAX_COMBINED_TERMS: int = 250` (line 25) limits Glossary terms. The function parameter `max_words: int = 250` (line 86) limits Previous Transcript words. Both use 250 by coincidence, creating a maintenance risk if someone adjusts one thinking they are the same.

**Recommendation:** Rename constant to `_MAX_GLOSSARY_TERMS = 250` to differentiate semantics.

---

## 3. Vocabulary Merging

**`build_initial_prompt()` merge logic (lines 152–166):**

```python
for w in list(hotwords or []) + list(auto_glossary or []):
    w = w.strip()
    if not w:
        continue
    key = w.lower()
    if key not in seen_lower:
        seen_lower.add(key)
        combined_terms.append(w)
    if len(combined_terms) >= _MAX_COMBINED_TERMS:
        break
```

**Finding W873-6 (INFO): Case-insensitive dedup preserves hotwords priority correctly**

Hotwords are processed first; auto_glossary duplicates are dropped. This is the documented contract. The implementation is correct.

**Finding W873-7 (INFO): `build_initial_prompt()` does not receive `ContextMemory.get_context_words()` output**

The engine call at `engine.py:736-739` passes `hotwords=stt_hotwords` which is the combination of user-configured hotwords + auto_glossary. It does **not** pass `ContextMemory.get_context_words()`. `ContextMemory` is updated via `recording_core_service.py:1134` after transcription completes, and it is exposed to Swift via the `get_context_memory` IPC method — but its extracted words are never fed back into `build_initial_prompt()` as a third vocabulary source. The two context mechanisms (`ContextMemory` word extraction and `build_initial_prompt` history-text context) are parallel, not integrated.

**Recommendation (optional enhancement):** Pass `ContextMemory.get_context_words()` as a third tier (after `auto_glossary`) so high-frequency terms from recent transcriptions boost the Glossary. Currently this intelligence is unused at inference time.

---

## 4. Concurrency

### ContextMemory

**Finding W873-8 (MEDIUM): Race window between eviction check and deque.append()**

```python
# Lines 155-166 (inside with self._lock)
if len(self._word_lists) == self._window_size:
    evicted = self._word_lists[0]
    for w in evicted:
        self._word_counter[key] -= 1
        ...

self._texts.append(text)         # ← deque auto-evicts here
self._word_lists.append(words)   # ← deque auto-evicts here
```

All operations are under `self._lock` (RLock), so **there is no actual race** between threads. However, there is a logical subtlety: after `self._texts.append(text)`, the deque has evicted its oldest text — but `self._word_counter` was already decremented before the append. If an exception is raised between the `_word_counter` update and `_word_lists.append()`, the counter would be permanently desynchronized. This is an extremely unlikely scenario (Counter operations do not raise), but the code lacks any rollback / consistency guarantee.

**Finding W873-9 (LOW): `to_dict()` calls `get_context_words()` + `get_recent_topics()` while holding the RLock**

```python
def to_dict(self) -> dict:
    with self._lock:
        ...
        context_words = self.get_context_words(max_words=20)   # re-acquires RLock (RLock OK)
        recent_topics = self.get_recent_topics()               # re-acquires RLock
```

This is safe because `threading.RLock` is reentrant. The comment on line 243 (`# RLock позволяет повторный захват из того же потока`) confirms the developer is aware. No issue.

### `transcript_context.py` — `_detector_cache` global

**Finding W873-10 (LOW): `_detector_cache` is a module-level global with unsynchronized write**

```python
_detector_cache: "CodeSwitchingDetector | None" = None

def _get_detector(threshold: float = 0.1) -> "CodeSwitchingDetector":
    global _detector_cache
    if _detector_cache is None or _detector_cache._threshold != threshold:
        _detector_cache = CodeSwitchingDetector(switch_threshold=threshold)
    return _detector_cache
```

In CPython, the GIL makes simple attribute writes effectively atomic, so two threads calling `_get_detector` simultaneously will not corrupt memory. However, if two threads call with different thresholds at the same moment, one could read an incompatible cached instance briefly. Since `build_initial_prompt` is always called with the default `threshold=0.1` in practice (the engine does not vary it), this is a de-facto non-issue — but the function signature allows varying the threshold and the cache would be incorrectly re-used.

**Recommendation:** If multi-threshold support is needed, use a `{threshold: detector}` dict as cache, or lock the check-and-set with a module-level lock.

---

## 5. Summary Table

| ID | Severity | Area | Description |
|----|----------|------|-------------|
| W873-1 | LOW | Window | Eviction guard works but manual counter bookkeeping is fragile if `maxlen` changes |
| W873-2 | INFO | Window | `_texts` deque maintained but only used for `size()` — minor memory overhead |
| W873-3 | MEDIUM | Window | `max_words=0` silently passes full text due to Python `-0` slice semantics |
| W873-4 | MEDIUM | Budget | Word-count proxy underestimates Cyrillic token cost; no tiktoken budget guard |
| W873-5 | LOW | Budget | `_MAX_COMBINED_TERMS=250` and `max_words=250` share magic number, different semantics |
| W873-6 | INFO | Vocab | Hotwords priority dedup is correct |
| W873-7 | INFO | Vocab | `ContextMemory.get_context_words()` is never fed back into `build_initial_prompt()` |
| W873-8 | MEDIUM | Concurrency | Eviction counter update not atomic with deque.append; exception mid-update would desync |
| W873-9 | LOW | Concurrency | `to_dict()` RLock reentrance safe (confirmed by dev comment) |
| W873-10 | LOW | Concurrency | `_detector_cache` global write unsynchronized; harmless under single-threshold usage |

**Total findings: 10 (2 MEDIUM, 4 LOW, 4 INFO)**

---

## 6. Actionable Recommendations (Priority Order)

1. **W873-3 (MEDIUM):** Fix `max_words=0` guard — add `if max_words > 0:` before the truncation block in `build_initial_prompt()`.
2. **W873-4 (MEDIUM):** Add a byte-budget guard. Whisper drops tokens beyond 224 silently. A simple heuristic: `max_chars = 900` (≈224 tokens for Cyrillic assuming 4 chars/token) applied to `combined` before constructing the prompt suffix.
3. **W873-7 (INFO/Enhancement):** Wire `ContextMemory.get_context_words()` as a third-tier vocabulary source in `build_initial_prompt()` to close the gap between the two context systems.
4. **W873-5 (LOW):** Rename `_MAX_COMBINED_TERMS` to `_MAX_GLOSSARY_TERMS` in `transcript_context.py`.
5. **W873-1 (LOW):** Add invariant test asserting `len(_word_lists) == len(_texts)` after concurrent stress tests.

No correctness bugs block production. Findings W873-3 and W873-4 represent silent degradation of STT prompt quality under edge conditions.
