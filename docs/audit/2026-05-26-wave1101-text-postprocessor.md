# Audit: TextPostProcessor pipeline — W1101

**Date:** 2026-05-26  
**Branch:** `audit-text-postprocessor-W1101`  
**File:** `KrabEar/core/text_postprocessor.py`  
**Test file:** `KrabEar/tests/test_text_postprocessor.py`  
**Tests:** 84 passed, 0 failed  
**Auditor:** W1101 sub-agent (read-only)

---

## Summary

`TextPostProcessor` is a clean, well-structured pipeline with solid error isolation and good test coverage. All 84 existing tests pass. Performance is well within budget (all steps <0.1 ms). Six findings were identified — two medium severity, four low.

---

## Findings

### F1 — MEDIUM: Shared singleton step instances across TextPostProcessor instances (thread-safety race)

**Location:** `text_postprocessor.py:179–185, 214`

`_BUILTIN_STEPS` is a module-level dict of pre-constructed step objects. `TextPostProcessor.__init__` does `dict(_BUILTIN_STEPS)` which copies the dict but **shares the same object references**. This means every `TextPostProcessor` instance across the entire process shares the same `ExpandAbbreviations`, `Anonymize`, and `FixPunctuation` objects.

Each of these uses a lazy-init pattern (`_get_expander`, `_get_anonymizer`, `_get_fixer`) with a bare `if self._attr is None:` check — **no lock**. If two threads call `process()` on two different `TextPostProcessor` instances simultaneously and both trigger lazy init of the same shared step object, there is a TOCTOU window where both threads enter the `if … is None` branch, both construct a collaborator, and the second write clobbers the first mid-use.

In practice this is low-probability (lazy init is fast; collaborators are stateless after construction) but it is a real race. The existing `test_concurrent_process` covers only `strip_whitespace` (pure function, no lazy state) and therefore does not exercise this path.

**Recommended fix:** either construct fresh step instances per `TextPostProcessor.__init__` (one-liner: replace `dict(_BUILTIN_STEPS)` with `{k: type(v)() for k, v in _BUILTIN_STEPS.items()}`), or guard each lazy-init with a `threading.Lock`.

---

### F2 — MEDIUM: Module docstring claims canonical order includes anonymize/abbreviations; DEFAULT_CHAIN omits both

**Location:** `text_postprocessor.py:3–4, 188`

The module-level docstring states the pipeline transforms as:

> нормализация пробелов → пунктуация → **сущности → аббревиатуры → анонимизация**

But `DEFAULT_CHAIN` is:

```python
DEFAULT_CHAIN = ["strip_whitespace", "fix_punctuation", "normalize_entities"]
```

`expand_abbreviations` and `anonymize` are not in the default chain and are therefore **never called unless the IPC caller explicitly requests them**. The docstring creates a false impression that the pipeline always performs all five transformations. A new developer reading the module will be confused about why PII is not redacted by default.

Additionally, there is no documented recommendation about which order callers should use when combining `anonymize` and `expand_abbreviations`. Empirically (verified by instrumented runs): the order does not matter for correctness — abbreviation patterns do not overlap with PII placeholder tokens (`[ТЕЛЕФОН]`, `[EMAIL]`, etc.), and neither step produces output that triggers the other. However, the **semantically correct order is anonymize-before-expand-abbreviations** (redact PII first, then expand textual abbreviations), which matches the docstring ordering. This should be documented as a recommended convention.

**Recommended fix:** correct the module docstring to reflect the actual DEFAULT_CHAIN, and add a comment on DEFAULT_CHAIN explaining that `expand_abbreviations` and `anonymize` are opt-in.

---

### F3 — LOW: `steps_applied` semantics ambiguous for failed steps

**Location:** `text_postprocessor.py:272–285`, `PostProcessResult` docstring

When a step raises an exception, the pipeline catches it, preserves the previous text, and appends the step name to `steps_applied` (line 285). The `PostProcessResult` docstring describes `steps_applied` as "имена шагов, которые были выполнены" ("names of steps that were executed"). "Executed" is ambiguous: it could mean "attempted" or "successfully applied".

A caller checking `steps_applied` to verify that anonymization ran has no way to distinguish between "anonymize succeeded" and "anonymize threw but is still listed". For privacy-sensitive callers this is a subtle hazard.

`changes_count` does not increment on failure (correct), so a caller can detect that a step ran but made no change — but that signal conflates "no PII found" with "anonymizer crashed".

**Recommended fix:** either (a) add a separate `failed_steps: list[str]` field to `PostProcessResult`, or (b) clarify the docstring to state that `steps_applied` contains steps that were *attempted* (including those that failed), and add a note in the exception handler.

---

