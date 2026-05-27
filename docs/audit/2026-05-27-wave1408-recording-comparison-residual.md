# Audit W1408 — `recording_comparison.py` residual re-audit (post-W1273)

**Date:** 2026-05-27
**Branch:** `audit-recording-comparison-residual-W1408`
**Base:** `codex/krab-ear-v2` (HEAD `6c900317`)
**File audited:** `KrabEar/backend/recording_comparison.py` (228 lines)
**Prior audit:** W1267 (`docs/audit/2026-05-26-wave1267-recording-comparison-insights.md`)
**Fix commit:** W1273 (`91f0e81e fix(wave1273): recording_comparison min-2 + recording_insights privacy gate`)

---

## W1273 Merge State

**NOT MERGED.** Commit `91f0e81e` exists only on branch `fix-comparison-insights-W1273`.
It is not reachable from `codex/krab-ear-v2` (`git merge-base --is-ancestor` returns false).

Consequence: the min-2 guard (W1267 F1) is absent on the production branch. Passing a
single `item_id` to `compare_recordings` succeeds silently, returning a misleading
`ComparisonView` with a 1×1 similarity matrix `[[1.0]]` and `common_words` containing
the single item's own tokens.

---

## Summary

5 new findings — all distinct from the 5 findings in W1267. No critical severity;
two are MEDIUM, three are LOW.

---

## Findings

### F1 — MEDIUM · `recording_comparison.py` + `service.py` · `compare_recordings` exposes raw transcript text without privacy-mode guard

**Location:** `_handle_compare_recordings` (`service.py` line 2885); `ComparisonView.items` field

**Description:** The IPC handler `_handle_compare_recordings` calls
`RecordingComparison.compare()` and returns `_comparison_view_to_dict(view)`.
The `items` key in the response is `[item.to_dict() for each HistoryItem]`, which is
`dataclasses.asdict(item)` — the full history record including `text`, `source_text`,
`translated_text`, `cleaned_text`, `diarization`, and `audio_path`.

The handler performs **no `privacy_mode_enabled` check**. By contrast, export handlers
(lines 3656, 3703, 3748) all guard with:

```python
if settings.get("privacy_mode_enabled"):
    return {"error": {"code": "privacy_mode", "message": "Экспорт отключён в режиме приватности"}}
```

**Risk:** A caller (Swift agent or REST client) can extract the full transcript text
of any history item via `compare_recordings` even when `privacy_mode_enabled=True`.
This is more severe than W1267 F2 (which was about `recording_insights` computing
aggregated text statistics); here the **verbatim transcript text** is returned directly
in the IPC response.

**Note:** W1267 F2 covered `recording_insights` privacy bypass. This is an independent
finding for `compare_recordings`.

**Fix:** In `_handle_compare_recordings`, add a privacy guard before calling `compare()`:

```python
def _handle_compare_recordings(self, params: dict[str, Any]) -> dict[str, Any]:
    if self._get_runtime_setting("privacy_mode_enabled", False):
        return {"ok": False, "error": "compare_recordings отключён в режиме приватности"}
    item_ids = params.get("item_ids")
    if not isinstance(item_ids, list) or not item_ids:
        raise ValueError("Параметр item_ids обязателен (список строк)")
    view = self._recording_comparison.compare(item_ids=item_ids, store=self.store)
    return _comparison_view_to_dict(view)
```

---

### F2 — MEDIUM · `recording_comparison.py` · Regex `_tokenize()` silently corrupts Spanish (and other Latin-Extended) words

**Location:** `_tokenize()`, line 62–65; `_STOP_WORDS`, lines 22–33

**Description:** The tokenization regex is:

```python
tokens = re.findall(r"[a-zA-Zа-яёА-ЯЁ]+", text.lower())
```

This covers ASCII Latin (EN) and Cyrillic (RU) but **not Latin Extended** (Spanish
accented vowels: `á é í ó ú ü`, `ñ`; French, German, etc.). Spanish accented
characters act as word-split points:

```
'comunicación'  →  ['comunicaci', 'n']     # split at 'ó'
'también'       →  ['tambi', 'n']          # split at 'é'
'están'         →  ['est', 'n']            # split at 'á'
'español'       →  ['espa', 'ol']          # split at 'ñ'
```

