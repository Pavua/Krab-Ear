# Ear Runner Private Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** После зелёных source-гейтов удалить Ear runner из public repo, зарегистрировать чистый instance только в private controller и доказать exact-SHA MLX run без вмешательства в production Ear.

**Architecture:** Cutover — последовательная внешняя операция с fail-closed preflight, API read-back и невозвратным security-инвариантом. Старый public listener останавливается до регистрации private listener; одновременно они не работают.

**Tech Stack:** GitHub REST through `gh`, GitHub Actions runner 2.x, launchd runner service, private controller workflow, Ear IPC probes.

**Spec:** `docs/superpowers/specs/2026-09-20-public-ci-runner-isolation-design.md`

## Global Constraints

- Начинать только после зелёных public isolation PR и private-controller security review, но до public merge.
- Merge каждого repo требует отдельного owner-approved решения.
- Не останавливать `ai.krab.ear.backend`, `ai.krab.ear.rest`, `ai.krab.ear.agent`, Krab или Voice Gateway.
- Не печатать tokens, `.credentials`, `.runner`, environment или полный argv.
- Не использовать `rm -rf`, broad glob, `$HOME` или `~` как destructive target.
- Если запись/встреча или runner busy — остановиться без mutation.
- Public runner после удаления не регистрируется обратно.

## Review Focus

- Между stop и remove старый listener не должен перезапуститься через launchd.
- Новый archive — официальный macOS ARM64 asset с проверенным digest.
- Public API должен показать ноль runners, а не просто offline.
- Новый runner не переиспользует старый public `_work`.
- Первый heavy run стартует только после повторного admission preflight.

---

### Task 1: Exact-state and quiet-window preflight

**Files:**
- No repository edits.

**Interfaces:**
- Consumes: merged SHAs and live GitHub/IPC/runner state.
- Produces: recorded preflight evidence or clean stop without mutation.

- [ ] **Step 1: Re-query source truth**

```bash
public_pr_number=$(gh pr list --repo Pavua/Krab-Ear \
  --head codex/public-ci-isolation-design-0920 --state all \
  --json number --jq '.[0].number')
test -n "$public_pr_number"
gh pr view "$public_pr_number" --repo Pavua/Krab-Ear \
  --json state,mergeCommit,statusCheckRollup
gh repo view Pavua/Krab-CI-Control --json visibility,defaultBranchRef
gh api repos/Pavua/Krab-CI-Control/commits/main --jq .sha
gh api repos/Pavua/Krab-Ear/actions/runners \
  --jq '{total_count,runners:[.runners[]|{name,status,busy,labels:[.labels[].name]}]}'
```

Stop unless exactly one matching PR exists, public checks are terminal green,
the controller is private, and its exact SHA passed security review. Public PR
must remain unmerged until private acceptance completes.

- [ ] **Step 2: Check idle/resource state twice**

Run the controller admission probe with `--dry-run`, wait 60 seconds, run it again. Both must admit. Read process state with `pid,user,pcpu,pmem,etime,comm` only; do not expose argv.

- [ ] **Step 3: Snapshot allowed services**

```bash
launchctl list | rg 'actions.runner.Pavua-Krab-Ear|ai.krab.ear.runner-health'
ps -axo pid,user,pcpu,pmem,etime,comm | rg 'Runner.Listener|Runner.Worker'
```

---

### Task 2: Stop and unregister the public runner

**Files:**
- External state: `/Users/pablito/actions-runner-krab-ear` registration/service.

**Interfaces:**
- Consumes: idle public `krab-ear-m4max`.
- Produces: stopped old service and public runner count `0`.

- [ ] **Step 1: Stop only the Ear runner service**

```bash
cd '/Users/pablito/actions-runner-krab-ear'
./svc.sh status
./svc.sh stop
./svc.sh status
```

- [ ] **Step 2: Obtain a remove token without printing and unregister**

```bash
ear_remove_token=$(gh api --method POST \
  repos/Pavua/Krab-Ear/actions/runners/remove-token --jq .token)
test -n "$ear_remove_token"
./config.sh remove --token "$ear_remove_token"
unset ear_remove_token
```

- [ ] **Step 3: Prove removal**

```bash
gh api repos/Pavua/Krab-Ear/actions/runners \
  --jq '{total_count,runners:[.runners[]|{name,status,busy}]}'
```

Expected: `total_count: 0`; offline/nonzero is not accepted.

---

### Task 3: Install a clean private runner instance

**Files:**
- Create: `/Users/pablito/actions-runner-krab-ci-control`
- External service: generated private runner LaunchAgent.

**Interfaces:**
- Consumes: official latest `actions/runner` macOS ARM64 archive and private registration token.
- Produces: `krab-ear-m4max-private` with label `krab-ear-device`.

- [ ] **Step 1: Resolve one official asset and digest**

Read `repos/actions/runner/releases/latest`; select exactly one asset matching `actions-runner-osx-arm64-*.tar.gz`. Require API `digest` to begin `sha256:`. Zero/multiple assets or missing digest blocks.

- [ ] **Step 2: Download and verify in unique staging**

