# Krab Ear Design System

Foundation for UI consistency между code (`KrabEarTheme.swift`) и designs (Figma, mockups).

## Files in this directory

| File | Purpose |
|------|---------|
| `krab-ear-tokens.json` | W3C Design Tokens Community Group format — 46+ tokens (source of truth) |
| `FIGMA_REST_API_SETUP.md` | Personal Access Token setup для unlimited automation (bypass MCP rate limit) |
| `sync-report-2026-04-20.md` | Figma ↔ Swift drift report (resolved in PR #143) |
| `figma-console-scripts/` | Paste-run JS для Figma Plugin Console (free-plan workaround) |

## Figma file

**URL**: https://www.figma.com/design/IPngmhIJEH93vCoeliJkuV

**5 collections / 46 variables** imported via Plugin API:
- **Colors** (13): backgrounds, borders, text, status (error/success/warning), shadows
- **Spacing** (7): 4/8/12/24pt grid + cornerRadius + controlHeight
- **Typography** (13): fontSize, fontWeight, fontFamily, lineHeight
- **Motion** (8): 4 durations (0.15–0.70s) + 4 easing presets
- **Interaction** (5): hover/press/disabled alphas + scale

## Source of truth

All tokens originate in `native/KrabEarAgent/Sources/KrabEarAgent/KrabEarTheme.swift` — Swift `@MainActor enum KrabEarTheme` with nested namespaces:
- `KrabEarTheme.Colors.*` (dynamic NSColor getters)
- `KrabEarTheme.Typography.*` (NSFont builders)
- `KrabEarTheme.Metrics.*` (CGFloat constants)
- `KrabEarTheme.Motion.Duration.*` + `Motion.Easing.*`
- `KrabEarTheme.Interaction.*` (hover/press constants)

Swift values are the canonical source; `krab-ear-tokens.json` is an exported mirror.

## Workflow options (pick one based on task)

### Option A — Figma MCP (convenient, limited on free plan)

~5-10 tool calls/day on Starter plan, then 24h wait for reset.

Claude Code automatically uses `mcp__ed92eeab-...__use_figma` to create/edit. Best for:
- Creating Variables initially (done ✓)
- Small edits (single variable update)
- Reading context for design review

### Option B — Figma Plugin Console (unlimited, manual paste)

Workaround для free plan rate limit. No account upgrade needed.

1. Open Figma file → Menu → **Plugins → Development → Open Console**
2. Paste JS from `figma-console-scripts/*.js`
3. Click **Run**
4. See `Done: created X variables` in console output

Current scripts:
- `elevation-collection.js` — adds 18 shadow tokens (card/popup/overlay × 6 each)
- `settings-mockup.js` — creates 900×700 Settings panel frame with 5 sections

### Option C — Figma REST API (unlimited, fully automated)

Best for CI pipelines and bulk operations.

Requires Personal Access Token (free, doesn't count toward MCP quota). See `FIGMA_REST_API_SETUP.md` for setup.

Once PAT is configured:
- Full variable CRUD without rate limit
- Bulk sync scripts possible (`scripts/regenerate-design-tokens.sh` TODO)
- CI integration: auto-detect drift, auto-open PR

## Dynamic colors note

macOS uses adaptive NSColors (different in light/dark mode). This file mirrors **light-mode approximations**:
- `borderDark` / `borderLight` — exported separately (Figma needs explicit modes)
- `textPrimary` / `textSecondary` / `textDisabled` — light-mode values
- `accent` — default macOS system blue (`#0066FF`), user may override in System Settings

For true light+dark mode parity: Figma Variables support **modes** (requires Professional plan). On free plan — keep as hex approximations.

## Drift detection

Every sync pass compares Figma Variables ↔ Swift tokens. Last check:
- 43/52 matches ✅
- 2 value mismatches (resolved in PR #143)
- 7 naming differences (acceptable, documented)

Re-run compare manually через Claude session ("check drift Figma vs Swift"), or via future CI script.

## Canva integration

Parallel to Figma: Canva MCP (`generate-design`) creates marketing assets. Krab Ear currently has 4 poster candidates saved to user's Canva account (check canva.com/projects).

Use Canva for: promo posters, social media graphics, product sheets, announcement docs.
Use Figma for: precise UI design system, app mockups, component variants.

## Update workflow

When you change `KrabEarTheme.swift`:

1. **Swift first** — edit Colors/Typography/Motion values
2. **Regenerate JSON** (TODO: script) — extract updated tokens
3. **Push to Figma** — via MCP or Console JS
4. **Verify** via sync report

Until `regenerate-design-tokens.sh` exists: manual JSON edit + Figma re-import.

## Links

- [Figma Variables docs](https://help.figma.com/hc/en-us/articles/15339657135383)
- [W3C Design Tokens Community Group spec](https://design-tokens.github.io/community-group/format/)
- [Tokens Studio for Figma plugin](https://docs.tokens.studio/)
- Swift source: `native/KrabEarAgent/Sources/KrabEarAgent/KrabEarTheme.swift`
