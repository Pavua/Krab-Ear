#!/bin/zsh
# ------------------------------------------------------------------
# Release checklist Krab Ear (automated checks)
# Ref: RELEASE_CHECKLIST.md
#
# Usage:
#   ./scripts/run_release_checklist.command         # full checks
#   ./scripts/run_release_checklist.command --quick # skip pytest
#
# Checks:
# 1) venv exists and python executable
# 2) python version >= 3.9
# 3) pip dependencies installed (pip check)
# 4) disk space >= 2GB
# 5) git status clean (no uncommitted changes)
# 6) shell lint .command scripts
# 7) swift release build
# 8) backend unit tests (skipped with --quick)
# 9) smoke release test
# 10) final report to docs/reports/
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$ROOT_DIR/docs/reports"
TS="$(date +%Y%m%d_%H%M%S)"
REPORT_PATH="$REPORT_DIR/release_checklist_${TS}.md"
VENV_PY="$ROOT_DIR/.venv_krab_ear/bin/python"
QUICK_MODE="${1:-}"

mkdir -p "$REPORT_DIR"

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

fail() {
  local step="$1"
  local message="$2"
  {
    echo "# Release Checklist Report — $(date -Iseconds)"
    echo
    echo "**Status:** FAILED"
    echo "**Failed Step:** \`$step\`"
    echo "**Message:** $message"
  } > "$REPORT_PATH"
  echo -e "${RED}❌ Release checklist FAILED ($step)${NC}"
  echo -e "${YELLOW}Report: $REPORT_PATH${NC}"
  exit 1
}

pass() {
  local check="$1"
  echo -e "${GREEN}✓${NC} $check"
}

print_section() {
  echo ""
  echo -e "${YELLOW}▶${NC} $1"
}

# =====================================================================
# Preflight checks
# =====================================================================

print_section "PREFLIGHT CHECKS"

# Check venv exists
if [ ! -d "$ROOT_DIR/.venv_krab_ear" ]; then
  fail "venv_missing" "Virtual environment not found at $ROOT_DIR/.venv_krab_ear"
fi
pass "venv exists"

# Check python executable
if [ ! -x "$VENV_PY" ]; then
  fail "python_missing" "Python executable not found at $VENV_PY"
fi
pass "python executable"

# Check python version >= 3.9
PY_VERSION=$("$VENV_PY" --version 2>&1 | awk '{print $2}')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]); then
  fail "python_version" "Python version must be >= 3.9, found $PY_VERSION"
fi
pass "python version $PY_VERSION (>= 3.9)"

# Check pip dependencies
if ! "$VENV_PY" -m pip check > /dev/null 2>&1; then
  fail "pip_check" "pip check failed; run: pip install -r KrabEar/requirements.txt"
fi
pass "pip dependencies OK"

# Check disk space >= 2GB
DISK_AVAIL=$(df "$ROOT_DIR" | awk 'NR==2 {print $4}')
if [ "$DISK_AVAIL" -lt 2097152 ]; then
  fail "disk_space" "Insufficient disk space: $(($DISK_AVAIL / 1024)) MB available (need >= 2 GB)"
fi
pass "disk space $(($DISK_AVAIL / 1048576)) GB available (>= 2 GB)"

# Check git status clean
if [ -d "$ROOT_DIR/.git" ]; then
  if ! git -C "$ROOT_DIR" diff-index --quiet HEAD --; then
    fail "git_status" "Uncommitted changes detected; run: git status"
  fi
  pass "git status clean"
fi

# =====================================================================
# Build checks
# =====================================================================

print_section "BUILD CHECKS"

# Shell lint
if find "$ROOT_DIR" -maxdepth 2 -name "*.command" -print0 | xargs -0 -n1 zsh -n 2>/dev/null; then
  pass "shell scripts syntax OK"
else
  fail "shell_lint" "One or more .command scripts have syntax errors"
fi

# Swift release build
if swift build -c release --package-path "$ROOT_DIR/native/KrabEarAgent" 2>&1 > /dev/null; then
  pass "swift release build OK"
else
  fail "swift_build" "Swift release build failed; check native/KrabEarAgent/build logs"
fi

# =====================================================================
# Test checks
# =====================================================================

print_section "TEST CHECKS"

if [ "$QUICK_MODE" = "--quick" ]; then
  echo -e "${YELLOW}⊘ Skipping unit tests (--quick mode)${NC}"
else
  if "$VENV_PY" -m unittest discover -s "$ROOT_DIR/KrabEar/tests" -p "test_*.py" -v 2>&1 | grep -q "Ran.*test"; then
    TEST_COUNT=$(cd "$ROOT_DIR/KrabEar/tests" && find . -name "test_*.py" -type f | wc -l)
    pass "backend unit tests (found ~$TEST_COUNT test files)"
  else
    fail "unit_tests" "Backend unit tests failed"
  fi
fi

# Smoke test
if "$ROOT_DIR/scripts/run_smoke_release.command" 2>&1 > /dev/null; then
  SMOKE_REPORT="$(ls -1t "$REPORT_DIR"/smoke_release_*.md 2>/dev/null | head -n 1 || true)"
  [ -n "$SMOKE_REPORT" ] && SMOKE_REPORT="$(basename "$SMOKE_REPORT")" || SMOKE_REPORT="-"
  pass "release smoke test OK"
else
  fail "release_smoke" "Smoke test failed; check scripts/run_smoke_release.command"
fi

# =====================================================================
# Generate final report
# =====================================================================

print_section "FINAL REPORT"

{
  echo "# Release Checklist Report — $(date -Iseconds)"
  echo ""
  echo "**Status:** ✅ OK"
  echo ""
  echo "## Checks Passed"
  echo ""
  echo "| Check | Status |"
  echo "|-------|--------|"
  echo "| venv exists | ✓ |"
  echo "| python version | ✓ $PY_VERSION |"
  echo "| pip dependencies | ✓ |"
  echo "| disk space | ✓ $(($DISK_AVAIL / 1048576)) GB |"
  echo "| git status | ✓ clean |"
  echo "| shell scripts | ✓ |"
  echo "| swift build | ✓ |"
  if [ "$QUICK_MODE" = "--quick" ]; then
    echo "| unit tests | ⊘ skipped (--quick) |"
  else
    echo "| unit tests | ✓ |"
  fi
  echo "| smoke test | ✓ |"
  echo ""
  echo "## Generated Reports"
  echo ""
  echo "- Release Checklist: \`$REPORT_PATH\`"
  [ -n "$SMOKE_REPORT" ] && [ "$SMOKE_REPORT" != "-" ] && echo "- Smoke Test: \`$REPORT_DIR/$SMOKE_REPORT\`"
  echo ""
  echo "## Next Steps"
  echo ""
  echo "See RELEASE_CHECKLIST.md section 4 (Functional Check) for manual smoke tests."
} > "$REPORT_PATH"

echo -e "${GREEN}✅ Release checklist PASSED${NC}"
echo -e "${YELLOW}Report: $REPORT_PATH${NC}"
echo ""
exit 0
