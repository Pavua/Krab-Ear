#!/bin/bash
# Removes git worktrees whose branches are merged into codex/krab-ear-v2.
# Safe-by-default: dry-run unless --apply. Caps at 50/run.
# W756: rewritten from W749 attempt to handle paths-with-spaces correctly.

set -euo pipefail
cd "$(dirname "$0")/.."

DRY=true
[ "${1:-}" = "--apply" ] && DRY=false
LIMIT=50

declare -a CANDS=()
P=""; B=""; LOCKED=""

while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        "worktree "*) P="${line#worktree }" ;;
        "branch "*)   B="${line#branch refs/heads/}" ;;
        "locked"*)    LOCKED=1 ;;
        "")
            if [ -n "$P" ] && [ -n "$B" ] && [ -z "$LOCKED" ]; then
                if [ "$P" != "$(pwd)" ]; then
                    if git branch --merged origin/codex/krab-ear-v2 2>/dev/null | grep -qE "^\s+${B}$|^\* ${B}$"; then
                        CANDS+=("$P|$B")
                    fi
                fi
            fi
            P=""; B=""; LOCKED=""
            ;;
    esac
done < <(git worktree list --porcelain)

echo "Candidates: ${#CANDS[@]} (cap $LIMIT/run)"
if $DRY; then
    echo "DRY-RUN (use --apply to remove):"
    for i in "${!CANDS[@]}"; do
        [ "$i" -ge "$LIMIT" ] && { echo "  ... (+$((${#CANDS[@]} - LIMIT)) more)"; break; }
        IFS='|' read -r p b <<< "${CANDS[$i]}"
        echo "  $p ($b)"
    done
    exit 0
fi

N=0
for e in "${CANDS[@]}"; do
    [ "$N" -ge "$LIMIT" ] && break
    IFS='|' read -r p b <<< "$e"
    if git worktree remove "$p" 2>&1; then
        git branch -d "$b" 2>/dev/null || true
        N=$((N + 1))
    fi
done
echo "Removed: $N"
