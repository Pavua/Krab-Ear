# Sentry Sweep — 2026-05-20 (Wave 260)

**Sweep window**: last 48 h (2026-05-18 → 2026-05-20)
**Projects**: krab-ear-backend, krab-ear-agent
**AGENT-J post-Wave-67 verification included**

---

## Backend — Unresolved Issues

| Issue | Title | Events | First Seen | Last Seen | Code tag | Release |
|-------|-------|--------|------------|-----------|----------|---------|
| KRAB-EAR-BACKEND-J | [warn batch x2] http_400_after_retry | 2 | 2026-05-18 19:43 | 2026-05-18 19:49 | `rewriter.timeout` | krab-ear@2.0.0 |

**1 unresolved backend issue.** No new issues created in the last 48 h.

**KRAB-EAR-BACKEND-J analysis:**
- Component: `rewriter`, Phase B error code `rewriter.timeout`
- Model: `gemma-4-26b-a4b-it-optiq` → LM Studio returned HTTP 400 after retry
- Occurred 2026-05-18 19:43–19:49, 2 events, then silent
- Likely a transient LM Studio 400 burst (warmup/model-switch race). No new occurrences in last 24 h.
- Recommendation: monitor for recurrence; if model is switched to a newer variant, consider marking resolved.

---

## Agent — Unresolved Issues

**0 unresolved issues.** All previously tracked issues are resolved or archived.

---

## AGENT-J Post-Wave-67 Trend

| Metric | Value |
|--------|-------|
| Status | `ignored` / `archived_forever` |
| Events at last sweep | 4 |
| Events now | **6** (+2) |
| Last event | 2026-05-19 15:15 |
| Culprit | `closure in in` (main.swift:1324) → CoreText font hang path |
| Release tag | `com.antigravity.krab-ear@2.0.2+2.0.2` |

**Verdict: REGRESSION — Wave 67 fix incomplete.**

The Wave 67 PR #412 replaced `●` Unicode literal with `circle.fill` SF Symbol in `StatusIndicatorView.swift`. However AGENT-J's stacktrace shows the hang still originates in the CoreText/glyph path:

```
CGFontCreateGlyphPath → FPFontCopyGlyphPath → TFPFont::CopyGlyphPath
→ TConcreteFontScaler::CopyGlyphPath → TTRenderGlyphs → AssureGlyphBlock
→ CreateScalerGlyphBlock → CreateGlyphElement → CreateGlyphOutline
→ StretchGlyph → ApplyFeaturesToGlyph → ApplyVariationsToGlyph
```

This is identical to the pre-fix font-rasterisation hang. Two explanations:
1. The running binary on `dist: 2.0.2` predates the Wave 67 fix (two-binary drift — old `native/runtime/KrabEarAgent` still in use).
2. A different Unicode glyph elsewhere in the codebase still triggers the same CoreText codepath.

The issue is `archived_forever` (ignored), so no alert noise, but the root cause is not fully resolved in production.

---

## NEW: KRAB-EAR-AGENT-M (48 h window)

| Issue | Title | Events | First Seen | Status | Culprit |
|-------|-------|--------|------------|--------|---------|
| KRAB-EAR-AGENT-M | App Hanging ≥2000 ms | 1 | 2026-05-18 23:26 | **resolved** | `BackendToast.show` |

**AGENT-M analysis:**
- Stack: `applicationDidFinishLaunching` → `showFatalAndTerminate` (main.swift:1033) → `BackendToast.show` (BackendToast.swift:40) → `NSWindow._doOrderWindow` → mach_msg hang
- This is a sister issue to AGENT-K (`BackendToast.createPanel`) and AGENT-H (`showFatalAndTerminate`). The fix applied for AGENT-K (PR #406 `guard nil window + weak capture`) did not cover the `BackendToast.show` → `NSWindow._doOrderWindow` → ColorSync/Space query path.
- 1 event, auto-resolved (likely the binary was rebuilt). But pattern is recurring.
- Recommendation: `BackendToast.show` needs the same main-thread guard applied — dispatch `orderFront` via `DispatchQueue.main.async` and add nil-window guard before `NSWindow._doOrderWindow` is called.

---

## Phase B Wired Error Codes — Event Volume (last 48 h)

### Wave 78 codes (wired Wave 205)

| Code | Events (48 h) | Status |
|------|--------------|--------|
| `stt.gigaam_worker_crashed` | 0 | Silent — zero production crashes or wiring broken |
| `ipc.rate_limit_exceeded` | 0 | Silent — no rate-limit hits or wiring broken |
| `stt.critical_recognition_error` | 0 | Silent |

### Wave 79 codes (wired Wave 224)

| Code | Events (48 h) | Status |
|------|--------------|--------|
| `stt.gigaam_hf_cache_miss` | 0 | Silent |
| `rewriter.model_unloaded` | 0 | Silent |
| `rewriter.output_ratio_fallback` | 0 | Silent |
| `stt.mlx_watchdog_hang` | 0 | Silent |
| `ipc.audio_device_poll_flood` | 0 | Silent |

**Note:** Zero events for all 8 codes across 48 h. Two interpretations:
1. No production triggers occurred (GigaAM was not invoked, no rate-limit hits, no MLX hangs).
2. The error bus `_push_error` Sentry guards added in Wave 248 may have suppressed event delivery in edge cases.

Recommended follow-up: manually trigger `ipc.rate_limit_exceeded` via a burst test to confirm the pipeline is live, rather than just silently clean.

---

## Release Health

| Release | Project | Verdict |
|---------|---------|---------|
| `krab-ear@2.0.0` | backend | 🟡 1 unresolved (`rewriter.timeout` x2, transient) |
| `com.antigravity.krab-ear@2.0.2` | agent | 🟡 AGENT-J persisting (6 events, ignored), AGENT-M new (resolved) |

**Overall: 🟡 YELLOW**

The system is functional with no crashing errors. Two yellow signals:
1. `rewriter.timeout` batch still showing (model gemma-4-26b-a4b-it-optiq under LM Studio).
2. AGENT-J CoreText hang not fully eliminated — two-binary drift is the suspected cause.

---

## Recommendations

1. **AGENT-J (CRITICAL for next release):** Rebuild Swift binary and copy to both `Krab Ear.app/Contents/MacOS/KrabEarAgent` AND `native/runtime/KrabEarAgent`. Verify `dist:` tag in next event is `>2.0.2`. If events continue post-rebuild, audit all remaining Unicode literals in Swift source.

2. **AGENT-M (BackendToast.show):** Apply the same `DispatchQueue.main.async` guard to `BackendToast.show` that was applied to `createPanel` in PR #406. The `showFatalAndTerminate` → `BackendToast.show` path runs from a background Task and hits main-thread-only AppKit APIs synchronously.

3. **KRAB-EAR-BACKEND-J (rewriter.timeout):** If model is upgraded from `gemma-4-26b-a4b-it-optiq` to a newer variant, mark resolved. Add model-version tag to rewriter timeout events for easier correlation.

4. **Phase B codes (Wave 78/79):** Run a smoke test for `ipc.rate_limit_exceeded` by triggering a burst of IPC calls above the token bucket limit to confirm the event pipeline is live end-to-end.

5. **Two-binary drift watchdog:** The `two-binary-drift-watch` routine (flagged as unregistered in Wave 248) remains unregistered. Register it to catch future drift before it causes silent production regressions.
