# Service Extraction Pattern — Krab Ear

> Canonical recipe for extracting a handler cluster from `backend/service.py` into
> a dedicated service module.  Waves W741/W742/W747/W751/W757/W772/W773 all follow
> this pattern.  Read this before starting any new extraction.

---

## 1. When to Extract

Extract a cluster of handlers from `BackendService` when ALL three conditions hold:

| Criterion | Threshold |
|-----------|-----------|
| Method count | ≥ 3 `_handle_*` methods share a coherent domain |
| LOC | Cluster ≥ 100 LOC in `service.py` |
| Domain coherence | Methods share collaborators and could be tested independently |

**Do not extract** for:
- A single fat handler that just happens to be long — refactor it in place.
- Two methods that happen to sit next to each other but share no collaborators.
- Modules that already exist as standalone backends (e.g., `history_service.py`).

**Reference examples:** `STTManagementService` (6 handlers, ~180 LOC),
`AppleIntegrationService` (6 handlers, ~220 LOC), `CallSessionService`
(~15 handlers, Phase 3).

---

## 2. Module Structure

### 2.1 File naming

```
KrabEar/backend/<domain>_service.py
```

Use snake_case that matches the class name:
`STTManagementService` → `stt_management_service.py`.
`AppleIntegrationService` → `apple_integration_service.py`.

### 2.2 Module-level docstring (required)

```python
"""<ServiceName> — <one-line purpose>.

Выделен из backend/service.py (Wave NNN) для снижения размера монолитного модуля.
Содержит N IPC-обработчиков:
  - method_a   — краткое описание
  - method_b   — краткое описание
"""
```

List every IPC method the class owns.  This doubles as the diff target for the
Wave audit audit script.

### 2.3 Imports

```python
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

# Only import what the service actually uses.
# Heavy collaborators (SettingsService, Transcriber) go under TYPE_CHECKING
# to avoid circular imports at module load time.
if TYPE_CHECKING:
    from backend.settings_service import SettingsService
    from backend.transcriber import Transcriber

logger = logging.getLogger("KrabEar.Backend.<ServiceName>")
```

Module-level constants belong here (before the class), e.g.:

```python
_STT_HOTWORDS_MAX: int = 100  # Whisper initial_prompt token budget
```

### 2.4 Class shape

```python
class <ServiceName>:
    """Обработчики IPC-команд для <domain>."""

    def __init__(
        self,
        required_dep: "SomeDep",
        optional_dep: "OtherDep | None" = None,
    ) -> None:
        self._required_dep = required_dep
        self._optional_dep = optional_dep

    # ── Section heading (dashes align to 72 chars) ───────────────────────────

    def handle_<ipc_method_name>(self, params: dict[str, Any]) -> dict[str, Any]:
        """One-line summary.

        Параметры:
          - param_a: type — description (required/optional).

        Возвращает:
          {key: type, ...}

        Ошибки:
          - "error_code" — when this error is raised.
        """
        ...
```

Rules:
- Every handler is a `handle_*` method, not `_handle_*`.  (The underscore-prefix
  convention belongs to `BackendService` shims, not to the extracted class.)
- Type-annotate params/return as `dict[str, Any]` — mirrors the IPC envelope.
- No business logic in `__init__`; only store collaborator references.
- Guard missing required params with `raise ValueError(...)` at the top of each
  handler before touching collaborators.

---

## 3. Wiring into BackendService

### 3.1 Import at module top

Add the import with the existing cluster of service imports (lines ~70-95 of
`service.py`):

```python
from backend.stt_management_service import STTManagementService
```

### 3.2 Instantiate in `__init__`

Instantiate immediately after the collaborators it depends on are ready:

```python
self._stt_management = STTManagementService(
    settings_svc=self._settings,
    transcriber=self.transcriber,
)
```

Keep the constructor call on multiple lines if there is more than one argument.

### 3.3 Dispatch-table delegation — two patterns

**Pattern A — direct dispatch (preferred for fully extracted services):**

```python
"add_stt_hotword":    self._stt_management.handle_add_stt_hotword,
"remove_stt_hotword": self._stt_management.handle_remove_stt_hotword,
"list_stt_hotwords":  self._stt_management.handle_list_stt_hotwords,
```