The fragments `comunicaci`, `tambi`, `espa` pass `_MIN_WORD_LEN=3` and are stored as
spurious tokens. This corrupts `common_words`, `unique_words_per_item`, similarity
vectors, and TF computation for any Spanish transcript.

`_STOP_WORDS` has an `# EN` block and a `# RU` block but **no `# ES` block**. Common
Spanish function words (`por`, `que`, `con`, `del`, `los`, `las`, `una`, `como`,
`pero`, `sin`, `más`) are not filtered. These appear in `common_words` for unrelated
Spanish transcripts, falsely suggesting shared content.

**Impact:** The project is described as "RU/ES primary, EN secondary" in CLAUDE.md.
Spanish is a primary language; this affects the majority of production ES transcripts.

**Reproduction:**

```python
from backend.recording_comparison import _tokenize
print(_tokenize("comunicación también"))  # {'comunicaci', 'tambi', 'n'} (wrong)
print(_tokenize("hola por favor"))        # {'hola', 'por', 'favor'}  # 'por' is noise
```

**Fix:**

1. Expand the regex character class to include Latin Extended:

```python
tokens = re.findall(r"[a-zA-ZáéíóúüñÁÉÍÓÚÜÑа-яёА-ЯЁ]+", text.lower())
```

2. Add ES stop words:

```python
# ES (añadir a _STOP_WORDS)
"que", "por", "con", "del", "los", "las", "una", "como",
"pero", "sin", "más", "muy", "bien", "hay", "ser", "fue",
"era", "son", "sus", "les", "nos", "eso", "ese", "esta",
```

---

### F3 — LOW · `recording_comparison.py` · Dead code branches remain after W1273 fix

**Location:** `RecordingComparison.compare()`, lines 195–200 and 204

**Description:** W1273 adds `if len(item_ids) < 2: raise ValueError(...)` immediately
after the empty-check. However, the fix commit does **not** remove the dead code that
was written defensively for the `n=1` case:

```python
# Lines 195–200 (current codex/krab-ear-v2, and in W1273):
if n >= 2:
    common_words = sorted(
        set.intersection(*token_sets) if all(token_sets) else set()
    )
else:
    # Для одного элемента нет смысла, но формально возвращаем его слова
    common_words = sorted(token_sets[0]) if token_sets else []   # ← dead

# Line 204:
others = set.union(*(token_sets[j] for j in range(n) if j != i)) if n > 1 else set()
#                                                                  ↑ dead branch
```

After the min-2 guard is merged, `n` will always be `>= 2` at these points. The
`else` branch in `common_words` (line 198–200) and the `if n > 1 else set()` ternary
(line 204) are unreachable. They add maintenance confusion: a reader must trace the
guard to understand why `n < 2` can never reach this code.

**Risk:** None at runtime. Confusion for future maintainers. Flagged here so the
cleanup is not forgotten when W1273 is merged.

**Fix:** After merging W1273:

1. Replace lines 195–200 with the unconditional form:
   ```python
   common_words = sorted(set.intersection(*token_sets) if all(token_sets) else set())
   ```
2. Replace line 204 with:
   ```python
   others = set.union(*(token_sets[j] for j in range(n) if j != i))
   ```

---

### F4 — LOW · `service.py` · `_handle_compare_recordings` does not validate `item_ids` element types

**Location:** `_handle_compare_recordings` (`service.py` line 2885–2891)

**Description:** The handler validates that `item_ids` is a non-empty list:

```python
if not isinstance(item_ids, list) or not item_ids:
    raise ValueError("Параметр item_ids обязателен (список строк)")
```

But it does not verify that each element is a `str`. If a caller sends
`{"item_ids": [123, 456]}` (integers — possible from a JSON client with a type
confusion), the call proceeds to `RecordingComparison.compare()`, which passes the
integers to `store.get_history_item_by_id(123)`. The `StateStore` looks up by string
key, returns `None`, and `compare()` raises:

```
ValueError: Запись с id=123 не найдена
```

This error message is misleading — the caller does not know whether `123` was an
invalid type or a non-existent ID.

