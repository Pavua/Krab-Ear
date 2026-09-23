# Private CI Controller Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать private `Pavua/Krab-CI-Control`, который запускает Ear MLX/Metal gate только для exact SHA доверенной default-ветки и не имеет public write credential.

**Architecture:** Controller хранит orchestration-код, разрешает SHA через hard-coded public remote, создаёт чистый detached checkout, выполняет admission preflight и запускает неизменённое MLX-подмножество. Runner подключается позднее отдельным cutover-планом.

**Tech Stack:** Private GitHub repository, GitHub Actions, Python standard library, Bash 3.2, Git CLI, macOS IPC and `memory_pressure`.

**Spec:** `Krab-Ear/docs/superpowers/specs/2026-09-20-public-ci-runner-isolation-design.md`

## Global Constraints

- Repo создаётся только с `visibility=PRIVATE`; API read-back обязателен.
- До cutover runner не регистрировать и heavy MLX не запускать.
- Не добавлять PAT, deploy key, webhook или public-repo write permission.
- Manual dispatch принимает только полный lowercase SHA из `codex/krab-ear-v2`.
- Pre-existing/symlink destination и unknown admission отклоняются.
- Никакого production restart или запуска Swift agent.

## Review Focus

- Short/non-hex SHA отклоняется до Git invocation.
- Commit orphan/fork branch отклоняется ancestry check.
- Checkout не следует symlink и не переиспользует каталог.
- IPC timeout/malformed/duplicate-key response закрывает admission.
- Cancellation завершает process group, а не только shell parent.

---

### Task 1: Private repository skeleton

**Files:**
- Create: `AGENTS.md`
- Create: `README.md`
- Create: `.gitignore`
- Create: `tests/__init__.py`

**Interfaces:**
- Consumes: authenticated `gh` account `Pavua`.
- Produces: `/Users/pablito/Antigravity_AGENTS/Krab-CI-Control` and private remote.

- [ ] **Step 1: Prove the name is unused**

```bash
if gh repo view Pavua/Krab-CI-Control --json nameWithOwner >/dev/null 2>&1; then
  echo 'REFUSED: inspect existing Pavua/Krab-CI-Control' >&2
  exit 1
fi
```

- [ ] **Step 2: Initialize an empty local repository**

```bash
test ! -e '/Users/pablito/Antigravity_AGENTS/Krab-CI-Control'
mkdir '/Users/pablito/Antigravity_AGENTS/Krab-CI-Control'
git -C '/Users/pablito/Antigravity_AGENTS/Krab-CI-Control' init -b main
```

`AGENTS.md` encodes private visibility, hard-coded Ear remote/default branch, no arbitrary refs, no secret output, one heavy job and exact-SHA evidence. Use:

```gitignore
.DS_Store
__pycache__/
*.pyc
.pytest_cache/
.runner/
_diag/
_work/
artifacts/
```

- [ ] **Step 3: Commit, create remote and verify privacy**

```bash
git add AGENTS.md README.md .gitignore tests/__init__.py
git commit -m "chore: создать private CI controller"
gh repo create Pavua/Krab-CI-Control --private \
  --source '/Users/pablito/Antigravity_AGENTS/Krab-CI-Control' --remote origin --push
gh repo view Pavua/Krab-CI-Control --json visibility,nameWithOwner \
  --jq '{nameWithOwner,visibility}'
```

Expected: `visibility=PRIVATE`; otherwise stop before workflows.

---

### Task 2: Trusted exact-SHA checkout

**Files:**
- Create: `scripts/trusted_ear_checkout.py`
- Create: `tests/test_trusted_ear_checkout.py`

**Interfaces:**
- Consumes: `--requested-sha`, `--destination`; production remote fixed to `https://github.com/Pavua/Krab-Ear.git`.
- Produces: JSON with `requested_sha`, `resolved_sha`, `checked_out_sha`; exit `0` accepted, `2` rejected.

- [ ] **Step 1: Write RED tests using a temporary local Git remote**