### F4 — LOW: ExpandAbbreviations default language hardcoded to "ru"; no language-auto-detect integration

**Location:** `text_postprocessor.py:109–134`, `_BUILTIN_STEPS:182`

The registry creates `ExpandAbbreviations(language="ru")` as the default singleton. Callers passing a Spanish or English transcript to the default step will silently get no expansions (the expander returns the text unchanged for unknown languages). There is no integration with `LanguageDetector` (W1019 fix) or any auto-language-routing.

Similarly, `FixPunctuation` defaults to `"ru"` (no inverted question-mark for Spanish text, etc.).

This is not a bug — callers must register custom steps with the right language if needed — but it is a footgun: the default chain silently does nothing useful for non-Russian input. There is no warning when `ExpandAbbreviations.expand()` returns unchanged text because the language is unknown.

**Recommended fix:** log a `debug`-level note when `expand()` falls through with a non-`"ru"` language and no compiled patterns exist for that language. Alternatively, document in the class docstring that the default builtin step is RU-only and callers should register language-specific instances.

---

### F5 — LOW: Idempotency confirmed; no test asserts it

**Location:** `text_postprocessor.py` (pipeline), `test_text_postprocessor.py`

Idempotency was verified empirically: running `DEFAULT_CHAIN` twice on the same text produces identical output (FixPunctuation does not double-add periods, StripWhitespace does not double-collapse). However, no test asserts this property explicitly. For a pipeline that is called multiple times on the same transcript (e.g., after an edit), idempotency is a correctness contract worth locking in.

**Recommended fix:** add a `TestTextPostProcessorIdempotency` test class with at least: (1) run DEFAULT_CHAIN twice and assert `r1.text == r2.text`, (2) same for `expand_abbreviations` (a known non-idempotent risk: "то есть" could be re-expanded if "т" matches some other abbreviation — verified clean for current builtin set).

---

### F6 — LOW: No performance tests for pipeline steps; "wire status" undefined in IPC_API_REFERENCE

**Location:** `backend/text_processing_service.py:350`, `docs/IPC_API_REFERENCE.md`

All steps are extremely fast (strip: 0.001 ms, fix_punctuation: 0.006 ms, expand: 0.034 ms, anonymize: 0.012 ms, normalize_entities: 0.063 ms per call). All are well within the 50 ms budget stated in the audit spec. No regression exists here.

However, `post_process_text` and `list_post_process_steps` IPC handlers are wired in `TextProcessingService` and delegated from `BackendService`, but their presence in `docs/IPC_API_REFERENCE.md` was not verified during this audit (the reference has 58% known drift per W657). The handlers themselves work correctly per `test_text_processing_service.py`.

**Recommended fix:** no action needed for performance. For the IPC reference, include `post_process_text` / `list_post_process_steps` in the next IPC_API_REFERENCE regen pass (W657 follow-up).

---

## Summary table

| # | Severity | Area | Fix cost |
|---|----------|------|----------|
| F1 | MEDIUM | Shared singleton thread-safety race in lazy-init | Low (one-liner constructor change) |
| F2 | MEDIUM | Docstring vs. DEFAULT_CHAIN mismatch + ordering not documented | Low (docs only) |
| F3 | LOW | steps_applied includes failed steps without signal | Medium (add field or clarify docs) |
| F4 | LOW | Language hardcoded to "ru", no auto-detect | Low (add warning log) |
| F5 | LOW | Idempotency not covered by tests | Low (add test class) |
| F6 | LOW | IPC_API_REFERENCE drift (unrelated to this module) | Deferred to W657 follow-up |

---

## What is clean

- **Pipeline error isolation:** one failing step does not abort the pipeline. Remaining steps run on the pre-failure text. Tested by `test_raising_step_does_not_abort_pipeline` and `test_raising_step_followed_by_valid_step`.
- **Idempotency:** DEFAULT_CHAIN is idempotent (verified empirically).
- **Anonymize/expand ordering:** either order produces identical PII redaction. No leakage path found.
- **Performance:** all 5 steps under 0.1 ms each. Normalize_entities fastest at 0.063 ms thanks to literal-hint fast-path in `TextUtils.normalize_entities`.
- **Test coverage:** 84 tests cover all public API surface, all 5 builtin steps, error isolation, Unicode, concurrency (strip_whitespace only), protocol compliance.
- **W1019 interaction:** language detection is not integrated into this pipeline (no regression risk from the W1019 fix; postprocessor is language-oblivious at the pipeline level).
- **W1081 interaction:** no evidence of W1081 (ambiguous abbreviations) affecting this module; `AbbreviationExpander` handles ambiguity via `no_after_digit` flags internally and is called correctly from `ExpandAbbreviations.process()`.
