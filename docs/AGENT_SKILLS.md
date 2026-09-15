# AGENT_SKILLS

## What this file is

Single place for agent-facing “skill” workflows used in Krab Ear operations.
Used as a discovery index by AI assistants when available.

## Skills in use

- **krab-runtime-qualification**
  - Goal: run CI + Sentry + launchd/PID + health checks before any runtime call.
  - Path (Codex local skills store): `~/.codex/skills/krab-runtime-qualification/SKILL.md`
  - Trigger phrases: CI checks, runtime qualification, Sentry status, launchd restart-readiness, safe test-call checks, PID/health verification.

- **remember**
  - Goal: write handoff notes in `.remember/remember.md`.
  - Path (Codex local skills store): `~/.codex/skills/remember/SKILL.md`.

## Cross-model rule

- If an assistant starts only inside the repository and cannot resolve `~/.codex/skills`, treat this file as the source of truth for available skill workflows.
- If an assistant can access user-level skill store, prefer the canonical skill files above and keep this file aligned.

