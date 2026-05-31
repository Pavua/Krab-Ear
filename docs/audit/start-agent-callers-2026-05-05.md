# start_agent callers audit — 2026-05-05

Audit of production source files that reference `native/runtime/KrabEarAgent`
(Phase C.6.2 followup, Wave 545).

## Scope

Scanned directories (production only):
- `KrabEar/backend/**/*.py`
- `KrabEar/core/**/*.py`
- `native/KrabEarAgent/Sources/**/*.swift`

Exempt from scan: `docs/`, `tests/`, `scripts/`, `.claude/`, `Makefile`.

## Hypothesis

Only `SingleInstanceGuard.swift` and `main.swift` are allowed to reference
`native/runtime/KrabEarAgent` in production code. All other production files
must not spawn the runtime binary directly.

## Findings

No unintended references to `native/runtime/KrabEarAgent` found in production
source files at the time of this audit (2026-05-05).

Allowed references confirmed:
- `native/KrabEarAgent/Sources/KrabEarAgent/SingleInstanceGuard.swift` (C.6.2 orphan killer)
- `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` (startup guard + comment)

## Recommended action

No immediate action required. The audit confirms the production codebase is clean.
If a new file is added that legitimately needs to reference the runtime binary,
it must be added to `_ALLOWED_RUNTIME_REFS` in `test_start_agent_audit.py`.

## Status

CLEAN — no violations detected.
