#!/usr/bin/env bash
# disk_emergency_cleanup.sh — DRY-RUN disk audit for Krab Ear repo.
# Prints sizes, identifies stale worktrees and cache, and suggests
# ready-to-paste rm commands. NOTHING is deleted automatically.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORKTREES_DIR="$REPO_ROOT/.claude/worktrees"

BOLD='\033[1m'
CYAN='\033[1;36m'
YELLOW='\033[1;33m'
RED='\033[1;31m'
GREEN='\033[1;32m'
RESET='\033[0m'

hr() { printf '%s\n' "────────────────────────────────────────────────────────"; }

echo ""
echo -e "${CYAN}${BOLD}=== Krab Ear Disk Emergency Cleanup Audit (DRY-RUN) ===${RESET}"
echo -e "  Repo root : $REPO_ROOT"
echo -e "  Date      : $(date '+%Y-%m-%d %H:%M %Z')"
hr

# ──────────────────────────────────────────────
# 1. Overall disk free
# ──────────────────────────────────────────────
echo ""
echo -e "${BOLD}[1/5] Disk free (root volume)${RESET}"
df -h / | awk 'NR==2 {printf "  Used: %s / %s  (%s used, %s free)\n", $3, $2, $5, $4}'

# ──────────────────────────────────────────────
# 2. Worktrees in .claude/worktrees/
# ──────────────────────────────────────────────
echo ""
echo -e "${BOLD}[2/5] Worktree sizes in .claude/worktrees/${RESET}"
hr

if [[ ! -d "$WORKTREES_DIR" ]]; then
    echo "  (directory not found: $WORKTREES_DIR)"
