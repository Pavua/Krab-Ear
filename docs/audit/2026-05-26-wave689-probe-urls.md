# Wave 689 — LM Studio Probe URL Audit

**Date:** 2026-05-26  
**Scope:** All LM Studio API call sites in `KrabEar/backend/` and `KrabEar/core/`  
**Risk axis:** `/v1/*` (JIT-triggering on some LM Studio builds) vs `/api/v1/*` (safe metadata) vs `/chat/completions` (inference, intentional).

---

## Call-site inventory

| # | File | Line | URL (actual code) | Method | JIT risk | Status |
|---|------|------|-------------------|--------|----------|--------|
| 1 | `backend/llm_rewriter.py` | 502, 553, 604, 632, 872, 969, 1061 | `{base_url}/chat/completions` | POST | Expected — inference call | OK |
| 2 | `backend/llm_rewriter.py` | 1179 | `{host}/api/v1/models` | GET | None — metadata only | OK |
| 3 | `backend/llm_probe.py` | (via `rewriter.passive_health_check()`) | `{host}/api/v1/models` | GET | None | OK |
| 4 | `backend/action_items_extractor.py` | 207 | `{base_url}/chat/completions` | POST | Expected — inference call | OK |
| 5 | `backend/va_multimodal.py` | 169 | `{base_url}/v1/chat/completions` | POST | Expected — inference call | **MISMATCH vs peers** |
| 6 | `backend/service.py` | 2866 | `{host}/api/v1/models` | GET | None — `list_llm_models` handler | OK |
| 7 | `backend/lm_studio_lifecycle.py` | 41 | `{api_root}/api/v0/models/{id}/unload` | POST | N/A — lifecycle, not inference | OK |
| 8 | `backend/lm_studio_lifecycle.py` | 59 | `{api_root}/api/v0/models/load` | POST | N/A — lifecycle, not inference | OK |
| 9 | `core/config.py` | 113 | `http://127.0.0.1:18789/v1/chat/completions` | — | Default config string for Voice Gateway (not LM Studio) | OK |

---

## Findings

### Finding 1 — `va_multimodal.py` uses `/v1/chat/completions` (minor inconsistency)

`MultimodalVAClient` posts to `{base_url}/v1/chat/completions` (line 169), while all other inference callers (`LLMRewriter`, `ActionItemsExtractor`) strip the `/v1` prefix and post to `{base_url}/chat/completions`.

**Risk:** Low. Both paths return the same result in current LM Studio (1234). The inconsistency could cause a 404 if LM Studio ever tightens the routing. `va_multimodal.py` is also not yet wired to IPC dispatch (status: Phase 2A skeleton only), so it carries zero production risk today.

**Recommended fix:** Align to `{base_url}/chat/completions` pattern used by peers.

### Finding 2 — Probe path is correct everywhere

`passive_health_check()` in `llm_rewriter.py` (line 1179) and `service.py` (line 2866, `list_llm_models`) both use `/api/v1/models`. `llm_probe.py` delegates to `passive_health_check()`. No `/v1/models` (JIT-triggering) path survives in production code — PR #396 / Wave 59 fix is confirmed effective.

### Finding 3 — `config.py` GATEWAY_URL is not an LM Studio endpoint

`GATEWAY_URL = "http://127.0.0.1:18789/v1/chat/completions"` (line 113) targets the Voice Gateway service, not LM Studio. No action needed.

---

## Summary

| Category | Count | Action |
|----------|-------|--------|
| JIT-triggering probe URLs (`/v1/models`) | 0 | None — fully remediated by Wave 59 |
| Inference POST (expected JIT trigger, acceptable) | 7 | None |
| Metadata GET (`/api/v1/models`, safe) | 3 | None |
| Inconsistent path in skeleton file | 1 (`va_multimodal.py:169`) | Low-pri fix before Phase 2A wiring |

**Action item:** Before `va_multimodal.py` is wired to IPC, change line 169 from `{base_url}/v1/chat/completions` to `{base_url}/chat/completions` to match the rest of the codebase.
