#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# pre_merge_py312_check.sh — reproduce the ubuntu krab-ear-ci environment LOCALLY
# so a PR can be validated BEFORE the slow remote CI, breaking the red-tip cycle.
#
# WHY: the dev venv (.venv_krab_ear) runs Python 3.14 and HAS mlx-whisper /
# mlx.core installed (macOS wheels exist). The ubuntu CI runner (krab-ear-ci.yml
# backend-tests) runs Python 3.12 and has NO mlx wheels at all. Any test that
# assumes `import mlx_whisper` succeeds, or asserts the STT-available branch,
# passes locally ("false green") but FAILS on ubuntu. This is the "mlx-masking"
# trap that caused three red tips in the wave-18..23 arc (see CLAUDE.md).
#
# This harness builds/reuses a Python 3.12 venv at $HARNESS_VENV with the full
# backend requirements MINUS mlx / mlx-whisper, exactly matching ubuntu, and runs
# the given test files memory-safe (one file at a time, no xdist), reaping
# orphaned MLX/inference subprocesses between files.
#
# USAGE:
#   scripts/pre_merge_py312_check.sh [TEST_FILE ...]
#     - with args: run exactly those test files (paths relative to repo root or absolute)
#     - no args:   auto-detect changed test files vs origin/codex/krab-ear-v2
#                  (git diff --name-only) and run those.
#
# ENV OVERRIDES:
#   HARNESS_VENV   (default /tmp/py312)        venv location
#   PY312          (default python3.12 on PATH) interpreter to build the venv
#   REBUILD=1      force-recreate the venv from scratch
#
# EXIT: 0 if all selected test files pass, non-zero on first failure / no tests.
# ---------------------------------------------------------------------------
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HARNESS_VENV="${HARNESS_VENV:-/tmp/py312}"
PY312="${PY312:-python3.12}"
BASE_REF="${BASE_REF:-origin/codex/krab-ear-v2}"

log() { printf '\033[1;36m[harness]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[harness]\033[0m %s\n' "$*" >&2; }

reap() {
  pkill -9 -f "import sys;ex" 2>/dev/null || true
  pkill -9 -f mlx_subprocess  2>/dev/null || true
  pkill -9 -f gigaam_worker   2>/dev/null || true
  return 0
}

# --- 1. ensure interpreter ------------------------------------------------
if ! command -v "$PY312" >/dev/null 2>&1; then
  for cand in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
    [ -x "$cand" ] && PY312="$cand" && break
  done
fi
command -v "$PY312" >/dev/null 2>&1 || { err "python3.12 not found (set PY312=...)"; exit 2; }

# --- 2. build / reuse venv ------------------------------------------------
needs_build=0
if [ "${REBUILD:-0}" = "1" ] || [ ! -x "$HARNESS_VENV/bin/python" ]; then
  needs_build=1
else
  # reuse only if backend imports AND mlx is genuinely absent (ubuntu parity)
  if ! PYTHONPATH="$REPO_ROOT/KrabEar" "$HARNESS_VENV/bin/python" -c "import backend.service" >/dev/null 2>&1; then
    log "existing venv cannot import backend → rebuild"
    needs_build=1
  elif "$HARNESS_VENV/bin/python" -c "import mlx_whisper" >/dev/null 2>&1 || \
       "$HARNESS_VENV/bin/python" -c "import mlx.core"     >/dev/null 2>&1; then
    log "existing venv still has mlx → purging to match ubuntu"
    "$HARNESS_VENV/bin/pip" uninstall -y mlx mlx-whisper mlx-lm >/dev/null 2>&1 || true
  fi
fi

if [ "$needs_build" = "1" ]; then
  log "building ubuntu-parity venv at $HARNESS_VENV ($($PY312 --version 2>&1)) ..."
  rm -rf "$HARNESS_VENV"
  "$PY312" -m venv "$HARNESS_VENV" || { err "venv create failed"; exit 2; }
  "$HARNESS_VENV/bin/pip" install -q --upgrade pip wheel >/dev/null 2>&1 || true
  log "installing KrabEar/requirements.txt (this is slow on first build) ..."
  "$HARNESS_VENV/bin/pip" install -q -r KrabEar/requirements.txt
  "$HARNESS_VENV/bin/pip" install -q pytest >/dev/null 2>&1 || true
  # CRITICAL: strip mlx so the env matches ubuntu (no Linux wheels exist there).
  log "purging mlx / mlx-whisper / mlx-lm to reproduce ubuntu (no Linux wheels)"
  "$HARNESS_VENV/bin/pip" uninstall -y mlx mlx-whisper mlx-lm >/dev/null 2>&1 || true
fi

# sanity: mlx MUST be absent, backend MUST import
if "$HARNESS_VENV/bin/python" -c "import mlx_whisper" >/dev/null 2>&1; then
  err "FATAL: mlx_whisper still importable in harness venv — not ubuntu-parity"; exit 2
fi
PYTHONPATH="$REPO_ROOT/KrabEar" "$HARNESS_VENV/bin/python" -c "import backend.service" >/dev/null 2>&1 \
  || { err "FATAL: backend.service import failed in harness venv"; exit 2; }
log "venv ready: $($HARNESS_VENV/bin/python --version 2>&1), mlx ABSENT (ubuntu parity), backend imports OK"

# --- 3. select test files -------------------------------------------------
declare -a TESTS=()
if [ "$#" -gt 0 ]; then
  TESTS=("$@")
else
  log "no args → auto-detecting changed test files vs $BASE_REF"
  while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$f" ] && TESTS+=("$f")
  done < <(git diff --name-only "$BASE_REF"...HEAD 2>/dev/null | grep -E 'KrabEar/tests/test_.*\.py$' ; \
           git diff --name-only 2>/dev/null | grep -E 'KrabEar/tests/test_.*\.py$')
  # de-dup
  if [ "${#TESTS[@]}" -gt 0 ]; then
    mapfile -t TESTS < <(printf '%s\n' "${TESTS[@]}" | sort -u)
  fi
fi

if [ "${#TESTS[@]}" -eq 0 ]; then
  err "no test files selected (pass paths as args, or have changed test files vs $BASE_REF)"
  exit 3
fi

log "running ${#TESTS[@]} test file(s) memory-safe (one at a time, no xdist):"
printf '  - %s\n' "${TESTS[@]}"

# --- 4. run each file, memory-safe ---------------------------------------
fails=()
for t in "${TESTS[@]}"; do
  log "→ $t"
  if PYTHONPATH="$REPO_ROOT/KrabEar" "$HARNESS_VENV/bin/python" -m pytest "$t" -p no:xdist -q; then
    :
  else
    fails+=("$t")
  fi
  reap
done

echo
if [ "${#fails[@]}" -eq 0 ]; then
  log "=== ALL GREEN (ubuntu-parity py3.12, mlx absent) | FAIL: none ==="
  exit 0
else
  err "=== RED on ${#fails[@]} file(s) (would fail ubuntu CI) ==="
  printf '   FAIL: %s\n' "${fails[@]}" >&2
  exit 1
fi
