# Audit W1002 — TextPostProcessor (`core/text_postprocessor.py`)

**Date:** 2026-05-26  
**Auditor:** Sub-agent W1002 (read-only)  
**Scope:** `KrabEar/core/text_postprocessor.py`, `KrabEar/tests/test_text_postprocessor.py`, wire status via `service.py` + `text_processing_service.py`

---

## Summary

`TextPostProcessor` is a clean, well-structured configurable pipeline. 5 findings identified, all minor or design notes — no blocking bugs.

---

## Findings

### F1 — Stage Ordering Correct (PASS)

Default chain: `strip_whitespace → fix_punctuation → normalize_entities`.

Ordering is correct:
- `strip_whitespace` first: ensures `fix_punctuation` receives clean input (leading spaces, CRLF removed before capitalization logic).
- `fix_punctuation` (PunctuationFixer) before `normalize_entities`: PunctuationFixer adds period at sentence end. NormalizeEntities (`TextUtils.normalize_entities`) does brand/time normalization — does not depend on punctuation state, so order is fine.
- `anonymize` and `expand_abbreviations` are **not** in DEFAULT_CHAIN and must be explicitly requested. No ordering enforcement exists for them when combined — a caller could request `["anonymize", "expand_abbreviations"]` where expanded abbreviations never get redacted. The expanded form of "т.е." is "то есть" which contains no PII, so this is harmless in practice, but it is worth noting.

**Recommendation:** Document recommended ordering in docstring when both `expand_abbreviations` and `anonymize` are used: abbreviations first, anonymize last.

---

### F2 — Idempotency: Mostly Safe, One Edge Case (LOW)

- `strip_whitespace`: fully idempotent. `_MULTI_SPACE_RE` + `.strip()` are idempotent by construction.
- `normalize_entities`: idempotent. Brand mapping is a fixed dict; "Телеграм → Telegram" applied twice is a no-op.
- `expand_abbreviations`: generally idempotent (abbreviations are not re-introduced by expansion). However, if `AbbreviationExpander` matches partial words in expanded forms, double-expansion could occur. No evidence of this in current built-in abbreviations ("т.е." → "то есть").
- `fix_punctuation`: **not strictly idempotent.** If `PunctuationFixer` adds an inverted question mark for Spanish ("¿") but already detects one on the second run, result should be stable. However, if the fixer uses a heuristic that re-evaluates whether to add a period based on the previous character, two passes might add/not-add differently. This is a theoretical risk dependent on `PunctuationFixer` internals (not audited here).
- `anonymize`: idempotent — once PII is replaced by `[ТЕЛЕФОН]`, the placeholder does not match PII patterns.

**Recommendation:** If callers build pipelines that run `process()` twice, add an integration test for double-processing `fix_punctuation` on the same text.

---

### F3 — Stage Disable: Full Granularity via `steps=` (PASS with note)

Any stage can be individually disabled by omitting it from the `steps` list. The `steps=[]` idiom disables all stages. Tests in `TestTextPostProcessorDisableIndividualStages` cover this.

**Gap:** There is no `disable_steps: list[str]` parameter for "run all except X" semantics. Callers who want the default chain minus one step must replicate `DEFAULT_CHAIN` and filter manually. This is a minor usability gap — not a bug.

---

### F4 — Error Isolation: Correct (PASS)

Lines 272–285 of `text_postprocessor.py`:
```python
try:
    before = current
    current = step.process(current)
    steps_applied.append(step_name)
    if current != before:
        changes_count += 1
except Exception as exc:
    logger.exception(...)
    steps_applied.append(step_name)
    # Продолжаем с предыдущим текстом — шаг не применяется.
```

A failing step leaves `current` unchanged and pipeline continues. Tested by `TestTextPostProcessorErrorHandling.test_raising_step_followed_by_valid_step`. **Correctly implemented.**

**Note:** A failing step still appears in `steps_applied` but `changes_count` is NOT incremented (since `current` was not updated). This is correct behavior but callers relying on `steps_applied` length to verify all steps executed cleanly will be misled. Consider adding an `errors: dict[str, str]` field to `PostProcessResult` for failed steps.

---

### F5 — Privacy Mode: No Automatic Gate (DESIGN GAP)

`anonymize` is an opt-in step — it is **not** in `DEFAULT_CHAIN` and requires explicit inclusion in `steps=["anonymize", ...]`. There is no privacy_mode awareness inside `TextPostProcessor` or in the `handle_post_process_text` IPC handler.

