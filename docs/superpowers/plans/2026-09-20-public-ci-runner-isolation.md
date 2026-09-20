# Public CI Runner Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать все self-hosted jobs из публичного `Pavua/Krab-Ear`, сохранить Swift-проверки на бесплатном `macos-latest` и добавить fail-closed контракт против возврата опасного пути.

**Architecture:** Публичные workflow исполняются только на disposable GitHub-hosted runners. Audit-скрипт без новой Python-зависимости разбирает workflow через системный Ruby/Psych, запрещает dynamic/self-hosted `runs-on` и `pull_request_target`, а repository-level тест фиксирует политику.

**Tech Stack:** GitHub Actions YAML, Python 3 standard library, Ruby/Psych, `pytest`, Bash 3.2-compatible shell.

**Spec:** `docs/superpowers/specs/2026-09-20-public-ci-runner-isolation-design.md`

## Global Constraints

- База только `origin/codex/krab-ear-v2`; canonical checkout с бинарями и scratch WIP не менять.
- Не запускать собранный `KrabEarAgent`, production backend, MLX pack или полный Python suite.
- Не менять runner registration или GitHub settings в этом плане.
- Использовать только standard `macos-latest`; larger runner labels не использовать.
- Не добавлять `pull_request_target`; `git add` только явными путями.

## Review Focus

- Invalid YAML должен красить audit, а не разрешать workflow.
- Quoted/unquoted `on`, `.yml` и `.yaml` обрабатываются одинаково.
- Строка, список или expression в `runs-on` не скрывают `self-hosted`.
- `pull_request_target` всегда запрещён.
- Public MLX workflow удаляется только после готовности private replacement.

---

### Task 1: Fail-closed audit engine

**Files:**
- Create: `scripts/audit_public_ci_runner_isolation.py`
- Create: `KrabEar/tests/test_public_ci_runner_isolation.py`

**Interfaces:**
- Consumes: repository root and executable `ruby` with standard `yaml`/`json` libraries.
- Produces: `audit_tree(root: Path) -> list[Finding]`; CLI `--root PATH --fail-on-found`.

- [ ] **Step 1: Write failing fixture tests**

Create a test loader for the not-yet-existing audit module and temporary workflow fixtures. Pin these exact cases:

```python
def test_accepts_hosted_pull_request_workflow(self) -> None:
    self.write("ci.yml", "on:\n  pull_request:\njobs:\n  test:\n    runs-on: macos-latest\n")
    self.assertEqual(self.reasons(), [])

def test_rejects_self_hosted_string_and_list(self) -> None:
    self.write("a.yml", "on: push\njobs:\n  a:\n    runs-on: self-hosted\n")
    self.write("b.yaml", "on: [push]\njobs:\n  b:\n    runs-on: [self-hosted, macOS, ARM64]\n")
    self.assertEqual(sum("self_hosted_runner" in value for value in self.reasons()), 2)

def test_rejects_dynamic_runs_on(self) -> None:
    self.write("ci.yml", "on: pull_request\njobs:\n  test:\n    runs-on: ${{ matrix.runner }}\n")
    self.assertIn("dynamic_runs_on", self.reasons())

def test_rejects_pull_request_target(self) -> None:
    self.write("ci.yml", "on:\n  pull_request_target:\njobs:\n  test:\n    runs-on: ubuntu-latest\n")
    self.assertIn("pull_request_target_forbidden", self.reasons())

def test_invalid_yaml_fails_closed(self) -> None:
    self.write("ci.yml", "on: [pull_request\njobs: {}\n")
    self.assertTrue(any(value.startswith("yaml_parse_error:") for value in self.reasons()))
```

- [ ] **Step 2: Observe RED**

```bash
"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" \
  -m pytest --noconftest KrabEar/tests/test_public_ci_runner_isolation.py -q
```

Expected: failure because `scripts/audit_public_ci_runner_isolation.py` is absent.

- [ ] **Step 3: Implement the audit**