Implement tests named:

```text
test_rejects_short_or_non_hex_sha
test_default_request_resolves_trusted_branch_head
test_accepts_ancestor_of_trusted_branch
test_rejects_orphan_branch_commit
test_rejects_existing_destination
test_rejects_symlink_destination
test_reports_identical_resolved_and_checked_out_sha
```

The fixture creates two commits on the trusted branch and one orphan commit. Every subprocess uses an argument list and `shell=False`. Run `python3 -m unittest tests.test_trusted_ear_checkout -v`; expected import failure.

- [ ] **Step 2: Implement validation and checkout**

Define exact interfaces:

```text
validate_requested_sha(value: str) -> str
run_git(args: list[str], cwd: Path | None = None) -> str
resolve_and_checkout(requested_sha: str, destination: Path, remote: str, trusted_branch: str) -> dict[str, str]
```

Validation is a full match of `[0-9a-f]{40}`. Required Git sequence:

```text
git init <new destination>
git -C <destination> remote add origin <remote>
git -C <destination> fetch --no-tags --filter=blob:none origin refs/heads/codex/krab-ear-v2:refs/remotes/origin/codex/krab-ear-v2
git -C <destination> rev-parse refs/remotes/origin/codex/krab-ear-v2
git -C <destination> cat-file -e <requested full SHA>^{commit}
git -C <destination> merge-base --is-ancestor <resolved SHA> refs/remotes/origin/codex/krab-ear-v2
git -C <destination> checkout --detach <resolved SHA>
git -C <destination> status --porcelain
git -C <destination> rev-parse HEAD
```

Reject dirty or mismatched checkout. Errors expose safe stage/type, never raw stderr.

- [ ] **Step 3: GREEN and commit**

```bash
python3 -m unittest tests.test_trusted_ear_checkout -v
python3 -m py_compile scripts/trusted_ear_checkout.py tests/test_trusted_ear_checkout.py
git add scripts/trusted_ear_checkout.py tests/test_trusted_ear_checkout.py
git commit -m "feat(ear): добавить trusted exact-SHA checkout"
```

---
### Task 3: Fail-closed admission probe

**Files:**
- Create: `scripts/check_ear_device_admission.py`
- Create: `tests/test_ear_device_admission.py`

**Interfaces:**
- Consumes: Ear IPC socket, `/usr/bin/memory_pressure -Q`, `ps -axo pid=,ppid=,comm=`.
- Produces: JSON `admitted`, `reason`, `memory_free_percent`, `other_runner_workers`; exit `0` admitted, `75` denied, `2` invalid configuration.

- [ ] **Step 1: Write RED tests with injected probes**

Cover active recording, active meeting, IPC timeout, malformed/duplicate-key JSON, memory below `20%`, second `Runner.Worker`, and clean admission. Run `python3 -m unittest tests.test_ear_device_admission -v`; expected import failure.

- [ ] **Step 2: Implement exact safety semantics**

Use the Ear `scripts/safe_backend_restart.command` envelope: methods `get_recording_state`/`get_meeting_live_state`, fields `is_recording`/`active`, 4-second timeout, 1 MiB cap and duplicate-key rejection. Parse only `System-wide memory free percentage: N%`; minimum is `20`.

Walk ancestors of `os.getpid()` to identify this job's `Runner.Worker`. Deny another executable ending `/Runner.Worker`, or external `pytest`, `xcodebuild`, or `swift-build`. Unknown output denies.

- [ ] **Step 3: GREEN, dry-run and commit**

```bash
python3 -m unittest tests.test_ear_device_admission -v
python3 -m py_compile scripts/check_ear_device_admission.py tests/test_ear_device_admission.py
python3 scripts/check_ear_device_admission.py --dry-run
git add scripts/check_ear_device_admission.py tests/test_ear_device_admission.py
git commit -m "feat(ear): добавить device-gate admission probe"
```

Dry-run exit `75` with an honest reason is acceptable and must not mutate state.

