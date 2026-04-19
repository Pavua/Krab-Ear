# Documentation Structure — Consolidated (2026-04-18)

Following AA audit P2 resolution, duplicate root vs docs/ markdown files have been consolidated:

## File Mapping

| Product | Root File | Docs File | Status |
|---------|-----------|-----------|--------|
| **Krab Ear** (Voice Assistant) | `/CHANGELOG.md` (authoritative) | `docs/CHANGELOG.md` | MERGED → `/CHANGELOG.md` (archived content) |
| **Krab Core** (Telegram Bot) | `/PRD-KRAB-CORE.md` | — | RENAMED from `/PRD.md` |
| **Krab Ear** (Voice Assistant) | — | `docs/PRD-KRAB-EAR.md` | RENAMED from `docs/PRD.md` |
| **Krab Core** (Telegram Bot) | `/ARCHITECTURE-KRAB-CORE.md` | — | RENAMED from `/ARCHITECTURE.md` |
| **Krab Ear** (Voice Assistant) | — | `docs/ARCHITECTURE-KRAB-EAR.md` | RENAMED from `docs/ARCHITECTURE.md` |

## Guidelines

- **Single source of truth**: CHANGELOG lives at root (`/CHANGELOG.md`), includes all historical versions (v2.0–v2.2 archived)
- **Product separation**: Core (Krab Telegram userbot) and Ear (voice assistant) PRD/ARCHITECTURE files are now explicitly named to avoid confusion
- **Krab Ear docs**: Primary technical docs live in `docs/` (ARCHITECTURE-KRAB-EAR.md, PRD-KRAB-EAR.md, and all research/specs)

## Related Files

- Root `/CLAUDE.md` — Engineering guidelines, architecture summary, common commands
- Root `README.md` — Quick start for Krab Ear
- `docs/ROADMAP_VA.md` — Voice Assistant phase roadmap (Phases 1–4)
- `docs/ROADMAP_ECOSYSTEM.md` — Krab ecosystem (Core + Ear + Voice + OpenClaw) integration

## Verification

No broken cross-references. All `.md` links still resolve (rename was consolidation-only, no content deletion except `/docs/CHANGELOG.md` which was merged to root).

PR: #48 (session 2026-04-18)
