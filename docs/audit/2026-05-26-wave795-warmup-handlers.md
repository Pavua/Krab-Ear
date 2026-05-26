# Wave 795 — Warmup/Init Handler Audit

**Date**: 2026-05-26  
**Branch**: `feature/extract-llm-warmup-W795`  
**Author**: Claude Sonnet 4.6 (automated)

## Scope

Audit of `_handle_warmup_*`, `_handle_probe_llm_http`, and `_handle_handshake` in
`KrabEar/backend/service.py` for inline logic opportunities.

## Findings

### Before this wave

| Handler | LOC | Dispatch target | Status |
|---------|-----|-----------------|--------|
| `_handle_handshake` | 27 | `self._handle_handshake` | **Inline** (real logic in service.py) |
| `_handle_probe_llm_http` | 3 | `self._handle_probe_llm_http` | Already a delegation shim |
| `_handle_warmup_stt` | 3 | `self._stt_mgmt_svc.handle_warmup_stt` (dispatch bypasses shim) | Dead shim (dispatch table bypassed it in W734) |
| `_handle_warmup_rewriter` | 3 | `self._handle_warmup_rewriter` | Already a delegation shim |

### Actions taken

**`_handle_handshake` → moved to `HealthCheckService`**

- Full 27-LOC logic moved to `HealthCheckService.handle_handshake()` (W795).
- `BackendService._handle_handshake()` reduced to a 1-line delegation shim.
- `backend_version` field now uses `self._app_version` (the real `APP_VERSION` from
  `__version__.py`) instead of the former hardcoded `"1.0.0"` string.
- Dispatch table entry `"handshake": self._handle_handshake` unchanged — dispatch
  invariant tests (wave768, wave790) continue to pass.

**Others — no change required**

- `_handle_probe_llm_http` and `_handle_warmup_rewriter` are already 1-line delegation
  shims; no further extraction benefit.
- `_handle_warmup_stt` shim kept intact: dispatch table already calls
  `self._stt_mgmt_svc.handle_warmup_stt` directly (W734), but `test_stt_warmup.py`
  calls the shim on `BackendService` directly — removing the shim would break those tests.

## Results

| Metric | Before | After |
|--------|--------|-------|
| Inline handlers (service.py) | 35 | 34 |
| Delegated stubs (service.py) | 49 | 50 |
| `HealthCheckService` handlers | 6 | 7 |
| `_handle_handshake` LOC (service.py) | 27 | 3 |

## Verification

- AST parse: `service.py` + `health_check_service.py` — OK
- Orphan import audit: 0 missing imports
- Tests: 72 passed (handshake, reconnect, dispatch invariants wave693/768, stt_warmup, log_config)