**Risk:** Low. JSON over Unix socket makes integer IDs unlikely from the Swift caller;
however, REST callers or test harnesses could trigger this. Error message could
mislead debugging.

**Fix:** Add per-element type check in the handler:

```python
if not all(isinstance(x, str) for x in item_ids):
    raise ValueError("Все элементы item_ids должны быть строками")
```

---

### F5 — LOW · `KrabEar/tests/test_recording_comparison.py` · No test for `privacy_mode_enabled` interaction with `compare_recordings` IPC

**Location:** `RecordingComparisonIPCTestCase` (`test_recording_comparison.py`, lines 167–214)

**Description:** The IPC test class (`RecordingComparisonIPCTestCase`) covers:
- OK response for 2 valid items
- Missing `item_ids` parameter → `ok=False`
- Empty list → `ok=False`
- Non-existent ID → `ok=False`

It does **not** test:

1. `privacy_mode_enabled=True` with a valid 2-item call — currently would succeed
   (privacy bypass confirmed in F1 above); after F1 fix, should return an error.
2. Single-element `item_ids` through the IPC handler — the min-2 guard (W1273)
   is verified only at the `RecordingComparison` unit level, not via `handle_request`.
3. Spanish accented text round-trip — no test verifies that `común`, `también`, etc.
   tokenize correctly (they do not — see F2).

**Risk:** Coverage gap means F1 regression can ship undetected; single-item IPC path
is not exercised after W1273 merge.

**Fix:** Add to `RecordingComparisonIPCTestCase`:

```python
def test_compare_recordings_privacy_mode_blocked(self) -> None:
    self.svc._settings_service._cache = {"privacy_mode_enabled": True}  # or mock
    resp = self.svc.handle_request({
        "id": "priv",
        "method": "compare_recordings",
        "params": {"item_ids": [self.id1, self.id2]},
    })
    self.assertFalse(resp["ok"])

def test_compare_recordings_single_item_rejected(self) -> None:
    resp = self.svc.handle_request({
        "id": "one",
        "method": "compare_recordings",
        "params": {"item_ids": [self.id1]},
    })
    self.assertFalse(resp["ok"])
```

---

## Non-findings (confirmed OK)

- **O(n²) memory for n=10:** `MAX_ITEMS=10` caps the matrix at 100 floats (trivial).
  `tf_vectors` and `token_sets` are similarly bounded. No memory risk.
- **`set.intersection` with empty sets:** `all(token_sets)` guard correctly short-circuits
  to `[]` when any token set is empty. Mechanically safe.
- **`set.union` generator empty:** The generator `(token_sets[j] for j in range(n) if j != i)`
  produces at least one item when `n >= 2`, so `set.union(*...)` never receives an empty
  unpacking argument.
- **Similarity matrix symmetry exploit (line 164):** `i > j → sim_matrix[j][i]` correctly
  halves cosine calls with no correctness risk. Verified: diagonal=1.0, off-diagonal
  symmetric to 6 decimal places.
- **IPC handler wiring:** `compare_recordings` is present in the handler table at line 1116
  and delegates correctly. Handler is reachable.
- **`_view_to_dict` serialization:** All fields are JSON-serializable (verified by test).
  Float values are rounded to 4 decimal places in `_cosine_sim` and `_stat_dict`.
- **TextAnonymizer interaction:** `self._text_anonymizer` is instantiated in
  `BackendService.__init__` (line 413) but is never called anywhere in the production
  backend code (only available via `core.text_postprocessor` pipeline). The comparison
  service does not use it. This is a pre-existing dead instantiation, not a new issue
  introduced here.

---

## Open items from W1267 (not yet merged / still unresolved)

| W1267 Finding | Fix Wave | Merge State |
|---|---|---|
| F1: min-2 guard | W1273 | NOT MERGED (branch `fix-comparison-insights-W1273`) |
| F2: recording_insights privacy bypass | W1273 | NOT MERGED |
| F3: `get_daily_insight` IPC missing | W1274 | Branch exists; merge state unverified |
| F4: unbounded `all_tokens` in topic analysis | — | OPEN |
| F5: TF binary-set formula | — | OPEN (acceptable limitation per W1267) |
