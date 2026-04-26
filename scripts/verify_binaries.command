#!/bin/zsh
# ------------------------------------------------------------------
# verify_binaries.command — проверяет что все три binary в sync:
#   1. .app/Contents/MacOS/KrabEarAgent (LaunchServices-managed)
#   2. native/runtime/KrabEarAgent (legacy dev path)
#   3. running KrabEarAgent process
#
# Все три должны иметь одинаковый SHA-256, иначе:
# - User видит UI старого билда / поведение нового билда (или наоборот)
# - timeout / hotkey / IPC изменения не действуют
# - Confused debugging session
#
# Если drift обнаружен — script:
# 1. Останавливает running KrabEarAgent
# 2. Копирует .build/release/KrabEarAgent в оба места
# 3. Codesign + restart .app
#
# Запуск:
#   bash scripts/verify_binaries.command            # report-only
#   bash scripts/verify_binaries.command --fix      # auto re-sync
#
# Background: после нескольких session'ов session 2026-04-26 нашли что
# `Update Krab Ear Agent.command` иногда rebuild только runtime/ без .app/,
# и наоборот. Активный binary (running PID) можно достать через `lsof -p`.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_BIN="$ROOT_DIR/Krab Ear.app/Contents/MacOS/KrabEarAgent"
RUNTIME_BIN="$ROOT_DIR/native/runtime/KrabEarAgent"
BUILD_BIN="$ROOT_DIR/native/KrabEarAgent/.build/release/KrabEarAgent"

# CLI flags
FIX_DRIFT=false
for arg in "$@"; do
  case "$arg" in
    --fix) FIX_DRIFT=true ;;
    --help|-h)
      echo "Usage: $0 [--fix]"
      echo "  --fix    Auto-resync binaries if drift detected (kills running, re-copies, restarts)"
      exit 0 ;;
  esac
done

# Hash helper.
hash_or_missing() {
  local path="$1"
  if [ -f "$path" ]; then
    /usr/bin/shasum -a 256 "$path" | awk '{print $1}'
  else
    echo "MISSING"
  fi
}

APP_HASH="$(hash_or_missing "$APP_BIN")"
RUNTIME_HASH="$(hash_or_missing "$RUNTIME_BIN")"
BUILD_HASH="$(hash_or_missing "$BUILD_BIN")"

# Find running PID + its binary path. Use `ps -o comm` (full executable
# path on macOS) — простой и reliable. lsof может показать stale text-segment
# entry если binary был replaced after spawn.
RUNNING_PID="$(/usr/bin/pgrep -f "KrabEarAgent" 2>/dev/null | grep -v "$$" | head -1 || true)"
RUNNING_BIN=""
RUNNING_HASH="NONE"
if [ -n "$RUNNING_PID" ]; then
  RUNNING_BIN="$(/bin/ps -p "$RUNNING_PID" -o comm= 2>/dev/null | head -1 || true)"
  if [ -n "$RUNNING_BIN" ] && [ -f "$RUNNING_BIN" ]; then
    RUNNING_HASH="$(/usr/bin/shasum -a 256 "$RUNNING_BIN" | awk '{print $1}')"
  fi
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Krab Ear Binary Verification"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
printf "  %-40s %s\n" ".app/Contents/MacOS/KrabEarAgent:" "${APP_HASH:0:16}…"
printf "  %-40s %s\n" "native/runtime/KrabEarAgent:" "${RUNTIME_HASH:0:16}…"
printf "  %-40s %s\n" ".build/release/KrabEarAgent:" "${BUILD_HASH:0:16}…"
if [ -n "$RUNNING_PID" ]; then
  printf "  %-40s %s\n" "running PID $RUNNING_PID ($(basename "${RUNNING_BIN:-?}")):" "${RUNNING_HASH:0:16}…"
else
  printf "  %-40s %s\n" "running:" "(none)"
fi
echo ""

# Drift detection.
DRIFT=false
if [ "$APP_HASH" != "$RUNTIME_HASH" ]; then
  echo "  ⚠️  DRIFT: .app/ ≠ native/runtime/"
  DRIFT=true
fi
if [ "$BUILD_HASH" != "MISSING" ] && [ "$BUILD_HASH" != "$APP_HASH" ]; then
  echo "  ⚠️  DRIFT: .build/release/ ≠ .app/ (forgot to deploy after rebuild?)"
  DRIFT=true
fi
if [ -n "$RUNNING_PID" ] && [ "$RUNNING_HASH" != "NONE" ] && [ "$RUNNING_HASH" != "$APP_HASH" ]; then
  echo "  ⚠️  DRIFT: running ≠ .app/ (process started со старого binary)"
  DRIFT=true
fi

if [ "$DRIFT" = false ]; then
  echo "  ✅ All binaries in sync"
  exit 0
fi

echo ""
if [ "$FIX_DRIFT" = false ]; then
  echo "  Run with --fix to auto-resync."
  exit 1
fi

echo "  Auto-fixing drift…"
echo ""

# 1. Kill running.
if [ -n "$RUNNING_PID" ]; then
  echo "  → kill running PID $RUNNING_PID"
  /bin/kill "$RUNNING_PID" 2>/dev/null || true
  /bin/sleep 2
fi

# 2. Source binary preference order: .build > .app > runtime.
SOURCE_BIN=""
if [ "$BUILD_HASH" != "MISSING" ]; then
  SOURCE_BIN="$BUILD_BIN"
  echo "  → using freshest source: .build/release/"
elif [ "$APP_HASH" != "MISSING" ]; then
  SOURCE_BIN="$APP_BIN"
  echo "  → using source: .app/Contents/MacOS/"
elif [ "$RUNTIME_HASH" != "MISSING" ]; then
  SOURCE_BIN="$RUNTIME_BIN"
  echo "  → using source: native/runtime/"
else
  echo "  ❌ No source binary found. Run 'make build' first."
  exit 2
fi

# 3. Copy + sign both.
/bin/cp -f "$SOURCE_BIN" "$APP_BIN"
/usr/bin/codesign -s - -f "$APP_BIN" >/dev/null 2>&1 || true
/bin/cp -f "$SOURCE_BIN" "$RUNTIME_BIN"
/usr/bin/codesign -s - -f "$RUNTIME_BIN" >/dev/null 2>&1 || true
echo "  → synced both paths to source"

# 4. Restart .app.
/usr/bin/open "$ROOT_DIR/Krab Ear.app"
/bin/sleep 2
NEW_PID="$(/usr/bin/pgrep -f "Krab Ear.app/Contents/MacOS/KrabEarAgent" 2>/dev/null | head -1 || true)"
if [ -n "$NEW_PID" ]; then
  echo "  ✅ .app restarted (PID $NEW_PID)"
else
  echo "  ⚠️  .app не запустился — проверь permissions"
  exit 3
fi

echo ""
echo "  ✅ Drift fixed"