The dispatch table in `handle_request` maps the IPC method name directly to the
service method.  No shim needed.

**Pattern B — local shim (transitional, during incremental extraction):**

When a handler still lives in `service.py` but a new service module also defines
the same logic (in parallel, for testing), keep a `_handle_*` shim in
`BackendService` that delegates:

```python
def _handle_send_to_telegram(self, params: dict[str, Any]) -> dict[str, Any]:
    return self._apple_integration.handle_send_to_telegram(params)
```

Delete the shim once all callers point to the service method directly.  Shims
left in place become dead code — the W746 hindsight (see §7).

---

## 4. Tests

Two complementary test layers are required.

### 4.1 Dispatch-wiring guard (source-grep pattern)

Create `KrabEar/tests/test_<domain>_wiring.py` (or add a class to an existing
`test_dispatch_invariants_wave*.py`).  The test verifies that every IPC method
owned by the service is present in the dispatch table, without instantiating
`BackendService`:

```python
import ast, pathlib, unittest

SERVICE_PY = pathlib.Path(__file__).parents[1] / "backend" / "service.py"
source = SERVICE_PY.read_text()

EXPECTED_METHODS = [
    "add_stt_hotword",
    "remove_stt_hotword",
    "list_stt_hotwords",
    "warmup_stt",
]

class TestSTTManagementWiring(unittest.TestCase):
    def test_all_methods_in_dispatch_table(self):
        for method in EXPECTED_METHODS:
            self.assertIn(
                f'"{method}"',
                source,
                f"IPC method '{method}' missing from service.py dispatch table",
            )
```

This guard catches the import-without-wiring anti-pattern (§7) at CI time.

### 4.2 Unit tests for the service class

Create `KrabEar/tests/test_<domain>_service.py`.  Use fake/stub collaborators —
no real `BackendService`, no temp files unless the service touches disk.

```python
class _FakeSettingsService:
    def __init__(self, initial=None):
        self._data = dict(initial or {})
        self._saved = []

    def cached_settings(self):
        return dict(self._data)

    def handle_set_settings(self, params):
        self._data.update(params)
        self._saved.append(dict(params))
        return {"ok": True}


class TestAddSttHotword(unittest.TestCase):
    def _make(self, initial=None):
        return STTManagementService(
            settings_svc=_FakeSettingsService(initial),
        )

    def test_add_new_word(self):
        svc = self._make()
        res = svc.handle_add_stt_hotword({"word": "Краб"})
        self.assertIn("Краб", res["hotwords"])

    def test_add_duplicate_is_idempotent(self):
        svc = self._make({"stt_hotwords": ["Краб"]})
        res = svc.handle_add_stt_hotword({"word": "Краб"})
        self.assertEqual(res["hotwords"].count("Краб"), 1)
        self.assertEqual(svc._settings_svc._saved, [])  # no write
```

Test coverage goals per extracted service:
- Happy path for each `handle_*` method.
- Empty / missing required param → `ValueError` or `RuntimeError`.
- At least one boundary condition (e.g., hotword list truncation at `_STT_HOTWORDS_MAX`).

### 4.3 Path setup in test files

Every test file that imports from `backend.*` or `core.*` must prepend the
`KrabEar/` subtree to `sys.path` manually:

```python
import os, sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)
```

---

## 5. Audit — catching orphan imports

`scripts/audit_dead_ipc_handlers.py` checks for handlers registered in the
dispatch table that have no corresponding implementation.  Run it after any
wiring change:

```bash
PYTHONPATH=$(pwd)/KrabEar python scripts/audit_dead_ipc_handlers.py
```

To catch the reverse problem (service file created but never imported into
`service.py`), grep for the class name:

```bash
grep -r "AppleIntegrationService" KrabEar/backend/service.py
```

Zero results = orphan module (see W746 hindsight, §7).  The dispatch-wiring
guard test (§4.1) is the automated CI equivalent of this grep.

---

## 6. Sentry Breadcrumbs