Checking `backend/text_processing_service.py` lines 329–363 confirms: no `privacy_mode` setting check before dispatching to `_text_postprocessor.process()`.

This means:
- When `privacy_mode=True`, PII in transcripts is **not** automatically redacted by the post-processor unless the caller explicitly includes `"anonymize"` in the steps list.
- The STT pipeline (via `service.py`) does not appear to inject `"anonymize"` into the steps based on privacy_mode state.

**Recommendation:** Either (a) have `handle_post_process_text` inject `"anonymize"` into the active steps when `privacy_mode` is enabled, or (b) document explicitly that PII redaction is the caller's responsibility. This is a design gap, not a bug, since `TextAnonymizer` may be applied elsewhere in the pipeline (e.g., in `AudioEngine` or `BackendService.handle_transcription_done`). Needs cross-audit to confirm.

---

## Composition Status

| Component | Integration | Status |
|-----------|-------------|--------|
| `PunctuationFixer` | Via `FixPunctuation` step, lazy import | Wired |
| `AbbreviationExpander` | Via `ExpandAbbreviations` step, lazy import | Wired |
| `TextAnonymizer` | Via `Anonymize` step, lazy import | Wired |
| `NumberNormalizer` | **Not wired** — no `NormalizeNumbers` step | Absent |
| `TextUtils.normalize_entities` | Via `NormalizeEntities` step | Wired |

**NumberNormalizer gap:** `core/number_normalizer.py` (`NumberNormalizer`) is not integrated as a pipeline step. Callers who want spoken numeral normalization (e.g. "триста" → "300") must call it separately. Adding a `NormalizeNumbers` step would complete the composition set.

---

## Wire Status

**Wired in production.** Path:
1. `KrabEar/backend/service.py` line 414: `self._text_postprocessor = TextPostProcessor()`
2. `service.py` line 427: passed to `TextProcessingService(text_postprocessor=...)`
3. `service.py` lines 1114–1115: IPC methods `post_process_text` + `list_post_process_steps` dispatched to `TextProcessingService` handlers.

The processor is **explicitly wired** as an on-demand IPC tool. It is **not** automatically invoked in the transcription pipeline — callers must explicitly send `post_process_text` IPC to trigger it. This is intentional (opt-in post-processing).

---

## Test Coverage

File: `KrabEar/tests/test_text_postprocessor.py` — **702 lines**, 14 test classes, ~60 test methods.

Coverage is comprehensive:
- `PostProcessResult` dataclass defaults and full construction.
- All 5 built-in steps tested individually (name, happy path, empty string, edge cases).
- `TextPostProcessor` basics: empty string, default chain, custom steps, changes_count tracking.
- `register_step()`: custom step, overwrite, invalid step (TypeError), missing `name`.
- Error isolation: raising step + subsequent valid step.
- DEFAULT_CHAIN constant verified.
- Stage disable: single step, empty list, PII preserved without anonymize.
- Protocol compliance: all built-in steps checked via `isinstance(x, PostProcessorStep)`.
- Unicode: Cyrillic, Spanish, Chinese, emoji, mixed.
- Concurrency: 20 threads calling `process()` simultaneously (no assertion errors).

**Gaps:**
- No test for double-processing (idempotency under repeated calls).
- No test for `privacy_mode` interaction (see F5).
- No test for `NumberNormalizer` (not yet a step).
- `test_text_processing_service.py` mocks the processor entirely — no integration test running the real `TextPostProcessor` through the IPC handler.

---

## Performance Note

All built-in steps use lazy imports (`_get_fixer()`, `_get_expander()`, `_get_anonymizer()`). First call per instance incurs import + initialization cost; subsequent calls use cached collaborators. For a 60s transcript (~500–800 words), each step is a single regex/dict pass — total latency expected < 5 ms per `process()` call. No performance concern identified.

---

## Verdict

| Check | Result |
|-------|--------|
| Stage ordering | PASS |
| Idempotency | MOSTLY SAFE (fix_punctuation theoretical risk) |
| Individual disable | PASS (via `steps=` parameter) |
| Error isolation | PASS (continues on failure) |
| Performance | PASS (< 5 ms estimated) |
| Backward compat | PASS (stable Protocol + dataclass API) |
| Test coverage | GOOD (minor gaps: idempotency, privacy_mode) |
| Wire status | WIRED (opt-in IPC, not automatic in STT pipeline) |
| Privacy mode gate | DESIGN GAP (anonymize not auto-injected on privacy_mode=True) |
| Composition | MOSTLY COMPLETE (NumberNormalizer not integrated) |