else
    # Total size first
    TOTAL=$(du -sh "$WORKTREES_DIR" 2>/dev/null | cut -f1)
    echo -e "  Total: ${YELLOW}${TOTAL}${RESET}"
    echo ""

    # Per-worktree size + branch status
    # Build list of branches known to git (merged into codex/krab-ear-v2)
    MAIN_BRANCH="codex/krab-ear-v2"
    MERGED_BRANCHES=$(git -C "$REPO_ROOT" branch --merged "$MAIN_BRANCH" 2>/dev/null | sed 's/^[* ]*//' || true)

    echo -e "  ${BOLD}Size        Worktree dir                              Branch status${RESET}"
    echo -e "  ----------  ----------------------------------------  ----------------"

    STALE_DIRS=()

    for wt_dir in "$WORKTREES_DIR"/*/; do
        [[ -d "$wt_dir" ]] || continue
        wt_name="$(basename "$wt_dir")"
        wt_size=$(du -sh "$wt_dir" 2>/dev/null | cut -f1)

        # Determine the branch for this worktree (HEAD file inside .git of wt)
        wt_branch=""
        if [[ -f "$wt_dir/.git" ]]; then
            # worktree: .git is a file pointing to the gitdir
            gitdir=$(sed 's/^gitdir: //' "$wt_dir/.git" 2>/dev/null || true)
            head_file="$gitdir/HEAD"
        else
            head_file="$wt_dir/.git/HEAD"
        fi

        if [[ -f "$head_file" ]]; then
            head_content=$(cat "$head_file")
            if [[ "$head_content" == ref:* ]]; then
                wt_branch="${head_content#ref: refs/heads/}"
            else
                wt_branch="(detached ${head_content:0:8})"
            fi
        else
            # Fallback: look up via git worktree list
            wt_branch=$(git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null \
                | awk -v p="$wt_dir" 'prev==p{print $2} {prev=$1}' \
                | head -1 || true)
            [[ -z "$wt_branch" ]] && wt_branch="(unknown)"
        fi

        # Check if branch is merged
        status_label=""
        if echo "$MERGED_BRANCHES" | grep -qxF "$wt_branch" 2>/dev/null; then
            status_label="${GREEN}MERGED — safe to remove${RESET}"
            STALE_DIRS+=("$wt_dir")
        else
            status_label="active / unmerged"
        fi

        printf "  %-10s  %-40s  %b\n" "$wt_size" "$wt_name" "$status_label"
    done

    echo ""
    if [[ ${#STALE_DIRS[@]} -eq 0 ]]; then
        echo -e "  ${GREEN}No merged worktrees detected.${RESET}"
    else
        echo -e "  ${YELLOW}${#STALE_DIRS[@]} merged worktree(s) detected. To remove them:${RESET}"
        echo ""
        for d in "${STALE_DIRS[@]}"; do
            wt_name="$(basename "$d")"
            echo -e "  ${RED}# Remove worktree: $wt_name${RESET}"
            echo "  git -C \"$REPO_ROOT\" worktree remove --force \"$d\""
            echo "  # or: rm -rf \"$d\""
        done
        echo ""
        echo -e "  ${YELLOW}Then prune stale refs:${RESET}"
        echo "  git -C \"$REPO_ROOT\" worktree prune"
        echo "  git -C \"$REPO_ROOT\" remote prune origin"
    fi
fi

# ──────────────────────────────────────────────
# 3. /tmp krab-* leftovers
# ──────────────────────────────────────────────
echo ""
echo -e "${BOLD}[3/5] /tmp krab-* and related temp files${RESET}"
hr

TMP_KRAB=$(find /tmp -maxdepth 2 -name 'krab-*' -o -name 'krab_ear_*' -o -name 'krabear_*' 2>/dev/null | head -50 || true)
if [[ -z "$TMP_KRAB" ]]; then
    echo "  No /tmp/krab-* files found."
else
    TMP_SIZE=$(du -shc $TMP_KRAB 2>/dev/null | tail -1 | cut -f1 || echo "?")
    echo -e "  Estimated size: ${YELLOW}${TMP_SIZE}${RESET}"
    echo ""
    echo "$TMP_KRAB" | while IFS= read -r f; do
        sz=$(du -sh "$f" 2>/dev/null | cut -f1 || echo "?")
        echo "  $sz  $f"
    done
    echo ""
    echo -e "  ${YELLOW}To remove all at once:${RESET}"
    echo "  find /tmp -maxdepth 2 \\( -name 'krab-*' -o -name 'krab_ear_*' -o -name 'krabear_*' \\) -exec rm -rf {} +"
fi

# ──────────────────────────────────────────────
# 4. HuggingFace model cache
# ──────────────────────────────────────────────
echo ""
echo -e "${BOLD}[4/5] HuggingFace model cache (~/.cache/huggingface/)${RESET}"
hr

HF_CACHE="$HOME/.cache/huggingface"
if [[ -d "$HF_CACHE" ]]; then
    HF_SIZE=$(du -sh "$HF_CACHE" 2>/dev/null | cut -f1)
    echo -e "  Total HF cache: ${YELLOW}${HF_SIZE}${RESET}"
    echo ""
    echo "  Top models by size:"
    du -sh "$HF_CACHE"/hub/models--* 2>/dev/null \
        | sort -rh \
        | head -15 \
        | while IFS=$'\t' read -r sz path; do
            model=$(basename "$path" | sed 's/models--//' | tr -- '-' '/')
            printf "  %-8s  %s\n" "$sz" "$model"
        done
    echo ""
    echo -e "  ${YELLOW}To remove a specific model cache entry (example):${RESET}"
    echo "  rm -rf \"$HF_CACHE/hub/models--<org>--<model-name>\""
    echo ""
    echo -e "  ${YELLOW}Or use the huggingface-cli scanner (no delete — lists only):${RESET}"
    echo "  huggingface-cli scan-cache"
    echo ""
    echo -e "  ${YELLOW}To delete specific revisions interactively:${RESET}"
    echo "  huggingface-cli delete-cache"
else
    echo "  HuggingFace cache not found at $HF_CACHE"
fi

# ──────────────────────────────────────────────
# 5. Other large suspects
# ──────────────────────────────────────────────
echo ""
echo -e "${BOLD}[5/5] Other large items to consider${RESET}"
hr

# git worktree build artefacts (.build/ inside each worktree)
BUILD_TOTAL=$(find "$WORKTREES_DIR" -maxdepth 3 -type d -name '.build' 2>/dev/null \
    | xargs du -shc 2>/dev/null | tail -1 | cut -f1 || echo "0B")
echo -e "  Swift .build/ dirs in worktrees : ${YELLOW}${BUILD_TOTAL}${RESET}"
echo "  # Clean with:"
echo "  find \"$WORKTREES_DIR\" -maxdepth 3 -type d -name '.build' -exec rm -rf {} +"
echo ""

# Python __pycache__ in worktrees
PYCACHE_TOTAL=$(find "$WORKTREES_DIR" -type d -name '__pycache__' 2>/dev/null \
    | xargs du -shc 2>/dev/null | tail -1 | cut -f1 || echo "0B")
echo -e "  __pycache__ dirs in worktrees   : ${YELLOW}${PYCACHE_TOTAL}${RESET}"
echo "  # Clean with:"
echo "  find \"$WORKTREES_DIR\" -type d -name '__pycache__' -exec rm -rf {} +"
echo ""

# dist/ folder in repo root
DIST_DIR="$REPO_ROOT/dist"
if [[ -d "$DIST_DIR" ]]; then
    DIST_SIZE=$(du -sh "$DIST_DIR" 2>/dev/null | cut -f1)
    echo -e "  dist/ in repo root              : ${YELLOW}${DIST_SIZE}${RESET}"
    echo "  # If no longer needed:"
    echo "  rm -rf \"$DIST_DIR\""
    echo ""
fi

# macOS diagnostic reports referencing krab
DIAG_COUNT=$(find "$HOME/Library/Logs/DiagnosticReports" -name '*rab*' 2>/dev/null | wc -l | tr -d ' ')
DIAG_SIZE=$(find "$HOME/Library/Logs/DiagnosticReports" -name '*rab*' 2>/dev/null \
    | xargs du -shc 2>/dev/null | tail -1 | cut -f1 || echo "0B")
echo -e "  DiagnosticReports *rab* files   : ${YELLOW}${DIAG_COUNT} files, ${DIAG_SIZE}${RESET}"
if [[ "$DIAG_COUNT" -gt 0 ]]; then
    echo "  # To remove:"
    echo "  find \"$HOME/Library/Logs/DiagnosticReports\" -name '*rab*' -delete"
fi
echo ""

hr
echo ""
echo -e "${GREEN}${BOLD}Audit complete. No files were modified.${RESET}"
echo -e "Review the commands above and run the ones that apply."
echo ""
