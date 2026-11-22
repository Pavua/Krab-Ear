#!/bin/bash
# Wave 311: Identify worktrees with merged branches and list deletion candidates.
# SAFE BY DEFAULT — dry-run mode shows what would be deleted.
# Pass --execute to actually remove.
#
# Usage:
#   ./scripts/cleanup_merged_worktrees.command             # dry-run (default)
#   ./scripts/cleanup_merged_worktrees.command --execute   # actually delete

set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$REPO_ROOT"

DRY_RUN=true
if [ "${1:-}" == "--execute" ]; then DRY_RUN=false; fi

echo "=== Worktree cleanup audit ==="
echo "Repo: $REPO_ROOT"
total=$(git worktree list | wc -l | tr -d ' ')
echo "Total worktrees: $total"
echo ""

main_branch="codex/krab-ear-v2"
echo "Fetching origin/$main_branch ..."
git fetch origin "$main_branch" 2>/dev/null || echo "  (fetch failed — using local ref)"
echo ""

candidates=0
preserved=0
locked_count=0
prunable_count=0

# Записи берём из `git worktree list --porcelain` (строки «worktree <путь целиком>»):
# разбор человекочитаемого списка по пробелу резал пути с пробелами. Поля через TAB,
# пустая ветка = "-". Первая запись porcelain — всегда основной worktree.
worktree_records() {
    git worktree list --porcelain | awk 'BEGIN { RS = ""; FS = "\n" } {
        path = ""; br = "-"; lk = 0; pr = 0
        for (i = 1; i <= NF; i++) {
            if ($i ~ /^worktree /) path = substr($i, 10)
            else if ($i ~ /^branch /) { br = substr($i, 8); sub("^refs/heads/", "", br) }
            else if ($i ~ /^locked/) lk = 1
            else if ($i ~ /^prunable/) pr = 1
        }
        printf "%s\t%s\t%d\t%d\n", path, br, lk, pr
    }'
}

record_idx=0
while IFS=$'\t' read -r path branch_raw locked_flag prunable_flag; do
    record_idx=$((record_idx + 1))
    [ "$branch_raw" != "-" ] || branch_raw=""

    # Skip main repo root (первая запись porcelain либо совпадение пути)
    if [ "$record_idx" -eq 1 ] || [ "$path" = "$REPO_ROOT" ]; then
        echo "  KEEP (main): $path  [$branch_raw]"
        preserved=$((preserved + 1))
        continue
    fi

    # Mark prunable worktrees (git worktree list marks them as "prunable")
    if [ "$prunable_flag" = "1" ]; then
        echo "  PRUNABLE (stale path): $path  [$branch_raw]"
        prunable_count=$((prunable_count + 1))
        candidates=$((candidates + 1))
        if [ "$DRY_RUN" = false ]; then
            git worktree prune --verbose 2>&1 | head -5
        fi
        continue
    fi

    # Skip if no branch info (detached HEAD, bare, etc.)
    if [ -z "$branch_raw" ]; then
        echo "  KEEP (no-branch/detached): $path"
        preserved=$((preserved + 1))
        continue
    fi

    # Skip locked worktrees (active agent sessions)
    if [ "$locked_flag" = "1" ]; then
        locked_count=$((locked_count + 1))
        preserved=$((preserved + 1))
        # Only print first 10 locked to avoid flooding output
        if [ $locked_count -le 10 ]; then
            echo "  KEEP (locked): $path  [$branch_raw]"
        elif [ $locked_count -eq 11 ]; then
            echo "  KEEP (locked): ... (suppressing further locked entries)"
        fi
        continue
    fi

    # Check if branch is fully merged into main
    if git merge-base --is-ancestor "$branch_raw" "origin/$main_branch" 2>/dev/null; then
        # «Предок main» не значит «работа сохранена»: свежий agent-worktree без коммитов
        # тоже предок. Грязное дерево (изменённое/неотслеживаемое) НИКОГДА не кандидат —
        # ни в dry-run, ни с --execute, и никакого флага, который это обходит, нет.
        if [ -n "$(git -C "$path" status --porcelain 2>/dev/null)" ]; then
            echo "  KEEP (dirty): $path  [$branch_raw]"
            preserved=$((preserved + 1))
            continue
        fi
        echo "  MERGED → candidate: $path  [$branch_raw]"
        candidates=$((candidates + 1))
        if [ "$DRY_RUN" = false ]; then
            # Без --force; ветку — только безопасным -d.
            echo "    Removing worktree: $path"
            remove_out=$(git worktree remove "$path" 2>&1); remove_rc=$?
            [ -n "$remove_out" ] && printf '%s\n' "$remove_out" | head -2
            if [ "$remove_rc" -eq 0 ]; then
                if git show-ref --verify --quiet "refs/heads/$branch_raw"; then
                    echo "    Deleting branch (safe -d): $branch_raw"
                    git branch -d "$branch_raw" 2>&1 | head -1
                fi
            else
                echo "    SKIP: git отказался удалять worktree — ветка остаётся"
            fi
        fi
    else
        echo "  KEEP (unmerged): $path  [$branch_raw]"
        preserved=$((preserved + 1))
    fi

done < <(worktree_records)

echo ""
echo "=== Summary ==="
echo "  Total worktrees scanned : $total"
echo "  Candidates for removal  : $candidates"
echo "    of which prunable     : $prunable_count"
echo "  Locked (skipped)        : $locked_count"
echo "  Preserved               : $preserved"

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "DRY RUN — no changes made."
    echo "Pass --execute to actually delete candidates."
    if [ "$candidates" -gt 0 ]; then
        echo ""
        echo "WARNING: $candidates worktrees would be deleted."
        echo "Review the MERGED lines above before running with --execute."
    fi
else
    echo ""
    echo "Done. Run 'git worktree list' to verify."
    echo "Recovery: 'git reflog show <branch>' if a branch was deleted by mistake."
fi
