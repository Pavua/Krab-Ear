# Audit W1085 — TemplateManager (`backend/template_manager.py`)

**Date:** 2026-05-26  
**Branch:** `fix/search-index-W1041` (audited `codex/krab-ear-v2`)  
**Auditor:** sub-agent W1085

---

## Summary

`TemplateManager` manages named text output templates with `{variable}` placeholder substitution.
The module is clean and well-tested overall, but has five concrete issues worth fixing.

---

## Findings

### F1 — Double-Substitution via Variable Values (MEDIUM)

**File:** `backend/template_manager.py:193–195`

`apply_template` iterates over `variables.items()` and calls `str.replace(f"{{{key}}}", value)`
sequentially in dict insertion order. If one variable's **value** contains a brace-enclosed token
that matches a later variable key, the second pass will substitute it:

```python
# Template: "A={a} B={b}"
apply_template("t", {"a": "{b}", "b": "INJECTED"})
# → Step 1: replace {a} → "A={b} B={b}"
# → Step 2: replace {b} → "A=INJECTED B=INJECTED"   ← attacker-controlled
```

A malicious IPC caller supplying `variables={"a": "{b}", "b": "secret"}` can cause a variable
that was never intended to appear in the output to be injected.

**Fix:** perform all substitutions on the *original* text, not the accumulating result:

```python
result = text
for key, value in variables.items():
    result = result.replace(f"{{{key}}}", str(value))
# → still sequential on 'result'; use a single re.sub instead, or snapshot original:
original = text
for key, value in variables.items():
    original = original  # NO — need to apply to same base
# Correct: pre-compute all replacements against the unchanged text
import re
result = re.sub(
    r"\{(\w+)\}",
    lambda m: str(variables.get(m.group(1), m.group(0))),
    text,
)
```

The `re.sub` approach applies all substitutions in a single pass against the unmodified template,
eliminating double-substitution entirely.

---

### F2 — No Maximum Template Size Cap (LOW)

**File:** `backend/template_manager.py:95–142`

`add_template` imposes no limit on `text` length. An IPC caller can store a multi-MB payload:

```python
tm.add_template("huge", "x" * 10_000_000)  # 10 MB — accepted without error
```

The resulting `templates.json` grows unboundedly, and every `_load()` call (which runs inside the
lock on every read) deserialises the entire file.

**Fix:** add a size guard in `add_template`:

```python
MAX_TEMPLATE_BYTES = 64 * 1024  # 64 KB
if len(text.encode()) > MAX_TEMPLATE_BYTES:
    raise ValueError(f"Текст шаблона превышает максимальный размер ({MAX_TEMPLATE_BYTES} байт)")
```

A category-length cap (e.g. 64 chars) would also be prudent.

---

### F3 — Non-Atomic Save (LOW)

**File:** `backend/template_manager.py:78–84`

`_save_user` writes directly with `Path.write_text`, which truncates the file first and then
writes. A process crash or SIGKILL between truncation and write completion leaves
`templates.json` empty or partially written, losing all user templates.

```python
self._file.write_text(json.dumps(...), encoding="utf-8")  # NOT atomic
```

**Fix:** write to a sibling temp file and `os.replace` (atomic on POSIX):

```python
import os, tempfile

tmp_fd, tmp_path = tempfile.mkstemp(dir=self._data_dir, suffix=".tmp")
try:
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(user_only, ensure_ascii=False, indent=2))
    os.replace(tmp_path, self._file)
except:
    os.unlink(tmp_path)
    raise
```

Note: `StateStore` (history storage) already uses file-lock + atomic write — `TemplateManager`
should follow the same pattern.

---

### F4 — `remove_template` Returns `True` for Builtins (False Signal) (LOW)

**File:** `backend/template_manager.py:144–162`

`_load()` returns the merged list (user + builtins). When a caller removes a builtin name such as
`"greeting_ru"`, the filter succeeds (`before != after → True`), but:

1. `_save_user` stores only `user_only` (excluding builtins) — so nothing actually changes on disk.
2. The next call to `_load()` re-injects the builtin automatically.
3. The API returned `removed=True` — a misleading signal.

A Swift UI observing `removed: true` will remove the entry from its list, then receive the
builtin again on the next `get_templates` call, causing a ghost-reappearance flicker.

**Fix:** before filtering in `remove_template`, check whether the named template is a builtin
and either (a) return `False` immediately, or (b) return `True` with `"ephemeral": true` so the
UI knows the item will reappear.

---

### F5 — No Privacy-Mode Gate on Template Text (INFO)

**File:** `backend/template_manager.py` (entire module)

`TemplateManager` has no awareness of the backend's privacy-mode flag. Templates can contain
sensitive variable values (e.g. `sender_name`, `sender_title`) that may be logged by the IPC
dispatcher at `DEBUG` level. The builtin `email_signature` template is a concrete example.

There is no path where templates are persisted to history or transmitted to Sentry; the risk is
limited to debug logs. However, for consistency with other services that gate sensitive output on
`privacy_mode`, the IPC handlers should avoid logging template text content at any level when
`privacy_mode` is active.

This is an INFO-level hygiene item, not a blocking issue.

---

## Coverage Assessment

`KrabEar/tests/test_template_manager.py` — **excellent** (598 lines, 9 test classes, ~50 test
methods). Covers: builtins, add/update/remove/apply, IPC handlers, persistence roundtrip, thread
safety, edge cases, export/import, placeholder extraction, and builtin protection behaviour.

Gaps:
- No test for the double-substitution F1 bug.
- No test for oversized template text (F2).
- No concurrent write stress test (F3 — atomicity).

---

## IPC Wire Status

All four handlers are wired in `service.py` (lines 1146–1149):

| IPC method | Handler |
|---|---|
| `get_templates` | `_template_manager.handle_get_templates` |
| `add_template` | `_template_manager.handle_add_template` |
| `remove_template` | `_template_manager.handle_remove_template` |
| `apply_template` | `_template_manager.handle_apply_template` |

Fully wired. No orphan handlers.

---

## Idempotency

`add_template` is idempotent: calling it twice with the same `name` replaces the existing entry
(upsert semantics, verified by test `test_add_template_updates_existing`). `remove_template` on
a non-existent name returns `False` without error. Both are safe for retry.

---

## Findings Count: 5 (2 MEDIUM/LOW actionable, 2 LOW, 1 INFO)
