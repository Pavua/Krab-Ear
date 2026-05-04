#!/bin/zsh
# ----------------------------------------------------------------
# Worktree-shadow cleanup — Phase B.2 followup #5
#
# Subagents с `isolation: "worktree"` создают копии репо в
# `.claude/worktrees/agent-*/` включая `Krab Ear.app/` bundle.
# macOS LaunchServices индексирует все bundle'ы с одинаковым
# CFBundleIdentifier=com.antigravity.krab-ear → может выбрать
# worktree-shadow вместо main bundle при `open` команде.
#
# Этот скрипт:
#   1. Находит все `Krab Ear.app/` под `.claude/worktrees/agent-*/`
#   2. Unregister каждый из LaunchServices DB через `lsregister -u`
#   3. Re-register canonical main bundle через `lsregister -f`
#   4. Optionally удаляет orphan worktree directories (если git worktree
#      больше не tracks их — `git worktree prune` first)
#
# Run periodically OR after long sessions с many agents:
#   ./scripts/cleanup_worktree_shadows.command
# ----------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKTREES_DIR="$ROOT_DIR/.claude/worktrees"
MAIN_BUNDLE="$ROOT_DIR/Krab Ear.app"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

if [ ! -x "$LSREGISTER" ]; then
    echo "[ERROR] lsregister not found at expected path"
    exit 1
fi

if [ ! -d "$MAIN_BUNDLE" ]; then
    echo "[ERROR] main bundle missing: $MAIN_BUNDLE"
    exit 1
fi

echo "=== Worktree-shadow .app cleanup ==="
echo "Main bundle: $MAIN_BUNDLE"
echo "Worktrees scan: $WORKTREES_DIR"
echo ""

# 1. Prune git worktrees first (removes references to deleted dirs)
if [ -d "$ROOT_DIR/.git" ] || [ -f "$ROOT_DIR/.git" ]; then
    echo "[1/4] git worktree prune..."
    cd "$ROOT_DIR" && git worktree prune --verbose 2>&1 | sed 's/^/      /' || true
fi

# 2. Find all Krab Ear.app under worktrees
shadow_count=0
shadows=()
if [ -d "$WORKTREES_DIR" ]; then
    while IFS= read -r app_path; do
        shadows+=("$app_path")
        shadow_count=$((shadow_count + 1))
    done < <(find "$WORKTREES_DIR" -maxdepth 4 -name "Krab Ear.app" -type d 2>/dev/null)
fi

echo ""
echo "[2/4] Found $shadow_count shadow .app bundle(s)"

# 3. Unregister each shadow
if [ "$shadow_count" -gt 0 ]; then
    echo ""
    echo "[3/4] Unregistering shadows from LaunchServices..."
    for app in "${shadows[@]}"; do
        echo "      unregister: $app"
        "$LSREGISTER" -u "$app" 2>&1 | sed 's/^/        /' || true
    done
else
    echo "[3/4] No shadows to unregister."
fi

# 4. Re-register canonical main bundle (forces it to highest priority)
echo ""
echo "[4/4] Re-registering canonical main bundle..."
"$LSREGISTER" -f "$MAIN_BUNDLE" 2>&1 | sed 's/^/      /' || true

echo ""
echo "=== Cleanup complete ==="
echo "Shadows unregistered: $shadow_count"
echo ""
echo "Tip: if Dock still shows duplicate Krab Ear icons, right-click"
echo "each → 'Options' → 'Show in Finder' to identify which path it"
echo "points to. Anything under .claude/worktrees/agent-*/ → orphan."
