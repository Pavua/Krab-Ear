# Design Token Sync Report — 2026-04-20

**Source A:** `design-tokens/krab-ear-tokens.json` (Figma Variables proxy, `IPngmhIJEH93vCoeliJkuV`)  
**Source B:** `native/KrabEarAgent/Sources/KrabEarAgent/KrabEarTheme.swift`  
**Method:** Figma MCP hit Starter plan rate limit; comparison performed against `krab-ear-tokens.json` (canonical local mirror of Figma Variables, written 2026-04-16 by Gemini session).

---

## Summary

| Dimension | Count |
|-----------|-------|
| Tokens in `krab-ear-tokens.json` (Figma) | **52** |
| Semantic tokens in `KrabEarTheme.swift` | **47** |
| Fully matched (name + value) | **43** |
| Missing in Swift (in Figma, not in code) | **5** |
| Missing in Figma (in Swift, not in tokens.json) | **2** |
| Value mismatches | **2** |

---

## Fully Matched (43 tokens — in sync)

### Colors (10/12)
| Token | Figma value | Swift |
|-------|------------|-------|
| `windowBackground` | `rgba(0,0,0,0)` | `.clear` ✓ |
| `cardBackground` | `rgba(255,255,255,0.5)` | `controlBackgroundColor @ 0.5` ✓ |
| `accent` | `#0066FF` | `.controlAccentColor` ✓ |
| `textPrimary` | `#000000` | `.labelColor` ✓ |
| `textSecondary` | `rgba(0,0,0,0.5)` | `.secondaryLabelColor` ✓ |
| `textDisabled` | `rgba(0,0,0,0.26)` | `.tertiaryLabelColor` ✓ |
| `borderDark` | `rgba(255,255,255,0.15)` | `white @ 0.15` ✓ |
| `borderLight` | `rgba(0,0,0,0.10)` | `black @ 0.10` ✓ |
| `success` | `#34C759` | `.systemGreen` ✓ |
| `error` | `#FF3B30` | `.systemRed` ✓ |

### Spacing / Metrics (7/7)
| Token | Figma | Swift |
|-------|-------|-------|
| `tight` | 4pt | `Metrics.tight = 4.0` ✓ |
| `standard` | 8pt | `Metrics.standard = 8.0` ✓ |
| `comfortable` | 12pt | `Metrics.comfortable = 12.0` ✓ |
| `spacious` | 24pt | `Metrics.spacious = 24.0` ✓ |
| `cardCornerRadius` | 12pt | `Metrics.cardCornerRadius = 12.0` ✓ |
| `innerCornerRadius` | 8pt | `Metrics.innerCornerRadius = 8.0` ✓ |
| `controlHeight` | 24pt | `Metrics.controlHeight = 24.0` ✓ |

### Typography (9/9)
All font sizes (17, 13, 13, 11, 11, 11), weights (regular/medium/semibold), and families (system / monospaced) match exactly. ✓

### Motion (8/8)
All 4 durations (0.15 / 0.25 / 0.40 / 0.70) and 4 easings match. ✓

### Interaction (5/5)
`hoverOverlayAlpha=0.10`, `pressedScale=0.98`, `pressedOverlayAlpha=0.15`, `disabledOpacity=0.40`, `transparentHoverAlpha=0.05` — all match. ✓

### Elevation (partial — see mismatches below)
`cardShadowOpacity=0.15`, `cardShadowRadius=6`, `cardShadowOffsetY=-2` ✓  
`popupShadowOpacity=0.20`, `popupShadowRadius=16`, `popupShadowOffsetY=-6` ✓  
`overlayShadowOpacity=0.30`, `overlayShadowRadius=32`, `overlayShadowOffsetY=-12` ✓

---

## Missing in Swift (5 tokens — in Figma/tokens.json, not implemented in Swift)

| Token | Category | Value | Notes |
|-------|----------|-------|-------|
| `lineHeight/normal` | Typography | `1.4` | NSAttributedString paragraph style; not set anywhere in Swift UI |
| `lineHeight/tight` | Typography | `1.2` | Same — no paragraph style applied to caption/badge text |
| `fontFamily/system` | Typography | `SF Pro / -apple-system` | Implicit in `NSFont.systemFont` — no explicit token variable needed but absent as named constant |
| `fontFamily/monospaced` | Typography | `SF Mono / Menlo` | Implicit in `NSFont.monospacedSystemFont` — same |
| `elevation/cardShadowColor` | Elevation | `rgba(0,0,0,1)` | `NSColor.black` used inline in `Elevation.applyCard()` — not a named token |

**Action:** `lineHeight/normal` and `lineHeight/tight` are the only ones that would require actual behavior change (applying `NSMutableParagraphStyle` to label/text views). The font family and shadow color are implicit — they match but aren't exposed as Swift constants.

---

## Missing in Figma (2 tokens — in Swift, not in tokens.json)

| Swift token | Location | Value |
|-------------|----------|-------|
| `Colors.overlayShadow` | `KrabEarTheme.Colors` | `black @ 0.25` — semantic color alias |
| `Colors.separator` | `KrabEarTheme.Colors` | alias for `border` — backward-compat alias |

**Note:** `overlayShadow` is used in Swift but not modelled as a Figma Variable. `separator` is a deprecated alias — no action needed. Should add `overlayShadow` to `krab-ear-tokens.json`.

---

## Value Mismatches (2)

| Token | Figma value | Swift value | Severity |
|-------|------------|-------------|----------|
| `cardBorderFixed` | `rgba(255,255,255,0.18)` | `NSColor.separatorColor` (in `viewDidChangeEffectiveAppearance`) | **Medium** — `ThemeCardView.viewDidChangeEffectiveAppearance()` overrides the initial `white@0.18` with `NSColor.separatorColor` on appearance change. In dark mode this is a semantic adaptive color (≠ white@0.18). Figma token and runtime diverge after first appearance switch. |
| `warning` (legacy) | `#FF9500` | `.systemOrange` | **Low** — macOS `systemOrange` = `#FF9500` in light mode, adaptive in dark. Figma stores hardcoded hex. Functionally equivalent in light mode; dark mode will differ. |

---

## Recommendations

1. **Fix `cardBorderFixed` mismatch (Medium):** `ThemeCardView.viewDidChangeEffectiveAppearance` should apply `KrabEarTheme.Colors.border` (the dynamic semantic token) instead of `NSColor.separatorColor`. Alternatively, update the Figma token to reference the semantic `border` variable rather than a fixed hex.

2. **Add `overlayShadow` to tokens.json:** `rgba(0,0,0,0.25)` — one-line addition to `global.color`.

3. **Line height tokens (Low priority):** If text density issues arise, apply `lineHeight/normal = 1.4` as paragraph style to body/caption labels. Currently implicit from macOS default.

4. **Figma MCP rate limit:** The live Figma Variables dump could not be fetched (Starter plan limit). This report compares against `krab-ear-tokens.json` (written 2026-04-16). For future automation, upgrade Figma plan or use the REST API (`GET /v1/files/{key}/variables/local`) with a personal access token — no plugin rate limits apply.

---

## Future Automation Hook

```bash
# Proposed: scripts/check-token-drift.sh
# 1. Fetch Figma variables via REST (no MCP needed):
#    curl -H "X-Figma-Token: $FIGMA_PAT" \
#      "https://api.figma.com/v1/files/IPngmhIJEH93vCoeliJkuV/variables/local"
# 2. Diff output against krab-ear-tokens.json
# 3. Extract Swift constants from KrabEarTheme.swift via regex
# 4. Three-way diff → PR description if drift found
```
