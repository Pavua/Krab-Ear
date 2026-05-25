#!/bin/bash
# check_two_binary_drift.sh — UUID-based two-binary drift check for Krab Ear.
#
# Krab Ear ships two binaries that MUST stay in sync:
#   1. Krab Ear.app/Contents/MacOS/KrabEarAgent  (bundle — what Dock/launchd launches)
#   2. native/runtime/KrabEarAgent               (dev runtime path, gitignored)
#
# Drift between them causes "backend недоступен" / stale-binary symptoms.
# This script uses dwarfdump UUID as a reliable, codesign-independent fingerprint.
#
# Usage:
#   bash scripts/check_two_binary_drift.sh            # report only (exit 0 = OK, 1 = drift)
#   bash scripts/check_two_binary_drift.sh --fix      # sync runtime← bundle + re-sign
#
# Called by: CI (optional), scheduled routine, manual pre-flight check.
# See also: scripts/verify_binaries.command (SHA/mtime-based, --fix capable, broader scope)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE_BIN="$ROOT_DIR/Krab Ear.app/Contents/MacOS/KrabEarAgent"
RUNTIME_BIN="$ROOT_DIR/native/runtime/KrabEarAgent"
SIGN_IDENTITY="Krab Ear Dev Local"  # stable self-signed cert (PR #235)

FIX=false
for arg in "$@"; do
  case "$arg" in
    --fix) FIX=true ;;
    --help|-h)
      echo "Usage: $(basename "$0") [--fix]"
      echo "  --fix   Sync runtime← bundle and re-sign with '$SIGN_IDENTITY'"
      exit 0 ;;
  esac
done

get_uuid() {
  local path="$1"
  if [ ! -f "$path" ]; then
    echo "MISSING"
    return
  fi
  dwarfdump -u "$path" 2>/dev/null | head -1 | awk '{print $2}'
}

bundle_uuid="$(get_uuid "$BUNDLE_BIN")"
runtime_uuid="$(get_uuid "$RUNTIME_BIN")"

echo "Bundle:  $bundle_uuid  ($BUNDLE_BIN)"
echo "Runtime: $runtime_uuid  ($RUNTIME_BIN)"

if [ "$bundle_uuid" = "MISSING" ] || [ "$runtime_uuid" = "MISSING" ]; then
  echo "ERROR: one or both binaries missing — run 'make sign' first" >&2
  exit 2
fi

if [ "$bundle_uuid" != "$runtime_uuid" ]; then
  echo "DRIFT: bundle=$bundle_uuid runtime=$runtime_uuid" >&2
  if [ "$FIX" = false ]; then
    echo "Run with --fix to sync runtime← bundle." >&2
    exit 1
  fi
  echo "Fixing: cp bundle → runtime + codesign…"
  cp -f "$BUNDLE_BIN" "$RUNTIME_BIN"
  if codesign -s "$SIGN_IDENTITY" -f "$RUNTIME_BIN" 2>/dev/null; then
    echo "Signed with '$SIGN_IDENTITY'."
  else
    echo "Warning: '$SIGN_IDENTITY' not found, falling back to ad-hoc sign."
    codesign -s - -f "$RUNTIME_BIN"
  fi
  new_uuid="$(get_uuid "$RUNTIME_BIN")"
  echo "OK: both binaries now UUID=$new_uuid"
  exit 0
fi

echo "OK: both binaries UUID=$bundle_uuid"