```bash
runner_stage=$(mktemp -d /tmp/krab-ci-runner.XXXXXX)
```

Download the selected asset, calculate `shasum -a 256`, and require equality with the API digest before extraction.

- [ ] **Step 3: Extract into a new explicit directory**

```bash
test ! -e '/Users/pablito/actions-runner-krab-ci-control'
mkdir '/Users/pablito/actions-runner-krab-ci-control'
tar -xzf "$runner_stage/$runner_asset_name" \
  -C '/Users/pablito/actions-runner-krab-ci-control'
```

Never copy old `_work`, `.runner`, `.credentials` or `_diag`.

- [ ] **Step 4: Register privately and start service**

```bash
private_registration_token=$(gh api --method POST \
  repos/Pavua/Krab-CI-Control/actions/runners/registration-token --jq .token)
test -n "$private_registration_token"
cd '/Users/pablito/actions-runner-krab-ci-control'
./config.sh \
  --url https://github.com/Pavua/Krab-CI-Control \
  --token "$private_registration_token" \
  --name krab-ear-m4max-private \
  --labels krab-ear-device \
  --work _work \
  --unattended
unset private_registration_token
./svc.sh install
./svc.sh start
```

- [ ] **Step 5: Read back private state**

```bash
gh api repos/Pavua/Krab-CI-Control/actions/runners \
  --jq '{total_count,runners:[.runners[]|{name,status,busy,labels:[.labels[].name]}]}'
```

Expected: one online idle runner with `self-hosted`, `macOS`, `ARM64`, `krab-ear-device`.

---

### Task 4: Retarget runner health monitor

**Files:**
- Modify external: `~/Library/LaunchAgents/ai.krab.ear.runner-health.plist`
- Source: merged `scripts/launchagents/ai.krab.ear.runner-health.plist`

**Interfaces:**
- Consumes: merged private repo/name flags.
- Produces: monitor observing only the private runner.

- [ ] **Step 1: Validate source**

```bash
plutil -lint scripts/launchagents/ai.krab.ear.runner-health.plist
plutil -p scripts/launchagents/ai.krab.ear.runner-health.plist | \
  rg 'Pavua/Krab-CI-Control|krab-ear-m4max-private'
```

- [ ] **Step 2: Install only monitor plist**

Back up the single current plist to a timestamped explicit file. Copy merged plist, lint destination, then bootout/bootstrap only `ai.krab.ear.runner-health`; do not touch Ear services.

- [ ] **Step 3: Dry-run checker without notification**

```bash
"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" \
  scripts/krab_ear_runner_health_check.py \
  --dry-run --no-telegram \
  --repo Pavua/Krab-CI-Control \
  --runner-name krab-ear-m4max-private
```

Expected: `status=online`, `actionable=False`.

---

### Task 5: Controlled exact-SHA acceptance run

**Files:**
- No source edits.

**Interfaces:**
- Consumes: online private runner, trusted Ear SHA and two clean admission probes.
- Produces: terminal private run URL plus unchanged production evidence.

- [ ] **Step 1: Capture runtime baseline**

Record Ear backend/REST/agent PIDs, IPC/REST health, memory free percentage and swap counters. Do not restart or load a model.

- [ ] **Step 2: Dispatch exact trusted SHA**

```bash
ear_sha=$(gh api repos/Pavua/Krab-Ear/commits/codex/krab-ear-v2 --jq .sha)
test "${#ear_sha}" -eq 40
gh workflow run krab-ear-mlx.yml \
  --repo Pavua/Krab-CI-Control \
  -f sha="$ear_sha"
```

Read back by matching creation time, actor and input SHA; do not accept an unmatched latest run.

- [ ] **Step 3: Wait for terminal result and cleanup**

Require identical requested/resolved SHA and terminal success. Verify no pytest/MLX child remains and runner is idle.

- [ ] **Step 4: Compare runtime**

Ear PIDs and health remain unchanged. Report memory/IO as deltas; old nonzero swap is not current-pressure evidence.

- [ ] **Step 5: Final boundary read-back**

```bash
gh api repos/Pavua/Krab-Ear/actions/runners --jq .total_count
gh api repos/Pavua/Krab-CI-Control/actions/runners \
  --jq '.runners[] | {name,status,busy}'
```

Expected: public `0`; private online and idle.

- [ ] **Step 6: Merge public hosted-only PR after private acceptance**

Only after an explicit owner-approved merge decision, merge the rebased public
PR and verify post-merge hosted CI on its exact merge SHA. Runner registration
must already be absent from public Ear; do not use merge as a reason to restore it.

---

### Task 6: Failure handling without security rollback

**Files:**
- No source edits unless a reviewed follow-up is required.

**Interfaces:**
- Consumes: failed registration, workflow or device gate.
- Produces: safe public-zero/private-only state and precise blocker report.

- [ ] **Step 1: Preserve invariant**

Never register back to `Pavua/Krab-Ear`. On private failure, keep public registration absent and use owner-controlled local exact-SHA execution only after identical admission probes.

- [ ] **Step 2: Report layers separately**

Report public source, private source, registration, admission, MLX terminal result and runtime impact as six distinct states.