The module starts with a Russian docstring and `from __future__ import annotations`.
Define immutable `Finding(workflow: str, job: str, reason: str)`,
`load_workflow(path: Path) -> dict[str, object]`,
`audit_workflow(path: Path, root: Path) -> list[Finding]`,
`audit_tree(root: Path) -> list[Finding]`, and
`main(argv: list[str] | None = None) -> int`.

`load_workflow` calls Ruby as an argument array, never shell text:

```python
RUBY_PARSE = r'''require "json"
require "yaml"
data = YAML.safe_load(File.read(ARGV.fetch(0)), permitted_classes: [], permitted_symbols: [], aliases: false)
STDOUT.write(JSON.generate(data))
'''
result = subprocess.run(
    [ruby_path, "-e", RUBY_PARSE, str(path)],
    capture_output=True,
    text=True,
    timeout=15,
    check=False,
)
```

Handle Psych's YAML-1.1 boolean conversion by treating top-level key `true` as `on` only when `on` is absent. Parse errors create a finding whose reason begins `yaml_parse_error:` and includes only the exception class; stderr content is not returned. Reject every `self-hosted` token, every expression-based `runs-on`, malformed `jobs`, and `pull_request_target`. Scan both workflow extensions and fail when no workflows exist.

- [ ] **Step 4: Verify GREEN and syntax**

```bash
"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" \
  -m pytest --noconftest KrabEar/tests/test_public_ci_runner_isolation.py -q
"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" -m py_compile \
  scripts/audit_public_ci_runner_isolation.py KrabEar/tests/test_public_ci_runner_isolation.py
ruby -e 'require "yaml"; puts Psych::VERSION'
```

Expected: five tests pass; syntax commands exit `0`.

- [ ] **Step 5: Commit**

```bash
git add scripts/audit_public_ci_runner_isolation.py KrabEar/tests/test_public_ci_runner_isolation.py
git commit -m "test(ci): добавить гард public runner isolation"
```

---
### Task 2: Repository RED and hosted runner migration

**Files:**
- Modify: `KrabEar/tests/test_public_ci_runner_isolation.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/krabear-ci.yml`
- Delete: `.github/workflows/mlx-nightly.yml`

**Interfaces:**
- Consumes: `audit_tree(REPO_ROOT)` from Task 1.
- Produces: zero self-hosted jobs across all public workflows.

- [ ] **Step 1: Add a repository policy test**

```python
class RepositoryPolicyTest(unittest.TestCase):
    def test_public_repository_has_no_self_hosted_jobs(self) -> None:
        mod = load_module()
        findings = mod.audit_tree(REPO_ROOT)
        self.assertEqual([finding.describe() for finding in findings], [])
```

- [ ] **Step 2: Observe RED**

```bash
"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" \
  -m pytest --noconftest \
  KrabEar/tests/test_public_ci_runner_isolation.py::RepositoryPolicyTest::test_public_repository_has_no_self_hosted_jobs \
  -q
```

Expected: FAIL naming self-hosted jobs in `ci.yml`, `krabear-ci.yml`, and `mlx-nightly.yml`.

- [ ] **Step 3: Move Swift jobs to hosted macOS**

In both PR workflows replace only:

```yaml
runs-on: [self-hosted, macOS, ARM64]
```

with:

```yaml
runs-on: macos-latest
```

Keep checkout, SPM cache, release build, build-tests and filtered tests unchanged.

- [ ] **Step 4: Remove public MLX workflow**

Stage deletion of `.github/workflows/mlx-nightly.yml` in this isolated branch so the public policy test proves the final state. Do not push or merge this deletion until the private-controller branch contains its reviewed replacement.

- [ ] **Step 5: Add a fast guard job to both PR workflows**

```yaml
  public-runner-isolation-guard:
    name: Public runner isolation guard
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Verify standard Ruby YAML parser exists
        run: ruby -e 'require "yaml"; puts Psych::VERSION'
      - name: Reject self-hosted jobs and pull_request_target
        run: python3 scripts/audit_public_ci_runner_isolation.py --fail-on-found
```

Insert the same named job in `.github/workflows/krabear-ci.yml`; the duplicate
visible check is intentional because either workflow can later change independently.