Add a breadcrumb in handlers that mutate persistent state or call external
services.  Reference: `backend/history_service.py` lines ~248-252.

```python
from backend.observability import add_breadcrumb
import time as _time

def handle_delete_something(self, params):
    ...
    _t0 = _time.monotonic()
    ok = self._store.delete(item_id)
    add_breadcrumb(
        category="<domain>",       # e.g. "history", "stt", "apple"
        message="delete_something",
        data={
            "ok": ok,
            "duration_ms": round((_time.monotonic() - _t0) * 1000),
        },
    )
    return {"deleted": ok}
```

Rules:
- `category` = lowercase domain name matching the service (not the IPC method).
- `data` must NOT contain transcript text or PII — only metadata
  (method name, duration, boolean flags, error type).
- Breadcrumbs are no-ops when Sentry DSN is absent; always safe to call.
- Read-only handlers (list, get) do not need breadcrumbs unless they are slow
  (> 50 ms expected) or security-sensitive.

---

## 7. Anti-Patterns

### Orphan module (W746 hindsight)

**Symptom:** A `*_service.py` file exists with a class and handler methods, but
`service.py` never imports or instantiates it.  All IPC calls silently fall
through to the old in-file handlers.

**Detection:** `grep -r "ClassName" KrabEar/backend/service.py` returns nothing.

**Fix:** Add the import + `__init__` instantiation + dispatch-table entries in
one atomic commit.  Never merge a new service module without all three.

### Import-without-instantiation-without-wiring

**Symptom:** Import line added to `service.py` but the `__init__` constructor
call or the dispatch-table entries are missing.  Causes `NameError` or silent
no-op depending on where the import is used.

**Detection:** CI wiring-guard test fails (`test_all_methods_in_dispatch_table`).

### Shim accumulation

**Symptom:** `_handle_*` shim methods in `BackendService` that only delegate to
a service method, never cleaned up.  Each shim is 2-4 lines of dead code that
survives the extraction and inflates the monolith.

**Fix:** After the service is wired with Pattern A (direct dispatch), delete
every corresponding shim from `BackendService` in the same PR.

### Circular import via non-TYPE_CHECKING import

**Symptom:** Importing a heavy backend module at module level in the service
file causes a circular import (`service.py` ← `new_service.py` ← `service.py`).

**Fix:** Use `TYPE_CHECKING` guard for any collaborator whose type is only needed
for annotations:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from backend.settings_service import SettingsService
```

Pass the actual instance through the constructor at runtime; the guard ensures
the import only runs during static analysis.

---

## Checklist for New Extractions

Before opening a PR for a new service extraction, verify each item:

- [ ] **Criteria met** — ≥ 3 handlers, ≥ 100 LOC, coherent domain (§1).
- [ ] **Module created** — `KrabEar/backend/<domain>_service.py` with module
      docstring listing all owned IPC methods (§2.2).
- [ ] **Logger name** — `"KrabEar.Backend.<ServiceName>"` (§2.3).
- [ ] **Constructor** — only stores collaborator references; no logic (§2.4).
- [ ] **Handler signatures** — `handle_*` (no leading underscore), typed as
      `dict[str, Any]` → `dict[str, Any]` (§2.4).
- [ ] **Import in service.py** — added at module top with existing cluster (§3.1).
- [ ] **Instantiation in `__init__`** — after all dependencies are ready (§3.2).
- [ ] **Dispatch table updated** — Pattern A (direct), shims removed if present (§3.3).
- [ ] **Wiring guard test** — `test_<domain>_wiring.py` with source-grep check for
      every owned method (§4.1).
- [ ] **Unit tests** — `test_<domain>_service.py` covering happy path + error
      cases for each handler (§4.2).
- [ ] **Orphan check** — `grep -r "ClassName" KrabEar/backend/service.py` returns
      import + instantiation lines (§5).
- [ ] **Breadcrumbs** — mutation handlers call `add_breadcrumb` with no PII (§6).
- [ ] **`service.py` LOC diff** — net negative (handlers removed, not duplicated).
- [ ] **`CLAUDE.md` updated** — add the new module to the `backend/` inventory
      list so future sessions know it exists.