---

### Task 4: Cancellation-safe MLX workflow

**Files:**
- Create: `scripts/process_group_runner.py`
- Create: `scripts/run_ear_mlx_gate.sh`
- Create: `tests/test_process_group_runner.py`
- Create: `tests/test_run_ear_mlx_gate_contract.py`
- Create: `.github/workflows/krab-ear-mlx.yml`

**Interfaces:**
- Consumes: trusted Ear checkout and runner temp paths.
- Produces: migrated MLX result, exact-SHA summary and no surviving child group.

- [ ] **Step 1: Write RED source-contract tests**

Pin launcher arguments, process-group signal forwarding, MLX floor `50`, eight chunks, 300-second chunk deadline, 120-second per-file fallback, and absence of Swift-agent launch. Pin workflow schedule `30 2 * * *`, optional full SHA input, `contents: read`, `cancel-in-progress: false`, and labels `[self-hosted, macOS, ARM64, krab-ear-device]`.

- [ ] **Step 2: Implement launcher with unchanged test semantics**

`process_group_runner.py` starts the supplied command with
`start_new_session=True`, forwards `SIGINT`/`SIGTERM` with
`os.killpg(child.pid, signal_number)`, waits for a bounded grace period, then
sends `SIGKILL` to the same group. Its test spawns a child and grandchild and
proves that both disappear on cancellation.

The shell launcher fixed prologue is:

```bash
#!/bin/bash
set -euo pipefail
checkout=${1:?checkout path required}
runner_temp=${2:?runner temp required}
test -d "$checkout/.git" || { echo 'invalid checkout' >&2; exit 2; }
test -d "$runner_temp" || { echo 'invalid runner temp' >&2; exit 2; }
cd "$checkout"
```

Transfer unchanged from Ear `mlx-nightly.yml`: selector `mlx|MLX`, floor `50`, `nchunks=8`, 300/120-second deadlines, orphan reap patterns and aggregate result. Replace `/tmp/pf.log` with `${runner_temp}/pf-${BASHPID}.log`.

- [ ] **Step 3: Implement private workflow**

Use schedule + manual dispatch only, `permissions: contents: read`, concurrency `krab-ear-device-gate`, `cancel-in-progress: false`, timeout 120 minutes. Steps: controller checkout; admission; trusted checkout into `$RUNNER_TEMP/ear-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT`; Python 3.12 venv; brew ffmpeg/coreutils; Ear requirements plus pytest/flake8; launcher; always-written requested/resolved SHA and attempt summary.

- [ ] **Step 4: Static GREEN and commit without dispatch**

```bash
bash -n scripts/run_ear_mlx_gate.sh
python3 -m unittest tests.test_process_group_runner -v
python3 -m unittest tests.test_run_ear_mlx_gate_contract -v
git diff --check
git add scripts/process_group_runner.py scripts/run_ear_mlx_gate.sh \
  tests/test_process_group_runner.py tests/test_run_ear_mlx_gate_contract.py \
  .github/workflows/krab-ear-mlx.yml
git commit -m "feat(ear): перенести MLX gate в private controller"
```

---

### Task 5: Controller source acceptance

**Files:**
- Review all controller files.

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: pushed private `main`, without heavy execution.

- [ ] **Step 1: Run lightweight gates**

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py tests/*.py
bash -n scripts/run_ear_mlx_gate.sh
git diff --check
```

- [ ] **Step 2: Push and read back boundary**

```bash
git push origin main
gh repo view Pavua/Krab-CI-Control --json visibility,defaultBranchRef \
  --jq '{visibility,default_branch:.defaultBranchRef.name}'
gh workflow list --repo Pavua/Krab-CI-Control
```

Expected: `PRIVATE`, `main`, workflow visible but not dispatched.

- [ ] **Step 3: Independent security review**

Review exact `main` SHA for injection, arbitrary-ref checkout, symlink/path escape, cancellation/reaping and secret exposure. Any HIGH/MED finding blocks cutover.