- [ ] **Step 6: Verify GREEN**

```bash
"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" \
  -m pytest --noconftest KrabEar/tests/test_public_ci_runner_isolation.py -q
python3 scripts/audit_public_ci_runner_isolation.py --fail-on-found
while IFS= read -r -d '' workflow; do
  ruby -e 'require "yaml"; YAML.safe_load(File.read(ARGV.fetch(0)), aliases: false)' "$workflow"
done < <(find .github/workflows -maxdepth 1 -type f \( -name '*.yml' -o -name '*.yaml' \) -print0)
```

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/ci.yml .github/workflows/krabear-ci.yml \
  KrabEar/tests/test_public_ci_runner_isolation.py
git add -u .github/workflows/mlx-nightly.yml
git commit -m "fix(ci): изолировать public jobs от личного Mac"
```

---

### Task 3: Monitor metadata and durable documentation

**Files:**
- Modify: `scripts/launchagents/ai.krab.ear.runner-health.plist`
- Modify: `docs/NOW.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: existing checker flags `--repo` and `--runner-name`.
- Produces: source plist targeting the future private runner.

- [ ] **Step 1: Retarget plist arguments**

After the checker script path, insert:

```xml
        <string>--repo</string>
        <string>Pavua/Krab-CI-Control</string>
        <string>--runner-name</string>
        <string>krab-ear-m4max-private</string>
```

Update the comment to state that public Ear has no self-hosted registration.

- [ ] **Step 2: Update current docs**

Record in `docs/NOW.md` and the current CI section of `CLAUDE.md`:

```text
Public PR/push Swift CI: standard macos-latest, disposable.
MLX/Metal gate: private Pavua/Krab-CI-Control, exact trusted SHA.
Public Pavua/Krab-Ear self-hosted runner count after cutover: 0.
```

Retain the measured reason that hosted macOS cannot prove real Metal behavior.

- [ ] **Step 3: Validate and commit**

```bash
plutil -lint scripts/launchagents/ai.krab.ear.runner-health.plist
python3 scripts/verify_claude_md.py
rg -n 'Pavua/Krab-CI-Control|krab-ear-m4max-private' \
  scripts/launchagents/ai.krab.ear.runner-health.plist docs/NOW.md CLAUDE.md
git add scripts/launchagents/ai.krab.ear.runner-health.plist docs/NOW.md CLAUDE.md
git commit -m "docs(ci): зафиксировать private device gate"
```

---

### Task 4: Public branch verification and PR

**Files:**
- Review all Task 1-3 paths.

**Interfaces:**
- Consumes: three isolated commits.
- Produces: reviewable PR; no merge and no runner mutation.

- [ ] **Step 1: Refresh base and prove ancestry**

```bash
git fetch origin
git merge-base --is-ancestor origin/codex/krab-ear-v2 HEAD
git diff --name-status origin/codex/krab-ear-v2...HEAD
```

If ancestry fails, transplant the commits into a fresh worktree instead of merging divergence.

- [ ] **Step 2: Run proportional gates**

```bash
"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" \
  -m pytest --noconftest KrabEar/tests/test_public_ci_runner_isolation.py -q
python3 scripts/audit_public_ci_runner_isolation.py --fail-on-found
plutil -lint scripts/launchagents/ai.krab.ear.runner-health.plist
python3 scripts/verify_claude_md.py
git diff --check origin/codex/krab-ear-v2...HEAD
```

Do not run full pytest, Swift build, MLX, production restart or a built agent.

- [ ] **Step 3: Push and open PR**

Prepare the PR body with `apply_patch`, then push branch and create a PR against `codex/krab-ear-v2`. Its body must list RED→GREEN evidence, hosted pricing boundary, deleted public MLX workflow, and the fact that runner registration is unchanged until cutover.

- [ ] **Step 4: Exact-head CI and security gate**

Require terminal success for all hosted checks on the PR head SHA plus independent security review. Query the jobs API and confirm both Swift jobs have GitHub-hosted runner names rather than `krab-ear-m4max`. Do not merge until the private-controller branch is present and reviewed.
