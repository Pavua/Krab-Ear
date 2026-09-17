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

## opencode wiring (2026-09-16, вступает после рестарта opencode)

- Skills: `~/.config/opencode/skill/` (16 копий: runtime-qualification,
  superpowers-набор, branch-handoff-governor, lmstudio-guard, remember)
  + авто-подхват `~/.claude/skills/` (10 krab-скиллов).
- Agents (файлами): `~/.config/opencode/agent/executor.md` (V4.1 Flash),
  `gate-security.md` (Fable, read-only).
- MCP (global `opencode.json`): `sentry` (нужен экспорт токена — см. ниже),
  `context7`, `chrome-devtools` (профиль владельца, `:9222`),
  `krab-hammerspoon` (`:8013/sse`, Mac-автоматизация).
  НЕ подключены осознанно: computer-use (нет бинарника), openclaw-browser
  (релей лежит), playwright (дубль), SaaS-зоопарк (раздувает контекст).
- Sentry-токен для MCP: деривация в `~/.zshrc` из Main Krab `.env`
  (проверено subshell). Малая модель: `opencode-go/glm-5.3-flash`.
- Команда `/goal`: `~/.config/opencode/command/goal.md` (пишет цель
  в `./.remember/goal.md` текущей сессии).

## Cross-model rule

- If an assistant starts only inside the repository and cannot resolve `~/.codex/skills`, treat this file as the source of truth for available skill workflows.
- If an assistant can access user-level skill store, prefer the canonical skill files above and keep this file aligned.

