# Source integration status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** привести оперативную документацию Ear к подтверждённому состоянию merged source/CI после #2084–#2088, сохранив границу с runtime и живой приёмкой.

**Architecture:** добавить один актуальный блок в начало NOW, сохранив прежние записи как исторические. Отдельный handoff содержит receipt GitHub/Git и команды повторной проверки; production source и runtime не меняются.

**Tech Stack:** Markdown, Git, существующий GitHub CLI; новые зависимости не нужны.

**Spec:** [NOW](../../NOW.md) и существующая [RMW-карточка](2026-10-11-rmw-roots.md); scope этой карточки — только синхронизация документационного статуса.

**База:** `origin/codex/krab-ear-v2`, `3c8a05bc67f51ef156034f4a37adf65f1807cec6`.
Owned worktree: `/Users/pablito/.codex/worktrees/ear-source-closeout-20261014/Krab Ear`.
Ветка: `codex/ear-source-closeout-20261014`.

## Global Constraints

- Ownership только `docs/NOW.md` и два документа `2026-10-14-source-integration-status.md` в `plans/` и `handoffs/`.
- Не изменять чужой WIP, behavior-код, shared checkout, `.remember` или глобальную память.
- Merge/deploy/restart, activation шифрования, Keychain, реальные history/shares/schedules и purge не входят в scope.
- OrbStack, VPN, процессы других сессий и соседние проекты не менять.
- Не переносить исторические CI/PID/health в текущий runtime-статус; loaded Python SHA остаётся UNKNOWN.
- Не commit/push в рамках worker-карточки; эти действия принадлежат координатору.

## Review Focus

- Старое DRAFT/unmerged в NOW: прежние записи явно обозначены историческими.
- CI другого SHA: оба post-merge runs должны относиться к полному `3c8a05bc67f51ef156034f4a37adf65f1807cec6`.
- Пропущенные Swift jobs: receipt различает 19 SUCCESS и 2 SKIPPED; повторный Swift run не заявляется.
- Source/runtime граница: release-конфигурация `9d7a0aac` не подтверждает загруженный Python SHA или deployment merged stack.
- UI/Keychain/activation: следующий source review жизненного цикла ключа и границы activation отделён от owner-scoped живой приёмки; причина текущих alerts не заявляется.

---

### Task 1: сверить source receipt

**Files:** Read `docs/NOW.md`, `docs/EXECUTOR_PLAYBOOK.md`, `docs/AGENT_SKILLS.md`.

- [x] Проверить Git root, чистый status, owned branch и базовый HEAD.

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --short
git rev-parse HEAD
```

- [x] Проверить merge SHA/date #2084–#2088, оба final CI runs, check conclusions и отсутствие открытых PR через read-only `gh`; точные команды сохранить в handoff.
- [x] Подтвердить пустой diff `KrabEar/` и `native/` между reviewed `7e49e4ced72b695834ced3c2872ebac8973f3d01` и final `3c8a05bc67f51ef156034f4a37adf65f1807cec6`.

```bash
git diff --exit-code 7e49e4ced72b695834ced3c2872ebac8973f3d01 3c8a05bc67f51ef156034f4a37adf65f1807cec6 -- KrabEar native
```

### Task 2: обновить оперативный статус и handoff

**Files:**
- Modify: `docs/NOW.md` — новый CURRENT перед историческими checkpoints.
- Create: `docs/superpowers/handoffs/2026-10-14-source-integration-status.md` — receipt, границы доказательств, следующий блок и воспроизводимые команды.
- Create: `docs/superpowers/plans/2026-10-14-source-integration-status.md` — эта карточка перед редактированием NOW/handoff.

- [x] Сохранить plan до остальных изменений.
- [x] Добавить CURRENT: пять merged PR, final SHA, два CI SUCCESS, 19/2 checks, tree parity и open PR 0.
- [x] Сохранить исторические записи без переписывания их результатов.
- [x] Добавить handoff с прямыми GitHub-ссылками и явным UNKNOWN для current loaded SHA/runtime-причины alerts.

### Task 3: проверить документационный diff

**Files:** только три owned Markdown-файла.

- [x] Выполнить `git diff --check` для NOW и whitespace-check новых файлов.
- [x] Проверить существование локальных относительных Markdown-ссылок во всех трёх документах: 34 ссылки, missing 0.
- [x] Просмотреть diff/stat, подтвердить отсутствие source/runtime-изменений и передать координатору без commit/push.

Behavior-тесты, Swift build и runtime E2E для этой docs-only карточки не запускаются: поведение не меняется. Проверки здесь — receipt Git/GitHub, whitespace, ссылки и review документационного diff.
