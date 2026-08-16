# W1 Repo Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать шум из GitHub Issues и зафиксировать инвентарь веток/worktree, не удаляя remote-ветки пачкой и не трогая код продукта.

**Architecture:** Генератор дублей — Claude scheduled-task `krab-ear-test-health` (`~/.claude/scheduled-tasks/krab-ear-test-health/SKILL.md`), не workflow репозитория. Он гоняет полный локальный pytest, который хронически виснет (~5%, `BackendService` без `close()`), и каждый день открывает `ci: test suite failure — YYYY-MM-DD`. W1: (1) сузить генератор, (2) закрыть дубли, оставив #1909 (hang) и #1919 (последний ubuntu-сигнал), (3) отчёт по worktree и `audit/*` без `git push --delete` и без `git worktree remove` без явного «да» владельца.

**Tech Stack:** `gh`, git, существующий `scripts/cleanup_worktree_shadows.command` (только lsregister, не массовое удаление деревьев).

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/w1-repo-hygiene`. Модель: Composer 2.5 Fast / Grok medium. Режим: локальный Agent. Cloud — нет.

**Баны:** база только `origin/codex/krab-ear-v2`; `git add` явными путями, никогда `-A`; не запускать `KrabEarAgent`; не `kickstart -k`; не мержить PR #1875; не удалять `origin/audit/*` пачкой; не `git worktree remove` без списка, подтверждённого владельцем; не коммитить `wake_word_models/hard_negatives_raw/tts_phrases.json`; не трогать Main Krab / VG `.env`.

**Снимок на 2026-08-16 (перепроверь командами в Task 1, не копируй вслепую если числа уехали):**

- HEAD: `5a6559df` на `codex/krab-ear-v2`
- Локальных веток: 1812, remote: 1626, локальных `audit*`: 298, remote `audit*`: 360
- Worktree: 1 основной + 3 под `.claude/worktrees/` + 26 под `.worktrees/` (даты в имени 20260720–20260725) + 1 соседний `/Users/pablito/Antigravity_AGENTS/KrabEar_gigaam_mlx_wave_20260731`
- Открытых issues: 55, почти все `ci: test suite failure — DATE`
- Открытый PR: только #1875 (не мержить)
- Неотслеживаемое: `wake_word_models/hard_negatives_raw/tts_phrases.json`

---

### Task 1: Зафиксировать инвентарь в репо

**Files:**
- Create: `docs/hygiene/2026-08-16-w1-inventory.md`

- [ ] **Step 1: Собрать живые числа**

Run (из корня репо, не из worktree-тени, если она detached):

```bash
git fetch origin
echo "HEAD=$(git rev-parse --short HEAD) $(git branch --show-current)"
echo -n "local_branches="; git branch | wc -l | tr -d ' '
echo -n "remote_branches="; git branch -r | wc -l | tr -d ' '
echo -n "local_audit="; git branch | grep -cE 'audit' || true
echo -n "remote_audit="; git branch -r | grep -cE 'audit' || true
git worktree list
gh issue list -R Pavua/Krab-Ear --state open --limit 100 --json number,title \
  --jq '[.[] | select(.title | test("^ci: "))] | length'
```

Expected: команды завершаются с кодом 0. Числа могут отличаться от снимка шапки — в отчёт пиши **живые**.

- [ ] **Step 2: Записать отчёт**

Создай `docs/hygiene/2026-08-16-w1-inventory.md` с таким телом (подставь живые числа вместо угловых скобок):

```markdown
# W1 inventory — 2026-08-16

База: `codex/krab-ear-v2` @ `<shortsha>`.
Снято: `<iso-local-time>`.

## Git

- local branches: <N>
- remote branches: <N>
- local audit*: <N>
- remote audit*: <N>
- open PRs besides #1875: <N или "none">

## Worktrees

Вставить вывод `git worktree list` целиком (code fence).

Кандидаты на поздний prune (НЕ удалять в этой волне): каталоги
`.worktrees/*` с датой 20260720–20260725 в имени, если
`git -C <path> status --short` пуст И
`git -C <path> log origin/codex/krab-ear-v2..HEAD --oneline` пуст.

Не трогать: основной checkout `Krab Ear`, любой worktree с dirty status,
`KrabEar_gigaam_mlx_wave_20260731` (чужой путь/ветка).

## Issues

- open `ci: *`: <N>
- keep open: #1909 (chronic local hang), #1919 (ubuntu 2026-08-13 unique files)
- close candidates: все остальные open с заголовком
  `ci: test suite failure — YYYY-MM-DD` или `ci: flaky test —`

## Untracked (не коммитить)

- `wake_word_models/hard_negatives_raw/tts_phrases.json`
```

- [ ] **Step 3: Проверить, что файл не пустой**

Run:

```bash
test -s docs/hygiene/2026-08-16-w1-inventory.md && wc -l docs/hygiene/2026-08-16-w1-inventory.md
```

Expected: exit 0, строк ≥ 30.

- [ ] **Step 4: Commit инвентаря** (явные пути, в worktree волны)

```bash
git add docs/hygiene/2026-08-16-w1-inventory.md
git commit -m "$(cat <<'EOF'
docs(w1): зафиксировать инвентарь веток, worktree и CI-issues

EOF
)"
```

---

### Task 2: Сузить генератор `krab-ear-test-health`

**Files:**
- Modify (ВНЕ репо, не `git add`): `/Users/pablito/.claude/scheduled-tasks/krab-ear-test-health/SKILL.md`

Файл не в git Krab Ear. Не копировать его в репозиторий. Не печатать секреты.

- [ ] **Step 1: Снять копию текущего SKILL.md**

Run:

```bash
cp /Users/pablito/.claude/scheduled-tasks/krab-ear-test-health/SKILL.md \
   /tmp/krab-ear-test-health.SKILL.md.bak
wc -l /tmp/krab-ear-test-health.SKILL.md.bak
```

Expected: файл существует, в копии есть строка `gh issue create`.

- [ ] **Step 2: Заменить SKILL.md целиком на текст ниже**

Запиши файл `/Users/pablito/.claude/scheduled-tasks/krab-ear-test-health/SKILL.md` **точно** так (frontmatter сохранить `name`/`model`):

```markdown
---
name: krab-ear-test-health
model: sonnet
description: Daily Python + Swift test suite health check for Krab Ear
---

You are a CI health agent for the Krab Ear project (macOS voice assistant, Swift + Python).

Working directory: /Users/pablito/Antigravity_AGENTS/Krab Ear

## Do not spam GitHub

The local full pytest (`KrabEar/tests/` without chunking) chronically hangs
(~5%, BackendService daemon threads). That hang is tracked in issue #1909.
**Never open a new issue whose only evidence is that hang.**

## Step 1: Swift tests

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent" && swift test 2>&1 | tail -20
```

## Step 2: Authoritative Python signal = ubuntu CI, not local full suite

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
HEAD=$(git rev-parse HEAD)
gh run list -R Pavua/Krab-Ear --workflow krabear-ci.yml --limit 15 --json databaseId,headSha,conclusion,status,url,displayTitle
```

Find the newest run whose `headSha` equals HEAD (or starts with it). If none, say so and stop without creating an issue.

## Step 3: Evaluate

- Swift green AND ubuntu `backend-tests` conclusion `success` → output "All tests passing" and **do not** create an issue.
- Swift red → create an issue (Swift section only).
- ubuntu `backend-tests` conclusion `failure` → create an issue only if no open issue already has that run URL in the body.

## Step 4: Create issue (rare)

Title: `ci: ubuntu backend-tests failure — <date> — <shortsha>`
Body must include: run URL, failing file names if visible, HEAD sha.
Command:

```bash
gh issue create -R Pavua/Krab-Ear --title "<title>" --body "<body>"
```

Do **not** title issues `ci: test suite failure — <date>` anymore (that pattern is retired).

Output a summary under 80 words.
```

- [ ] **Step 3: Проверить, что старый заголовок больше не предписан**

Run:

```bash
grep -n "ci: test suite failure" /Users/pablito/.claude/scheduled-tasks/krab-ear-test-health/SKILL.md || true
grep -n "ubuntu backend-tests failure" /Users/pablito/.claude/scheduled-tasks/krab-ear-test-health/SKILL.md
grep -n "#1909" /Users/pablito/.claude/scheduled-tasks/krab-ear-test-health/SKILL.md
```

Expected: первая команда ничего не печатает (или только «Never open» если оставишь упоминание в прозе — тогда допустимо). Вторая и третья печатают совпадение. Старый `gh issue create` с title `ci: test suite failure — $(date` **отсутствует**.

- [ ] **Step 4: Commit в репо не делать.** В `docs/hygiene/2026-08-16-w1-inventory.md` добавь секцию:

```markdown
## Scheduled task

Patched `~/.claude/scheduled-tasks/krab-ear-test-health/SKILL.md` (backup `/tmp/krab-ear-test-health.SKILL.md.bak`).
Issue title pattern `ci: test suite failure — DATE` retired.
```

```bash
git add docs/hygiene/2026-08-16-w1-inventory.md
git commit -m "$(cat <<'EOF'
docs(w1): отметить правку scheduled-task test-health

EOF
)"
```

---

### Task 3: Закрыть дубликаты CI-issues

**Keep open:** #1909, #1919. **Не закрывать** PR #1875 (это PR, не issue-дубликат).

- [ ] **Step 1: Получить список на закрытие**

Run:

```bash
gh issue list -R Pavua/Krab-Ear --state open --limit 100 --json number,title \
  --jq '.[] | select(.title | test("^ci: (test suite failure|flaky test)")) | select(.number != 1909 and .number != 1919) | "\(.number)\t\(.title)"'
```

Expected: таблица number/title. #1909 и #1919 в выводе **нет**. Если список пуст — волна уже сделана, переходи к Task 4.

- [ ] **Step 2: Закрыть каждый номер из списка**

Для каждого `$N` из Step 1:

```bash
gh issue close "$N" -R Pavua/Krab-Ear --reason "not_planned" --comment "$(cat <<'EOF'
Дубликат ежедневного scheduled-task `krab-ear-test-health`.

Хронический локальный hang полного pytest tracked в #1909.
Последний осмысленный ubuntu-сигнал — #1919 (2026-08-13).

Закрыто волной W1 (2026-08-16). Генератор больше не должен открывать
`ci: test suite failure — DATE` на тот же hang.
EOF
)"
```

Expected: `Closed #N`. Не использовать `gh issue delete`. Не закрывать issues без префикса `ci:`.

- [ ] **Step 3: Проверить остаток**

Run:

```bash
gh issue list -R Pavua/Krab-Ear --state open --limit 100 --json number,title \
  --jq '.[] | select(.title | startswith("ci:")) | "\(.number) \(.title)"'
```

Expected: в списке есть #1909 и #1919. Дата-штампованных `ci: test suite failure — 2026-0` либо нет, либо только те, что открылись **после** патча Task 2 (тогда закрой их тем же комментарием и проверь SKILL.md ещё раз).

- [ ] **Step 4: Дописать инвентарь и commit**

В `docs/hygiene/2026-08-16-w1-inventory.md` секция `## Closed issues` со списком номеров.

```bash
git add docs/hygiene/2026-08-16-w1-inventory.md
git commit -m "$(cat <<'EOF'
docs(w1): закрыть дубликаты ci test-health issues

EOF
)"
```

---

### Task 4: Кандидаты worktree — отчёт, не удаление

- [ ] **Step 1: Для каждого пути из `git worktree list` кроме основного checkout**

Run (подставь `$WT`):

```bash
echo "=== $WT ==="
git -C "$WT" status --short --branch
git -C "$WT" log --oneline origin/codex/krab-ear-v2..HEAD | head -20
```

Expected: команды не падают. Если `log` непустой — ветка имеет уникальные коммиты, **не кандидат**. Если status dirty — **не кандидат**.

- [ ] **Step 2: Записать таблицу в инвентарь**

Колонки: path, branch, dirty (yes/no), commits_not_in_base (count), candidate_for_remove (yes/no).

`candidate_for_remove=yes` только если dirty=no И commits_not_in_base=0.

Не вызывать `git worktree remove`, `rm -rf`, `git worktree prune` кроме уже существующего `--verbose` внутри `scripts/cleanup_worktree_shadows.command`, и то **только если карточка явно дойдёт до Step 3**.

- [ ] **Step 3: Только unregister shadow .app (безопасно, не удаляет git worktree)**

Run:

```bash
./scripts/cleanup_worktree_shadows.command
```

Expected: скрипт доходит до «Re-registering canonical main bundle» без `rm` деревьев исходников. Если `lsregister` отсутствует — залогируй и иди дальше, не чини macOS.

- [ ] **Step 4: Commit отчёта**

```bash
git add docs/hygiene/2026-08-16-w1-inventory.md
git commit -m "$(cat <<'EOF'
docs(w1): таблица worktree-кандидатов без удаления

EOF
)"
```

---

### Task 5: Remote `audit/*` — отчёт, не delete

- [ ] **Step 1: Посчитать и взять 20 самых старых remote audit-веток**

Run:

```bash
git fetch origin
git for-each-ref --sort=committerdate --format='%(committerdate:short) %(refname:short)' refs/remotes/origin | grep '/audit' | head -20
echo -n "remote_audit_count="; git branch -r | grep -cE 'audit'
```

Expected: список дат + число. Не `git push origin --delete`.

- [ ] **Step 2: В инвентарь секция `## Remote audit branches`**

Текст дословно:

```markdown
## Remote audit branches

Count: <N>. Oldest 20 listed above.
Mass delete is out of scope for W1.
Next wave (owner-gated): delete only refs with no open PR
(`gh pr list --head <branch>` empty) and last commit older than 30 days.
PR #1875 (`feat/wake-word-hard-negatives`) is not an audit branch — keep.
```

- [ ] **Step 3: Commit**

```bash
git add docs/hygiene/2026-08-16-w1-inventory.md
git commit -m "$(cat <<'EOF'
docs(w1): отчёт remote audit-веток без массового delete

EOF
)"
```

---

### Task 6: Обновить NOW.md после закрытия W1

**Files:**
- Modify: `docs/NOW.md`

- [ ] **Step 1: В секции «Следующая волна» заменить W1 на W2**

Новый блок (остальной файл не переписывать):

```markdown
## Следующая волна

**W1 — гигиена репо: закрыта 2026-08-16.** Отчёт: `docs/hygiene/2026-08-16-w1-inventory.md`.

**Следующая: W2 — стабильность ежедневки** (код только после явного Approve владельца):
[`docs/superpowers/specs/2026-08-16-w2-daily-stability-design.md`](superpowers/specs/2026-08-16-w2-daily-stability-design.md)
```

- [ ] **Step 2: Commit**

```bash
git add docs/NOW.md
git commit -m "$(cat <<'EOF'
docs(w1): NOW.md — гигиена закрыта, фронт = W2

EOF
)"
```

---

## DoD волны

- Инвентарь в git, числа сняты командами, не из памяти.
- SKILL.md test-health больше не велят открывать `ci: test suite failure — DATE` на локальный hang.
- Дубликаты issues закрыты; #1909 и #1919 открыты.
- Ни одного `git push --delete`, ни одного `git worktree remove`.
- `tts_phrases.json` не в коммите.
- Код Python/Swift продукта не изменён.
